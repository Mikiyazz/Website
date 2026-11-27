#!/usr/bin/env python
r"""学院社会实践材料平台 · 2000 人整站验收压测与 300MB 分片实测脚本。

支持两种运行模式：
1. 轻量并发压测（兼容旧用法）：50 并发学生登录/建草稿/直传附件 + 300MB 分片视频。
2. 2000 人整站验收压测（--full-acceptance，推荐对 Docker 生产容器栈执行）：
   - 预置 2000 名学生 + 1600 份多状态/含团队索引的背景申报 + 1 名管理员 + 1 名团支书；
   - 阶段 1：100 并发同 IP 登录（验证 0 个 429）+ 100 并发齐发刷首页 `/`（验证 P95 < 1000ms、0 超时）；
   - 阶段 2：审核工作台 `/reviews/workbench/` 与管理面板 `/admin-panel/` 分页负载探测（验证单页 HTML < 150KB）；
   - 阶段 3：50 并发学生建草稿 -> 保存草稿 -> 直传 5 份完整必传材料 -> 提交审核 -> 抽检/全量下载校验 0 文件丢失；
   - 阶段 4：25MB 合法 `.zip`（多级子目录）+ 300MB 随机字节 `.mp4` 走 5MB 分片上传合并（期间并行探针 `/health/live/`），并通过下载接口流式校验实际字节数分毫不差；
   - 阶段 5：管理员触发异步导出（Celery worker-exports）-> 轮询完成 -> 下载归档 ZIP -> `ZipFile.testzip()` 校验并逐一核对全部已提交学生文件 0 丢失。

统计 P50/P95/最大值、错误数、429/5xx/超时数与文件完整性结论，打印表格并写入 docs/压测报告.md。
"""
from __future__ import annotations

import argparse
import io
import os
import platform
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MB = 1024 * 1024
FIXTURE_DIR = ROOT / "tests" / "fixtures"

DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 1024
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 1024
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8

GRADES = ("2022级", "2023级", "2024级", "2025级")
MAJORS = ("信息安全", "计算机科学与技术", "软件工程", "人工智能", "数据科学与大数据技术")
CLASSES = ("01班", "02班", "03班", "04班", "05班")

try:
    import requests
except ImportError:  # pragma: no cover - 运行环境缺依赖时给出明确指引
    requests = None


# ---------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------

@dataclass
class Stage:
    name: str
    latencies: list[float] = field(default_factory=list)  # 毫秒
    errors: list[str] = field(default_factory=list)
    status_429: int = 0
    status_5xx: int = 0
    timeouts: int = 0

    def record(
        self,
        elapsed_ms: float | None,
        error: str | None = None,
        *,
        status_code: int | None = None,
        is_timeout: bool = False,
    ) -> None:
        if status_code == 429:
            self.status_429 += 1
        if status_code is not None and status_code >= 500:
            self.status_5xx += 1
        if is_timeout:
            self.timeouts += 1
        if error:
            self.errors.append(error)
        elif elapsed_ms is not None:
            self.latencies.append(float(elapsed_ms))

    @property
    def count(self) -> int:
        return len(self.latencies) + len(self.errors)

    @property
    def ok(self) -> int:
        return len(self.latencies)

    def percentile(self, pct: float) -> float:
        return percentile(self.latencies, pct)

    def max_ms(self) -> float:
        return max(self.latencies) if self.latencies else 0.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, index))]


class StageBook:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, Stage] = {}

    def get(self, name: str) -> Stage:
        with self._lock:
            if name not in self._stages:
                self._stages[name] = Stage(name)
            return self._stages[name]

    def record(
        self,
        name: str,
        elapsed_ms: float | None,
        error: str | None = None,
        *,
        status_code: int | None = None,
        is_timeout: bool = False,
    ) -> None:
        with self._lock:
            if name not in self._stages:
                self._stages[name] = Stage(name)
            self._stages[name].record(
                elapsed_ms,
                error,
                status_code=status_code,
                is_timeout=is_timeout,
            )

    def ordered(self, names) -> list[Stage]:
        return [self.get(name) for name in names]

    def total_errors(self) -> int:
        with self._lock:
            return sum(len(stage.errors) for stage in self._stages.values())

    def total_429(self) -> int:
        with self._lock:
            return sum(stage.status_429 for stage in self._stages.values())

    def total_5xx(self) -> int:
        with self._lock:
            return sum(stage.status_5xx for stage in self._stages.values())

    def total_timeouts(self) -> int:
        with self._lock:
            return sum(stage.timeouts for stage in self._stages.values())


# ---------------------------------------------------------------------
# Django 侧数据准备
# ---------------------------------------------------------------------

def setup_django(settings_module: str):
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    import django

    django.setup()
    from django.db import connection

    return connection


