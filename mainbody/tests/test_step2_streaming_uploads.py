import os
from datetime import timedelta
import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from config.settings import base as base_settings
from apps.accounts.models import User
from apps.activities.models import Activity
from apps.common.errors import Conflict, DomainError
from apps.reviews.models import ReviewAssignment
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.models import FileAsset, StorageQuota, UploadSession
from apps.uploads.policies import (
    STANDARD_MATERIAL_RULES,
    validate_filename_and_extension,
    validate_magic_bytes,
)
from apps.uploads.services import (
    delete_uploaded_file,
    direct_upload,
    ensure_standard_material_requirements,
    resolve_file_disk_path,
)


# =====================================================================
# 1. 本地磁盘存储与 2C2G 内存保护配置测试
# =====================================================================

def test_media_root_and_memory_protection_settings():
    """验证 MEDIA_ROOT 指向 mainbody/media，且 FILE_UPLOAD_MAX_MEMORY_SIZE = 2621440 (2.5MB)。"""
    assert base_settings.MEDIA_ROOT == base_settings.BASE_DIR / "media"
    assert settings.FILE_UPLOAD_MAX_MEMORY_SIZE == 2621440
    assert "django.core.files.uploadhandler.TemporaryFileUploadHandler" in settings.FILE_UPLOAD_HANDLERS


# =====================================================================
# 2. 五类标准材料规则与双重硬性校验（文件名 + 32字节魔数）测试
# =====================================================================

def test_five_standard_material_rules_configuration(activity):
    """验证五类标准材料规则配置完整且参数准确。"""
    reqs = ensure_standard_material_requirements(activity)
    assert set(reqs.keys()) == {
        "application_form",
        "practice_report",
        "scene_photos",
        "video",
        "other_materials",
    }

    assert reqs["application_form"].required is True
    assert reqs["application_form"].allowed_extensions == [".docx"]
    assert reqs["application_form"].max_file_bytes == 20 * 1024 * 1024
    assert reqs["application_form"].max_count == 1

    assert reqs["practice_report"].required is True
    assert reqs["practice_report"].allowed_extensions == [".pdf"]
    assert reqs["practice_report"].max_file_bytes == 20 * 1024 * 1024
    assert reqs["practice_report"].max_count == 1

    assert reqs["scene_photos"].required is True
    assert reqs["scene_photos"].allowed_extensions == [".jpg", ".jpeg", ".png"]
    assert reqs["scene_photos"].max_file_bytes == 10 * 1024 * 1024
    assert reqs["scene_photos"].min_count == 3
    assert reqs["scene_photos"].max_count == 5

    assert reqs["video"].required is False
    assert reqs["video"].allowed_extensions == [".mp4"]
    assert reqs["video"].max_file_bytes == 300 * 1024 * 1024
    assert reqs["video"].max_count == 1

    assert reqs["other_materials"].required is False
    assert reqs["other_materials"].allowed_extensions == [".pdf", ".jpg", ".jpeg", ".png"]
    assert reqs["other_materials"].max_file_bytes == 20 * 1024 * 1024


def test_filename_and_magic_bytes_double_hard_validation(client, student, activity):
    """验证文件名合法性拦截及伪造扩展名（如把纯文本改成 .pdf 或 .docx）被魔数校验拦截。"""
    reqs = ensure_standard_material_requirements(activity)

    # 1) 路径穿越符、控制字符、超长文件名拦截
    with pytest.raises(DomainError):
        validate_filename_and_extension("../secret.pdf", [".pdf"])
    with pytest.raises(DomainError):
        validate_filename_and_extension("bad\x00name.pdf", [".pdf"])
    with pytest.raises(DomainError):
        validate_filename_and_extension("a" * 256 + ".pdf", [".pdf"])
    with pytest.raises(DomainError):
        validate_filename_and_extension("malware.exe", [".pdf", ".docx"])

    # 2) 伪造扩展名：纯文本改成 .pdf / .docx / .jpg / .png / .mp4 被魔数校验拦截
    with pytest.raises(DomainError, match="PDF"):
        validate_magic_bytes(b"This is plain text pretending to be PDF", ".pdf")
    with pytest.raises(DomainError, match="DOCX"):
        validate_magic_bytes(b"This is plain text pretending to be DOCX", ".docx")
    with pytest.raises(DomainError, match="JPG"):
        validate_magic_bytes(b"Not a jpeg image", ".jpg")
    with pytest.raises(DomainError, match="PNG"):
        validate_magic_bytes(b"Not a png image", ".png")
    with pytest.raises(DomainError, match="MP4"):
        validate_magic_bytes(b"Not an mp4 video header", ".mp4")

    # 3) 真实 MP4 ftyp box 签名通过校验
    valid_mp4_header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    assert validate_magic_bytes(valid_mp4_header, ".mp4") == "video/mp4"

    # 4) 通过 API 上传伪造的 .pdf 和 .docx，确保接口返回 400 且未落盘
    client.force_login(student)
    fake_pdf = SimpleUploadedFile("伪造报告.pdf", b"Hello world, I am a txt file renamed to .pdf")
    resp_pdf = client.post(
        "/api/v1/uploads/direct-upload/",
        {"requirement_id": str(reqs["practice_report"].id), "file": fake_pdf},
    )
    assert resp_pdf.status_code == 400
    assert resp_pdf.json()["error"]["code"] == "invalid_request"

    fake_docx = SimpleUploadedFile("伪造申报表.docx", b"Plain text renamed to .docx")
    resp_docx = client.post(
        "/api/v1/uploads/direct-upload/",
        {"requirement_id": str(reqs["application_form"].id), "file": fake_docx},
    )
    assert resp_docx.status_code == 400
    assert UploadSession.objects.count() == 0


