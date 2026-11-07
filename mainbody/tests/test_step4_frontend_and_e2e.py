from datetime import timedelta
from pathlib import Path
import json
import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from apps.accounts.models import User
from apps.activities.models import Activity
from apps.submissions.models import Submission
from apps.uploads.services import direct_upload, ensure_standard_material_requirements
from tests.conftest import seed_mandatory_materials


# =====================================================================
# 1. 账号登录、学生自主注册与首次改密页面测试 (templates/registration/)
# =====================================================================

def test_login_register_and_first_password_change_flow(client, db):
    """验证登录页、自主注册页（含大一至研究生）、默认后6位密码提示、must_change_password 顶部提示与改密闭环。"""
    # 1) 登录页渲染
    login_resp = client.get("/accounts/login/")
    assert login_resp.status_code == 200
    login_html = login_resp.content.decode("utf-8")
    assert "学号后 6 位" in login_html
    assert "/accounts/register/" in login_html

    # 2) 注册页渲染（检查学号、姓名、大一至大四及研究生、专业、班级、手机号、密码及规则说明）
    reg_get = client.get("/accounts/register/")
    assert reg_get.status_code == 200
    reg_html = reg_get.content.decode("utf-8")
    for grade_label in ("大一", "大二", "大三", "大四", "研究生"):
        assert f'value="{grade_label}"' in reg_html
    assert "学号后 6 位" in reg_html

    # 3) 提交学生自主注册表单（密码留空 -> 自动使用学号后6位，must_change_password=True）
    reg_post = client.post(
        "/accounts/register/",
        {
            "student_number": "2025080988",
            "real_name": "周同学",
            "grade": "大二",
            "major": "信息安全",
            "class_name": "信安25-1班",
            "phone": "13800138088",
            "password": "",
        },
    )
    assert reg_post.status_code == 302
    user = User.objects.get(username="2025080988")
    assert user.real_name == "周同学"
    assert user.grade == "大二"
    assert user.major == "信息安全"
    assert user.class_name == "信安25-1班"
    assert user.phone == "13800138088"
    assert user.role == User.Role.STUDENT
    assert user.must_change_password is True
    assert user.check_password("080988") is True

    # 4) 登录后访问工作台，顶部显著展示 must_change_password=True 首次改密提醒横幅
    dash_resp = client.get("/")
    assert dash_resp.status_code == 200
    dash_html = dash_resp.content.decode("utf-8")
    assert 'id="must-change-password-banner"' in dash_html
    assert 'data-must-change-password="true"' in dash_html
    assert "/accounts/password-change/" in dash_html

    # 5) 访问修改密码页面并完成首次改密
    pwd_get = client.get("/accounts/password-change/")
    assert pwd_get.status_code == 200

    pwd_post = client.post(
        "/accounts/password-change/",
        {
            "old_password": "080988",
            "new_password": "UpdatedPass#2026",
            "confirm_password": "UpdatedPass#2026",
        },
    )
    assert pwd_post.status_code == 302
    user.refresh_from_db()
    assert user.must_change_password is False
    assert user.check_password("UpdatedPass#2026") is True

    # 6) 改密完成后再次访问工作台，顶部首次改密警告横幅已消失
    dash_after = client.get("/")
    assert 'id="must-change-password-banner"' not in dash_after.content.decode("utf-8")


# =====================================================================
# 2. 学生工作台、拖拽上传进度条、打回修改红色提示框与只读锁定测试
# =====================================================================