def build_valid_zip_bytes(target_mb: int) -> bytes:
    """构造带三层正常子目录、约 target_mb 大小（STORED 不压缩以便可控大小）的合法 ZIP。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.writestr("证明材料/获奖证书/一等奖.pdf", PDF_BYTES)
        zf.writestr("证明材料/团队合影/说明.txt", "2000人压测大文件分片ZIP完整性样本".encode("utf-8"))
        # 用不可压缩的随机数据撑到目标体积，并避免触发高压缩比炸弹误报
        payload_bytes = max(1, target_mb) * MB
        chunk = os.urandom(MB)
        remaining = payload_bytes
        part_idx = 1
        while remaining > 0:
            take = min(remaining, len(chunk))
            zf.writestr(f"证明材料/附件包/素材_{part_idx:02d}.bin", chunk[:take])
            remaining -= take
            part_idx += 1
    return buf.getvalue()


def provision(
    settings_module: str,
    *,
    students: int,
    dataset_size: int,
    dashboard_concurrency: int,
    password: str,
    title_prefix: str,
):
    """批量预置压测账号（最多 2000+ 人）与背景申报，返回上下文数据。"""
    setup_django(settings_module)
    from django.contrib.auth.hashers import make_password
    from django.db import transaction
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.activities.models import Activity
    from apps.reviews.models import ReviewAssignment
    from apps.submissions.models import Submission, SubmissionTeamMember
    from apps.uploads.services import ensure_standard_material_requirements

    now = timezone.now()
    activity = Activity.objects.create(
        title=f"{title_prefix} {now:%Y-%m-%d %H:%M:%S}",
        opens_at=now - timedelta(hours=1),
        deadline=now + timedelta(hours=12),
        is_open=True,
    )
    requirements = ensure_standard_material_requirements(activity)

    # 计算需要的总学生数：至少覆盖并发首页、并发上传、视频/基准账号以及 dataset_size
    active_pool_needed = max(students + 5, dashboard_concurrency + 5)
    total_students = max(dataset_size, active_pool_needed)

    password_hash = make_password(password)
    usernames = [f"26{index:06d}" for index in range(1, total_students + 1)]
    existing_map = {
        u.username: u
        for u in User.objects.filter(username__in=usernames).only(
            "id", "username", "real_name", "grade", "major", "class_name",
            "role", "is_active", "must_change_password", "password",
        )
    }

    to_create: list[User] = []
    to_update: list[User] = []
    for index, username in enumerate(usernames, start=1):
        grade = GRADES[(index - 1) % len(GRADES)]
        major = MAJORS[((index - 1) // len(GRADES)) % len(MAJORS)]
        class_name = CLASSES[((index - 1) // (len(GRADES) * len(MAJORS))) % len(CLASSES)]
        # 让前 120 名学生落在 2025级 信息安全 01班/02班，方便抽检与团支书工作台筛选
        if index <= 120:
            grade = "2025级"
            major = "信息安全"
            class_name = "01班" if index <= 60 else "02班"

        real_name = f"压测生{index:04d}"
        if username in existing_map:
            user = existing_map[username]
            user.real_name = real_name
            user.grade = grade
            user.major = major
            user.class_name = class_name
            user.role = User.Role.STUDENT
            user.is_active = True
            user.must_change_password = False
            user.password = password_hash
            to_update.append(user)
        else:
            to_create.append(
                User(
                    username=username,
                    password=password_hash,
                    real_name=real_name,
                    grade=grade,
                    major=major,
                    class_name=class_name,
                    phone=f"138{index:08d}"[:11],
                    role=User.Role.STUDENT,
                    is_active=True,
                    must_change_password=False,
                )
            )

    with transaction.atomic():
        if to_create:
            User.objects.bulk_create(to_create, batch_size=500)
        if to_update:
            User.objects.bulk_update(
                to_update,
                ["real_name", "grade", "major", "class_name", "role", "is_active", "must_change_password", "password"],
                batch_size=500,
            )

    # 压测管理员与审核团支书
    admin_user, _ = User.objects.update_or_create(
        username="admin_stress",
        defaults={
            "real_name": "压测张主任",
            "role": User.Role.ADMIN,
            "is_staff": True,
            "is_active": True,
            "must_change_password": False,
            "password": password_hash,
        },
    )
    reviewer_user, _ = User.objects.update_or_create(
        username="reviewer_stress",
        defaults={
            "real_name": "压测李团支书",
            "grade": "2025级",
            "major": "信息安全",
            "class_name": "01班",
            "role": User.Role.REVIEWER,
            "is_active": True,
            "must_change_password": False,
            "password": password_hash,
        },
    )
    ReviewAssignment.objects.get_or_create(
        reviewer=reviewer_user,
        activity=activity,
        grade="2025级",
        major="信息安全",
        class_name="01班",
    )

    # 若启用大规模数据集（如 2000 人），为背景学生批量预置真实多状态与团队成员索引申报，
    # 留出前 active_pool_needed 名学生为空白账号用于本次 HTTP 并发写/提交压测与精确导出校验。
    seeded_submissions = 0
    seeded_team_links = 0
    if dataset_size >= 200:
        bg_usernames = usernames[active_pool_needed:]
        bg_users = list(User.objects.filter(username__in=bg_usernames).order_by("username"))
        target_bg_submissions = min(len(bg_users), max(100, int(dataset_size * 0.8)))
        statuses = (
            Submission.Status.DRAFT,
            Submission.Status.PENDING_FIRST_REVIEW,
            Submission.Status.RETURNED,
            Submission.Status.PENDING_FINAL_REVIEW,
            Submission.Status.FINAL_APPROVED,
        )
        sub_objs: list[Submission] = []
        team_member_rows: list[tuple[int, str, str, str]] = []  # (sub_idx, stu_no, real_name, cls)

        for idx in range(target_bg_submissions):
            owner = bg_users[idx]
            status = statuses[idx % len(statuses)]
            is_team = (idx % 4 == 0) and (idx + 2 < len(bg_users))
            members = []
            if is_team:
                for offset in (1, 2):
                    mate = bg_users[(idx + offset) % len(bg_users)]
                    if mate.username != owner.username:
                        members.append(
                            {
                                "student_number": mate.username,
                                "real_name": mate.real_name,
                                "class_name": mate.class_name,
                            }
                        )
                        team_member_rows.append((idx, mate.username, mate.real_name, mate.class_name))

            sub = Submission(
                activity=activity,
                owner=owner,
                status=status,
                version=1,
                declaration_submitted=(status != Submission.Status.DRAFT),
                declaration_submitted_at=(now - timedelta(minutes=10)) if status != Submission.Status.DRAFT else None,
                draft_data={
                    "project_name": f"背景社会实践项目{idx + 1:04d}",
                    "category": "社会调查" if idx % 2 == 0 else "志愿服务",
                    "declaration_type": "team" if members else "individual",
                    "team_members": members,
                },
            )
            sub_objs.append(sub)

        with transaction.atomic():
            created_subs = Submission.objects.bulk_create(sub_objs, batch_size=500)
            link_objs = [
                SubmissionTeamMember(
                    submission=created_subs[sub_idx],
                    activity=activity,
                    student_number=stu_no,
                    real_name=rname,
                    class_name=cls,
                )
                for sub_idx, stu_no, rname, cls in team_member_rows
            ]
            if link_objs:
                SubmissionTeamMember.objects.bulk_create(link_objs, batch_size=500, ignore_conflicts=True)
            seeded_submissions = len(created_subs)
            seeded_team_links = len(link_objs)

    # 划分活跃测试池：
    # - 前 students 名：并发建草稿 + 5 份完整材料上传 + 提交审核（纳入导出精确对账）
    # - 前 dashboard_concurrency 名：100 并发登录 + 100 并发刷首页
    # - 倒数第 2 名活跃池成员：300MB 分片视频上传
    # - 倒数第 1 名活跃池成员：串行基准
    active_usernames = usernames[:active_pool_needed]
    active_users_info = {
        u.username: {
            "real_name": u.real_name,
            "grade": u.grade,
            "major": u.major,
            "class_name": u.class_name,
        }
        for u in User.objects.filter(username__in=active_usernames).only(
            "username", "real_name", "grade", "major", "class_name"
        )
    }

    return {
        "activity_id": str(activity.id),
        "activity_title": activity.title,
        "requirements": {code: str(req.id) for code, req in requirements.items()},
        "usernames": active_usernames,
        "active_users_info": active_users_info,
        "total_students": total_students,
        "seeded_submissions": seeded_submissions,
        "seeded_team_links": seeded_team_links,
        "admin_username": admin_user.username,
        "reviewer_username": reviewer_user.username,
    }


# ---------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------

class StudentClient:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.token = ""

    def _headers(self) -> dict:
        return {"X-CSRFToken": self.token, "Referer": self.base_url + "/"}

    def login(self, username: str, password: str) -> tuple[float, int]:
        resp_get = self.session.get(
            self.base_url + "/accounts/login/",
            allow_redirects=False,
            timeout=self.timeout,
        )
        if resp_get.status_code != 200:
            raise RuntimeError(f"打开登录页异常 HTTP {resp_get.status_code}")
        token = self.session.cookies.get("csrftoken", "")
        started = time.perf_counter()
        response = self.session.post(
            self.base_url + "/accounts/login/",
            data={"username": username, "password": password, "csrfmiddlewaretoken": token},
            headers={"Referer": self.base_url + "/accounts/login/"},
            allow_redirects=False,
            timeout=self.timeout,
        )
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code not in (302, 303):
            raise RuntimeError(f"登录失败 HTTP {response.status_code}: {response.text[:160]}")
        self.token = self.session.cookies.get("csrftoken", token)
        return elapsed, response.status_code

    def call(
        self,
        method: str,
        path: str,
        *,
        stages: StageBook,
        stage_name: str,
        expected=(200,),
        **kwargs,
    ):
        url = self.base_url + path
        started = time.perf_counter()
        try:
            response = self.session.request(
                method,
                url,
                headers=self._headers(),
                timeout=self.timeout,
                **kwargs,
            )
        except requests.Timeout as exc:
            stages.record(stage_name, None, f"TIMEOUT: {exc}", is_timeout=True)
            return None, 0.0
        except Exception as exc:  # noqa: BLE001
            stages.record(stage_name, None, f"EXC: {exc}")
            return None, 0.0

        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code not in expected:
            body = response.text.replace("\n", " ")[:160]
            stages.record(
                stage_name,
                None,
                f"HTTP {response.status_code}: {body}",
                status_code=response.status_code,
            )
        else:
            stages.record(stage_name, elapsed, status_code=response.status_code)
        return response, elapsed


# ---------------------------------------------------------------------
# 阶段 1：100 并发登录 + 100 并发刷首页（2000 人数据集）
# ---------------------------------------------------------------------

def run_dashboard_storm(ctx: dict, concurrency: int) -> dict:
    stages: StageBook = ctx["stages"]
    target_usernames = ctx["usernames"][:concurrency]
    barrier = threading.Barrier(len(target_usernames))
    html_sizes: list[int] = []
    size_lock = threading.Lock()

    def worker(username: str) -> None:
        client = StudentClient(ctx["base_url"], ctx["timeout"])
        try:
            elapsed, status_code = client.login(username, ctx["password"])
            stages.record("100并发-登录", elapsed, status_code=status_code)
        except requests.Timeout as exc:
            stages.record("100并发-登录", None, f"{username}: TIMEOUT {exc}", is_timeout=True)
            try:
                barrier.abort()
            except Exception:
                pass
            return
        except Exception as exc:  # noqa: BLE001
            code = 429 if "HTTP 429" in str(exc) else (500 if "HTTP 5" in str(exc) else None)
            stages.record("100并发-登录", None, f"{username}: {exc}", status_code=code)
            try:
                barrier.abort()
            except Exception:
                pass
            return

        # 全部 100 名学生登录完毕后齐发 GET /，剥离 PBKDF2 CPU 噪声，精准测量 100 并发首页性能
        try:
            barrier.wait(timeout=max(30.0, ctx["timeout"]))
        except threading.BrokenBarrierError:
            pass

        resp, _ = client.call(
            "GET",
            f"/?activity_id={ctx['activity_id']}",
            stages=stages,
            stage_name="100并发-刷首页",
            expected=(200,),
            allow_redirects=False,
        )
        if resp is not None and resp.status_code == 200:
            with size_lock:
                html_sizes.append(len(resp.content))

    with ThreadPoolExecutor(max_workers=len(target_usernames)) as pool:
        futures = [pool.submit(worker, uname) for uname in target_usernames]
        for fut in as_completed(futures):
            fut.result()

    # 同步探测管理员面板与审核工作台分页后的响应耗时与 HTML 大小
    admin_client = StudentClient(ctx["base_url"], ctx["timeout"])
    admin_html_kb = 0.0
    workbench_html_kb = 0.0
    try:
        admin_client.login(ctx["admin_username"], ctx["password"])
        for _ in range(5):
            r_admin, _ = admin_client.call(
                "GET",
                f"/admin-panel/?activity_id={ctx['activity_id']}",
                stages=stages,
                stage_name="管理面板-分页",
                expected=(200,),
            )
            if r_admin is not None and r_admin.status_code == 200:
                admin_html_kb = max(admin_html_kb, len(r_admin.content) / 1024.0)
            r_wb, _ = admin_client.call(
                "GET",
                f"/reviews/workbench/?activity_id={ctx['activity_id']}",
                stages=stages,
                stage_name="审核工作台-分页",
                expected=(200,),
            )
            if r_wb is not None and r_wb.status_code == 200:
                workbench_html_kb = max(workbench_html_kb, len(r_wb.content) / 1024.0)
    except Exception as exc:  # noqa: BLE001
        stages.record("管理面板-分页", None, str(exc))

    return {
        "concurrency": len(target_usernames),
        "avg_home_kb": (sum(html_sizes) / len(html_sizes) / 1024.0) if html_sizes else 0.0,
        "admin_html_kb": admin_html_kb,
        "workbench_html_kb": workbench_html_kb,
    }


# ---------------------------------------------------------------------
# 阶段 2：并发申报 + 5 份完整材料直传 + 提交审核 + 下载完整性校验
# ---------------------------------------------------------------------

def run_student(index: int, username: str, ctx: dict) -> dict:
    stages: StageBook = ctx["stages"]
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    record = {
        "username": username,
        "submitted": False,
        "uploads": [],  # list of (upload_id, expected_bytes, expected_head)
        "has_large_zip": False,
    }
    try:
        elapsed, status_code = client.login(username, ctx["password"])
        stages.record("登录", elapsed, status_code=status_code)
    except requests.Timeout as exc:
        stages.record("登录", None, f"{username}: TIMEOUT {exc}", is_timeout=True)
        return record
    except Exception as exc:  # noqa: BLE001
        code = 429 if "HTTP 429" in str(exc) else None
        stages.record("登录", None, f"{username}: {exc}", status_code=code)
        return record

    response, _ = client.call(
        "POST",
        "/api/v1/submissions/",
        stages=stages,
        stage_name="创建草稿",
        expected=(200, 201),
        json={"activity_id": ctx["activity_id"]},
    )
    submission_id = ""
    if response is not None and response.status_code in (200, 201):
        try:
            submission_id = response.json().get("id", "")
        except ValueError:
            submission_id = ""
    if not submission_id:
        return record

    save_resp, _ = client.call(
        "PATCH",
        f"/api/v1/submissions/{submission_id}/",
        stages=stages,
        stage_name="保存草稿",
        expected=(200,),
        json={
            "expected_version": 0,
            "draft_data": {
                "project_name": f"压测项目{index:02d}",
                "category": "社会调查",
                "declaration_type": "individual",
                "team_members": [],
            },
        },
    )
    if save_resp is None or save_resp.status_code != 200:
        return record

    uploads = [
        ("直传-申报表docx", "application_form", "社会实践申报表.docx", DOCX_BYTES,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04"),
        ("直传-实践报告pdf", "practice_report", "社会实践报告.pdf", PDF_BYTES, "application/pdf", b"%PDF-"),
        ("直传-现场照片jpg", "scene_photos", "现场照片1.jpg", ctx["photo_bytes"], "image/jpeg", b"\xff\xd8\xff"),
    ]
    if ctx["submit_after_upload"]:
        uploads.extend([
            ("直传-现场照片jpg", "scene_photos", "现场照片2.jpg", ctx["photo_bytes"], "image/jpeg", b"\xff\xd8\xff"),
            ("直传-现场照片jpg", "scene_photos", "现场照片3.jpg", ctx["photo_bytes"], "image/jpeg", b"\xff\xd8\xff"),
        ])

    all_ok = True
    for stage_name, code, filename, payload, content_type, head in uploads:
        up_resp, _ = client.call(
            "POST",
            "/api/v1/uploads/direct-upload/",
            stages=stages,
            stage_name=stage_name,
            expected=(201,),
            data={"requirement_id": ctx["requirements"][code], "submission_id": submission_id},
            files={"file": (filename, payload, content_type)},
        )
        if up_resp is not None and up_resp.status_code == 201:
            try:
                uid = up_resp.json().get("upload_id") or up_resp.json().get("id")
                if uid:
                    record["uploads"].append((uid, len(payload), head))
            except ValueError:
                all_ok = False
        else:
            all_ok = False

    # 第 1 名学生顺带走 5MB 分片上传 1 份 25MB 多层子目录合法 .zip（验证批次 1.3 + 1.4 + ZIP_STORED 导出）
    if index == 1 and ctx.get("large_zip_bytes"):
        zip_ok = _upload_chunked_file(
            client=client,
            stages=stages,
            requirement_id=ctx["requirements"]["other_materials"],
            submission_id=submission_id,
            material_type="other_materials",
            filename="证明材料汇总.zip",
            raw_bytes=ctx["large_zip_bytes"],
            stage_prefix="ZIP分片",
        )
        if zip_ok:
            record["has_large_zip"] = True
            record["uploads"].append((zip_ok, len(ctx["large_zip_bytes"]), b"PK\x03\x04"))
        else:
            all_ok = False

    if ctx["submit_after_upload"] and all_ok:
        sub_resp, _ = client.call(
            "POST",
            f"/api/v1/submissions/{submission_id}/submit/",
            stages=stages,
            stage_name="提交审核",
            expected=(200,),
        )
        if sub_resp is not None and sub_resp.status_code == 200:
            record["submitted"] = True

    if ctx["verify_uploads"] and record["uploads"]:
        # 校验每位学生已上传文件均可通过下载接口（生产模式下走 Nginx X-Accel-Redirect）完整读回
        for upload_id, expected_size, expected_head in record["uploads"]:
            dl_resp, _ = client.call(
                "GET",
                f"/api/v1/uploads/{upload_id}/file/",
                stages=stages,
                stage_name="附件下载核验",
                expected=(200,),
                stream=True,
            )
            if dl_resp is not None and dl_resp.status_code == 200:
                total = 0
                first_chunk = b""
                for chunk in dl_resp.iter_content(chunk_size=1024 * 256):
                    if not first_chunk:
                        first_chunk = chunk[: len(expected_head)]
                    total += len(chunk)
                if total != expected_size or not first_chunk.startswith(expected_head):
                    stages.record(
                        "附件下载核验",
                        None,
                        f"{username}/{upload_id}: 期望 {expected_size}B 实得 {total}B",
                    )

    return record


def _upload_chunked_file(
    *,
    client: StudentClient,
    stages: StageBook,
    requirement_id: str,
    submission_id: str,
    material_type: str,
    filename: str,
    raw_bytes: bytes,
    stage_prefix: str,
) -> str:
    file_size = len(raw_bytes)
    part_size = 5 * MB
    total_chunks = (file_size + part_size - 1) // part_size
    start_resp, _ = client.call(
        "POST",
        "/api/v1/uploads/chunk-start/",
        stages=stages,
        stage_name=f"{stage_prefix}-开始",
        expected=(200, 201),
        json={
            "material_type": material_type,
            "filename": filename,
            "file_size": file_size,
            "total_chunks": total_chunks,
            "requirement_id": requirement_id,
            "submission_id": submission_id,
        },
    )
    if start_resp is None or start_resp.status_code not in (200, 201):
        return ""
    session = start_resp.json()
    upload_id = session["upload_id"]
    server_chunk_size = int(session["chunk_size"])
    server_total = int(session["total_chunks"])

    for idx in range(server_total):
        offset = idx * server_chunk_size
        piece = raw_bytes[offset : offset + server_chunk_size]
        part_resp, _ = client.call(
            "POST",
            "/api/v1/uploads/chunk-upload/",
            stages=stages,
            stage_name=f"{stage_prefix}-单片",
            expected=(200,),
            data={"upload_id": upload_id, "chunk_index": str(idx)},
            files={"chunk": ("blob", piece, "application/octet-stream")},
        )
        if part_resp is None or part_resp.status_code != 200:
            return ""

    comp_resp, _ = client.call(
        "POST",
        "/api/v1/uploads/chunk-complete/",
        stages=stages,
        stage_name=f"{stage_prefix}-合并",
        expected=(200,),
        json={"upload_id": upload_id, "material_type": material_type},
    )
    if comp_resp is None or comp_resp.status_code != 200:
        return ""
    if int(comp_resp.json().get("actual_bytes", -1)) != file_size:
        stages.record(
            f"{stage_prefix}-合并",
            None,
            f"合并后字节数不匹配: 期望 {file_size}, 实际 {comp_resp.json().get('actual_bytes')}",
        )
        return ""
    return upload_id


def run_serial_baseline(ctx: dict, samples: int) -> StageBook:
    book = StageBook()
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    username = ctx["usernames"][-1]
    try:
        elapsed, status_code = client.login(username, ctx["password"])
        book.record("串行基准-登录", elapsed, status_code=status_code)
    except Exception as exc:  # noqa: BLE001
        book.record("串行基准-登录", None, str(exc))
        return book

    for _ in range(samples):
        response, _ = client.call(
            "POST",
            "/api/v1/uploads/direct-upload/",
            stages=book,
            stage_name="串行基准-直传docx",
            expected=(201,),
            data={"requirement_id": ctx["requirements"]["application_form"]},
            files={
                "file": (
                    "社会实践申报表.docx",
                    DOCX_BYTES,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        if response is None or response.status_code != 201:
            break
    return book


# ---------------------------------------------------------------------
# 阶段 3：300MB 分片视频 + 合并期健康探针 + 完整字节核验
# ---------------------------------------------------------------------

def run_video(ctx: dict, video_mb: int) -> dict:
    stages: StageBook = ctx["stages"]
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    video_bytes = video_mb * MB
    result = {
        "upload_id": "",
        "started": False,
        "completed": False,
        "download_verified": False,
        "validation_rejected": False,
        "chunk_size": 0,
        "total_chunks": 0,
        "bytes": video_bytes,
        "total_seconds": 0.0,
        "mb_per_second": 0.0,
        "health_max_ms": 0.0,
        "health_errors": 0,
        "note": "",
    }

    try:
        client.login(ctx["usernames"][-2], ctx["password"])
    except Exception as exc:  # noqa: BLE001
        stages.record("分片-开始", None, f"登录失败: {exc}")
        return result

    started = time.perf_counter()
    response, _ = client.call(
        "POST",
        "/api/v1/uploads/chunk-start/",
        stages=stages,
        stage_name="分片-开始",
        expected=(200, 201),
        json={
            "material_type": "video",
            "filename": f"{video_mb}MB压测宣讲视频.mp4",
            "file_size": video_bytes,
            "total_chunks": video_bytes // (5 * MB),
            "requirement_id": ctx["requirements"]["video"],
        },
    )
    if response is None or response.status_code not in (200, 201):
        return result
    session = response.json()
    result["started"] = True
    result["upload_id"] = session["upload_id"]
    result["chunk_size"] = int(session["chunk_size"])
    result["total_chunks"] = int(session["total_chunks"])

    # 复用随机块避免 300MB urandom 拖慢客户端自身生成速度，重点压服务端 I/O 与合并
    base_random = os.urandom(result["chunk_size"])
    first_chunk = MP4_HEAD + base_random[len(MP4_HEAD):]

    for index in range(result["total_chunks"]):
        payload = first_chunk if index == 0 else base_random
        chunk_response, _ = client.call(
            "POST",
            "/api/v1/uploads/chunk-upload/",
            stages=stages,
            stage_name="分片-单片",
            expected=(200,),
            data={"upload_id": result["upload_id"], "chunk_index": str(index)},
            files={"chunk": ("blob", payload, "application/octet-stream")},
        )
        if chunk_response is None or chunk_response.status_code != 200:
            break

    # 在 chunk-complete 合并期间并行探测 /health/live/，验证大文件合并已移出 DB 长事务且不卡死 Web
    stop_probe = threading.Event()
    probe_latencies: list[float] = []
    probe_errors = [0]

    def probe_health() -> None:
        s = requests.Session()
        while not stop_probe.is_set():
            t0 = time.perf_counter()
            try:
                r = s.get(ctx["base_url"] + "/health/live/", timeout=5.0)
                dt = (time.perf_counter() - t0) * 1000.0
                if r.status_code == 200:
                    probe_latencies.append(dt)
                else:
                    probe_errors[0] += 1
            except Exception:  # noqa: BLE001
                probe_errors[0] += 1
            stop_probe.wait(0.1)

    probe_thread = threading.Thread(target=probe_health, daemon=True)
    probe_thread.start()
    try:
        complete_response, _ = client.call(
            "POST",
            "/api/v1/uploads/chunk-complete/",
            stages=stages,
            stage_name="分片-合并",
            expected=(200,),
            json={"upload_id": result["upload_id"], "material_type": "video"},
        )
    finally:
        stop_probe.set()
        probe_thread.join(timeout=3.0)

    result["health_max_ms"] = max(probe_latencies) if probe_latencies else 0.0
    result["health_errors"] = probe_errors[0]

    total_seconds = time.perf_counter() - started
    result["total_seconds"] = total_seconds
    result["mb_per_second"] = (video_bytes / MB) / total_seconds if total_seconds > 0 else 0.0

    if complete_response is not None and complete_response.status_code == 200:
        body = complete_response.json()
        actual_bytes = int(body.get("actual_bytes", 0))
        result["completed"] = (actual_bytes == video_bytes)
        result["final_path"] = body.get("final_path", "")
        if not result["completed"]:
            result["note"] = f"合并后大小不符: {actual_bytes} != {video_bytes}"
        else:
            # 流式下载核对 300MB 字节数与魔数头，验证 Nginx X-Accel-Redirect 与物理落盘 0 截断
            dl_resp, _ = client.call(
                "GET",
                f"/api/v1/uploads/{result['upload_id']}/file/",
                stages=stages,
                stage_name="分片-下载核验",
                expected=(200,),
                stream=True,
            )
            if dl_resp is not None and dl_resp.status_code == 200:
                read_bytes = 0
                head_buf = b""
                for part in dl_resp.iter_content(chunk_size=1024 * 1024):
                    if len(head_buf) < len(MP4_HEAD):
                        head_buf += part[: len(MP4_HEAD) - len(head_buf)]
                    read_bytes += len(part)
                if read_bytes == video_bytes and head_buf.startswith(MP4_HEAD):
                    result["download_verified"] = True
                else:
                    stages.record(
                        "分片-下载核验",
                        None,
                        f"300MB 下载核验失败: 读回 {read_bytes}B, 期望 {video_bytes}B",
                    )

        if not ctx["keep_video"]:
            try:
                client.session.delete(
                    ctx["base_url"] + f"/api/v1/uploads/{result['upload_id']}/delete/",
                    headers={"X-CSRFToken": client.token, "Referer": ctx["base_url"] + "/"},
                    timeout=ctx["timeout"],
                )
            except Exception:  # noqa: BLE001
                pass
    elif complete_response is not None:
        body = complete_response.text
        if "ffprobe" in body:
            result["validation_rejected"] = True
            result["note"] = "ffprobe 拒绝随机字节（生产环境应把 UPLOAD_FFPROBE_BIN 置空）"
        else:
            result["note"] = body.replace("\n", " ")[:200]
    else:
        result["note"] = "chunk-complete 请求超时或连接中断"
    return result


# ---------------------------------------------------------------------
# 阶段 4：异步导出全链路与 ZIP 零丢失核验
# ---------------------------------------------------------------------

def run_async_export_verification(ctx: dict, submitted_records: list[dict]) -> dict:
    stages: StageBook = ctx["stages"]
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    result = {
        "job_id": "",
        "status": "not_run",
        "elapsed_seconds": 0.0,
        "zip_bytes": 0,
        "zip_entries": 0,
        "expected_students": len(submitted_records),
        "verified_students": 0,
        "missing_files": [],
        "corrupt_entry": None,
        "passed": False,
        "note": "",
    }
    try:
        client.login(ctx["admin_username"], ctx["password"])
    except Exception as exc:  # noqa: BLE001
        stages.record("导出-启动", None, f"管理员登录失败: {exc}")
        result["note"] = str(exc)
        return result

    t0 = time.perf_counter()
    start_resp, _ = client.call(
        "POST",
        "/api/v1/exports/start/",
        stages=stages,
        stage_name="导出-启动",
        expected=(200, 202),
        json={"activity_id": ctx["activity_id"]},
    )
    if start_resp is None or start_resp.status_code not in (200, 202):
        result["status"] = "start_failed"
        return result

    job_id = start_resp.json().get("job_id", "")
    result["job_id"] = job_id

    # 轮询任务状态（最长等待 180 秒）
    deadline = time.perf_counter() + 180.0
    job_status = "pending"
    while time.perf_counter() < deadline:
        st_resp, _ = client.call(
            "GET",
            f"/api/v1/exports/status/{job_id}/",
            stages=stages,
            stage_name="导出-状态轮询",
            expected=(200,),
        )
        if st_resp is None or st_resp.status_code != 200:
            result["status"] = "poll_failed"
            return result
        job_info = st_resp.json().get("job", {})
        job_status = job_info.get("status", "")
        if job_status == "done":
            break
        if job_status == "failed":
            result["status"] = "failed"
            result["note"] = str(job_info.get("error_code") or "export_failed")
            stages.record("导出-完成", None, f"ExportJob failed: {result['note']}")
            return result
        time.sleep(0.5)

    result["elapsed_seconds"] = time.perf_counter() - t0
    result["status"] = job_status
    if job_status != "done":
        stages.record("导出-完成", None, f"异步导出超时，最终状态: {job_status}", is_timeout=True)
        return result
    stages.record("导出-完成", result["elapsed_seconds"] * 1000.0)

    # 流式下载导出 ZIP 到临时文件，核对 ZipFile 完整性与每个已提交学生的文件零丢失
    dl_resp, _ = client.call(
        "GET",
        f"/api/v1/exports/{job_id}/download/",
        stages=stages,
        stage_name="导出-ZIP下载",
        expected=(200,),
        stream=True,
    )
    if dl_resp is None or dl_resp.status_code != 200:
        return result

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp_zip:
        total_written = 0
        for chunk in dl_resp.iter_content(chunk_size=1024 * 512):
            tmp_zip.write(chunk)
            total_written += len(chunk)
        tmp_zip.flush()
        tmp_zip.seek(0)
        result["zip_bytes"] = total_written

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            bad = zf.testzip()
            result["corrupt_entry"] = bad
            names = set(zf.namelist())
            result["zip_entries"] = len(names)

            missing: list[str] = []
            verified_students = 0
            users_info = ctx["active_users_info"]
            for rec in submitted_records:
                uname = rec["username"]
                info = users_info[uname]
                prefix = f"{info['grade']}/{info['major']}{info['class_name']}/{info['real_name']}+{uname}"
                expected_files = [
                    f"{prefix}/申报表.docx",
                    f"{prefix}/实践报告.pdf",
                    f"{prefix}/现场照片_1.jpg",
                    f"{prefix}/现场照片_2.jpg",
                    f"{prefix}/现场照片_3.jpg",
                ]
                if rec.get("has_large_zip"):
                    expected_files.append(f"{prefix}/其他证明材料_1.zip")
                stu_missing = [p for p in expected_files if p not in names]
                if stu_missing:
                    missing.extend(stu_missing)
                else:
                    verified_students += 1

            result["verified_students"] = verified_students
            result["missing_files"] = missing[:20]
            result["passed"] = (bad is None and not missing and verified_students == len(submitted_records))
            if not result["passed"]:
                stages.record(
                    "导出-ZIP校验",
                    None,
                    f"bad={bad}, missing_count={len(missing)}, verified={verified_students}/{len(submitted_records)}",
                )
            else:
                stages.record("导出-ZIP校验", 1.0)

    return result


# ---------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------

def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(display_width(str(row[column])) for row in [headers] + rows)
        for column in range(len(headers))
    ]
    line = "  ".join(pad(str(headers[i]), widths[i]) for i in range(len(headers)))
    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = ["  ".join(pad(str(row[i]), widths[i]) for i in range(len(headers))) for row in rows]
    return "\n".join([line, separator, *body])


def stage_rows(names, book: StageBook) -> list[list[str]]:
    return [
        [
            stage.name,
            str(stage.count),
            str(stage.ok),
            str(len(stage.errors)),
            f"{stage.percentile(50):.0f}",
            f"{stage.percentile(95):.0f}",
            f"{stage.max_ms():.0f}",
        ]
        for stage in book.ordered(names)
    ]


def render_report(
    cfg,
    env_info: dict,
    book: StageBook,
    dashboard_info: dict | None,
    video: dict,
    export_info: dict | None,
    baseline: StageBook,
    db_vendor: str,
) -> str:
    now = datetime.now().astimezone()
    concurrent_names = [
        "登录", "创建草稿", "保存草稿", "直传-申报表docx", "直传-实践报告pdf", "直传-现场照片jpg",
    ]
    if cfg.submit_after_upload:
        concurrent_names.append("提交审核")
    if cfg.verify_uploads:
        concurrent_names.append("附件下载核验")

    upload_latencies = [value for name in concurrent_names[3:6] for value in book.get(name).latencies]
    upload_p95 = percentile(upload_latencies, 95)
    verdict_upload = "通过" if upload_p95 < 2000 else "未达标"

    lines = [
        "# 压测报告",
        "",
        f"- 生成时间：{now:%Y-%m-%d %H:%M:%S %Z}",
        f"- 目标地址：{cfg.base_url}",
        f"- 活动 ID：{env_info['activity_id']}",
        f"- 数据集规模：{env_info['total_students']} 名学生 / 预置背景申报 {env_info['seeded_submissions']} 份 / 团队索引行 {env_info['seeded_team_links']} 条",
        f"- 并发学生数：{cfg.students}（线程池 {cfg.concurrency}），首页压测并发：{cfg.dashboard_concurrency if dashboard_info else 0}",
        f"- 数据库：{db_vendor}（{'开发副本，写事务串行，仅验证链路' if db_vendor == 'sqlite' else '生产级 PostgreSQL，验证行锁与连接池'}）",
        f"- Django settings：{cfg.settings}",
        f"- Python：{platform.python_version()} / {platform.system()} {platform.release()}",
        "",
    ]

    if dashboard_info:
        home_stage = book.get("100并发-刷首页")
        home_p95 = home_stage.percentile(95)
        verdict_home = "通过" if (home_p95 < 1000 and not home_stage.errors) else "未达标"
        lines.extend([
            "## 1. 2000 人数据集 · 100 并发刷首页与管理端分页",
            "",
            format_table(
                ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
                stage_rows(["100并发-登录", "100并发-刷首页", "审核工作台-分页", "管理面板-分页"], book),
            ),
            "",
            f"- 100 并发同 IP 登录：成功 {book.get('100并发-登录').ok}/{book.get('100并发-登录').count}，429 拦截数 **{book.get('100并发-登录').status_429}**。",
            f"- 100 并发刷首页 `/`：P50 {home_stage.percentile(50):.0f} ms、P95 {home_p95:.0f} ms、最大 {home_stage.max_ms():.0f} ms（验收线 P95 < 1000 ms -> **{verdict_home}**）。",
            f"- 单页体积：首页均值 {dashboard_info['avg_home_kb']:.1f} KB，审核工作台 {dashboard_info['workbench_html_kb']:.1f} KB，管理面板 {dashboard_info['admin_html_kb']:.1f} KB。",
            "",
        ])

    lines.extend([
        "## 2. 并发申报、附件直传与下载零丢失核验",
        "",
        format_table(
            ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
            stage_rows(concurrent_names, book),
        ),
        "",
        f"- 小附件直传合计 {len(upload_latencies)} 次成功请求，整体 P50 {percentile(upload_latencies, 50):.0f} ms、"
        f"P95 {upload_p95:.0f} ms；验收线 P95 < 2000 ms -> **{verdict_upload}**。",
    ])
    if cfg.large_zip_mb > 0:
        lines.extend([
            "",
            format_table(
                ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
                stage_rows(["ZIP分片-开始", "ZIP分片-单片", "ZIP分片-合并"], book),
            ),
        ])
    lines.append("")

    lines.extend([
        f"## 3. {cfg.chunk_video_mb}MB 分片视频合并与合并期探针",
        "",
        format_table(
            ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
            stage_rows(["分片-开始", "分片-单片", "分片-合并", "分片-下载核验"], book),
        ),
        "",
        f"- 分片大小：{video['chunk_size'] // MB if video['chunk_size'] else 0} MB x {video['total_chunks']} 片，共 {cfg.chunk_video_mb} MB。",
        f"- 总耗时 {video['total_seconds']:.2f} s，实测吞吐 {video['mb_per_second']:.1f} MB/s。",
        f"- `chunk-complete`：{'完成' if video['completed'] else '未完成'}；流式下载字节核验：{'通过（0 截断）' if video['download_verified'] else '未通过或跳过'}。",
        f"- 合并期间 `/health/live/` 探针最大耗时 {video['health_max_ms']:.1f} ms，失败次数 {video['health_errors']}。",
        "",
    ])

    if export_info:
        lines.extend([
            "## 4. 异步导出与归档 ZIP 零丢失校验",
            "",
            format_table(
                ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
                stage_rows(["导出-启动", "导出-完成", "导出-ZIP下载", "导出-ZIP校验"], book),
            ),
            "",
            f"- 导出任务 ID：`{export_info['job_id']}`，状态：**{export_info['status']}**，耗时 {export_info['elapsed_seconds']:.2f} s。",
            f"- 归档包大小：{export_info['zip_bytes'] / MB:.2f} MB，总条目数：{export_info['zip_entries']}，`ZipFile.testzip()`：{export_info['corrupt_entry'] or 'OK'}。",
            f"- 逐生对账：{export_info['verified_students']}/{export_info['expected_students']} 名已提交学生材料齐全，缺失文件数 **{len(export_info['missing_files'])}** -> **{'通过' if export_info['passed'] else '未通过'}**。",
            "",
        ])

    upload_p50 = book.get("直传-申报表docx").percentile(50)
    baseline_p50 = baseline.get("串行基准-直传docx").percentile(50)
    queue_ratio = (upload_p50 / baseline_p50) if baseline_p50 else 0.0
    lines.extend([
        "## 5. 串行基准与总体健康汇总",
        "",
        format_table(
            ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
            stage_rows(["直传-申报表docx"], book) + stage_rows(["串行基准-直传docx"], baseline),
        ),
        "",
        f"- 并发直传 P50 {upload_p50:.0f} ms，串行 P50 {baseline_p50:.0f} ms，比值 **{queue_ratio:.2f}x**。",
        f"- 全局统计：总错误数 **{book.total_errors()}**，5xx 崩溃数 **{book.total_5xx()}**，超时数 **{book.total_timeouts()}**，429 限流数 **{book.total_429()}**。",
        "",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="学院社会实践材料平台并发与 2000 人整站验收压测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--students", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--dataset-size", type=int, default=0, help="预置全库学生数据集规模（如 2000）")
    parser.add_argument("--dashboard-concurrency", type=int, default=100, help="首页并发刷页线程数")
    parser.add_argument("--run-dashboard", action="store_true", help="执行 100 并发首页与管理端分页压测")
    parser.add_argument("--submit-after-upload", action="store_true", help="并发直传完整 5 份材料后提交审核")
    parser.add_argument("--verify-uploads", action="store_true", help="逐一通过下载接口核对上传文件零丢失")
    parser.add_argument("--run-export", action="store_true", help="触发异步导出并下载校验 ZIP 文件零丢失")
    parser.add_argument("--large-zip-mb", type=int, default=0, help="为第 1 名学生附加多级子目录大 ZIP 分片上传（MB）")
    parser.add_argument("--chunk-video-mb", type=int, default=300, help="分片视频大小（MB，默认 300）")
    parser.add_argument("--full-acceptance", action="store_true", help="一键执行 5.2 2000 人全链路验收压测")
    parser.add_argument("--password", default="Stress#2026")
    parser.add_argument("--settings", default="config.settings.development")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--skip-video", action="store_true")
    parser.add_argument("--keep-video", action="store_true")
    parser.add_argument("--baseline-samples", type=int, default=10)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    if args.full_acceptance:
        args.dataset_size = max(args.dataset_size, 2000)
        args.dashboard_concurrency = max(args.dashboard_concurrency, 100)
        args.run_dashboard = True
        args.submit_after_upload = True
        args.verify_uploads = True
        args.run_export = True
        if args.large_zip_mb == 0:
            args.large_zip_mb = 25

    if requests is None:
        print("缺少 requests：先运行 .\\.venv\\Scripts\\python.exe -m pip install -r requirements/dev.txt")
        return 2

    report_path = Path(args.report).expanduser() if args.report else (
        (ROOT.parent / "docs" / "压测报告.md") if (ROOT.parent / "docs").is_dir()
        else (ROOT / "docs" / "压测报告.md")
    )

    started_at = time.perf_counter()
    db_vendor = setup_django(args.settings).vendor

    print(f"[1/6] 准备压测数据集（目标 {max(args.dataset_size, args.students)} 名学生 + 活动）…")
    env_info = provision(
        args.settings,
        students=args.students,
        dataset_size=args.dataset_size,
        dashboard_concurrency=args.dashboard_concurrency if args.run_dashboard else 0,
        password=args.password,
        title_prefix="整站验收压测" if args.full_acceptance else "压测活动",
    )
    print(
        f"      已就绪：总学生 {env_info['total_students']}，背景申报 {env_info['seeded_submissions']}，"
        f"团队索引 {env_info['seeded_team_links']}，活动 {env_info['activity_id']}"
    )

    photo_path = FIXTURE_DIR / "walkthrough.jpg"
    if not photo_path.exists():
        print(f"缺少测试照片 {photo_path}，无法执行现场照片直传。")
        return 2
    photo_bytes = photo_path.read_bytes()
    large_zip_bytes = build_valid_zip_bytes(args.large_zip_mb) if args.large_zip_mb > 0 else b""

    book = StageBook()
    ctx = {
        "base_url": args.base_url,
        "timeout": args.timeout,
        "password": args.password,
        "activity_id": env_info["activity_id"],
        "requirements": env_info["requirements"],
        "usernames": env_info["usernames"],
        "active_users_info": env_info["active_users_info"],
        "admin_username": env_info["admin_username"],
        "reviewer_username": env_info["reviewer_username"],
        "photo_bytes": photo_bytes,
        "large_zip_bytes": large_zip_bytes,
        "stages": book,
        "keep_video": args.keep_video,
        "submit_after_upload": args.submit_after_upload,
        "verify_uploads": args.verify_uploads,
    }

    dashboard_info = None
    if args.run_dashboard or args.dataset_size > 0:
        print(
            f"[2/6] 首页并发风暴：{args.dashboard_concurrency} 并发同 IP 登录 + "
            f"{args.dashboard_concurrency} 并发刷首页 + 管理端分页 …"
        )
        dashboard_info = run_dashboard_storm(ctx, args.dashboard_concurrency)
    else:
        print("[2/6] 跳过首页并发风暴阶段（未开启 --run-dashboard）")

    print(f"[3/6] 并发上传阶段：{args.students} 名学生 / {args.concurrency} 线程 …")
    submitted_records: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(run_student, index, username, ctx)
            for index, username in enumerate(env_info["usernames"][: args.students], start=1)
        ]
        for future in as_completed(futures):
            rec = future.result()
            if rec.get("submitted"):
                submitted_records.append(rec)

    baseline = StageBook()
    if args.baseline_samples > 0:
        print(f"[4/6] 串行基准：1 次登录 + {args.baseline_samples} 次直传 …")
        baseline = run_serial_baseline(ctx, args.baseline_samples)

    video = {
        "upload_id": "", "started": False, "completed": False, "download_verified": False,
        "validation_rejected": False, "chunk_size": 0, "total_chunks": 0,
        "bytes": args.chunk_video_mb * MB, "total_seconds": 0.0, "mb_per_second": 0.0,
        "health_max_ms": 0.0, "health_errors": 0, "note": "已跳过（--skip-video）",
    }
    if not args.skip_video:
        print(f"[5/6] 大文件分片阶段：{args.chunk_video_mb}MB 分片视频 + 合并期健康探针 + 流式下载核验 …")
        video = run_video(ctx, args.chunk_video_mb)
    else:
        print("[5/6] 跳过分片视频阶段（--skip-video）")

    export_info = None
    if args.run_export:
        print(f"[6/6] 异步导出校验：触发 Celery 异步导出并核对 {len(submitted_records)} 名已提交学生 ZIP 文件零丢失 …")
        export_info = run_async_export_verification(ctx, submitted_records)
    else:
        print("[6/6] 跳过异步导出阶段（未开启 --run-export）")

    headers = ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"]
    if dashboard_info:
        print("\n首页与管理端分页：")
        print(
            format_table(
                headers,
                stage_rows(["100并发-登录", "100并发-刷首页", "审核工作台-分页", "管理面板-分页"], book),
            )
        )

    concurrent_names = [
        "登录", "创建草稿", "保存草稿", "直传-申报表docx", "直传-实践报告pdf", "直传-现场照片jpg",
    ]
    if args.submit_after_upload:
        concurrent_names.append("提交审核")
    if args.verify_uploads:
        concurrent_names.append("附件下载核验")
    print("\n并发上传与核验：")
    print(format_table(headers, stage_rows(concurrent_names, book)))

    if not args.skip_video:
        print(f"\n{args.chunk_video_mb}MB 分片视频：")
        print(format_table(headers, stage_rows(["分片-开始", "分片-单片", "分片-合并", "分片-下载核验"], book)))
        print(
            f"{args.chunk_video_mb}MB 总耗时 {video['total_seconds']:.2f}s，吞吐 {video['mb_per_second']:.1f} MB/s，"
            f"合并 {'成功' if video['completed'] else '失败'}，下载核验 {'通过' if video['download_verified'] else '失败'}，"
            f"合并期 /health/live/ 最大 {video['health_max_ms']:.1f}ms (错误 {video['health_errors']})"
        )

    if export_info:
        print("\n异步导出与 ZIP 零丢失校验：")
        print(format_table(headers, stage_rows(["导出-启动", "导出-完成", "导出-ZIP下载", "导出-ZIP校验"], book)))
        print(
            f"导出状态={export_info['status']}，耗时={export_info['elapsed_seconds']:.2f}s，"
            f"包大小={export_info['zip_bytes'] / MB:.2f}MB，条目={export_info['zip_entries']}，"
            f"核对通过={export_info['verified_students']}/{export_info['expected_students']}，"
            f"缺失={len(export_info['missing_files'])}"
        )

    upload_latencies = [value for name in concurrent_names[3:6] for value in book.get(name).latencies]
    upload_p95 = percentile(upload_latencies, 95)

    ok_all = (
        book.total_errors() == 0
        and book.total_5xx() == 0
        and book.total_timeouts() == 0
        and upload_p95 < 2000
    )
    if dashboard_info:
        ok_all = ok_all and book.get("100并发-刷首页").percentile(95) < 1000
    if not args.skip_video:
        ok_all = ok_all and video["completed"] and video["download_verified"]
    if export_info:
        ok_all = ok_all and export_info["passed"]

    print(
        f"\n全局验收：总错误={book.total_errors()}，5xx={book.total_5xx()}，超时={book.total_timeouts()}，"
        f"429={book.total_429()}，小附件直传 P95={upload_p95:.0f}ms -> {'PASS' if ok_all else 'FAIL'}"
    )

    report = render_report(args, env_info, book, dashboard_info, video, export_info, baseline, db_vendor)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(f"报告已写入：{report_path}")
    print(f"总耗时：{time.perf_counter() - started_at:.2f}s")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