# =====================================================================
# 3. 真实流式上传落盘与重复上传覆盖并物理删除磁盘旧文件测试
# =====================================================================

def test_direct_upload_and_overwrite_physically_deletes_old_file(client, student, activity):
    """验证真实文件上传落盘，且重复上传同类单文件材料时数据库更新并调用 os.remove 物理删除磁盘旧文件。"""
    reqs = ensure_standard_material_requirements(activity)
    client.force_login(student)

    # 第一次上传申报表 v1 (.docx)
    v1_bytes = b"PK\x03\x04" + b"version-1-docx-content-" * 20
    resp1 = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(reqs["application_form"].id),
            "file": SimpleUploadedFile("申报表_初版.docx", v1_bytes),
        },
    )
    assert resp1.status_code == 201
    data1 = resp1.json()
    upload1 = UploadSession.objects.get(pk=data1["upload_id"])
    asset1 = FileAsset.objects.get(upload=upload1)
    old_disk_path = resolve_file_disk_path(upload1.object_key)

    # 验证第一次上传真实落盘且配额准确
    assert os.path.exists(old_disk_path) is True
    assert old_disk_path.read_bytes() == v1_bytes
    assert upload1.status == UploadSession.Status.COMPLETE
    assert asset1.status == FileAsset.Status.VALID
    quota = StorageQuota.objects.get(owner=student, activity=activity)
    assert quota.used_bytes == len(v1_bytes)

    # 第二次重复上传同一材料类别（申报表 v2），触发覆盖清理机制
    v2_bytes = b"PK\x03\x04" + b"version-2-updated-docx-payload-" * 30
    resp2 = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(reqs["application_form"].id),
            "file": SimpleUploadedFile("申报表_终版.docx", v2_bytes),
        },
    )
    assert resp2.status_code == 201
    data2 = resp2.json()
    assert str(upload1.id) in data2["replaced_upload_ids"]

    upload1.refresh_from_db()
    asset1.refresh_from_db()
    upload2 = UploadSession.objects.get(pk=data2["upload_id"])
    asset2 = FileAsset.objects.get(upload=upload2)
    new_disk_path = resolve_file_disk_path(upload2.object_key)

    # 验证旧记录被标记为作废，旧磁盘文件被真实物理删除（os.path.exists 为 False）
    assert upload1.status == UploadSession.Status.ABORTED
    assert asset1.status == FileAsset.Status.INVALID
    assert os.path.exists(old_disk_path) is False

    # 验证新记录有效、新文件在磁盘上存在、且配额释放了旧文件字节数
    assert upload2.status == UploadSession.Status.COMPLETE
    assert asset2.status == FileAsset.Status.VALID
    assert os.path.exists(new_disk_path) is True
    assert new_disk_path.read_bytes() == v2_bytes
    quota.refresh_from_db()
    assert quota.used_bytes == len(v2_bytes)


def test_explicit_delete_attachment_releases_quota_and_removes_disk_file(student, activity):
    """验证学生显式删除某附件时，释放配额并物理删除磁盘文件。"""
    reqs = ensure_standard_material_requirements(activity)
    photo_bytes = b"\x89PNG\r\n\x1a\n" + b"png-image-data" * 10
    upload, asset, _ = direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        file_obj=SimpleUploadedFile("现场图1.png", photo_bytes),
    )
    disk_path = resolve_file_disk_path(upload.object_key)
    assert os.path.exists(disk_path) is True

    delete_uploaded_file(actor=student, upload_id=upload.id)
    upload.refresh_from_db()
    asset.refresh_from_db()
    quota = StorageQuota.objects.get(owner=student, activity=activity)

    assert upload.status == UploadSession.Status.ABORTED
    assert asset.status == FileAsset.Status.INVALID
    assert quota.used_bytes == 0
    assert os.path.exists(disk_path) is False


# =====================================================================
# 4. submit_for_review 必交材料完整性校验测试
# =====================================================================

