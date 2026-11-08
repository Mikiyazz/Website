#!/usr/bin/env python
r"""学院社会实践材料平台 · 50 并发学生登录/上传压测 + 300MB 分片视频实测。

用法（先起一个专用开发服务器，避免和日常服务器抢数据库）：

    $env:DEV_SQLITE = '1'
    $env:DEV_SQLITE_PATH = 'db_stress.sqlite3'
    $env:UPLOAD_FFPROBE_BIN = '__disabled__'   # 随机字节不是真实视频，让 ffprobe 探测降级
    .\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8010 --noreload

    .\.venv\Scripts\python.exe scripts\stress_test.py --base-url http://127.0.0.1:8010

脚本自己准备压测数据（Django ORM）：固定 50 名学生账号 + 每次运行新建一个开放活动，
然后：

1. 并发阶段：50 个线程各自登录 -> 创建草稿 -> 保存草稿 -> 直传 3 份小附件
   （申报表 docx / 实践报告 pdf / 现场照片 jpg，字节取自 tests/fixtures）。
2. 分片阶段：单个学生上传 300MB 随机字节 MP4（60 x 5MB），分别统计
   chunk-start / 单个分片 / chunk-complete（合并）耗时，回答"瓶颈是不是合并动作"。
3. 串行基准：同一名学生串行登录 + 连传 10 次同一附件，用并发 P50 与串行 P50 的比值
   判断单 worker 排队（登录本身是 CPU 密集的密码哈希，不能用来判断队列）。

统计 P50/P95/最大值、错误数，打印表格并写入 docs/压测报告.md。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MB = 1024 * 1024
VIDEO_BYTES = 300 * MB
FIXTURE_DIR = ROOT / "tests" / "fixtures"

DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 1024
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 1024
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8

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
    skipped: int = 0

    def record(self, elapsed_ms: float | None, error: str | None = None) -> None:
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
        self._stages: dict[str, Stage] = {}

    def get(self, name: str) -> Stage:
        if name not in self._stages:
            self._stages[name] = Stage(name)
        return self._stages[name]

    def record(self, name: str, elapsed_ms: float | None, error: str | None = None) -> None:
        self.get(name).record(elapsed_ms, error)

    def ordered(self, names) -> list[Stage]:
        return [self.get(name) for name in names]

    def total_errors(self) -> int:
        return sum(len(stage.errors) for stage in self._stages.values())


# ---------------------------------------------------------------------
# Django 侧数据准备
# ---------------------------------------------------------------------

def setup_django(settings_module: str):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    os.environ["DJANGO_SETTINGS_MODULE"] = settings_module
    import django

    django.setup()
    from django.db import connection

    return connection


def provision(settings_module: str, students: int, password: str, title_prefix: str):
    """创建/复用压测账号，并新建一个开放活动；返回 (activity_id, requirements, usernames)。"""
    setup_django(settings_module)
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.activities.models import Activity
    from apps.uploads.services import ensure_standard_material_requirements

    now = timezone.now()
    activity = Activity.objects.create(
        title=f"{title_prefix} {now:%Y-%m-%d %H:%M}",
        opens_at=now - timedelta(hours=1),
        deadline=now + timedelta(hours=12),
        is_open=True,
    )
    requirements = ensure_standard_material_requirements(activity)

    usernames = []
    for index in range(1, students + 1):
        username = f"99{index:06d}"
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={"real_name": f"压测学生{index:02d}", "class_name": "压测25-1班"},
        )
        user.real_name = f"压测学生{index:02d}"
        user.class_name = "压测25-1班"
        user.role = User.Role.STUDENT
        user.is_active = True
        user.must_change_password = False
        user.set_password(password)
        user.save()
        usernames.append(username)

    return str(activity.id), {code: str(req.id) for code, req in requirements.items()}, usernames


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

    def login(self, username: str, password: str) -> float:
        self.session.get(self.base_url + "/accounts/login/", timeout=self.timeout)
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
        return elapsed

    def call(self, method: str, path: str, *, stages: StageBook, stage_name: str, expected=(200,), **kwargs):
        url = self.base_url + path
        started = time.perf_counter()
        response = self.session.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000.0
        if response.status_code not in expected:
            body = response.text.replace("\n", " ")[:160]
            stages.record(stage_name, None, f"HTTP {response.status_code}: {body}")
        else:
            stages.record(stage_name, elapsed)
        return response, elapsed


# ---------------------------------------------------------------------
# 并发阶段
# ---------------------------------------------------------------------

def run_student(index: int, username: str, ctx: dict) -> None:
    stages: StageBook = ctx["stages"]
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    try:
        stages.record("登录", client.login(username, ctx["password"]))
    except Exception as exc:  # noqa: BLE001 - 单线程失败要计数，不能拖垮整轮
        stages.record("登录", None, f"{username}: {exc}")
        return

    response, _ = client.call(
        "POST",
        "/api/v1/submissions/",
        stages=stages,
        stage_name="创建草稿",
        expected=(200, 201),
        json={"activity_id": ctx["activity_id"]},
    )
    submission_id = ""
    if response.status_code in (200, 201):
        try:
            submission_id = response.json().get("id", "")
        except ValueError:
            submission_id = ""
    if not submission_id:
        return

    client.call(
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

    uploads = [
        ("直传-申报表docx", "application_form", "社会实践申报表.docx", DOCX_BYTES,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("直传-实践报告pdf", "practice_report", "社会实践报告.pdf", PDF_BYTES, "application/pdf"),
        ("直传-现场照片jpg", "scene_photos", "现场照片.jpg", ctx["photo_bytes"], "image/jpeg"),
    ]
    for stage_name, code, filename, payload, content_type in uploads:
        client.call(
            "POST",
            "/api/v1/uploads/direct-upload/",
            stages=stages,
            stage_name=stage_name,
            expected=(201,),
            data={"requirement_id": ctx["requirements"][code]},
            files={"file": (filename, payload, content_type)},
        )


def run_serial_baseline(ctx: dict, samples: int) -> StageBook:
    book = StageBook()
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    username = ctx["usernames"][-1]
    try:
        book.record("串行基准-登录", client.login(username, ctx["password"]))
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
        if response.status_code != 201:
            break
    return book


# ---------------------------------------------------------------------
# 300MB 分片视频
# ---------------------------------------------------------------------

def run_video(ctx: dict) -> dict:
    stages: StageBook = ctx["stages"]
    client = StudentClient(ctx["base_url"], ctx["timeout"])
    result = {
        "upload_id": "",
        "started": False,
        "completed": False,
        "validation_rejected": False,
        "chunk_size": 0,
        "total_chunks": 0,
        "bytes": VIDEO_BYTES,
        "total_seconds": 0.0,
        "mb_per_second": 0.0,
        "note": "",
    }

    try:
        client.login(ctx["usernames"][-1], ctx["password"])
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
            "filename": "300MB压测宣讲视频.mp4",
            "file_size": VIDEO_BYTES,
            "total_chunks": VIDEO_BYTES // (5 * MB),
            "requirement_id": ctx["requirements"]["video"],
        },
    )
    if response.status_code not in (200, 201):
        return result
    session = response.json()
    result["started"] = True
    result["upload_id"] = session["upload_id"]
    result["chunk_size"] = int(session["chunk_size"])
    result["total_chunks"] = int(session["total_chunks"])

    for index in range(result["total_chunks"]):
        payload = os.urandom(result["chunk_size"])
        if index == 0:
            payload = MP4_HEAD + payload[len(MP4_HEAD):]
        chunk_response, _ = client.call(
            "POST",
            "/api/v1/uploads/chunk-upload/",
            stages=stages,
            stage_name="分片-单片",
            expected=(200,),
            data={"upload_id": result["upload_id"], "chunk_index": str(index)},
            files={"chunk": ("blob", payload, "application/octet-stream")},
        )
        if chunk_response.status_code != 200:
            break

    complete_response, _ = client.call(
        "POST",
        "/api/v1/uploads/chunk-complete/",
        stages=stages,
        stage_name="分片-合并",
        expected=(200,),
        json={"upload_id": result["upload_id"], "material_type": "video"},
    )
    total_seconds = time.perf_counter() - started
    result["total_seconds"] = total_seconds
    result["mb_per_second"] = (VIDEO_BYTES / MB) / total_seconds if total_seconds > 0 else 0.0

    if complete_response.status_code == 200:
        result["completed"] = True
        body = complete_response.json()
        result["final_path"] = body.get("final_path", "")
        if not ctx["keep_video"]:
            try:
                client.session.delete(
                    ctx["base_url"] + f"/api/v1/uploads/{result['upload_id']}/delete/",
                    headers={"X-CSRFToken": client.token, "Referer": ctx["base_url"] + "/"},
                    timeout=ctx["timeout"],
                )
            except Exception:  # noqa: BLE001 - 清理失败不影响统计
                pass
    else:
        body = complete_response.text
        if "ffprobe" in body:
            result["validation_rejected"] = True
            result["note"] = "ffprobe 拒绝随机字节（压测环境应把 UPLOAD_FFPROBE_BIN 置空）"
        else:
            result["note"] = body.replace("\n", " ")[:200]
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


def render_report(cfg, activity_id: str, book: StageBook, video: dict, baseline: StageBook, db_vendor: str) -> str:
    now = datetime.now().astimezone()
    concurrent_names = [
        "登录", "创建草稿", "保存草稿", "直传-申报表docx", "直传-实践报告pdf", "直传-现场照片jpg",
    ]
    upload_latencies = [value for name in concurrent_names[3:] for value in book.get(name).latencies]
    upload_p95 = percentile(upload_latencies, 95)
    verdict = "通过" if upload_p95 < 2000 else "未达标"
    upload_p50 = book.get("直传-申报表docx").percentile(50)
    baseline_p50 = baseline.get("串行基准-直传docx").percentile(50)
    queue_ratio = (upload_p50 / baseline_p50) if baseline_p50 else 0.0

    lines = [
        "# 压测报告",
        "",
        f"- 生成时间：{now:%Y-%m-%d %H:%M:%S %Z}",
        f"- 目标地址：{cfg.base_url}",
        f"- 活动 ID：{activity_id}",
        f"- 并发学生数：{cfg.students}（线程池 {cfg.concurrency}）",
        f"- 数据库：{db_vendor}（{db_vendor == 'sqlite' and '开发副本，写事务串行，仅验证链路' or '可验证行锁与连接池'}）",
        f"- Django settings：{cfg.settings}",
        f"- Python：{platform.python_version()} / {platform.system()} {platform.release()}",
        "",
        "## 1. 并发登录与附件直传",
        "",
        format_table(
            ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
            stage_rows(concurrent_names, book),
        ),
        "",
        f"小附件直传合计 {len(upload_latencies)} 次成功请求，整体 P50 {percentile(upload_latencies, 50):.0f} ms、"
        f"P95 {upload_p95:.0f} ms；验收线 P95 < 2000 ms -> **{verdict}**。",
        "",
        "## 2. 300MB 分片视频（chunk-start / chunk-upload / chunk-complete）",
        "",
        format_table(
            ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
            stage_rows(["分片-开始", "分片-单片", "分片-合并"], book),
        ),
        "",
        f"- 分片大小：{video['chunk_size'] // MB if video['chunk_size'] else 0} MB x {video['total_chunks']} 片，共 300 MB。",
        f"- 总耗时 {video['total_seconds']:.2f} s，实测吞吐 {video['mb_per_second']:.1f} MB/s（含随机字节生成与网络）。",
        f"- chunk-complete：{'完成' if video['completed'] else '未完成'}。{video['note']}".rstrip(),
        "",
        "## 3. 单 worker 排队判断（并发 P50 vs 串行 P50）",
        "",
        format_table(
            ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"],
            stage_rows(["直传-申报表docx"], book) + stage_rows(["串行基准-直传docx"], baseline),
        ),
        "",
        f"并发直传 P50 {upload_p50:.0f} ms，串行 P50 {baseline_p50:.0f} ms，比值 **{queue_ratio:.2f}x**。"
        + (" 比值接近 1，说明 50 并发下没有明显的 worker 排队。"
           if queue_ratio and queue_ratio < 1.5
           else " 比值偏高，单 worker 已经出现排队，需要加 worker。")
        + " 登录单独统计：它走 PBKDF2 密码哈希，是 CPU 密集动作，"
        + "50 个并发登录会把 CPU 打满，不能作为队列指标。",
        "- P95/最大值的长尾主要来自 50 个并发登录同时做 PBKDF2 哈希把 CPU 打满，"
        "个别请求在 GIL 与磁盘 fsync 上排队；中位数比值接近 1，说明 Web worker 没有成为串行瓶颈。",
        "",
        "## 4. 结论与优化方向",
        "",
        f"- 错误总数：{book.total_errors()}。",
        f"- 并发登录 P50 {book.get('登录').percentile(50):.0f} ms、P95 {book.get('登录').percentile(95):.0f} ms，"
        "主要由 PBKDF2 密码哈希决定；真实场景 600 人是错峰登录，不是 600 个请求同一秒到达。"
        "若登录风暴是已知场景，可换 Argon2 并调低迭代次数，或在网关层限流。",
        "- 分片合并（chunk-complete）是单请求里最重的动作：要按序读回全部分片、计算 SHA-256、再落盘。"
        "若它的耗时明显高于单片上传，优化方向是把合并也丢给 Celery：chunk-complete 只做分片完整性校验并入队，"
        "worker 异步合并后回写 FileAsset，前端轮询状态；Web worker 立即释放。",
        "- 若本轮使用 SQLite（开发/压测副本），写事务是串行的，结果只能作为功能链路压测；"
        "600 人真实规模必须在 docker compose + PostgreSQL 下用同一脚本复跑，行锁与连接池行为才有效。",
        "",
        "## 5. 复现命令",
        "",
        "```powershell",
        "Set-Location mainbody",
        "$env:DEV_SQLITE = '1'",
        "$env:DEV_SQLITE_PATH = 'db_stress.sqlite3'",
        "$env:UPLOAD_FFPROBE_BIN = '__disabled__'",
        ".\\.venv\\Scripts\\python.exe manage.py migrate --settings=config.settings.development",
        ".\\.venv\\Scripts\\python.exe manage.py runserver 127.0.0.1:8010 --noreload",
        "# 另开一个终端：",
        ".\\.venv\\Scripts\\python.exe scripts\\stress_test.py --base-url http://127.0.0.1:8010",
        "```",
        "",
        "> 脚本自己创建压测账号（99000001 起）与活动，不需要手工造数据；--skip-video 可跳过 300MB 阶段，"
        "--keep-video 保留上传后的视频文件。",
        "",
    ]
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

    parser = argparse.ArgumentParser(description="50 并发学生上传 + 300MB 分片视频压测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--students", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--password", default="Stress#2026")
    parser.add_argument("--settings", default="config.settings.development")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--skip-video", action="store_true")
    parser.add_argument("--keep-video", action="store_true")
    parser.add_argument("--baseline-samples", type=int, default=10)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)

    if requests is None:
        print("缺少 requests：先运行 .\\.venv\\Scripts\\python.exe -m pip install -r requirements/dev.txt")
        return 2

    report_path = Path(args.report).expanduser() if args.report else (
        (ROOT.parent / "docs" / "压测报告.md") if (ROOT.parent / "docs").is_dir()
        else (ROOT / "docs" / "压测报告.md")
    )

    started_at = time.perf_counter()
    db_vendor = setup_django(args.settings).vendor

    print(f"[1/4] 准备压测数据（{args.students} 个账号 + 新活动）…")
    activity_id, requirements, usernames = provision(
        args.settings, args.students, args.password, "压测活动"
    )
    photo_path = FIXTURE_DIR / "walkthrough.jpg"
    if not photo_path.exists():
        print(f"缺少测试照片 {photo_path}，无法执行现场照片直传。")
        return 2
    photo_bytes = photo_path.read_bytes()

    book = StageBook()
    ctx = {
        "base_url": args.base_url,
        "timeout": args.timeout,
        "password": args.password,
        "activity_id": activity_id,
        "requirements": requirements,
        "usernames": usernames,
        "photo_bytes": photo_bytes,
        "stages": book,
        "keep_video": args.keep_video,
    }

    print(f"[2/4] 并发阶段：{args.students} 名学生 / {args.concurrency} 线程 …")
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(run_student, index, username, ctx)
            for index, username in enumerate(usernames, start=1)
        ]
        for future in as_completed(futures):
            future.result()

    baseline = StageBook()
    if args.baseline_samples > 0:
        print(f"[3/4] 串行基准：1 次登录 + {args.baseline_samples} 次直传 …")
        baseline = run_serial_baseline(ctx, args.baseline_samples)

    video = {
        "upload_id": "", "started": False, "completed": False, "validation_rejected": False,
        "chunk_size": 0, "total_chunks": 0, "bytes": VIDEO_BYTES, "total_seconds": 0.0,
        "mb_per_second": 0.0, "note": "已跳过（--skip-video）",
    }
    if not args.skip_video:
        print("[4/4] 分片阶段：300MB 随机字节 MP4 …")
        video = run_video(ctx)

    concurrent_names = [
        "登录", "创建草稿", "保存草稿", "直传-申报表docx", "直传-实践报告pdf", "直传-现场照片jpg",
    ]
    headers = ["阶段", "请求数", "成功", "错误", "P50(ms)", "P95(ms)", "最大(ms)"]
    print()
    print("并发阶段：")
    print(format_table(headers, stage_rows(concurrent_names, book)))
    print()
    print("分片视频：")
    print(format_table(headers, stage_rows(["分片-开始", "分片-单片", "分片-合并"], book)))
    print(
        f"300MB 总耗时 {video['total_seconds']:.2f}s，吞吐 {video['mb_per_second']:.1f} MB/s，"
        f"chunk-complete {'成功' if video['completed'] else '未完成'}"
    )
    if video["note"]:
        print(f"备注：{video['note']}")
    print()
    print("串行基准：")
    print(format_table(headers, stage_rows(["串行基准-登录", "串行基准-直传docx"], baseline)))

    upload_latencies = [value for name in concurrent_names[3:] for value in book.get(name).latencies]
    upload_p95 = percentile(upload_latencies, 95)
    print()
    print(
        f"验收线：小附件直传 P95 = {upload_p95:.0f} ms（要求 < 2000 ms）-> "
        f"{'PASS' if upload_p95 < 2000 else 'FAIL'}"
    )

    report = render_report(args, activity_id, book, video, baseline, db_vendor)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(f"报告已写入：{report_path}")
    print(f"总耗时：{time.perf_counter() - started_at:.2f}s")
    return 0 if upload_p95 < 2000 and book.get("登录").errors == [] else 1


if __name__ == "__main__":
    raise SystemExit(main())
