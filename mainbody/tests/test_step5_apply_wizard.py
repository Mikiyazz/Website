"""学生端「社会实践申报」四步向导：页面骨架、材料规则、学号查询与静态资产。

对应本轮交付：
- apps/uploads/policies.py 按 A 方案升级 video / other_materials 规则（无别名、无重复落库行）
- templates/submissions/apply.html 四步向导
- static/vendor/filepond 本地资产（无 CDN）
- /submissions/apply/ 页面 + /api/students/lookup/ 占位接口
"""
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.common.errors import Conflict
from apps.uploads.models import StorageQuota, UploadSession
from apps.uploads.services import assert_extension_count_available, reserve_upload
from apps.activities.models import Activity
from apps.submissions.models import Submission
from apps.common.errors import DomainError
from apps.uploads.policies import (
    STANDARD_MATERIAL_CODES,
    STANDARD_MATERIAL_RULES,
    extension_max_count,
    rule_max_file_bytes,
    validate_magic_bytes,
    validate_metadata,
)
from apps.uploads.services import ensure_standard_material_requirements


MB = 1024 * 1024


# =====================================================================
# 1. 材料规则：video / other_materials 升级到新规范（A 方案，无别名）
# =====================================================================

def test_material_rules_upgraded_in_place():
    """宣讲视频支持四种容器格式；其他证明材料新增 .zip 并把打包上限特批到 2000MB。"""
    video = STANDARD_MATERIAL_RULES["video"]
    assert video["code"] == "video"
    assert video["title"] == "宣讲视频"
    assert video["required"] is False
    assert video["allowed_extensions"] == [".mp4", ".mov", ".avi", ".mkv"]
    assert video["max_file_bytes"] == 300 * MB
    assert video["max_count"] == 1

    proof = STANDARD_MATERIAL_RULES["other_materials"]
    assert proof["code"] == "other_materials"
    assert proof["title"] == "其他证明材料"
    assert proof["required"] is False
    assert proof["allowed_extensions"] == [".pdf", ".jpg", ".jpeg", ".png", ".zip"]
    assert proof["max_file_bytes"] == 50 * MB
    assert proof["max_count"] == 10
    assert proof["extension_max_bytes"] == {".zip": 2000 * MB}
    assert proof["extension_max_count"] == {".zip": 5}
    assert extension_max_count("other_materials", ".zip") == 5
    assert extension_max_count("other_materials", ".pdf") is None

    # 按扩展名的有效上限：zip 特批 2000MB，其余沿用类别上限
    assert rule_max_file_bytes("other_materials", ".zip") == 2000 * MB
    assert rule_max_file_bytes("other_materials", ".pdf") == 50 * MB
    assert rule_max_file_bytes("video", ".mkv") == 300 * MB

    # 无别名残留：规则表就是五个标准类别
    assert set(STANDARD_MATERIAL_RULES) == set(STANDARD_MATERIAL_CODES)


def test_requirement_seeding_still_five_rows(activity):
    """ensure_standard_material_requirements 仍然只落五个标准类别。"""
    reqs = ensure_standard_material_requirements(activity)
    assert set(reqs.keys()) == set(STANDARD_MATERIAL_CODES)
    assert reqs["video"].allowed_extensions == [".mp4", ".mov", ".avi", ".mkv"]
    assert reqs["other_materials"].allowed_extensions == [".pdf", ".jpg", ".jpeg", ".png", ".zip"]


