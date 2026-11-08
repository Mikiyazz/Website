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

    # 步骤条四个可点击步骤
    assert 'id="wizard-steps"' in html
    assert html.count('class="step-item') == 4
    for step in ("项目信息", "团队成员", "附件上传", "确认提交"):
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

    # Step3 五个独立 FilePond 实例
    for code in ("application_form", "practice_report", "scene_photos", "video", "other_materials"):
        assert 'id="pond-' + code + '"' in html
    assert html.count('class="filepond-zone"') == 5

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
    """已提交/已通过的申报在向导中只读锁定，不再允许再次提交。"""
    client.force_login(student)
    client.get("/submissions/apply/")
    sub = Submission.objects.get(owner=student, activity=activity)
    sub.status = Submission.Status.PENDING_FIRST_REVIEW
    sub.save(update_fields=["status", "updated_at"])

    html = client.get("/submissions/apply/").content.decode("utf-8")
    assert 'id="apply-locked-banner"' in html
    assert "申报已锁定" in html
    assert "待初审" in html
    assert "data-can-submit=\"false\"" in html

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
    """页面把「ZIP 最多 5 份」同时写进文案和给 JS 的 JSON 载荷。"""
    client.force_login(student)
    html = client.get("/submissions/apply/").content.decode("utf-8")
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