def test_student_workbench_drag_upload_returned_banner_and_readonly_lock(client, activity, db):
    """验证学生工作台个人/团队切换、拖拽上传与 upload.onprogress 进度条、打回红色提示框、原草稿修改与只读锁定。"""
    student = User.objects.create_user(
        username="20250601",
        password="password123",
        real_name="张队长",
        class_name="信安25-1班",
        grade="大二",
        major="信息安全",
        must_change_password=False,
    )
    reviewer = User.objects.create_user(
        username="reviewer_union",
        password="password123",
        real_name="李团支书",
        role=User.Role.REVIEWER,
        must_change_password=False,
    )
    teacher = User.objects.create_user(
        username="teacher_admin",
        password="password123",
        real_name="王指导老师",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )

    client.force_login(student)
    resp = client.get(f"/?activity_id={activity.id}")
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")

    # 验证工作台包含活动标题、草稿状态徽章、个人/团队切换、拖拽上传组件与原生 XHR upload.onprogress 实时进度条
    assert activity.title in html
    assert "草稿" in html
    assert 'id="radio_decl_individual"' in html
    assert 'id="radio_decl_team"' in html
    assert "addTeamMemberRow" in html
    assert "removeTeamMemberRow" in html
    assert "XMLHttpRequest" in html
    assert "xhr.upload.onprogress" in html
    assert "validateFileBeforeUpload" in html
    assert 'id="dropzone-application_form"' in html
    assert 'id="dropzone-practice_report"' in html
    assert 'id="dropzone-scene_photos"' in html
    assert 'id="dropzone-video"' in html
    assert 'id="dropzone-other_materials"' in html

    # 学生通过工作台表单保存团队申报草稿
    sub = Submission.objects.get(owner=student, activity=activity)
    save_resp = client.post(
        "/student/submissions/save/",
        {
            "activity_id": str(activity.id),
            "submission_id": str(sub.id),
            "expected_version": str(sub.version),
            "project_name": "矿区网络安全靶场科普实践",
            "category": "科普宣讲",
            "declaration_type": "team",
            "member_student_number": ["20250602", "20250603"],
            "member_real_name": ["陈队员", "林队员"],
            "member_class_name": ["信安25-1班", "信安25-2班"],
        },
    )
    assert save_resp.status_code == 302
    sub.refresh_from_db()
    assert sub.draft_data["project_name"] == "矿区网络安全靶场科普实践"
    assert sub.draft_data["declaration_type"] == "team"
    assert len(sub.draft_data["team_members"]) == 2

    # 上传必交材料 + 选交视频并点击正式提交审核
    reqs = seed_mandatory_materials(student, activity)
    direct_upload(
        actor=student,
        requirement_id=reqs["video"].id,
        file_obj=SimpleUploadedFile("宣讲视频.mp4", b"\x00\x00\x00ftypmp42\x00\x00\x00\x00mp42isom" + b"v" * 64),
    )

    submit_resp = client.post(f"/student/submissions/{sub.id}/submit/")
    assert submit_resp.status_code == 302
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW

    # 待初审状态下，学生工作台表单与上传区只读锁定
    locked_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert 'id="readonly-lock-banner"' in locked_html
    assert 'data-readonly-locked="true"' in locked_html
    assert 'data-form-locked="true"' in locked_html
    assert 'data-upload-locked="true"' in locked_html

    # =====================================================================
    # 3. 团支书初审与指导老师终审工作台、在线预览/下载、打回修改双重校验
    # =====================================================================
    client.force_login(reviewer)
    rev_wb = client.get(f"/?activity_id={activity.id}&status=待初审&class_name=信安25-1班")
    assert rev_wb.status_code == 200
    rev_html = rev_wb.content.decode("utf-8")
    assert "矿区网络安全靶场科普实践" in rev_html
    assert "张队长" in rev_html
    assert "浏览器新窗口在线预览" in rev_html
    assert 'target="_blank"' in rev_html
    assert "inline=1" in rev_html
    assert "下载查看" in rev_html

    # 独立审核详情页检查
    detail_resp = client.get(f"/reviews/submissions/{sub.id}/")
    assert detail_resp.status_code == 200
    detail_html = detail_resp.content.decode("utf-8")
    assert "陈队员" in detail_html
    assert "林队员" in detail_html
    assert "浏览器新窗口在线预览" in detail_html
    assert "下载查看" in detail_html

    # 团支书点击“打回修改”但打回意见为空 -> 后端强制拦截返回 400
    empty_reject = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "returned", "comment": "   "},
    )
    assert empty_reject.status_code == 400
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW

    # 团支书填写具体打回意见执行打回
    reject_reason = "现场照片中缺少团队旗帜合影，请在原草稿补充后重交！"
    ok_reject = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "returned", "comment": reject_reason},
    )
    assert ok_reject.status_code == 302
    sub.refresh_from_db()
    assert sub.status == Submission.Status.RETURNED

    # 学生重新登录查看工作台：顶部出现醒目红色打回提示框展示打回意见原文，且允许在原草稿直接修改
    client.force_login(student)
    returned_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert 'id="returned-alert-banner"' in returned_html
    assert 'class="alert-returned"' in returned_html
    assert reject_reason in returned_html
    assert 'data-readonly-locked="true"' not in returned_html

    # 学生在原草稿上直接修改项目名称并再次提交审核
    resave_resp = client.post(
        f"/student/submissions/{sub.id}/submit/",
        {
            "project_name": "矿区网络安全靶场科普实践（修订版）",
            "category": "科普宣讲",
            "declaration_type": "team",
            "member_student_number": ["20250602"],
            "member_real_name": ["陈队员"],
            "member_class_name": ["信安25-1班"],
        },
    )
    assert resave_resp.status_code == 302
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert sub.draft_data["project_name"] == "矿区网络安全靶场科普实践（修订版）"
    assert Submission.objects.filter(owner=student, activity=activity).count() == 1

    # 团支书执行“初审通过” -> 状态流转为待终审
    client.force_login(reviewer)
    first_approve = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "approved", "comment": "初审材料齐全，同意上报学院终审"},
    )
    assert first_approve.status_code == 302
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FINAL_REVIEW

    # 指导老师/管理员筛选“待终审”并执行“终审通过” -> 状态流转为终审通过
    client.force_login(teacher)
    teacher_wb = client.get(f"/?activity_id={activity.id}&status=pending_final_review")
    assert teacher_wb.status_code == 200
    assert "矿区网络安全靶场科普实践（修订版）" in teacher_wb.content.decode("utf-8")

    # 指导老师若点击打回修改但未填意见，同样被强制拦截
    teacher_empty_reject = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "returned", "comment": ""},
    )
    assert teacher_empty_reject.status_code == 400

    final_approve = client.post(
        f"/reviews/submissions/{sub.id}/action/",
        {"decision": "approved", "comment": "学院终审通过，准予结项归档"},
    )
    assert final_approve.status_code == 302
    sub.refresh_from_db()
    assert sub.status == Submission.Status.FINAL_APPROVED

    # 学生端查看已终审通过的申报：处于只读锁定且状态为“终审通过”
    client.force_login(student)
    final_student_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert "终审通过" in final_student_html
    assert 'data-readonly-locked="true"' in final_student_html