@pytest.mark.django_db
def test_video_container_magic_bytes(activity):
    """MP4/MOV/AVI/MKV 四种容器都要过魔数校验，伪造内容要被拦下。"""
    samples = {
        ".mp4": (b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00", "video/mp4"),
        ".mov": (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        ".avi": (b"RIFF\x24\x00\x00\x00AVI LIST", "video/x-msvideo"),
        ".mkv": (b"\x1a\x45\xdf\xa3\x42\x86\x81\x01", "video/x-matroska"),
    }
    for ext, (payload, expected_type) in samples.items():
        assert validate_magic_bytes(payload + b"\x00" * 20, ext) == expected_type
        with pytest.raises(DomainError):
            validate_magic_bytes(b"plain text pretending to be video", ext)


@pytest.mark.django_db
def test_zip_package_allows_2000mb_while_other_materials_stay_50mb(activity):
    """打包 .zip 单份可到 2000MB；同类别里的普通材料仍按 50MB 拦截。"""
    reqs = ensure_standard_material_requirements(activity)
    requirement = reqs["other_materials"]

    assert validate_metadata(filename="材料包.zip", declared_bytes=2000 * MB, requirement=requirement) == ".zip"
    assert validate_metadata(filename="说明.pdf", declared_bytes=50 * MB, requirement=requirement) == ".pdf"

    with pytest.raises(DomainError):
        validate_metadata(filename="超大包.zip", declared_bytes=2000 * MB + 1, requirement=requirement)
    with pytest.raises(DomainError):
        validate_metadata(filename="超大说明.pdf", declared_bytes=50 * MB + 1, requirement=requirement)


# =====================================================================
# 2. 页面入口
# =====================================================================

def test_apply_page_renders_four_step_wizard(client, student, activity):
    client.force_login(student)
    resp = client.get("/submissions/apply/")
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")

    # 步骤条四个可点击步骤（项目信息 / 团队成员 / 申报表附件 / 确认提交）
    assert 'id="wizard-steps"' in html
    assert html.count('class="step-item') == 4
    for step in ("项目信息", "团队成员", "申报表附件", "确认提交"):
        assert step in html

    # Step1 字段
    for field_id in (
        "field-project-name",
        "field-advisor",
        "field-place",
        "field-date-start",
        "field-date-end",
        "field-intro",
    ):
        assert 'id="' + field_id + '"' in html

    # Step2 申报模式与队员
    assert 'id="radio_decl_individual"' in html
    assert 'id="radio_decl_team"' in html
    assert "个人申报无需成员" in html
    assert 'id="btn-add-member"' in html
    assert 'id="team-member-template"' in html

    # Step3 仅展示 1 个 .docx 申报表 FilePond 实例；其余 4 项成果材料位于独立栏目「成果收集」
    assert 'id="pond-application_form"' in html
    assert html.count('class="filepond-zone"') == 1
    for code in ("practice_report", "scene_photos", "video", "other_materials"):
        assert 'id="pond-' + code + '"' not in html

    outcomes_resp = client.get("/submissions/outcomes/")
    assert outcomes_resp.status_code == 200
    outcomes_html = outcomes_resp.content.decode("utf-8")
    for code in ("practice_report", "scene_photos", "video", "other_materials"):
        assert 'id="pond-' + code + '"' in outcomes_html
    assert 'id="pond-application_form"' not in outcomes_html
    assert outcomes_html.count('class="filepond-zone"') == 4

    # Step4 确认提交
    assert 'id="summary-materials"' in html
    assert 'id="btn-confirm-submit"' in html
    assert 'id="submit-error"' in html


def test_apply_page_loads_local_filepond_and_chinese_locale(client, student, activity):
    client.force_login(student)
    html = client.get("/submissions/apply/").content.decode("utf-8")
    assert "vendor/filepond/filepond.min.js" in html
    assert "vendor/filepond/filepond.min.css" in html
    assert "vendor/filepond/locale/zh-cn.global.js" in html
    for plugin in (
        "filepond-plugin-file-validate-type.min.js",
        "filepond-plugin-file-validate-size.min.js",
        "filepond-plugin-image-validate-size.min.js",
        "filepond-plugin-image-preview.min.js",
        "filepond-plugin-image-exif-orientation.min.js",
        "filepond-plugin-file-poster.min.js",
        "filepond-plugin-file-metadata.min.js",
    ):
        assert plugin in html
    # 无外链 CDN
    assert "cdn.jsdelivr.net" not in html
    assert "unpkg.com" not in html


def test_apply_page_reports_no_open_activity(client, student):
    now = timezone.now()
    Activity.objects.create(
        title="已关闭活动",
        opens_at=now,
        deadline=now + timedelta(days=1),
        is_open=False,
    )
    client.force_login(student)
    resp = client.get("/submissions/apply/")
    assert resp.status_code == 200
    assert "当前没有开放中的申报活动" in resp.content.decode("utf-8")


def test_apply_page_redirects_non_student(client, activity):
    guest = User.objects.create_user(
        username="guest_apply",
        password="password123",
        real_name="访客",
        role="external_guest",
        must_change_password=False,
    )
    client.force_login(guest)
    resp = client.get("/submissions/apply/")
    assert resp.status_code == 302
    assert resp["Location"] == "/"
    follow_resp = client.get("/submissions/apply/", follow=True)
    assert follow_resp.status_code == 200
    assert "工作台" in follow_resp.content.decode("utf-8")


def test_apply_page_allows_reviewer_and_admin(client, activity):
    reviewer = User.objects.create_user(
        username="reviewer_apply_allowed",
        password="password123",
        real_name="团支书",
        role=User.Role.REVIEWER,
        class_name="信安25-1班",
        must_change_password=False,
    )
    client.force_login(reviewer)
    resp = client.get("/submissions/apply/")
    assert resp.status_code == 200
    assert "社会实践申报" in resp.content.decode("utf-8")


def test_apply_page_creates_draft_and_submit_target(client, student, activity):
    client.force_login(student)
    client.get("/submissions/apply/")
    sub = Submission.objects.get(owner=student, activity=activity)
    html = client.get("/submissions/apply/").content.decode("utf-8")
    assert "/api/v1/submissions/" + str(sub.id) + "/submit/" in html
    assert Submission.objects.filter(owner=student, activity=activity).count() == 1


# =====================================================================
# 3. 学号查询占位接口
# =====================================================================

def test_student_lookup_endpoint(client, student, activity):
    User.objects.create_user(
        username="20250602",
        password="password123",
        real_name="陈队员",
        class_name="信安25-1班",
        must_change_password=False,
    )
    client.force_login(student)

    hit = client.get("/api/students/lookup/", {"student_number": "20250602"})
    assert hit.status_code == 200
    payload = hit.json()
    assert payload["exists"] is True
    assert payload["real_name"] == "陈队员"

    miss = client.get("/api/students/lookup/", {"student_number": "99999999"})
    assert miss.status_code == 200
    assert miss.json() == {"exists": False, "student_number": "99999999"}

    blank = client.get("/api/students/lookup/", {"student_number": "  "})
    assert blank.status_code == 400


def test_student_lookup_requires_auth(client, db):
    resp = client.get("/api/students/lookup/", {"student_number": "20250602"})
    assert resp.status_code == 401


# =====================================================================
# 4. 本地 vendor 资产
# =====================================================================

def test_filepond_vendor_assets_present():
    static_dir = Path(settings.BASE_DIR) / "static" / "vendor" / "filepond"
    required = [
        "filepond.min.js",
        "filepond.min.css",
        "locale/zh-cn.js",
        "locale/zh-cn.global.js",
        "plugins/filepond-plugin-file-validate-type.min.js",
        "plugins/filepond-plugin-file-validate-size.min.js",
        "plugins/filepond-plugin-image-validate-size.min.js",
        "plugins/filepond-plugin-image-preview.min.js",
        "plugins/filepond-plugin-image-preview.min.css",
        "plugins/filepond-plugin-image-exif-orientation.min.js",
        "plugins/filepond-plugin-file-poster.min.js",
        "plugins/filepond-plugin-file-metadata.min.js",
        "README.md",
    ]
    missing = [name for name in required if not (static_dir / name).exists()]
    assert not missing, "缺少 FilePond 本地资产: " + repr(missing)
    assert (static_dir / "filepond.min.js").stat().st_size > 50_000

    locale = (static_dir / "locale" / "zh-cn.global.js").read_text(encoding="utf-8")
    assert "拖放文件" in locale

def test_apply_page_locks_submitted_application(client, student, activity):
    """活动开放期内已提交的申报允许继续修改覆盖；活动截止后进入只读锁定。"""
    client.force_login(student)
    client.get("/submissions/apply/")
    sub = Submission.objects.get(owner=student, activity=activity)
    sub.status = Submission.Status.PENDING_FIRST_REVIEW
    sub.save(update_fields=["status", "updated_at"])

    open_html = client.get("/submissions/apply/").content.decode("utf-8")
    assert 'data-can-submit="true"' in open_html

    activity.deadline = timezone.now() - timedelta(hours=1)
    activity.save(update_fields=["deadline", "updated_at"])
    locked_html = client.get("/submissions/apply/").content.decode("utf-8")
    assert 'id="apply-locked-banner"' in locked_html
    assert "申报已锁定" in locked_html
    assert 'data-can-submit="false"' in locked_html

@pytest.mark.django_db
def test_zip_pack_count_is_capped_at_five(student, activity):
    """other_materials 里 .zip 最多 5 份：第 6 个打包件在预留阶段就被拒，其他格式不受影响。"""
    reqs = ensure_standard_material_requirements(activity)
    requirement = reqs["other_materials"]
    quota, _ = StorageQuota.objects.get_or_create(
        owner=student,
        activity=activity,
        defaults={"limit_bytes": activity.quota_bytes_per_student},
    )
    UploadSession.objects.bulk_create(
        [
            UploadSession(
                owner=student,
                requirement=requirement,
                quota=quota,
                object_key="activities/demo/pack-" + str(idx) + ".zip",
                original_name="实践材料包" + str(idx) + ".zip",
                declared_bytes=1024,
                status=UploadSession.Status.COMPLETE,
                expires_at=timezone.now() + timedelta(days=3),
            )
            for idx in range(1, 6)
        ]
    )

    with pytest.raises(Conflict) as excinfo:
        reserve_upload(
            actor=student,
            requirement_id=requirement.pk,
            filename="第六个包.zip",
            declared_bytes=1024,
            request_key=uuid.uuid4(),
        )
    assert ".zip" in str(excinfo.value)
    assert "最多 5 份" in str(excinfo.value)

    # 同一类别里的 pdf 不受 zip 份数限制（总数仍是 5/10）
    reserved = reserve_upload(
        actor=student,
        requirement_id=requirement.pk,
        filename="补充说明.pdf",
        declared_bytes=1024,
        request_key=uuid.uuid4(),
    )
    assert reserved.status == UploadSession.Status.RESERVED


@pytest.mark.django_db
def test_extension_count_helper_handles_locked_list(activity):
    """直传完成路径传入的是已加锁的列表，同样要按扩展名判份数。"""
    reqs = ensure_standard_material_requirements(activity)
    requirement = reqs["other_materials"]
    rows = [
        UploadSession(original_name="包" + str(idx) + ".zip", status=UploadSession.Status.COMPLETE)
        for idx in range(1, 6)
    ]
    with pytest.raises(Conflict):
        assert_extension_count_available(rows, requirement, ".zip")
    assert_extension_count_available(rows, requirement, ".pdf")
    assert_extension_count_available(rows[:4], requirement, ".zip")


def test_apply_page_exposes_zip_count_cap(client, student, activity):
    """成果收集页面把「ZIP 最多 5 份」同时写进文案和给 JS 的 JSON 载荷。"""
    client.force_login(student)
    html = client.get("/submissions/outcomes/").content.decode("utf-8")
    assert "ZIP 最多 5 份" in html

    payload = re.search(
        r'<script type="application/json" id="wizard-materials-json">(.*?)</script>',
        html,
        re.S,
    )
    assert payload is not None
    materials = json.loads(payload.group(1))
    proof = next(item for item in materials if item["code"] == "other_materials")
    assert proof["extension_count_limits"] == [{"ext": ".zip", "max_count": 5}]
    assert proof["extension_limits"] == [{"ext": ".zip", "max_mb": 2000}]
    assert "ZIP 最多 5 份" in proof["count_hint"]


# =====================================================================
# 5. 首页仪表盘去上传化 & 我的申报闭环增强
# =====================================================================

@pytest.mark.django_db
def test_dashboard_has_no_file_upload_and_routes_declaration_to_apply(client, student, activity):
    """首页仪表盘与学生工作台入口均不得包含文件上传控件或旧版申报表单，全部统一跳转「我的申报」。"""
    from apps.submissions.services import create_draft, save_draft
    from tests.conftest import seed_mandatory_materials

    sub = create_draft(actor=student, activity_id=activity.id)
    save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": "矿区工业互联网安全宣讲",
            "category": "科普宣讲",
            "advisor_name": "李教授",
            "practice_place": "徐州市泉山区",
            "date_start": "2026-07-01",
            "date_end": "2026-07-10",
            "declaration_type": "individual",
            "team_members": [],
        },
    )
    seed_mandatory_materials(student, activity)

    client.force_login(student)
    for url in (f"/?activity_id={activity.id}", f"/student/workbench/?activity_id={activity.id}"):
        resp = client.get(url)
        assert resp.status_code == 200
        html = resp.content.decode("utf-8")
        assert 'type="file"' not in html
        assert "dropzone-" not in html
        assert 'id="upload-workbench-section"' not in html
        assert 'id="student-declaration-form"' not in html
        assert "/submissions/apply/" in html
        assert "/submissions/outcomes/" in html
        assert "矿区工业互联网安全宣讲" in html
        assert "李教授" in html
        assert "2/2 成果必交" in html


