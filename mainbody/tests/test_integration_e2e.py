"""阶段 0~4 收口集成测试：八个端到端场景 + 一条 PostgreSQL 行锁用例。

本文件不替代既有单元测试，而是把"学生从注册到导出"的完整链路按真实页面/接口串起来：

1. 注册 -> 登录 -> 首改密提醒 -> 改密 -> 申报页
2. 个人申报四步 -> 直传附件 -> 300MB 分片视频 -> 提交（Submitted）
3. 团队申报：队长 + 2 名队员学号，/api/students/lookup/ 实时校验
4. 审核老师：工作台班级筛选 -> 展开历史 -> 驳回 -> 学生端看到驳回
5. 白名单补交：截止后非白名单被拒、白名单提交成功且徽章 LATE_SUBMITTED
6. 越权：学生 A 拿学生 B 的 upload_id 读写均 403
7. 安全测验：fake_docx.exe 改名 .docx 被魔数拦截
8. 导出：10 份申报异步导出成功，ZIP 目录结构正确

SQLite 轻量模式（TEST_SQLITE=1）会跳过标了 postgresql 的用例：SQLite 没有真正的
SELECT ... FOR UPDATE 行锁，并发语义必须在 docker compose + PostgreSQL 下复测。
"""
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection, connections
from django.utils import timezone

from apps.accounts.models import StudentRoster, User
from apps.activities.models import Activity
from apps.audit.models import AuditLog
from apps.exports.models import ExportJob
from apps.reviews.models import ReviewAssignment
from apps.submissions.models import Submission, SubmissionRevision
from apps.uploads.models import UploadSession
from apps.uploads.services import (
    direct_upload,
    ensure_standard_material_requirements,
    resolve_file_disk_path,
)
from tests.fixtures.samples import REAL_JPEG

MB = 1024 * 1024

APPLY_URL = "/submissions/apply/"
DASHBOARD_URL = "/"
WORKBENCH_URL = "/reviews/workbench/"
LOOKUP_URL = "/api/students/lookup/"
DIRECT_UPLOAD_URL = "/api/v1/uploads/direct-upload/"
CHUNK_START_URL = "/api/v1/uploads/chunk-start/"
CHUNK_UPLOAD_URL = "/api/v1/uploads/chunk-upload/"
CHUNK_COMPLETE_URL = "/api/v1/uploads/chunk-complete/"
START_EXPORT_URL = "/api/v1/exports/start/"

DOCX_BYTES = b"PK\x03\x04" + b"docx-content" * 8
PDF_BYTES = b"%PDF-1.7\n" + b"pdf-content" * 8
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def make_student(username, real_name, class_name="信安25-1班", must_change_password=False):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name=real_name,
        class_name=class_name,
        must_change_password=must_change_password,
    )


def make_admin(username="admin_e2e"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name="张主任",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )


def make_reviewer(username="reviewer_e2e", real_name="李团支书"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name=real_name,
        role=User.Role.REVIEWER,
        must_change_password=False,
    )


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def upload_material(client, requirement, filename, payload, content_type="application/octet-stream"):
    return client.post(
        DIRECT_UPLOAD_URL,
        {
            "requirement_id": str(requirement.id),
            "file": SimpleUploadedFile(filename, payload, content_type=content_type),
        },
    )


def upload_required_materials_via_http(client, activity):
    """申报表 docx + 实践报告 pdf + 现场照片 3 张，全部走浏览器同款直传接口。"""
    reqs = ensure_standard_material_requirements(activity)
    assert upload_material(client, reqs["application_form"], "社会实践申报表.docx", DOCX_BYTES).status_code == 201
    assert upload_material(client, reqs["practice_report"], "社会实践报告.pdf", PDF_BYTES).status_code == 201
    for index in range(1, 4):
        assert (
            upload_material(
                client,
                reqs["scene_photos"],
                f"现场照片{index}.jpg",
                REAL_JPEG,
                content_type="image/jpeg",
            ).status_code
            == 201
        )
    return reqs