# =====================================================================
# 4. 管理员活动配置、补交白名单管理与一键导出页面测试
# =====================================================================

def test_admin_activity_config_whitelist_and_export_ui(client, activity, db):
    """验证管理员在前端工作台修改活动时间、添加/移除补交白名单学号、一键导出 Excel 及按班级打包 ZIP。"""
    admin_user = User.objects.create_user(
        username="college_admin_01",
        password="password123",
        real_name="张主任",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )
    late_student = User.objects.create_user(
        username="20250999",
        password="password123",
        real_name="补交同学",
        class_name="计算机25-1班",
        must_change_password=False,
    )

    client.force_login(admin_user)

    # 1) 管理员更新活动开始时间与截止时间（设为已截止）
    now = timezone.now()
    past_open = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
    past_deadline = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    save_act_resp = client.post(
        "/admin-panel/activities/save/",
        {
            "activity_id": str(activity.id),
            "title": "2026暑期社会实践（已截止补交期）",
            "opens_at": past_open,
            "deadline": past_deadline,
            "is_open": "true",
        },
    )
    assert save_act_resp.status_code == 302
    activity.refresh_from_db()
    assert activity.title == "2026暑期社会实践（已截止补交期）"

    # 2) 管理员添加补交白名单学号 20250999
    add_wl_resp = client.post(
        f"/admin-panel/activities/{activity.id}/whitelist/",
        {"action": "add", "student_numbers": "20250999, 20250998"},
    )
    assert add_wl_resp.status_code == 302
    activity.refresh_from_db()
    assert "20250999" in activity.late_whitelist_student_numbers
    assert "20250998" in activity.late_whitelist_student_numbers

    # 3) 管理员移除 20250998，保留 20250999
    rem_wl_resp = client.post(
        f"/admin-panel/activities/{activity.id}/whitelist/",
        {"action": "remove", "student_numbers": "20250998"},
    )
    assert rem_wl_resp.status_code == 302
    activity.refresh_from_db()
    assert activity.late_whitelist_student_numbers == ["20250999"]

    # 4) 白名单学生 20250999 登录后在活动截止后仍可在工作台正常保存草稿
    client.force_login(late_student)
    late_wb = client.get(f"/?activity_id={activity.id}")
    assert late_wb.status_code == 200
    late_html = late_wb.content.decode("utf-8")
    assert "本人已在补交白名单内" in late_html
    assert 'data-readonly-locked="true"' not in late_html

    late_sub = Submission.objects.get(owner=late_student, activity=activity)
    client.post(
        "/student/submissions/save/",
        {
            "activity_id": str(activity.id),
            "submission_id": str(late_sub.id),
            "expected_version": str(late_sub.version),
            "project_name": "白名单补交调研项目",
            "category": "社会调查",
            "declaration_type": "individual",
        },
    )
    seed_mandatory_materials(late_student, activity)
    client.post(f"/student/submissions/{late_sub.id}/submit/")
    late_sub.refresh_from_db()
    assert late_sub.status == Submission.Status.PENDING_FIRST_REVIEW

    # 5) 管理员工作台渲染一键导出 Excel 与按班级（计算机25-1班）打包下载 ZIP 按钮
    client.force_login(admin_user)
    admin_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert f"/api/v1/exports/activities/{activity.id}/excel/" in admin_html
    assert f"/api/v1/exports/activities/{activity.id}/zip/" in admin_html
    assert "计算机25-1班" in admin_html


# =====================================================================
# 5. 部署配置对齐校验 (deploy/nginx.conf & deploy/gunicorn.conf.py)
# =====================================================================

def test_nginx_and_gunicorn_deployment_config_aligned():
    """验证 deploy/nginx.conf 中 client_max_body_size 设为 350m 及超时参数对齐 300MB 视频上传需求。"""
    nginx_conf_path = Path(settings.BASE_DIR) / "deploy" / "nginx.conf"
    nginx_text = nginx_conf_path.read_text(encoding="utf-8")
    assert "client_max_body_size 350m;" in nginx_text
    assert "client_body_timeout 300s;" in nginx_text
    assert "proxy_read_timeout 300s;" in nginx_text
    assert "proxy_send_timeout 300s;" in nginx_text

    gunicorn_conf_path = Path(settings.BASE_DIR) / "deploy" / "gunicorn.conf.py"
    gunicorn_text = gunicorn_conf_path.read_text(encoding="utf-8")
    assert "timeout = 300" in gunicorn_text