@pytest.mark.django_db
def test_apply_page_prefills_draft_shows_returned_reason_and_delete_controls(client, student, activity):
    """我的申报完整回填草稿、支持删除已传附件、展示打回意见，且支持已截止活动的历史提交只读查看。"""
    from apps.reviews.models import ReviewRecord
    from apps.reviews.services import review_submission
    from apps.submissions.services import create_draft, save_draft, submit_for_review
    from tests.conftest import seed_mandatory_materials

    reviewer = User.objects.create_user(
        username="reviewer_apply_test",
        password="password123",
        real_name="张团支书",
        role=User.Role.REVIEWER,
        class_name=student.class_name,
        grade=student.grade,
        major=student.major,
        must_change_password=False,
    )
    sub = create_draft(actor=student, activity_id=activity.id)
    save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": "乡村数字素养提升实践",
            "category": "乡村振兴",
            "advisor_name": "陈老师",
            "practice_place": "新沂市棋盘镇",
            "date_start": "2026-07-05",
            "date_end": "2026-07-15",
            "project_intro": "开展老年防诈骗宣讲与数字技能指导。",
            "declaration_type": "team",
            "team_members": [
                {"student_number": "20250609", "real_name": "赵队员", "class_name": "信安25-1班"}
            ],
        },
    )
    seed_mandatory_materials(student, activity)
    submit_for_review(actor=student, submission_id=sub.id)
    review_submission(
        actor=reviewer,
        activity_id=activity.id,
        submission_id=sub.id,
        decision=ReviewRecord.Decision.RETURNED,
        comment="请补充团队旗帜合影并核对指导老师职称。",
    )

    client.force_login(student)
    resp = client.get(f"/submissions/apply/?activity_id={activity.id}")
    assert resp.status_code == 200
    html = resp.content.decode("utf-8")

    # 完整字段回填与打回提示展示（未锁定时严禁输出“只读锁定”）
    assert "乡村数字素养提升实践" in html
    assert 'value="乡村振兴" selected' in html
    assert 'value="陈老师"' in html
    assert 'value="新沂市棋盘镇"' in html
    assert 'value="2026-07-05"' in html
    assert 'value="2026-07-15"' in html
    assert "开展老年防诈骗宣讲与数字技能指导。" in html
    assert "赵队员" in html
    assert 'id="returned-alert-banner"' in html
    assert "请补充团队旗帜合影并核对指导老师职称。" in html
    assert 'id="apply-review-history"' in html
    assert 'id="btn-save-draft"' in html
    assert "btn-delete-saved-upload" in html
    assert "只读锁定" not in html

    # 验证在我的申报中通过 PATCH 更新草稿并删除已保存附件后生效
    sub.refresh_from_db()
    patch_resp = client.patch(
        f"/api/v1/submissions/{sub.id}/",
        data=json.dumps(
            {
                "expected_version": sub.version,
                "draft_data": {
                    "project_name": "乡村数字素养提升实践（修订）",
                    "category": "乡村振兴",
                    "advisor_name": "陈老师",
                    "practice_place": "新沂市棋盘镇",
                    "date_start": "2026-07-05",
                    "date_end": "2026-07-15",
                    "project_intro": "修订简介。",
                    "declaration_type": "team",
                    "team_members": [
                        {"student_number": "20250609", "real_name": "赵队员", "class_name": "信安25-1班"}
                    ],
                },
            }
        ),
        content_type="application/json",
    )
    assert patch_resp.status_code == 200
    target_upload = UploadSession.objects.filter(
        owner=student,
        requirement__activity=activity,
        status=UploadSession.Status.COMPLETE,
    ).first()
    del_resp = client.post(f"/api/v1/uploads/{target_upload.id}/delete/")
    assert del_resp.status_code == 200
    after_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    assert "乡村数字素养提升实践（修订）" in after_html
    assert str(target_upload.id) not in after_html

    # 已截止但有历史提交的活动，在我的申报中仍可只读查看且隐藏删除按钮
    now = timezone.now()
    activity.deadline = now - timedelta(hours=1)
    activity.save(update_fields=["deadline", "updated_at"])

    expired_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    assert 'id="apply-locked-banner"' in expired_html
    assert 'data-upload-locked="true"' in expired_html
    assert "btn-delete-saved-upload" not in expired_html