def upload_required_materials(actor, activity):
    """服务层直传，供批量造 10 份申报的导出用例使用（比 HTTP 快一个数量级）。"""
    reqs = ensure_standard_material_requirements(activity)
    direct_upload(
        actor=actor,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("申报表.docx", DOCX_BYTES),
    )
    direct_upload(
        actor=actor,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("实践报告.pdf", PDF_BYTES),
    )
    for index in range(1, 4):
        direct_upload(
            actor=actor,
            requirement_id=reqs["scene_photos"].id,
            file_obj=SimpleUploadedFile(f"照片{index}.jpg", REAL_JPEG),
        )
    return reqs


def create_and_submit(actor, activity, project_name="矿区网络安全调研"):
    from apps.submissions.services import create_draft, save_draft, submit_for_review

    draft = create_draft(actor=actor, activity_id=activity.pk)
    save_draft(
        actor=actor,
        submission_id=draft.pk,
        expected_version=0,
        data={
            "project_name": project_name,
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    upload_required_materials(actor, activity)
    submission, _ = submit_for_review(actor=actor, submission_id=draft.pk)
    return submission


# =====================================================================
# 场景 1：注册 -> 登录 -> 首改密提醒 -> 改密 -> 进入申报页
# =====================================================================

def test_scenario1_register_login_force_password_change_then_apply(client, activity, db):
    student_number = "2025990001"

    # 1) 注册页可打开，POST 后自动登录；密码留空 = 学号后 6 位
    assert client.get("/accounts/register/").status_code == 200
    registered = client.post(
        "/accounts/register/",
        {
            "student_number": student_number,
            "real_name": "周达",
            "grade": "大二",
            "major": "信息安全",
            "class_name": "信安25-1班",
            "phone": "13800138000",
            "password": "",
        },
    )
    assert registered.status_code == 302

    user = User.objects.get(username=student_number)
    assert user.check_password("990001") is True
    assert user.must_change_password is True
    assert user.role == User.Role.STUDENT

    # 2) 登录后工作台顶部出现首次改密横幅，且链到改密页
    dashboard = client.get(DASHBOARD_URL)
    assert dashboard.status_code == 200
    html = dashboard.content.decode("utf-8")
    assert 'id="must-change-password-banner"' in html
    assert 'data-must-change-password="true"' in html
    assert "/accounts/password-change/" in html

    # 3) 改密页闭环：旧密码 = 初始密码，改完横幅消失
    assert client.get("/accounts/password-change/").status_code == 200
    changed = client.post(
        "/accounts/password-change/",
        {
            "old_password": "990001",
            "new_password": "UpdatedPass#2026",
            "confirm_password": "UpdatedPass#2026",
        },
    )
    assert changed.status_code == 302
    user.refresh_from_db()
    assert user.must_change_password is False
    assert user.check_password("UpdatedPass#2026") is True
    assert 'id="must-change-password-banner"' not in client.get(DASHBOARD_URL).content.decode("utf-8")

    # 4) 退出后用新密码重新登录，能直接进入四步申报向导
    assert client.post("/accounts/logout/").status_code == 302
    relogin = client.post(
        "/accounts/login/",
        {"username": student_number, "password": "UpdatedPass#2026"},
    )
    assert relogin.status_code == 302

    apply_page = client.get(APPLY_URL, {"activity_id": str(activity.pk)})
    assert apply_page.status_code == 200
    apply_html = apply_page.content.decode("utf-8")
    assert 'id="wizard-steps"' in apply_html
    for step in (1, 2, 3, 4):
        assert f'data-step="{step}"' in apply_html
    assert "项目信息" in apply_html and "确认提交" in apply_html


# =====================================================================
# 场景 2：个人申报全流程（四步填写 -> 三类附件 -> 300MB 分片视频 -> 提交）
# =====================================================================

@pytest.mark.django_db
def test_scenario2_individual_full_flow_with_300mb_chunked_video(client, activity, isolated_media_root):
    student = make_student("2025020001", "周达")

    # 1) 四步填写：第 1 步项目信息 + 第 2 步个人申报（无队员）
    client.force_login(student)
    draft_resp = client.post(
        "/student/submissions/save/",
        {
            "activity_id": str(activity.pk),
            "project_name": "矿区网络安全调研",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members_json": "[]",
        },
    )
    assert draft_resp.status_code == 302
    submission = Submission.objects.get(owner=student, activity=activity)
    assert submission.status == Submission.Status.DRAFT
    assert submission.draft_data["project_name"] == "矿区网络安全调研"
    assert submission.draft_data["declaration_type"] == "individual"

    # 2) 第 3 步附件：申报表 / 实践报告 / 现场照片直传
    reqs = upload_required_materials_via_http(client, activity)

    # 3) 宣讲视频 300MB：chunk-start -> 60 x 5MB -> chunk-complete
    chunk_size = 5 * MB
    total_chunks = 60
    file_size = chunk_size * total_chunks
    started = client.post(
        CHUNK_START_URL,
        {
            "material_type": "video",
            "filename": "300MB宣讲视频.mp4",
            "file_size": str(file_size),
            "total_chunks": str(total_chunks),
            "requirement_id": str(reqs["video"].id),
        },
    )
    assert started.status_code == 201, started.content[:300]
    session = started.json()
    assert session["chunk_size"] == chunk_size
    assert session["total_chunks"] == total_chunks

    filler = b"\x00" * chunk_size
    for index in range(total_chunks):
        part = client.post(
            CHUNK_UPLOAD_URL,
            {
                "upload_id": session["upload_id"],
                "chunk_index": str(index),
                "chunk": SimpleUploadedFile(
                    "blob",
                    (MP4_HEAD + b"\x00" * (chunk_size - len(MP4_HEAD))) if index == 0 else filler,
                    content_type="application/octet-stream",
                ),
            },
        )
        assert part.status_code == 200, part.content[:200]

    completed = client.post(
        CHUNK_COMPLETE_URL,
        {"upload_id": session["upload_id"], "material_type": "video"},
    )
    assert completed.status_code == 200, completed.content[:300]
    completed_body = completed.json()
    assert completed_body["actual_bytes"] == file_size
    video_path = resolve_file_disk_path(completed_body["final_path"])
    assert video_path.is_file() and video_path.stat().st_size == file_size
    assert video_path.parent.name == "promotional_video"

    # 4) 第 4 步确认提交：Draft -> Submitted（pending_first_review）
    submit_resp = client.post(f"/student/submissions/{submission.id}/submit/")
    assert submit_resp.status_code == 302
    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_FIRST_REVIEW
    assert submission.status == Submission.Status.SUBMITTED
    assert submission.revisions.count() == 1
    assert submission.revisions.first().snapshot["project_name"] == "矿区网络安全调研"

    # 提交后申报页只读锁定，工作台显示"已提交"
    locked_html = client.get(APPLY_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert "只读锁定" in locked_html
    dashboard_html = client.get(DASHBOARD_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert "已提交" in dashboard_html


# =====================================================================
# 场景 3：团队申报 —— 队长添加 2 名队员学号，lookup 实时校验
# =====================================================================

def test_scenario3_team_declaration_members_lookup(client, activity, db):
    leader = make_student("2025030001", "张队长")
    member_account = make_student("2025030002", "李队员", "信安25-2班")
    # 第二名队员只存在于导入花名册，用来覆盖 lookup 的 roster 回退分支
    StudentRoster.objects.create(
        student_number="2025030003",
        real_name="王同学",
        class_name="信安25-3班",
        grade="大二",
        major="信息安全",
        active=True,
    )

    client.force_login(leader)

    # 1) 学号存在性校验：账号命中 + 花名册命中 + 不存在三态
    in_account = client.get(LOOKUP_URL, {"student_number": member_account.username})
    assert in_account.status_code == 200
    assert in_account.json()["exists"] is True
    assert in_account.json()["real_name"] == "李队员"
    assert in_account.json()["source"] == "account"

    in_roster = client.get(LOOKUP_URL, {"student_number": "2025030003"})
    assert in_roster.status_code == 200
    assert in_roster.json()["exists"] is True
    assert in_roster.json()["source"] == "roster"

    missing = client.get(LOOKUP_URL, {"student_number": "2099000000"})
    assert missing.status_code == 200
    assert missing.json()["exists"] is False

    # 2) 团队申报：两名队员写入草稿，declaration_type=team
    members = [
        {"student_number": "2025030002", "real_name": "李队员", "class_name": "信安25-2班"},
        {"student_number": "2025030003", "real_name": "王同学", "class_name": "信安25-3班"},
    ]
    saved = client.post(
        "/student/submissions/save/",
        {
            "activity_id": str(activity.pk),
            "project_name": "矿区安全科普进社区",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members_json": json.dumps(members, ensure_ascii=False),
        },
    )
    assert saved.status_code == 302

    submission = Submission.objects.get(owner=leader, activity=activity)
    assert submission.draft_data["declaration_type"] == "team"
    assert [m["student_number"] for m in submission.draft_data["team_members"]] == [
        "2025030002",
        "2025030003",
    ]

    # 3) 重复学号会被服务层拒绝（409/400 都算被拦，页面端点统一回 400）
    duplicated = client.post(
        "/student/submissions/save/",
        {
            "activity_id": str(activity.pk),
            "project_name": "重复队员测试",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members_json": json.dumps([members[0], members[0]], ensure_ascii=False),
        },
    )
    assert duplicated.status_code == 400


# =====================================================================
# 场景 4：审核老师工作台筛选班级 -> 展开历史 -> 驳回 -> 学生端可见
# =====================================================================

def test_scenario4_reviewer_workbench_filter_history_reject_and_student_view(client, activity, db):
    reviewer = make_reviewer()
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name="信安25-1班")

    target_student = make_student("2025040001", "周达", "信安25-1班")
    other_student = make_student("2025040002", "林溪", "计算机25-2班")

    target_sub = create_and_submit(target_student, activity, "红色基地调研")
    other_sub = create_and_submit(other_student, activity, "智慧矿山巡检")

    # 1) 工作台按班级筛选：只出现本班学生
    client.force_login(reviewer)
    filtered = client.get(
        WORKBENCH_URL,
        {"activity_id": str(activity.pk), "class_name": "信安25-1班"},
    )
    assert filtered.status_code == 200
    filtered_html = filtered.content.decode("utf-8")
    assert target_student.username in filtered_html
    assert other_student.username not in filtered_html

    # 2) 展开历史：行内折叠容器存在（提交后尚无审核记录，容器仍渲染）
    assert f'data-bs-target="#history-{target_sub.id}"' in filtered_html
    assert f'id="history-{target_sub.id}"' in filtered_html

    # 3) 初审驳回：必须填意见
    missing_comment = client.post(
        f"/reviews/submissions/{target_sub.id}/action/",
        {"decision": "returned", "comment": ""},
    )
    assert missing_comment.status_code == 400

    rejected = client.post(
        f"/reviews/submissions/{target_sub.id}/action/",
        {"decision": "returned", "comment": "现场照片不足，请补充后重新提交。"},
        follow=True,
    )
    assert rejected.status_code == 200
    target_sub.refresh_from_db()
    assert target_sub.status == Submission.Status.RETURNED

    # 4) 工作台历史时间线里能看到驳回人与意见
    history_html = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert "现场照片不足，请补充后重新提交。" in history_html
    assert "李团支书" in history_html
    assert "打回修改" in history_html

    # 5) 学生端：工作台红色提示 + 意见原文，申报页可再次编辑
    client.force_login(target_student)
    student_html = client.get(DASHBOARD_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert "打回修改" in student_html
    assert "现场照片不足，请补充后重新提交。" in student_html
    assert 'id="returned-comment-content"' in student_html
    apply_html = client.get(APPLY_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert "只读锁定" not in apply_html


# =====================================================================
# 场景 5：白名单补交（截止后非白名单被拒 / 白名单成功且徽章 LATE_SUBMITTED）
# =====================================================================

def test_scenario5_late_whitelist_submission_after_deadline(client, db):
    now = timezone.now()
    expired = Activity.objects.create(
        title="暑期社会实践（已截止）",
        opens_at=now - timedelta(days=10),
        deadline=now - timedelta(hours=1),
        is_open=True,
    )
    normal = make_student("2025050001", "普通学生")
    whitelisted = make_student("2025050002", "补交同学")
    expired.add_to_late_whitelist(whitelisted.username)
    assert expired.late_submission_allowed_at() is True

    # 1) 截止后普通学生：页面写操作 400、API 409（截止属于状态冲突，403 只用于角色/归属越权）
    client.force_login(normal)
    page_rejected = client.post(
        "/student/submissions/save/",
        {"activity_id": str(expired.pk), "project_name": "普通学生迟交", "category": "社会调查"},
    )
    assert page_rejected.status_code == 400
    api_rejected = post_json(client, "/api/v1/submissions/", {"activity_id": str(expired.pk)})
    assert api_rejected.status_code == 409
    assert api_rejected.json()["error"]["code"] == "conflict"
    assert not Submission.objects.filter(owner=normal, activity=expired).exists()
    # 上传同样被挡（截止后非白名单不能补材料）
    reqs = ensure_standard_material_requirements(expired)
    blocked_upload = upload_material(client, reqs["application_form"], "申报表.docx", DOCX_BYTES)
    assert blocked_upload.status_code == 409

    # 2) 截止后白名单学生：可建草稿、可上传、可提交，并打上迟交徽章
    client.force_login(whitelisted)
    draft = post_json(client, "/api/v1/submissions/", {"activity_id": str(expired.pk)})
    assert draft.status_code == 200
    submission_id = draft.json()["id"]

    upload_required_materials_via_http(client, expired)
    # 保存草稿只接受 PATCH
    patch_resp = client.patch(
        f"/api/v1/submissions/{submission_id}/",
        data=json.dumps(
            {
                "expected_version": 0,
                "draft_data": {
                    "project_name": "白名单补交项目",
                    "category": "社会调查",
                    "declaration_type": "individual",
                    "team_members": [],
                },
            }
        ),
        content_type="application/json",
    )
    assert patch_resp.status_code == 200, patch_resp.content[:300]

    submitted = client.post(f"/api/v1/submissions/{submission_id}/submit/")
    assert submitted.status_code == 200, submitted.content[:300]

    late_sub = Submission.objects.get(pk=submission_id)
    assert late_sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert late_sub.is_late_submission is True
    assert late_sub.badge_status == Submission.LATE_SUBMITTED
    assert late_sub.badge_label == "迟交已提交"

    # 3) 审核工作台按"迟交"筛选与紫色徽章
    admin = make_admin("admin_late_e2e")
    client.force_login(admin)
    workbench = client.get(WORKBENCH_URL, {"activity_id": str(expired.pk), "late": "1"})
    html = workbench.content.decode("utf-8")
    assert whitelisted.username in html
    assert 'data-status="late_submitted"' in html
    assert "badge-status-purple" in html


# =====================================================================
# 场景 6：越权 —— 学生 A 拿学生 B 的 upload_id 读/删都 403
# =====================================================================

@pytest.mark.django_db
def test_scenario6_cross_student_upload_file_and_delete_are_forbidden(client, activity):
    owner = make_student("2025060001", "文件主人")
    intruder = make_student("2025060002", "越权同学")

    reqs = ensure_standard_material_requirements(activity)
    upload, _asset, _replaced = direct_upload(
        actor=owner,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("申报表.docx", DOCX_BYTES),
    )
    disk_path = resolve_file_disk_path(upload.object_key)
    assert disk_path.is_file()

    client.force_login(intruder)
    read_resp = client.get(f"/api/v1/uploads/{upload.id}/file/")
    assert read_resp.status_code == 403
    assert read_resp.json()["error"]["code"] == "forbidden"

    delete_resp = client.post(f"/api/v1/uploads/{upload.id}/delete/")
    assert delete_resp.status_code == 403
    delete_verb = client.delete(f"/api/v1/uploads/{upload.id}/delete/")
    assert delete_verb.status_code == 403

    # 记录与磁盘文件都没被越权动作改动
    upload.refresh_from_db()
    assert upload.status == UploadSession.Status.COMPLETE
    assert disk_path.is_file()
    assert AuditLog.objects.filter(action__startswith="upload.file_access.rejected").exists()
    assert AuditLog.objects.filter(action__startswith="upload.delete.rejected").exists()


# =====================================================================
# 场景 7：安全测验 —— fake_docx.exe 改名 .docx，魔数层拒绝
# =====================================================================

@pytest.mark.django_db
def test_scenario7_fake_docx_executable_rejected_by_magic_header(client, activity, isolated_media_root):
    student = make_student("2025070001", "好奇同学")
    reqs = ensure_standard_material_requirements(activity)
    fake_bytes = (Path(settings.BASE_DIR) / "tests" / "fixtures" / "fake_docx.exe").read_bytes()
    assert fake_bytes[:2] == b"MZ"

    client.force_login(student)
    response = upload_material(
        client,
        reqs["application_form"],
        "社会实践申报表.docx",
        fake_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert response.status_code == 400
    body = response.json()
    assert "魔数" in body["error"]["message"]

    # 没有任何上传记录落库，磁盘上也没有残留文件
    assert not UploadSession.objects.filter(owner=student, requirement=reqs["application_form"]).exists()
    activity_dir = Path(settings.MEDIA_ROOT) / str(activity.id)
    assert not activity_dir.exists() or not any(activity_dir.rglob("*.docx"))

    # 拒绝动作已审计（含原因）
    audit = AuditLog.objects.filter(action="upload.direct.rejected").order_by("-created_at").first()
    assert audit is not None
    assert "魔数" in audit.reason


# =====================================================================
# 场景 8：导出任务 —— 10 份申报异步导出，ZIP 结构正确
# =====================================================================

@pytest.mark.django_db
def test_scenario8_export_ten_submissions_zip_structure(client, activity):
    admin = make_admin("admin_export_e2e")
    students = [make_student(f"20250800{index:02d}", f"学生{index:02d}") for index in range(1, 11)]
    for index, student in enumerate(students, start=1):
        create_and_submit(student, activity, f"第{index}组社会实践")

    client.force_login(admin)
    started = post_json(client, START_EXPORT_URL, {"activity_id": str(activity.pk)})
    assert started.status_code == 202, started.content[:300]
    job_id = started.json()["job_id"]

    # test settings 下 CELERY_TASK_ALWAYS_EAGER=True，POST 返回时任务已完成
    job = ExportJob.objects.get(pk=job_id)
    assert job.status == ExportJob.Status.DONE
    assert job.total > 0 and job.current == job.total
    assert job.file_path.startswith(f"exports/{activity.pk}/")

    status_resp = client.get(f"/api/v1/exports/status/{job_id}/")
    assert status_resp.status_code == 200
    assert status_resp.json()["job"]["percent"] == 100

    download = client.get(f"/api/v1/exports/{job_id}/download/")
    assert download.status_code == 200
    assert download["Content-Type"] == "application/zip"
    zip_bytes = b"".join(download.streaming_content)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert len(names) == 10 * 5  # 每人：申报表 1 + 实践报告 1 + 现场照片 3
        student_dirs = {name.split("/", 1)[0] for name in names}
        assert len(student_dirs) == 10
        for student in students:
            expected_dir = f"{student.username}_{student.real_name}"
            assert expected_dir in student_dirs
            own = [name for name in names if name.startswith(expected_dir + "/")]
            assert f"{expected_dir}/申报表/申报表.docx" in own
            assert f"{expected_dir}/实践报告/实践报告.pdf" in own
            photos = [name for name in own if name.startswith(f"{expected_dir}/现场照片/")]
            assert len(photos) == 3
        first_docx = next(name for name in names if name.endswith("申报表.docx"))
        assert zf.read(first_docx).startswith(b"PK")


# =====================================================================
# PostgreSQL 行锁：SQLite 无法验证 select_for_update，必须 Postgres 复测
# =====================================================================

@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="SELECT ... FOR UPDATE 是 PostgreSQL 语义；SQLite 会放行双提交，必须用 docker compose 的 Postgres 复测。",
)
def test_postgres_row_lock_serializes_concurrent_submissions(activity):
    """两个并发提交只能有一个成功：select_for_update 让第二个看到已变更状态并 409。"""
    student = make_student("2025090001", "并发同学")
    upload_required_materials(student, activity)
    from apps.submissions.services import create_draft, save_draft

    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(
        actor=student,
        submission_id=draft.pk,
        expected_version=0,
        data={
            "project_name": "并发提交测试",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )

    barrier = threading.Barrier(2)

    def submit_once(_):
        from apps.common.errors import Conflict
        from apps.submissions.services import submit_for_review

        close_old_connections()
        try:
            actor = User.objects.get(pk=student.pk)
            barrier.wait(timeout=10)
            try:
                submit_for_review(actor=actor, submission_id=draft.pk)
                return "submitted"
            except Conflict:
                return "conflict"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit_once, range(2)))

    assert sorted(results) == ["conflict", "submitted"]
    assert SubmissionRevision.objects.filter(submission=draft).count() == 1
    assert Submission.objects.get(pk=draft.pk).status == Submission.Status.PENDING_FIRST_REVIEW
