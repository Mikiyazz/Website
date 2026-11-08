"""阶段 9：审核工作台 + 异步导出进度 + 补交白名单设置卡片。"""
import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import StudentRoster, User
from apps.exports.models import ExportJob
from apps.reviews.services import review_submission
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.services import direct_upload, ensure_standard_material_requirements
from tests.fixtures.samples import REAL_JPEG

WORKBENCH_URL = "/reviews/workbench/"
START_EXPORT_URL = "/api/v1/exports/start/"


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------

def make_admin(username="admin_wb"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name="张主任",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )


def make_student(username, real_name, class_name="信安25-1班"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name=real_name,
        class_name=class_name,
        must_change_password=False,
    )


def upload_required_materials(student, activity):
    reqs = ensure_standard_material_requirements(activity)
    direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("我的申请表_初稿.docx", b"PK\x03\x04" + b"docx" * 16),
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("社会实践总结.pdf", b"%PDF-1.7\n" + b"pdf" * 16),
    )
    for idx in range(1, 4):
        direct_upload(
            actor=student,
            requirement_id=reqs["scene_photos"].id,
            file_obj=SimpleUploadedFile(f"合影{idx}.jpg", REAL_JPEG),
        )
    return reqs


def submit_student(student, activity, project_name="矿区网络安全调研", video_bytes=None, video_name="宣讲视频.mp4"):
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(
        actor=student,
        submission_id=draft.pk,
        expected_version=0,
        data={
            "project_name": project_name,
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    reqs = upload_required_materials(student, activity)
    if video_bytes is not None:
        direct_upload(
            actor=student,
            requirement_id=reqs["video"].id,
            file_obj=SimpleUploadedFile(video_name, video_bytes),
        )
    submission, _ = submit_for_review(actor=student, submission_id=draft.pk)
    return submission


def post_json(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


# ---------------------------------------------------------------------
# 1. 工作台页面：权限、表格列、徽章、折叠时间线
# ---------------------------------------------------------------------

def test_workbench_page_renders_table_badges_and_history(client, activity, db):
    admin = make_admin()
    student = make_student("20250801", "周达")
    submission = submit_student(student, activity)
    review_submission(
        actor=admin,
        activity_id=activity.pk,
        submission_id=submission.pk,
        decision="approved",
        comment="初审材料齐全，同意上报学院终审",
    )

    # 第二个学生保持“已提交（待初审）”，用于验证蓝色徽章
    submit_student(make_student("20250802", "林溪", "信安25-2班"), activity, "智慧矿山巡检")

    client.force_login(admin)
    resp = client.get(WORKBENCH_URL)
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")

    # 表头七列
    for header in ("学号", "姓名", "班级", "活动名", "提交时间", "状态", "审核历史"):
        assert header in html

    # 服务端筛选栏（活动 / 班级 / 状态 / 迟交白名单）
    assert 'id="filter_activity_id"' in html
    assert 'id="filter_class_name"' in html
    assert 'id="filter_status"' in html
    assert 'id="filter_late"' in html

    # 状态徽章规范：草稿灰 / 已提交蓝 / 复审中黄 / 通过绿 / 驳回红
    assert "badge-status-blue" in html and "已提交" in html
    assert "badge-status-yellow" in html and "复审中" in html
    css_path = Path(settings.BASE_DIR) / "static" / "css" / "app.css"
    css_text = css_path.read_text(encoding="utf-8")
    for tone in ("gray", "blue", "yellow", "green", "red"):
        assert f".badge-status-{tone}" in css_text

    # 每行折叠 + Tabler timeline 渲染审核历史
    assert f'data-bs-target="#history-{submission.id}"' in html
    assert f'id="history-{submission.id}"' in html
    assert "timeline" in html
    assert "初审材料齐全，同意上报学院终审" in html
    assert "张主任" in html

    # 顶部批量导出按钮
    assert 'id="btn-start-export"' in html
    assert "批量导出" in html

    # 学生禁止访问（管理端端点）
    client.force_login(student)
    assert client.get(WORKBENCH_URL).status_code == 403


def test_workbench_server_side_filters(client, activity, db):
    admin = make_admin("admin_filter")
    stu_a = make_student("20250801", "周达", "信安25-1班")
    stu_b = make_student("20250802", "林溪", "计算机25-2班")

    sub_a = submit_student(stu_a, activity, "红色基地调研")
    review_submission(actor=admin, activity_id=activity.pk, submission_id=sub_a.pk, decision="approved")
    review_submission(actor=admin, activity_id=activity.pk, submission_id=sub_a.pk, decision="approved")

    sub_b = submit_student(stu_b, activity, "智慧矿山巡检")

    client.force_login(admin)

    only_a = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "class_name": "信安25-1班"})
    html_a = only_a.content.decode("utf-8")
    assert "20250801" in html_a
    assert "20250802" not in html_a

    only_b = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "class_name": "计算机25-2班"})
    html_b = only_b.content.decode("utf-8")
    assert "20250802" in html_b
    assert "20250801" not in html_b

    approved = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "status": "final_approved"})
    html_approved = approved.content.decode("utf-8")
    assert "20250801" in html_approved
    assert "20250802" not in html_approved

    pending = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "status": "pending_first_review"})
    html_pending = pending.content.decode("utf-8")
    assert "20250802" in html_pending
    assert "20250801" not in html_pending

    # 状态过滤参数无法解析时不应 500，只是不额外收窄
    bogus = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "status": "不存在的状态"})
    assert bogus.status_code == 200