def test_apply_wizard_js_parsable_and_saves_draft_before_submit(client, student, activity):
    """apply-wizard.js 与 apply.html 内联脚本语法可解析，且在提交前通过 PATCH DETAIL_URL 保存含 class_name 的草稿。"""
    import shutil
    import subprocess
    import tempfile

    wizard_js = Path(settings.BASE_DIR) / "static" / "js" / "apply-wizard.js"
    source = wizard_js.read_text(encoding="utf-8")
    assert "DETAIL_URL" in source
    assert "saveDraft" in source
    assert "class_name" in source
    assert "btn-delete-saved-upload" in source
    assert "btn-save-draft" in source

    client.force_login(student)
    apply_html = client.get("/submissions/apply/").content.decode("utf-8")
    node = shutil.which("node")
    if node:
        js_check = subprocess.run([node, "--check", str(wizard_js)], capture_output=True, text=True)
        assert js_check.returncode == 0, js_check.stderr
        scripts = re.findall(
            r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
            apply_html,
            re.S | re.I,
        )
        for body in scripts:
            if not body.strip():
                continue
            handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
            try:
                handle.write(body)
                handle.close()
                inline_check = subprocess.run([node, "--check", handle.name], capture_output=True, text=True)
            finally:
                Path(handle.name).unlink(missing_ok=True)
            assert inline_check.returncode == 0, inline_check.stderr


