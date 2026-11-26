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


def make_student(username, real_name, class_name="信安25-1班", grade="大二"):
    return User.objects.create_user(
        username=username,
        password="password123",
        real_name=real_name,
        class_name=class_name,
        grade=grade,
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
        prefix = "大二/信安25-1班/周达+20250801"
        expected = {
            f"{prefix}/申报表.docx",
            f"{prefix}/实践报告.pdf",
            f"{prefix}/现场照片_1.jpg",
            f"{prefix}/现场照片_2.jpg",
            f"{prefix}/现场照片_3.jpg",
            f"{prefix}/宣讲视频.mp4",
        }
        assert names == expected
        # 小文件 deflate，视频（<20MB）也走 deflate；结构按 {年级}/{班级}/{姓名}+{学号}/{标准材料名}
        assert zf.read(f"{prefix}/申报表.docx").startswith(b"PK")

    # 学生不能发起导出
    client.force_login(student)
    forbidden = post_json(client, START_EXPORT_URL, {"activity_id": str(activity.pk)})
    assert forbidden.status_code == 403


def test_async_export_two_modes_strict_filtering_and_nested_naming(client, activity, db):
    """验证 2 种导出模式严格按提交状态过滤，且 ZIP 内统一按 年级/班级/姓名+学号/材料文件 归档。"""
    from apps.submissions.services import submit_declaration

    admin = make_admin("admin_export_modes")
    reqs = ensure_standard_material_requirements(activity)

    # 1) 完整提交申报表和成果（大二 / 信安25-1班）
    stu_full = make_student("20250811", "周达", "信安25-1班", grade="大二")
    submit_student(stu_full, activity, "完整提交项目")

    # 2) 仅提交了立项申报与申报表，尚未提交成果（大三 / 计科24-2班）
    stu_decl_only = make_student("20240812", "林溪", "计科24-2班", grade="大三")
    d_decl = create_draft(actor=stu_decl_only, activity_id=activity.pk)
    save_draft(
        actor=stu_decl_only,
        submission_id=d_decl.pk,
        expected_version=0,
        data={
            "project_name": "仅交申报项目",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    direct_upload(
        actor=stu_decl_only,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("仅立项申报表.docx", b"PK\x03\x04" + b"decl" * 16),
    )
    submit_declaration(actor=stu_decl_only, submission_id=d_decl.pk)

    # 3) 草稿态上传了申报表与报告，但既未点提交申报也未点提交成果（两种模式都不应导出）
    stu_draft = make_student("20250813", "草稿同学", "信安25-1班", grade="大二")
    d_draft = create_draft(actor=stu_draft, activity_id=activity.pk)
    save_draft(
        actor=stu_draft,
        submission_id=d_draft.pk,
        expected_version=0,
        data={
            "project_name": "未提交草稿项目",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    upload_required_materials(stu_draft, activity)

    # 4) 未填年级的已提交学生，验证年级兜底为“未填年级”
    stu_no_grade = make_student("20250814", "无年级同学", "网安25-1班", grade="")
    submit_student(stu_no_grade, activity, "无年级项目")

    client.force_login(admin)

    # 模式 1：导出申报表（未提交申报表的不导出，仅打包申报表）
    resp_decl = post_json(
        client,
        START_EXPORT_URL,
        {"activity_id": str(activity.pk), "mode": "declaration_only"},
    )
    assert resp_decl.status_code == 202
    job_decl = resp_decl.json()["job"]
    assert job_decl["mode"] == "declaration_only"
    assert job_decl["mode_label"] == "导出申报表"
    assert job_decl["status"] == "done"
    assert job_decl["result_key"].endswith("-申报表归档.zip")

    dl_decl = client.get(job_decl["download_url"])
    assert dl_decl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(b"".join(dl_decl.streaming_content))) as zf:
        decl_names = set(zf.namelist())
        assert decl_names == {
            "大二/信安25-1班/周达+20250811/申报表.docx",
            "大三/计科24-2班/林溪+20240812/申报表.docx",
            "未填年级/网安25-1班/无年级同学+20250814/申报表.docx",
        }
        assert not any("20250813" in n for n in decl_names)

    # 模式 2：导出申报表和成果（只导出已提交成果的；仅交申报表与草稿均不导出）
    resp_both = post_json(
        client,
        START_EXPORT_URL,
        {"activity_id": str(activity.pk), "mode": "declaration_and_outcomes"},
    )
    assert resp_both.status_code == 202
    job_both = resp_both.json()["job"]
    assert job_both["mode"] == "declaration_and_outcomes"
    assert job_both["mode_label"] == "导出申报表和成果"
    assert job_both["status"] == "done"
    assert job_both["result_key"].endswith("-申报表和成果归档.zip")

    dl_both = client.get(job_both["download_url"])
    assert dl_both.status_code == 200
    with zipfile.ZipFile(io.BytesIO(b"".join(dl_both.streaming_content))) as zf:
        both_names = set(zf.namelist())
        expected_both = {
            "大二/信安25-1班/周达+20250811/申报表.docx",
            "大二/信安25-1班/周达+20250811/实践报告.pdf",
            "大二/信安25-1班/周达+20250811/现场照片_1.jpg",
            "大二/信安25-1班/周达+20250811/现场照片_2.jpg",
            "大二/信安25-1班/周达+20250811/现场照片_3.jpg",
            "未填年级/网安25-1班/无年级同学+20250814/申报表.docx",
            "未填年级/网安25-1班/无年级同学+20250814/实践报告.pdf",
            "未填年级/网安25-1班/无年级同学+20250814/现场照片_1.jpg",
            "未填年级/网安25-1班/无年级同学+20250814/现场照片_2.jpg",
            "未填年级/网安25-1班/无年级同学+20250814/现场照片_3.jpg",
        }
        assert both_names == expected_both
        assert not any("20240812" in n for n in both_names)
        assert not any("20250813" in n for n in both_names)

    # 非法导出模式返回 400
    bad_mode = post_json(
        client,
        START_EXPORT_URL,
        {"activity_id": str(activity.pk), "mode": "unknown_mode"},
    )
    assert bad_mode.status_code == 400

    # 工作台页面渲染模式选择下拉框与任务模式展示
    wb_html = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert 'id="export-mode-select"' in wb_html
    assert 'value="declaration_only"' in wb_html
    assert 'value="declaration_and_outcomes"' in wb_html
    assert "导出申报表" in wb_html
    assert "导出申报表和成果" in wb_html


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
        assert any(name.startswith("大二/信安25-1班/补交同学+20250809/late_申报表.docx") for name in names)
        video_name = next(name for name in names if name.endswith("late_宣讲视频.mp4"))
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


def test_dashboard_progress_bar_opens_workbench_in_new_tab_with_filters(client, activity, db):
    """首页顶部 4 个审核进度条在新标签页打开审核工作台并带入对应状态筛选条件、刷新条目。"""
    admin = make_admin("admin_seg_jump")
    stu_first = make_student("20250901", "初审同学", "信安25-1班")
    stu_final = make_student("20250902", "终审同学", "信安25-1班")
    stu_approved = make_student("20250903", "通过同学", "信安25-1班")
    stu_returned = make_student("20250904", "退回同学", "信安25-1班")

    submit_student(stu_first, activity, "待初审项目")

    sub_final = submit_student(stu_final, activity, "待终审项目")
    review_submission(actor=admin, activity_id=activity.pk, submission_id=sub_final.pk, decision="approved")

    sub_approved = submit_student(stu_approved, activity, "终审通过项目")
    review_submission(actor=admin, activity_id=activity.pk, submission_id=sub_approved.pk, decision="approved")
    review_submission(actor=admin, activity_id=activity.pk, submission_id=sub_approved.pk, decision="approved")

    sub_returned = submit_student(stu_returned, activity, "打回修改项目")
    review_submission(
        actor=admin,
        activity_id=activity.pk,
        submission_id=sub_returned.pk,
        decision="returned",
        comment="需补充材料",
    )

    client.force_login(admin)
    dash_html = client.get(f"/?activity_id={activity.pk}").content.decode("utf-8")

    # 仪表盘仅保留简略概览面板，不再重复渲染大号多段进度条和柱状分布图
    assert 'id="dash-compact-summary"' in dash_html
    assert 'id="dash-segmented-bar"' not in dash_html
    assert 'id="dash-distribution-chart"' not in dash_html

    # 管理面板前面不展示任何统计概览面板与图表
    admin_panel_html = client.get(f"/admin-panel/?activity_id={activity.pk}").content.decode("utf-8")
    assert 'id="dash-compact-summary"' not in admin_panel_html
    assert 'id="dash-segmented-bar"' not in admin_panel_html
    assert 'id="dash-distribution-chart"' not in admin_panel_html
    assert 'id="admin-management-panel"' in admin_panel_html

    # 审核工作台完整保留详细 KPI、多段进度条与柱状分布图
    wb_overview_html = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert 'id="wb-segmented-bar"' in wb_overview_html
    assert 'id="wb-distribution-chart"' in wb_overview_html

    expected_cases = [
        ("pending_first_review", "20250901", {"20250902", "20250903", "20250904"}),
        ("pending_final_review", "20250902", {"20250901", "20250903", "20250904"}),
        ("final_approved", "20250903", {"20250901", "20250902", "20250904"}),
        ("returned", "20250904", {"20250901", "20250902", "20250903"}),
    ]
    for status_code, included_sn, excluded_sns in expected_cases:
        expected_href = f'href="/reviews/workbench/?activity_id={activity.pk}&status={status_code}#workbench-queue-card"'
        assert expected_href in dash_html
        assert 'target="_blank" rel="noopener"' in dash_html

        wb_resp = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "status": status_code})
        assert wb_resp.status_code == 200
        wb_html = wb_resp.content.decode("utf-8")
        assert f'<option value="{status_code}" selected>' in wb_html
        assert included_sn in wb_html
        for excluded_sn in excluded_sns:
            assert excluded_sn not in wb_html


def test_workbench_activity_collapsible_and_category_filter_everywhere(client, activity, db):
    """验证审核工作台活动名默认折叠按钮，以及所有筛选入口的活动类别与管理员配置一致并准确过滤。"""
    import re
    import shutil
    import subprocess
    import tempfile
    import openpyxl

    activity.project_categories = ["母校行专项", "红色寻访专项"]
    activity.save(update_fields=["project_categories", "updated_at"])

    admin = make_admin("admin_cat_filter")
    stu_a = make_student("20250911", "母校行同学", "信安25-1班")
    stu_b = make_student("20250912", "寻访同学", "信安25-1班")

    for stu, proj, cat in (
        (stu_a, "寒假母校宣讲", "母校行专项"),
        (stu_b, "红色场馆调研", "红色寻访专项"),
    ):
        draft = create_draft(actor=stu, activity_id=activity.pk)
        save_draft(
            actor=stu,
            submission_id=draft.pk,
            expected_version=0,
            data={
                "project_name": proj,
                "category": cat,
                "declaration_type": "individual",
                "team_members": [],
            },
        )
        upload_required_materials(stu, activity)
        submit_for_review(actor=stu, submission_id=draft.pk)

    client.force_login(admin)

    # 1) 审核工作台：活动名默认折叠按钮 + 活动类别下拉与管理员配置一致 + 过滤生效
    wb_resp = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "category": "母校行专项"})
    assert wb_resp.status_code == 200
    wb_html = wb_resp.content.decode("utf-8")
    assert 'class="wb-activity-toggle"' in wb_html
    assert 'aria-expanded="false"' in wb_html
    assert 'id="filter_category"' in wb_html
    assert '<option value="母校行专项" selected>母校行专项</option>' in wb_html
    assert '<option value="红色寻访专项" >红色寻访专项</option>' in wb_html or '<option value="红色寻访专项">红色寻访专项</option>' in wb_html
    assert "20250911" in wb_html
    assert "20250912" not in wb_html

    node = shutil.which("node")
    if node:
        scripts = re.findall(
            r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
            wb_html,
            re.S | re.I,
        )
        for body in scripts:
            if not body.strip():
                continue
            handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
            try:
                handle.write(body)
                handle.close()
                chk = subprocess.run([node, "--check", handle.name], capture_output=True, text=True)
            finally:
                Path(handle.name).unlink(missing_ok=True)
            assert chk.returncode == 0, chk.stderr

    # 2) 首页仪表盘：活动类别下拉与管理员配置一致 + 审核列表过滤生效
    dash_resp = client.get(f"/?activity_id={activity.pk}&category=红色寻访专项")
    assert dash_resp.status_code == 200
    dash_html = dash_resp.content.decode("utf-8")
    assert 'id="filter_category"' in dash_html
    assert '<option value="红色寻访专项" selected>红色寻访专项</option>' in dash_html
    review_tbl_html = dash_html.split('id="review-submissions-table"', 1)[1].split('id="account-progress-section"', 1)[0]
    assert "20250912" in review_tbl_html
    assert "20250911" not in review_tbl_html

    # 3) 审核列表 JSON API 按活动类别过滤
    api_resp = client.get(f"/api/v1/reviews/?activity_id={activity.pk}&category=母校行专项")
    assert api_resp.status_code == 200
    api_items = api_resp.json()["items"]
    assert [it["student_number"] for it in api_items] == ["20250911"]

    # 4) Excel 筛选导出按活动类别过滤
    excel_resp = client.get(f"/api/v1/exports/activities/{activity.pk}/excel/?category=母校行专项")
    assert excel_resp.status_code == 200
    wb_xlsx = openpyxl.load_workbook(io.BytesIO(excel_resp.content))
    ws = wb_xlsx.active
    exported_sns = [row[0] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert exported_sns == ["20250911"]


def test_declaration_type_filter_and_live_table_scripts(client, activity, db):
    """验证个人/团队筛选（含缺省个人草稿）在工作台、首页、API 与导出全面生效，且无刷新内联脚本语法无误。"""
    import re
    import shutil
    import subprocess
    import tempfile
    import openpyxl

    admin = make_admin("admin_decl_filter")
    stu_ind = make_student("20250921", "个人同学", "信安25-1班")
    stu_legacy = make_student("20250922", "历史个人同学", "信安25-1班")
    stu_team = make_student("20250923", "团队队长", "信安25-1班")

    # 1) 显式个人申报
    d1 = create_draft(actor=stu_ind, activity_id=activity.pk)
    save_draft(
        actor=stu_ind,
        submission_id=d1.pk,
        expected_version=0,
        data={
            "project_name": "个人调研项目",
            "category": "社会调查",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    upload_required_materials(stu_ind, activity)
    submit_for_review(actor=stu_ind, submission_id=d1.pk)

    # 2) 未显式写 declaration_type 的历史个人草稿
    d2 = create_draft(actor=stu_legacy, activity_id=activity.pk)
    Submission.objects.filter(pk=d2.pk).update(
        draft_data={"project_name": "历史个人项目", "category": "社会调查"},
        status=Submission.Status.PENDING_FIRST_REVIEW,
    )

    # 3) 团队申报
    d3 = create_draft(actor=stu_team, activity_id=activity.pk)
    save_draft(
        actor=stu_team,
        submission_id=d3.pk,
        expected_version=0,
        data={
            "project_name": "团队实践项目",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [
                {"student_number": "20250999", "real_name": "队员甲", "class_name": "信安25-1班"}
            ],
        },
    )
    upload_required_materials(stu_team, activity)
    submit_for_review(actor=stu_team, submission_id=d3.pk)

    client.force_login(admin)

    # 审核工作台：筛选团队 vs 个人
    wb_team = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "declaration_type": "team"}).content.decode("utf-8")
    assert 'id="filter_declaration_type"' in wb_team
    assert '<option value="team" selected>团队申报</option>' in wb_team
    assert "20250923" in wb_team
    assert "20250921" not in wb_team
    assert "20250922" not in wb_team

    wb_ind = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk), "declaration_type": "individual"}).content.decode("utf-8")
    assert '<option value="individual" selected>个人申报</option>' in wb_ind
    assert "20250921" in wb_ind
    assert "20250922" in wb_ind
    assert "20250923" not in wb_ind

    # 首页仪表盘：审核列表筛选团队 vs 个人
    dash_team = client.get(f"/?activity_id={activity.pk}&declaration_type=team").content.decode("utf-8")
    assert 'id="filter_declaration_type"' in dash_team
    assert '<option value="team" selected>团队申报</option>' in dash_team
    dash_team_tbl = dash_team.split('id="review-submissions-table"', 1)[1].split('id="account-progress-section"', 1)[0]
    assert "20250923" in dash_team_tbl
    assert "20250921" not in dash_team_tbl

    dash_ind = client.get(f"/?activity_id={activity.pk}&declaration_type=individual").content.decode("utf-8")
    dash_ind_tbl = dash_ind.split('id="review-submissions-table"', 1)[1].split('id="account-progress-section"', 1)[0]
    assert "20250921" in dash_ind_tbl
    assert "20250922" in dash_ind_tbl
    assert "20250923" not in dash_ind_tbl

    # API 与 Excel 筛选导出
    api_team = client.get(f"/api/v1/reviews/?activity_id={activity.pk}&declaration_type=team")
    assert api_team.status_code == 200
    assert [it["student_number"] for it in api_team.json()["items"]] == ["20250923"]

    excel_team = client.get(f"/api/v1/exports/activities/{activity.pk}/excel/?declaration_type=team")
    assert excel_team.status_code == 200
    wb_xlsx = openpyxl.load_workbook(io.BytesIO(excel_team.content))
    ws = wb_xlsx.active
    assert [row[0] for row in ws.iter_rows(min_row=2, values_only=True)] == ["20250923"]

    # 检查两页无刷新筛选内联 JS 语法全部合法
    node = shutil.which("node")
    if node:
        for html in (wb_team, dash_team):
            scripts = re.findall(
                r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
                html,
                re.S | re.I,
            )
            for body in scripts:
                if not body.strip():
                    continue
                handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
                try:
                    handle.write(body)
                    handle.close()
                    chk = subprocess.run([node, "--check", handle.name], capture_output=True, text=True)
                finally:
                    Path(handle.name).unlink(missing_ok=True)
                assert chk.returncode == 0, chk.stderr