# ---------------------------------------------------------------------
# 2. 异步导出：job_id、进度轮询、ZIP 目录结构、late_ 前缀、大文件 STORED
# ---------------------------------------------------------------------

def test_async_export_progress_zip_layout_and_download(client, activity, db):
    admin = make_admin("admin_export_async")
    student = make_student("20250801", "周达")
    submit_student(
        student,
        activity,
        video_bytes=b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"v" * 64,
    )

    client.force_login(admin)
    start = post_json(client, START_EXPORT_URL, {"activity_id": str(activity.pk)})
    assert start.status_code == 202
    payload = start.json()
    job_id = payload["job_id"]
    assert job_id
    assert payload["job"]["activity_id"] == str(activity.pk)

    # development/test 使用 CELERY_TASK_ALWAYS_EAGER，POST 返回时任务已同步完成
    job = ExportJob.objects.get(pk=job_id)
    assert job.status == ExportJob.Status.DONE
    assert job.total > 0 and job.current == job.total
    assert job.file_path.startswith(f"exports/{activity.pk}/")
    assert job.file_path.endswith(".zip")

    status_resp = client.get(f"/api/v1/exports/status/{job_id}/")
    assert status_resp.status_code == 200
    status_job = status_resp.json()["job"]
    assert status_job["status"] == "done"
    assert status_job["percent"] == 100
    assert status_job["download_url"] == f"/api/v1/exports/{job_id}/download/"

    download = client.get(f"/api/v1/exports/{job_id}/download/")
    assert download.status_code == 200
    assert download["Content-Type"] == "application/zip"
    zip_bytes = b"".join(download.streaming_content)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        prefix = "20250801_周达"
        expected = {
            f"{prefix}/申报表/我的申请表_初稿.docx",
            f"{prefix}/实践报告/社会实践总结.pdf",
            f"{prefix}/现场照片/合影1.jpg",
            f"{prefix}/现场照片/合影2.jpg",
            f"{prefix}/现场照片/合影3.jpg",
            f"{prefix}/宣讲视频/宣讲视频.mp4",
        }
        assert names == expected
        # 小文件 deflate，视频（<20MB）也走 deflate；结构按 {学号}_{姓名}/{材料类型中文名}/{文件}
        assert zf.read(f"{prefix}/申报表/我的申请表_初稿.docx").startswith(b"PK")

    # 学生不能发起导出
    client.force_login(student)
    forbidden = post_json(client, START_EXPORT_URL, {"activity_id": str(activity.pk)})
    assert forbidden.status_code == 403


def test_export_large_video_uses_stored_and_late_prefix(client, activity, db):
    admin = make_admin("admin_export_late")
    student = make_student("20250809", "补交同学")
    now = timezone.now()
    activity.deadline = now - timedelta(hours=2)
    activity.opens_at = now - timedelta(days=2)
    activity.save(update_fields=["deadline", "opens_at", "updated_at"])
    activity.add_to_late_whitelist("20250809")

    big_video = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * (21 * 1024 * 1024)
    # 补交视频在提交前上传，避免提交后上传被状态机拦截
    submission = submit_student(
        student,
        activity,
        "白名单补交项目",
        video_bytes=big_video,
        video_name="补交宣讲.mp4",
    )
    assert submission.is_late_submission is True
    assert submission.badge_status == Submission.LATE_SUBMITTED
    assert submission.badge_label == "迟交已提交"
    assert submission.export_name_prefix == "late_"

    client.force_login(admin)
    start = post_json(client, START_EXPORT_URL, {"activity_id": str(activity.pk)})
    assert start.status_code == 202
    job_id = start.json()["job_id"]

    download = client.get(f"/api/v1/exports/{job_id}/download/")
    assert download.status_code == 200
    zip_bytes = b"".join(download.streaming_content)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert any(name.startswith("20250809_补交同学/申报表/late_") for name in names)
        video_name = next(name for name in names if name.endswith("补交宣讲.mp4"))
        info = zf.getinfo(video_name)
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.file_size == len(big_video)

    # 工作台徽章展示迟交口径
    workbench = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk)})
    html = workbench.content.decode("utf-8")
    assert 'data-status="late_submitted"' in html
    assert "badge-status-purple" in html
    assert "迟交已提交" in html