def test_admin_configurable_project_categories_end_to_end(client, student, activity):
    """管理员按活动自定义项目类别：表单保存去重 -> 学生端动态下拉 -> 非法类别拦截 -> 停用提示 -> 清空恢复默认。"""
    import shutil
    import subprocess
    import tempfile
    from apps.activities.models import DEFAULT_PROJECT_CATEGORIES

    admin = User.objects.create_user(
        username="admin_cat_cfg",
        password="AdminPassword123!",
        real_name="配置管理员",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )
    client.force_login(admin)

    # 1) 管理面板展示类别配置框，且内联脚本可被 node 解析
    panel_resp = client.get(f"/admin-panel/?activity_id={activity.id}")
    assert panel_resp.status_code == 200
    panel_html = panel_resp.content.decode("utf-8")
    assert 'id="admin_act_categories"' in panel_html
    node = shutil.which("node")
    if node:
        scripts = re.findall(
            r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
            panel_html,
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

    # 2) 管理员通过管理面板配置自定义类别（混用换行、逗号、顿号及重复项）
    save_act_resp = client.post(
        "/admin-panel/activities/save/",
        {
            "activity_id": str(activity.id),
            "title": activity.title,
            "opens_at": activity.opens_at.strftime("%Y-%m-%dT%H:%M"),
            "deadline": activity.deadline.strftime("%Y-%m-%dT%H:%M"),
            "is_open": "true",
            "project_categories": "人工智能赋能专项\n绿色双碳调研，基层治理实践、绿色双碳调研",
        },
    )
    assert save_act_resp.status_code == 302
    activity.refresh_from_db()
    assert activity.project_categories == ["人工智能赋能专项", "绿色双碳调研", "基层治理实践"]

    # 3) 学生申报页下拉框动态展示该活动自定义类别，且不再出现旧默认项
    client.force_login(student)
    apply_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    select_match = re.search(r'<select[^>]*id="field-category"[^>]*>(.*?)</select>', apply_html, re.S)
    assert select_match is not None
    select_html = select_match.group(1)
    assert 'value="人工智能赋能专项"' in select_html
    assert 'value="绿色双碳调研"' in select_html
    assert 'value="基层治理实践"' in select_html
    assert 'value="社会调查"' not in select_html

    # 4) 合法自定义类别可正常保存草稿，不在列表中的旧类别被后端拒绝
    sub = Submission.objects.get(owner=student, activity=activity)
    ok_patch = client.patch(
        f"/api/v1/submissions/{sub.id}/",
        data=json.dumps(
            {
                "expected_version": sub.version,
                "draft_data": {
                    "project_name": "双碳矿区实践",
                    "category": "绿色双碳调研",
                    "declaration_type": "individual",
                    "team_members": [],
                },
            }
        ),
        content_type="application/json",
    )
    assert ok_patch.status_code == 200
    sub.refresh_from_db()
    assert sub.draft_data["category"] == "绿色双碳调研"

    bad_patch = client.patch(
        f"/api/v1/submissions/{sub.id}/",
        data=json.dumps(
            {
                "expected_version": sub.version,
                "draft_data": {
                    "project_name": "双碳矿区实践",
                    "category": "社会调查",
                    "declaration_type": "individual",
                    "team_members": [],
                },
            }
        ),
        content_type="application/json",
    )
    assert bad_patch.status_code == 400
    assert "当前活动不支持项目类别" in bad_patch.json()["error"]["message"]

    # 5) 管理员移除已选类别后，学生再进页面可看到历史停用提示
    activity.project_categories = ["人工智能赋能专项", "基层治理实践"]
    activity.save(update_fields=["project_categories", "updated_at"])
    legacy_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    assert "绿色双碳调研" in legacy_html
    assert "该类别已停用，请重新选择" in legacy_html

    # 6) 管理员留空保存恢复系统默认 8 项
    client.force_login(admin)
    reset_resp = client.post(
        "/admin-panel/activities/save/",
        {
            "activity_id": str(activity.id),
            "title": activity.title,
            "opens_at": activity.opens_at.strftime("%Y-%m-%dT%H:%M"),
            "deadline": activity.deadline.strftime("%Y-%m-%dT%H:%M"),
            "is_open": "true",
            "project_categories": "   ",
        },
    )
    assert reset_resp.status_code == 302
    activity.refresh_from_db()
    assert activity.project_categories == []
    assert activity.get_project_categories() == list(DEFAULT_PROJECT_CATEGORIES)


def test_activity_manage_api_supports_project_categories(client, activity):
    """活动管理 JSON API 支持读写自定义 project_categories。"""
    admin = User.objects.create_user(
        username="admin_cat_api",
        password="AdminPassword123!",
        real_name="接口管理员",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )
    client.force_login(admin)
    resp = client.post(
        "/api/v1/activities/manage/",
        data=json.dumps(
            {
                "activity_id": str(activity.id),
                "title": activity.title,
                "opens_at": activity.opens_at.isoformat(),
                "deadline": activity.deadline.isoformat(),
                "is_open": True,
                "project_categories": ["产教融合专项", "乡村振兴实践"],
            }
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200
    payload = resp.json()["activity"]
    assert payload["project_categories"] == ["产教融合专项", "乡村振兴实践"]
    activity.refresh_from_db()
    assert activity.project_categories == ["产教融合专项", "乡村振兴实践"]


@pytest.mark.django_db
def test_declaration_and_outcomes_split_end_to_end(client, student, activity):
    """验证假期前立项申报（免审核，仅需项目信息+成员+.docx申报表）与开学后成果收集（需审核）完全分离闭环。"""
    import shutil
    import subprocess
    import tempfile
    from django.core.files.uploadedfile import SimpleUploadedFile
    from apps.uploads.services import direct_upload, ensure_standard_material_requirements
    from tests.conftest import seed_mandatory_materials

    client.force_login(student)
    reqs = ensure_standard_material_requirements(activity)

    # 1) 导航栏并列展示「我的申报」与「成果收集」两个独立栏目
    dash_html = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert "/submissions/apply/" in dash_html
    assert "/submissions/outcomes/" in dash_html
    assert "成果收集" in dash_html

    # 2) 进入「我的申报」保存项目信息 + 团队成员，但尚未传 .docx 申报表 -> 调用 /declare/ 被拒
    apply_resp = client.get(f"/submissions/apply/?activity_id={activity.id}")
    assert apply_resp.status_code == 200
    sub = Submission.objects.get(owner=student, activity=activity)

    patch_resp = client.patch(
        f"/api/v1/submissions/{sub.id}/",
        data=json.dumps(
            {
                "expected_version": sub.version,
                "draft_data": {
                    "project_name": "假期前立项实践队",
                    "category": "社会调查",
                    "advisor_name": "王老师",
                    "practice_place": "江苏省徐州市",
                    "date_start": "2026-07-01",
                    "date_end": "2026-07-10",
                    "declaration_type": "team",
                    "team_members": [
                        {"student_number": "2025099001", "real_name": "张队员", "class_name": "信安25-1班"}
                    ],
                },
            }
        ),
        content_type="application/json",
    )
    assert patch_resp.status_code == 200

    missing_doc_resp = client.post(f"/api/v1/submissions/{sub.id}/declare/")
    assert missing_doc_resp.status_code == 400
    assert "申报表" in missing_doc_resp.json()["error"]["message"]

    # 3) 仅上传 1 份 .docx 申报表（不传报告与现场照片）即可完成项目申报，免审核且不进入审核队列
    docx_file = SimpleUploadedFile(
        "立项申报表.docx",
        b"PK\x03\x04" + b"0" * 128,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    direct_upload(actor=student, requirement_id=reqs["application_form"].id, file_obj=docx_file)

    declare_resp = client.post(f"/api/v1/submissions/{sub.id}/declare/")
    assert declare_resp.status_code == 200
    declare_body = declare_resp.json()
    assert declare_body["declaration_submitted"] is True
    assert declare_body["status"] == Submission.Status.DRAFT

    sub.refresh_from_db()
    assert sub.declaration_submitted is True
    assert sub.declaration_submitted_at is not None
    assert sub.status == Submission.Status.DRAFT

    # 4) 进入「成果收集」页：关联回显立项项目信息与已传 .docx 申报表，且内联脚本可被 node 解析
    outcomes_resp = client.get(f"/submissions/outcomes/?activity_id={activity.id}")
    assert outcomes_resp.status_code == 200
    outcomes_html = outcomes_resp.content.decode("utf-8")
    assert 'data-wizard-mode="outcomes"' in outcomes_html
    assert 'data-declaration-submitted="true"' in outcomes_html
    assert "假期前立项实践队" in outcomes_html
    assert "立项申报表.docx" in outcomes_html
    assert 'id="declaration-required-banner"' not in outcomes_html

    node = shutil.which("node")
    if node:
        scripts = re.findall(
            r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
            outcomes_html,
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

    # 5) 下学期补齐成果材料后在成果收集提交审核 -> 进入 pending_first_review，开放期内仍允许修改覆盖
    seed_mandatory_materials(student, activity)
    submit_resp = client.post(
        f"/api/v1/submissions/{sub.id}/submit/",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert submit_resp.status_code == 200
    sub.refresh_from_db()
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW

    apply_after = client.get(f"/submissions/apply/?activity_id={activity.id}&submission_id={sub.id}").content.decode("utf-8")
    outcomes_after_submit = client.get(f"/submissions/outcomes/?activity_id={activity.id}&submission_id={sub.id}").content.decode("utf-8")
    assert 'data-upload-locked="false"' in apply_after
    assert 'data-upload-locked="false"' in outcomes_after_submit
    assert "更新并重新提交成果审核" in outcomes_after_submit


@pytest.mark.django_db
def test_unconfirmed_declaration_file_hidden_on_outcomes_page(client, student, activity):
    """申报阶段上传的文件未点击确认提交申报前，不得出现在成果提交界面；替换后未重新提交同样隐藏。"""
    from datetime import timedelta
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone
    from apps.uploads.models import UploadSession
    from apps.uploads.services import direct_upload, ensure_standard_material_requirements

    client.force_login(student)
    reqs = ensure_standard_material_requirements(activity)

    # 创建草稿并仅上传 .docx 申报表（不调用 /declare/ 确认提交）
    apply_resp = client.get(f"/submissions/apply/?activity_id={activity.id}")
    assert apply_resp.status_code == 200
    sub = Submission.objects.get(owner=student, activity=activity)

    patch_resp = client.patch(
        f"/api/v1/submissions/{sub.id}/",
        data=json.dumps(
            {
                "expected_version": sub.version,
                "draft_data": {
                    "project_name": "未确认立项项目",
                    "category": "社会调查",
                    "advisor_name": "李老师",
                    "practice_place": "徐州市",
                    "date_start": "2026-07-01",
                    "date_end": "2026-07-05",
                    "declaration_type": "individual",
                    "team_members": [],
                },
            }
        ),
        content_type="application/json",
    )
    assert patch_resp.status_code == 200

    first_doc = SimpleUploadedFile(
        "未提交的初版申报表.docx",
        b"PK\x03\x04" + b"A" * 128,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    first_upload, _, _ = direct_upload(
        actor=student, requirement_id=reqs["application_form"].id, file_obj=first_doc
    )

    # 1) 「我的申报」页仍应回显该草稿文件，便于学生继续确认提交
    apply_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    assert "未提交的初版申报表.docx" in apply_html

    # 2) 「成果收集」页绝不能出现未点击确认提交的申报表
    outcomes_before = client.get(f"/submissions/outcomes/?activity_id={activity.id}").content.decode("utf-8")
    assert "未提交的初版申报表.docx" not in outcomes_before
    assert f"/api/v1/uploads/{first_upload.id}/file/" not in outcomes_before
    assert 'data-declaration-submitted="false"' in outcomes_before
    assert 'id="declaration-required-banner"' in outcomes_before

    # 3) 点击确认提交申报后，成果提交界面正常展示该申报表
    declare_resp = client.post(f"/api/v1/submissions/{sub.id}/declare/")
    assert declare_resp.status_code == 200

    outcomes_after = client.get(f"/submissions/outcomes/?activity_id={activity.id}").content.decode("utf-8")
    assert "未提交的初版申报表.docx" in outcomes_after
    assert 'data-declaration-submitted="true"' in outcomes_after
    assert 'id="declaration-required-banner"' not in outcomes_after

    # 4) 已提交后在申报页替换上传新文件，但未再次点击确认提交 -> 成果页不应展示新文件
    second_doc = SimpleUploadedFile(
        "替换未重新确认的二版申报表.docx",
        b"PK\x03\x04" + b"B" * 128,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    second_upload, _, _ = direct_upload(
        actor=student, requirement_id=reqs["application_form"].id, file_obj=second_doc
    )
    # 确保二版上传时间严格晚于上次确认提交时间（规避同一微秒）
    UploadSession.objects.filter(pk=second_upload.pk).update(
        created_at=timezone.now() + timedelta(seconds=2)
    )

    outcomes_replaced = client.get(f"/submissions/outcomes/?activity_id={activity.id}").content.decode("utf-8")
    assert "替换未重新确认的二版申报表.docx" not in outcomes_replaced
    assert 'data-declaration-submitted="false"' in outcomes_replaced

    # 5) 再次确认提交后（重置时间到当前之前），新申报表正常出现在成果页
    UploadSession.objects.filter(pk=second_upload.pk).update(
        created_at=timezone.now() - timedelta(seconds=1)
    )
    redeclare_resp = client.post(f"/api/v1/submissions/{sub.id}/declare/")
    assert redeclare_resp.status_code == 200

    outcomes_reconfirmed = client.get(f"/submissions/outcomes/?activity_id={activity.id}").content.decode("utf-8")
    assert "替换未重新确认的二版申报表.docx" in outcomes_reconfirmed
    assert 'data-declaration-submitted="true"' in outcomes_reconfirmed


@pytest.mark.django_db
def test_dashboard_and_apply_checklists_structure_and_progress(client, student, activity):
    """验证首页实践完成进度仅 2 项（提交申报/提交成果）、我的申报清单 4 项（含确认提交）、成果收集清单 3 项不变。"""
    import shutil
    import subprocess
    import tempfile
    from django.core.files.uploadedfile import SimpleUploadedFile
    from apps.uploads.services import direct_upload, ensure_standard_material_requirements
    from tests.conftest import seed_mandatory_materials

    client.force_login(student)
    reqs = ensure_standard_material_requirements(activity)

    # 1) 初始状态：首页为实践完成进度 0/2，仅有提交申报与提交成果两项
    dash_0 = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert "实践完成进度" in dash_0
    assert "0/2" in dash_0
    assert 'data-check-key="declaration"' in dash_0
    assert 'data-check-key="outcomes"' in dash_0
    assert "提交申报" in dash_0
    assert "提交成果" in dash_0
    assert "基础信息已保存" not in dash_0
    assert "选交/成员核验就绪" not in dash_0

    # 2) 我的申报页包含第 4 项「确认提交」，初始 0/4，且内联脚本语法正确
    apply_html = client.get(f"/submissions/apply/?activity_id={activity.id}").content.decode("utf-8")
    assert "0/4" in apply_html
    assert 'data-check-key="declaration_submitted"' in apply_html
    assert "确认提交" in apply_html

    node = shutil.which("node")
    if node:
        scripts = re.findall(
            r'<script(?![^>]*\b(?:src|type\s*=\s*["\']application/json["\']))[^>]*>(.*?)</script>',
            apply_html,
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

    # 3) 成果收集页清单保持 3 项不变
    outcomes_html = client.get(f"/submissions/outcomes/?activity_id={activity.id}").content.decode("utf-8")
    assert "0/3" in outcomes_html
    assert "成果检查清单" in outcomes_html

    # 4) 仅保存草稿并上传申报表但未确认提交 -> 首页仍为 0/2
    sub = Submission.objects.get(owner=student, activity=activity)
    client.patch(
        f"/api/v1/submissions/{sub.id}/",
        data=json.dumps(
            {
                "expected_version": sub.version,
                "draft_data": {
                    "project_name": "进度核验测试项目",
                    "category": "社会调查",
                    "advisor_name": "赵老师",
                    "practice_place": "徐州市",
                    "date_start": "2026-07-01",
                    "date_end": "2026-07-08",
                    "declaration_type": "individual",
                    "team_members": [],
                },
            }
        ),
        content_type="application/json",
    )
    direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("进度申报表.docx", b"PK\x03\x04" + b"X" * 128),
    )
    dash_unconfirmed = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert "0/2" in dash_unconfirmed

    # 5) 确认提交申报后 -> 首页变为 1/2
    assert client.post(f"/api/v1/submissions/{sub.id}/declare/").status_code == 200
    dash_1 = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert "1/2" in dash_1

    # 6) 提交成果后 -> 首页变为 2/2
    seed_mandatory_materials(student, activity)
    assert (
        client.post(
            f"/api/v1/submissions/{sub.id}/submit/",
            data=json.dumps({}),
            content_type="application/json",
        ).status_code
        == 200
    )
    dash_2 = client.get(f"/?activity_id={activity.id}").content.decode("utf-8")
    assert "2/2" in dash_2