def test_team_member_inherits_leader_submission_and_admin_account_progress_panel(client, activity, db):
    """验证团队申报由队长提交即可（队员自动同步团队进度且不在审核队列单独显示），以及管理员全员注册账号与未提交筛选板块。"""
    import re
    import shutil
    import subprocess
    import tempfile

    admin = make_admin("admin_acct_panel")
    leader = make_student("20251001", "王队长", "信安25-1班")
    member = make_student("20251002", "赵队员", "信安25-1班")
    decl_only_stu = make_student("20251003", "陈只报", "信安25-2班")
    unsubmitted_stu = make_student("20251004", "李未交", "信安25-2班")

    # 模拟队员在加入团队前曾登录首页留下一条空白占位草稿
    create_draft(actor=member, activity_id=activity.pk)

    # 1) 队长创建团队申报（含赵队员）、上传材料并提交成果审核
    leader_draft = create_draft(actor=leader, activity_id=activity.pk)
    save_draft(
        actor=leader,
        submission_id=leader_draft.pk,
        expected_version=0,
        data={
            "project_name": "矿大安全先锋团队",
            "category": "社会调查",
            "advisor_name": "周教授",
            "practice_place": "徐州市",
            "date_start": "2026-07-01",
            "date_end": "2026-07-10",
            "declaration_type": "team",
            "team_members": [
                {"student_number": "20251002", "real_name": "赵队员", "class_name": "信安25-1班"}
            ],
        },
    )
    reqs = upload_required_materials(leader, activity)
    submit_for_review(actor=leader, submission_id=leader_draft.pk)

    # 2) 陈只报仅完成立项申报（上传 .docx 并确认提交），尚未提交成果
    decl_only_draft = create_draft(actor=decl_only_stu, activity_id=activity.pk)
    save_draft(
        actor=decl_only_stu,
        submission_id=decl_only_draft.pk,
        expected_version=0,
        data={
            "project_name": "个人立项未交成果",
            "category": "社会调查",
            "advisor_name": "刘老师",
            "practice_place": "南京市",
            "date_start": "2026-07-02",
            "date_end": "2026-07-08",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    direct_upload(
        actor=decl_only_stu,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("个人申报表.docx", b"PK\x03\x04" + b"D" * 64),
    )
    client.force_login(decl_only_stu)
    assert client.post(f"/api/v1/submissions/{decl_only_draft.id}/declare/").status_code == 200

    # 3) 验证队员端：登录首页/申报页/成果页均同步显示队长团队申报、进度 2/2、只读且可下载队长文件但不可删除
    client.force_login(member)
    member_dash = client.get(f"/?activity_id={activity.pk}").content.decode("utf-8")
    assert "矿大安全先锋团队" in member_dash
    assert "王队长" in member_dash
    assert "2/2" in member_dash
    assert 'data-readonly-locked="true"' in member_dash

    member_apply = client.get(f"/submissions/apply/?activity_id={activity.pk}").content.decode("utf-8")
    assert "矿大安全先锋团队" in member_apply
    assert 'data-upload-locked="true"' in member_apply

    member_outcomes = client.get(f"/submissions/outcomes/?activity_id={activity.pk}").content.decode("utf-8")
    assert "矿大安全先锋团队" in member_outcomes
    assert 'data-upload-locked="true"' in member_outcomes

    from apps.uploads.models import UploadSession

    leader_upload = UploadSession.objects.filter(
        owner=leader, requirement=reqs["application_form"], status=UploadSession.Status.COMPLETE
    ).first()
    assert leader_upload is not None
    assert client.get(f"/api/v1/uploads/{leader_upload.id}/file/").status_code == 200
    assert client.post(f"/api/v1/uploads/{leader_upload.id}/delete/").status_code == 403

    # 4) 验证审核队列：队员空草稿不单独显示，审核列表/工作台只展示队长团队申报（不把 20251002 当成单独申报行）
    client.force_login(admin)
    wb_html = client.get(WORKBENCH_URL, {"activity_id": str(activity.pk)}).content.decode("utf-8")
    assert "20251001" in wb_html
    assert "20251002" not in wb_html

    api_items = client.get(f"/api/v1/reviews/?activity_id={activity.pk}").json()["items"]
    api_sns = [it["student_number"] for it in api_items]
    assert "20251001" in api_sns
    assert "20251002" not in api_sns

    # 5) 验证管理员「全院注册账号提交情况」板块及各类未提交筛选
    dash_all = client.get(f"/?activity_id={activity.pk}").content.decode("utf-8")
    assert 'id="account-progress-section"' in dash_all
    for sn in ("20251001", "20251002", "20251003", "20251004"):
        assert f'data-account-row="{sn}"' in dash_all
    assert "团队申报（队长）" in dash_all
    assert "团队申报（队员）" in dash_all

    # 未提交申报：仅李未交（队长与赵队员均已同步为已提交，陈只报已交申报）
    dash_decl_unsub = client.get(f"/?activity_id={activity.pk}&acct_submit=decl_unsubmitted").content.decode("utf-8")
    assert 'data-account-row="20251004"' in dash_decl_unsub
    assert 'data-account-row="20251001"' not in dash_decl_unsub
    assert 'data-account-row="20251002"' not in dash_decl_unsub
    assert 'data-account-row="20251003"' not in dash_decl_unsub

    # 未提交成果：陈只报 + 李未交（队长和赵队员均已提交成果）
    dash_out_unsub = client.get(f"/?activity_id={activity.pk}&acct_submit=outcome_unsubmitted").content.decode("utf-8")
    assert 'data-account-row="20251003"' in dash_out_unsub
    assert 'data-account-row="20251004"' in dash_out_unsub
    assert 'data-account-row="20251001"' not in dash_out_unsub
    assert 'data-account-row="20251002"' not in dash_out_unsub

    # 均已提交：王队长 + 赵队员
    dash_both_sub = client.get(f"/?activity_id={activity.pk}&acct_submit=both_submitted").content.decode("utf-8")
    assert 'data-account-row="20251001"' in dash_both_sub
    assert 'data-account-row="20251002"' in dash_both_sub
    assert 'data-account-row="20251003"' not in dash_both_sub
    assert 'data-account-row="20251004"' not in dash_both_sub

    # 校验管理员首页内联 JS 语法
    node = shutil.which("node")
    if node:
        scripts = re.findall(
            r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
            dash_all,
            re.S | re.I,
        )
        for body in scripts:
            if not body.strip():
                continue
            handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
            try:
                handle.write(body)
                handle.close()
                chk = subprocess.run([node, "--check", handle.name], capture_output=True, text=True)
            finally:
                Path(handle.name).unlink(missing_ok=True)
            assert chk.returncode == 0, chk.stderr