def test_submit_for_review_enforces_mandatory_materials_completeness(student, activity):
    """验证缺少必交材料（申报表1份、实践报告1份、现场照片3~5张）时无法提交审核。"""
    reqs = ensure_standard_material_requirements(activity)
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(
        actor=student,
        submission_id=draft.pk,
        expected_version=0,
        data={"project_name": "乡村振兴实践调研", "category": "社会调查", "declaration_type": "individual", "team_members": []},
    )

    # 1) 未上传任何材料 -> 拦截并提示缺少申报表
    with pytest.raises(DomainError, match="申报表"):
        submit_for_review(actor=student, submission_id=draft.pk)

    # 2) 仅上传申报表，缺少实践报告 -> 拦截并提示缺少实践报告
    direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("申报表.docx", b"PK\x03\x04" + b"a" * 50),
    )
    with pytest.raises(DomainError, match="实践报告"):
        submit_for_review(actor=student, submission_id=draft.pk)

    # 3) 上传实践报告，但现场照片仅上传 2 张（不足 3 张） -> 拦截并提示现场照片数量不足
    direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("实践报告.pdf", b"%PDF-1.7\n" + b"b" * 50),
    )
    for i in range(2):
        direct_upload(
            actor=student,
            requirement_id=reqs["scene_photos"].id,
            file_obj=SimpleUploadedFile(f"照片_{i}.jpg", b"\xff\xd8\xff\xe0" + b"c" * 50),
        )
    with pytest.raises(DomainError, match="现场照片"):
        submit_for_review(actor=student, submission_id=draft.pk)

    # 4) 补齐第 3 张现场照片 -> 必交材料完整，提交初审成功
    direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        file_obj=SimpleUploadedFile("照片_3.png", b"\x89PNG\r\n\x1a\n" + b"d" * 50),
    )
    sub, rev = submit_for_review(actor=student, submission_id=draft.pk)
    assert sub.status == Submission.Status.PENDING_FIRST_REVIEW
    assert rev.number == 1


# =====================================================================
# 5. 受权限保护的在线预览与下载接口测试 (GET /api/v1/uploads/<id>/file/)
# =====================================================================

def test_file_preview_and_download_permissions_and_headers(client, student, other_student, activity):
    """验证 PDF/图片在线预览响应头 Content-Disposition: inline、Word/视频 attachment 下载及角色权限校验。"""
    reqs = ensure_standard_material_requirements(activity)

    pdf_upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("调研报告.pdf", b"%PDF-1.7\nPDF-BODY-CONTENT"),
    )
    docx_upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["application_form"].id,
        file_obj=SimpleUploadedFile("申报表.docx", b"PK\x03\x04DOCX-BODY-CONTENT"),
    )

    # 1) 本人访问 PDF 并携带 inline=1 -> Content-Disposition: inline
    client.force_login(student)
    resp_inline = client.get(f"/api/v1/uploads/{pdf_upload.id}/file/?inline=1")
    assert resp_inline.status_code == 200
    assert resp_inline["Content-Type"] == "application/pdf"
    assert resp_inline["Content-Disposition"].lower().startswith("inline")
    assert b"".join(resp_inline.streaming_content) == b"%PDF-1.7\nPDF-BODY-CONTENT"

    # 2) 本人访问 PDF 不带 inline=1 -> 默认 Content-Disposition: attachment
    resp_attach = client.get(f"/api/v1/uploads/{pdf_upload.id}/file/")
    assert resp_attach.status_code == 200
    assert resp_attach["Content-Disposition"].lower().startswith("attachment")

    # 3) 本人访问 .docx 即使带 inline=1 也强制为 Content-Disposition: attachment 下载
    resp_docx = client.get(f"/api/v1/uploads/{docx_upload.id}/file/?inline=1")
    assert resp_docx.status_code == 200
    assert resp_docx["Content-Disposition"].lower().startswith("attachment")

    # 4) 其他非本人学生访问 -> 403 Forbidden
    client.force_login(other_student)
    assert client.get(f"/api/v1/uploads/{pdf_upload.id}/file/?inline=1").status_code == 403

    # 5) 该活动的初审人员（负责该学生班级）访问 -> 200 OK
    reviewer = User.objects.create_user(username="rev_step2", password="pwd", role=User.Role.REVIEWER)
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)
    client.force_login(reviewer)
    resp_rev = client.get(f"/api/v1/uploads/{pdf_upload.id}/file/?inline=1")
    assert resp_rev.status_code == 200
    assert resp_rev["Content-Disposition"].lower().startswith("inline")

    # 6) 学院管理员访问 -> 200 OK
    admin_user = User.objects.create_user(username="admin_step2", password="pwd", role=User.Role.ADMIN, is_staff=True)
    client.force_login(admin_user)
    resp_admin = client.get(f"/api/v1/uploads/{pdf_upload.id}/file/?inline=1")
    assert resp_admin.status_code == 200
    assert resp_admin["Content-Disposition"].lower().startswith("inline")