def test_export_status_and_download_permission_boundaries(client, activity, db):
    admin_a = make_admin("admin_a")
    admin_b = make_admin("admin_b")
    student = make_student("20250801", "周达")
    submit_student(student, activity)

    client.force_login(admin_a)
    job_id = post_json(client, START_EXPORT_URL, {"activity_id": str(activity.pk)}).json()["job_id"]

    # 其他管理员可以查看；学生 403
    client.force_login(admin_b)
    assert client.get(f"/api/v1/exports/status/{job_id}/").status_code == 200
    client.force_login(student)
    assert client.get(f"/api/v1/exports/status/{job_id}/").status_code == 403
    assert client.get(f"/api/v1/exports/{job_id}/download/").status_code == 403

    # 缺少 activity_id -> 400
    client.force_login(admin_a)
    assert post_json(client, START_EXPORT_URL, {}).status_code == 400


# ---------------------------------------------------------------------
# 3. 补交白名单设置卡片：60 个学号批量解析 + StudentRoster 存在性校验标红
# ---------------------------------------------------------------------

def test_late_whitelist_bulk_parse_60_numbers_and_invalid_highlight(client, activity, db):
    admin = make_admin("admin_wl")
    numbers = [f"2026{index:04d}" for index in range(1, 61)]
    for index, sn in enumerate(numbers):
        StudentRoster.objects.create(
            student_number=sn,
            real_name=f"学生{index + 1}",
            class_name="信安25-1班",
            active=True,
        )

    client.force_login(admin)
    textarea = "\n".join(numbers) + "\n  " + "，".join(["20260001", "20260002"]) + "\n99999999"

    # 实时解析端点：去重 + 校验
    parse_resp = post_json(
        client,
        f"/admin-panel/activities/{activity.pk}/whitelist/parse/",
        {"student_numbers": textarea},
    )
    assert parse_resp.status_code == 200
    parsed = parse_resp.json()
    assert parsed["valid_count"] == 60
    assert parsed["invalid"] == ["99999999"]

    # 保存补交设置：开关 + 补交截止 + 全量替换白名单
    deadline = (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    save_resp = client.post(
        f"/admin-panel/activities/{activity.pk}/late-settings/",
        {
            "late_submission_enabled": "on",
            "late_submission_deadline": deadline,
            "late_whitelist_text": textarea,
        },
        follow=True,
    )
    assert save_resp.status_code == 200
    activity.refresh_from_db()
    assert activity.late_whitelist_student_numbers == numbers
    assert activity.late_submission_enabled is True
    assert activity.late_submission_deadline is not None

    html = save_resp.content.decode("utf-8")
    assert "badge-invalid-student" in html
    assert "99999999" in html
    assert "不存在" in html

    # 关闭开关后白名单通道失效
    client.post(
        f"/admin-panel/activities/{activity.pk}/late-settings/",
        {
            "late_submission_deadline": deadline,
            "late_whitelist_text": "\n".join(numbers),
        },
    )
    activity.refresh_from_db()
    assert activity.late_submission_enabled is False
    assert activity.late_submission_allowed_at() is False

    # 学生不能保存补交设置
    student = make_student("20250801", "周达")
    client.force_login(student)
    denied = client.post(
        f"/admin-panel/activities/{activity.pk}/late-settings/",
        {"late_whitelist_text": "20260001"},
    )
    assert denied.status_code == 403


def test_workbench_late_filter_and_class_options_from_roster(client, activity, db):
    admin = make_admin("admin_late_filter")
    StudentRoster.objects.create(student_number="20250801", real_name="周达", class_name="信安25-1班")
    StudentRoster.objects.create(student_number="20250999", real_name="补交同学", class_name="计算机25-1班")
    activity.add_to_late_whitelist("20250999")

    client.force_login(admin)
    resp = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "late": "1"})
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")
    # 班级下拉来自 StudentRoster 去重
    assert "计算机25-1班" in html
    assert "信安25-1班" in html
    assert "仅白名单学生" in html
