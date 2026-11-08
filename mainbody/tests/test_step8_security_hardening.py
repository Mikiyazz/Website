"""阶段 2 安全加固验收测试（六类攻击向量各一组）。

对应关系
--------
一、文件名消毒        -> test_object_key_*
二、MIME 与魔数双校验 -> test_*magic* / test_*mime* / test_*photo_horse* / test_ffprobe_*
三、越权与归属        -> test_*forbidden* / test_reviewer* / test_student_cannot_*
四、ZipSlip 与炸弹    -> test_zip_*
五、CSRF 与会话       -> test_csrf_*
六、日志与审计        -> test_audit_*
"""
import io
import logging
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.audit.admin import AuditLogAdmin
from apps.audit.models import AuditLog
from apps.common.errors import DomainError
from apps.reviews.models import ReviewAssignment
from apps.submissions.services import create_draft
from apps.uploads import policies
from apps.uploads.archive import inspect_zip_archive, safe_extract_zip
from apps.uploads.models import UploadSession
from apps.uploads.services import (
    direct_upload,
    ensure_standard_material_requirements,
    resolve_file_disk_path,
)
from tests.fixtures.samples import (
    FAKE_DOCX_EXE,
    PHOTO_HORSE_JPEG,
    REAL_JPEG,
    REAL_PNG,
    fixture_path,
)

MB = 1024 * 1024
HEX32 = re.compile(r"^[0-9a-f]{32}$")
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8


def _reqs(activity):
    return ensure_standard_material_requirements(activity)


def _upload(client, requirement, name, payload, *, content_type="text/plain"):
    return client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(requirement.id),
            "file": SimpleUploadedFile(name, payload, content_type=content_type),
        },
    )


def _file_paths(media_root: Path):
    return [path for path in Path(media_root).rglob("*") if path.is_file()]


# =====================================================================
# 一、文件名消毒：original_filename 永不进物理路径
# =====================================================================

@pytest.mark.django_db
def test_object_key_is_uuid_hex_and_ignores_original_filename(client, student, activity, isolated_media_root):
    """物理路径固定 <activity_id>/<学号>/<material_type>/<uuid4().hex><白名单扩展名>。"""
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload(client, reqs["scene_photos"], "现场 照片-决赛!.JPG", REAL_JPEG)
    assert resp.status_code == 201, resp.content[:300]

    upload = UploadSession.objects.get(pk=resp.json()["upload_id"])
    assert upload.original_name == "现场 照片-决赛!.JPG"  # 展示用，原样存 DB
    parts = Path(upload.object_key).parts
    assert parts[0] == str(activity.id)
    assert parts[1] == student.username
    assert parts[2] == "scene_photos"
    stem, dot, ext = parts[3].rpartition(".")
    assert dot == "." and ext == "jpg"  # 白名单反查 + lowercase
    assert HEX32.match(stem)
    # 原始文件名（含中文、空格、感叹号）一个字符都不许出现在物理路径里
    assert "现场" not in upload.object_key and "!" not in upload.object_key
    assert resolve_file_disk_path(upload.object_key).is_file()


@pytest.mark.django_db
def test_object_key_for_chunked_video_uses_promotional_video_directory(student, activity):
    key = policies.build_object_key(
        activity_id=activity.id,
        student_number=student.username,
        material_code="video",
        extension=".MP4",
    )
    assert key == f"{activity.id}/{student.username}/promotional_video/" + key.rsplit("/", 1)[1]
    assert key.endswith(".mp4")
    assert HEX32.match(key.rsplit("/", 1)[1].rsplit(".", 1)[0])


@pytest.mark.parametrize(
    "nasty",
    ["../evil.jpg", "a/b.jpg", "a" + chr(92) + "b.jpg", "bad" + chr(0) + ".jpg", "..jpg"],
)
def test_policy_rejects_dangerous_original_names(nasty):
    """原始文件名一旦带穿越/分隔符/控制字符，在策略层就直接拒绝（不依赖框架兜底）。"""
    with pytest.raises(DomainError, match="路径穿越|控制字符"):
        policies.validate_filename_and_extension(nasty, [".jpg"])


@pytest.mark.django_db
def test_traversal_name_still_lands_on_a_uuid_path(client, student, activity, isolated_media_root):
    """即使框架把 ../evil.jpg 压成 evil.jpg 放行，物理路径也只由 build_object_key 决定。"""
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload(client, reqs["scene_photos"], "../evil.jpg", REAL_JPEG)
    assert resp.status_code == 201
    upload = UploadSession.objects.get(pk=resp.json()["upload_id"])
    expected_dir = Path(isolated_media_root) / str(activity.id) / student.username / "scene_photos"
    disk_path = resolve_file_disk_path(upload.object_key).resolve()
    assert disk_path.parent == expected_dir.resolve()
    assert "evil" not in upload.object_key
    assert HEX32.match(disk_path.stem)


def test_safe_path_segment_collapses_traversal_into_a_single_segment():
    assert policies.safe_path_segment("../../etc/passwd") == "etc_passwd"
    assert policies.safe_path_segment("..\\..\\windows") == "windows"
    assert policies.safe_path_segment("") == "unknown"
    assert "/" not in policies.safe_path_segment("a/b")


# =====================================================================
# 二、MIME 与魔数双校验
# =====================================================================

@pytest.mark.django_db
def test_executable_renamed_to_docx_rejected_by_magic_header(client, student, activity, isolated_media_root):
    """tests/fixtures/fake_docx.exe：MZ 头的可执行文件改名 .docx，必须在魔数层被拒。"""
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload(client, reqs["application_form"], "申报表.docx", FAKE_DOCX_EXE)
    assert resp.status_code == 400
    assert "魔数" in resp.json()["error"]["message"]
    assert UploadSession.objects.count() == 0
    assert _file_paths(isolated_media_root) == []
    assert AuditLog.objects.filter(action="upload.direct.rejected").exists()


@pytest.mark.django_db
def test_declared_mime_contradiction_rejected(client, student, activity, isolated_media_root):
    """声明 image/jpeg 却传 .pdf：MIME 与扩展名不一致，直接拒绝。"""
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload(
        client, reqs["practice_report"], "实践报告.pdf", b"%PDF-1.7\n" + b"0" * 32, content_type="image/jpeg"
    )
    assert resp.status_code == 400
    assert "MIME" in resp.json()["error"]["message"]
    assert _file_paths(isolated_media_root) == []


@pytest.mark.django_db
def test_photo_horse_jpeg_with_php_tail_is_accepted(client, student, activity, isolated_media_root):
    """按运维要求关闭图片马扫描：JPEG 尾部拼 PHP 一句话也照常入库。

    保留的约束只有"必须是一张能真实解码的图片"（SOI/SOF/EOI + 可选 Pillow 解码）；
    图片不会再被要求"恰好以 EOI 结尾"，因此尾巴上挂什么字节都不再触发拒绝。
    对外访问由 Content-Disposition 与受限 inline 白名单兜底，不会被当脚本解释。
    """
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload(client, reqs["scene_photos"], "合影.jpg", PHOTO_HORSE_JPEG)
    assert resp.status_code == 201
    assert UploadSession.objects.get().status == UploadSession.Status.COMPLETE
    assert len(_file_paths(isolated_media_root)) == 1


@pytest.mark.django_db
def test_png_with_trailing_php_is_accepted(client, student, activity, isolated_media_root):
    """PNG 尾部拼 PHP 同样放行：只校验 IHDR/IEND 与 CRC，不解析附加数据。"""
    reqs = _reqs(activity)
    client.force_login(student)

    payload = REAL_PNG + b"<?php system($_GET['c']); ?>"
    resp = _upload(client, reqs["scene_photos"], "现场.png", payload)
    assert resp.status_code == 201
    assert UploadSession.objects.get().status == UploadSession.Status.COMPLETE


@pytest.mark.django_db
def test_png_with_broken_ihdr_crc_rejected_and_truncated_jpeg_rejected(
    client, student, activity, isolated_media_root
):
    reqs = _reqs(activity)
    client.force_login(student)

    broken_png = bytearray(REAL_PNG)
    broken_png[20] ^= 0xFF  # 破坏 IHDR 数据，CRC 立刻对不上
    resp_png = _upload(client, reqs["scene_photos"], "现场.png", bytes(broken_png))
    assert resp_png.status_code == 400
    assert "PNG" in resp_png.json()["error"]["message"]

    truncated = REAL_JPEG[:-2]  # 砍掉 EOI
    resp_jpg = _upload(client, reqs["scene_photos"], "现场.jpg", truncated)
    assert resp_jpg.status_code == 400
    assert "JPEG" in resp_jpg.json()["error"]["message"]

    assert _file_paths(isolated_media_root) == []


def test_declared_mime_allowlist_accepts_real_types_and_octet_stream():
    assert policies.validate_declared_mime("image/jpeg", ".jpg") == "image/jpeg"
    assert policies.validate_declared_mime("application/pdf; charset=binary", ".pdf") == "application/pdf"
    assert policies.validate_declared_mime("", ".png") == ""
    assert policies.validate_declared_mime("application/octet-stream", ".zip") == ""
    with pytest.raises(DomainError, match="MIME"):
        policies.validate_declared_mime("video/mp4", ".pdf")


def test_ffprobe_probe_success_requires_a_video_stream(monkeypatch, settings):
    settings.UPLOAD_FFPROBE_BIN = "ffprobe"  # 测试默认关闭探测，这里显式打开
    monkeypatch.setattr(policies.shutil, "which", lambda name: "/fake/ffprobe")
    monkeypatch.setattr(
        policies.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout=b'{"streams": [{"codec_type": "video"}]}',
            stderr=b"",
        ),
    )
    path = fixture_path("walkthrough.jpg")  # 魔数不看内容，这里只测 probe 分支
    mp4 = Path(str(path) + ".mp4")
    mp4.write_bytes(MP4_HEAD + b"v" * 64)
    try:
        assert policies.verify_video_file(mp4, ".mp4") == "probed"
    finally:
        mp4.unlink(missing_ok=True)


def test_ffprobe_failure_rejects_and_missing_binary_degrades_with_warning(monkeypatch, caplog, settings):
    settings.UPLOAD_FFPROBE_BIN = "ffprobe"  # 测试默认关闭探测，这里显式打开
    path = Path("tmp_probe_sample.mp4")
    path.write_bytes(MP4_HEAD + b"v" * 64)
    try:
        # 1) ffprobe 存在但解析失败 -> 拒绝
        monkeypatch.setattr(policies.shutil, "which", lambda name: "/fake/ffprobe")
        monkeypatch.setattr(
            policies.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"invalid data"),
        )
        with pytest.raises(DomainError, match="ffprobe"):
            policies.verify_video_file(path, ".mp4")

        # 2) ffprobe 解析成功但没有视频流 -> 拒绝
        monkeypatch.setattr(
            policies.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b'{"streams": [{"codec_type": "audio"}]}', stderr=b""),
        )
        with pytest.raises(DomainError, match="视频流"):
            policies.verify_video_file(path, ".mp4")

        # 3) 环境没有 ffprobe -> 降级为扩展名+大小校验，并打 warning
        monkeypatch.setattr(policies.shutil, "which", lambda name: None)
        caplog.set_level(logging.WARNING, logger="apps.uploads.policies")
        assert policies.verify_video_file(path, ".mp4") == "degraded"
        assert any("video_probe_degraded" in record.getMessage() for record in caplog.records)
    finally:
        path.unlink(missing_ok=True)


# =====================================================================
# 三、越权与归属
# =====================================================================

@pytest.mark.django_db
def test_other_student_cannot_read_or_delete_foreign_upload(client, student, other_student, activity):
    reqs = _reqs(activity)
    upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("调研报告.pdf", b"%PDF-1.7\n" + b"0" * 32),
    )

    client.force_login(other_student)
    read = client.get(f"/api/v1/uploads/{upload.id}/file/?inline=1")
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "forbidden"

    delete = client.delete(f"/api/v1/uploads/{upload.id}/delete/")
    assert delete.status_code == 403

    # 两次拒绝都要落审计，且 actor 是越权的那个人
    rows = AuditLog.objects.filter(actor=other_student).order_by("created_at")
    assert [row.action for row in rows] == [
        "upload.file_access.rejected",
        "upload.delete.rejected",
    ]
    assert all(row.target_id == str(upload.id) for row in rows)
    # 目标文件仍在
    assert resolve_file_disk_path(upload.object_key).is_file()


@pytest.mark.django_db
def test_reviewer_and_admin_can_delete_materials_of_their_activity(client, student, activity):
    reqs = _reqs(activity)
    reviewer = User.objects.create_user(username="rev_del", password="pwd", role=User.Role.REVIEWER)
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)
    outsider_reviewer = User.objects.create_user(username="rev_other", password="pwd", role=User.Role.REVIEWER)
    ReviewAssignment.objects.create(reviewer=outsider_reviewer, activity=activity, class_name="别的班级")
    admin_user = User.objects.create_user(username="admin_del", password="pwd", role=User.Role.ADMIN, is_staff=True)

    first, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("报告.pdf", b"%PDF-1.7\n" + b"0" * 20),
    )
    client.force_login(outsider_reviewer)
    assert client.delete(f"/api/v1/uploads/{first.id}/delete/").status_code == 403

    client.force_login(reviewer)
    assert client.delete(f"/api/v1/uploads/{first.id}/delete/").status_code == 200
    first.refresh_from_db()
    assert first.status == UploadSession.Status.ABORTED
    assert AuditLog.objects.filter(action="upload.deleted_by_staff", target_id=str(first.id)).exists()

    second, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("报告v2.pdf", b"%PDF-1.7\n" + b"1" * 20),
    )
    client.force_login(admin_user)
    assert client.delete(f"/api/v1/uploads/{second.id}/delete/").status_code == 200
    assert not resolve_file_disk_path(second.object_key).exists()


@pytest.mark.django_db
def test_student_cannot_reach_admin_and_reviewer_endpoints(client, student, activity):
    client.force_login(student)
    draft = create_draft(actor=student, activity_id=activity.id)

    assert client.get("/admin-panel/").status_code == 403
    assert client.post("/admin-panel/activities/save/", {"title": "越权改名"}).status_code == 403
    assert client.get("/reviews/workbench/").status_code == 403
    assert client.get(f"/reviews/submissions/{draft.id}/").status_code == 403
    assert client.post(f"/reviews/submissions/{draft.id}/action/", {"decision": "approved"}).status_code == 403

    denials = AuditLog.objects.filter(actor=student, action="access.role_denied")
    assert denials.count() >= 4


@pytest.mark.django_db
def test_student_cannot_read_someone_elses_submission(client, student, other_student, activity):
    foreign = create_draft(actor=other_student, activity_id=activity.id)
    client.force_login(student)
    assert client.get(f"/api/v1/submissions/{foreign.id}/").status_code == 404
    assert AuditLog.objects.filter(
        actor=student, action="submission.detail.rejected", target_id=str(foreign.id)
    ).exists()


# =====================================================================
# 四、ZipSlip 与解压炸弹
# =====================================================================

def _upload_zip(client, reqs, name):
    return client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(reqs["other_materials"].id),
            "file": SimpleUploadedFile(name, fixture_path(name).read_bytes(), content_type="application/zip"),
        },
    )


@pytest.mark.django_db
def test_zip_slip_archive_rejected_file_removed_and_audited(client, student, activity, isolated_media_root):
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload_zip(client, reqs, "zipslip.zip")
    assert resp.status_code == 400
    assert "ZipSlip" in resp.json()["error"]["message"]
    assert _file_paths(isolated_media_root) == []
    assert UploadSession.objects.count() == 0
    assert AuditLog.objects.filter(action="upload.direct.rejected", reason__contains="ZipSlip").exists()


@pytest.mark.django_db
def test_zip_bomb_and_entry_count_and_nested_zip_all_rejected(client, student, activity, isolated_media_root):
    reqs = _reqs(activity)
    client.force_login(student)

    bomb = _upload_zip(client, reqs, "bomb.zip")
    assert bomb.status_code == 400
    assert "解压炸弹" in bomb.json()["error"]["message"]

    nested = _upload_zip(client, reqs, "nested.zip")
    assert nested.status_code == 400
    assert "嵌套" in nested.json()["error"]["message"]

    too_many = io.BytesIO()
    with zipfile.ZipFile(too_many, "w") as archive:
        for index in range(201):
            archive.writestr(f"f{index}.txt", b"x")
    resp_many = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(reqs["other_materials"].id),
            "file": SimpleUploadedFile("many.zip", too_many.getvalue(), content_type="application/zip"),
        },
    )
    assert resp_many.status_code == 400
    assert "条目数" in resp_many.json()["error"]["message"]
    assert _file_paths(isolated_media_root) == []


@pytest.mark.django_db
def test_legit_zip_is_accepted_and_safe_extract_stays_inside_root(client, student, activity, isolated_media_root, tmp_path):
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _upload_zip(client, reqs, "material_pack.zip")
    assert resp.status_code == 201, resp.content[:300]
    upload = UploadSession.objects.get(pk=resp.json()["upload_id"])
    assert upload.status
    inspection = inspect_zip_archive(resolve_file_disk_path(upload.object_key))
    assert inspection.entry_count == 2
    assert inspection.total_uncompressed_bytes > 0

    destination = tmp_path / "unpacked"
    destination.mkdir()
    extracted = safe_extract_zip(resolve_file_disk_path(upload.object_key), destination)
    assert len(extracted) == 2
    for path in extracted:
        assert destination.resolve() in path.parents


def test_zip_inspector_rejects_absolute_backslash_and_dotdot_names():
    def build(entry_name):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(entry_name, b"payload")
        return buffer.getvalue()

    for nasty in ("/etc/passwd", "..\\..\\evil.php", "a/../../evil.php", "C:/Windows/evil.php"):
        with pytest.raises(DomainError):
            inspect_zip_archive(io.BytesIO(build(nasty)))


# =====================================================================
# 五、CSRF 与会话（含分片续传）
# =====================================================================

@pytest.mark.django_db
def test_csrf_is_enforced_on_direct_and_chunk_uploads(student, activity):
    from django.test import Client

    reqs = _reqs(activity)
    client = Client(enforce_csrf_checks=True)
    client.force_login(student)

    payload = {"requirement_id": str(reqs["practice_report"].id)}
    blob = SimpleUploadedFile("报告.pdf", b"%PDF-1.7\n" + b"0" * 16)

    # 1) 没有 CSRF token -> 中间件直接 403
    assert client.post("/api/v1/uploads/direct-upload/", {**payload, "file": blob}).status_code == 403

    # 2) 页面带上 token 后，fetch 请求必须带 X-CSRFToken 头才放行
    page = client.get("/submissions/apply/")
    assert page.status_code == 200
    token = client.cookies["csrftoken"].value
    assert token
    ok = client.post(
        "/api/v1/uploads/direct-upload/",
        {**payload, "file": SimpleUploadedFile("报告.pdf", b"%PDF-1.7\n" + b"0" * 16)},
        HTTP_X_CSRFTOKEN=token,
    )
    assert ok.status_code == 201

    # 3) 分片上传三个端点同样受 CSRF 保护
    start_payload = {
        "material_type": "video",
        "filename": "宣讲.mp4",
        "file_size": str(5 * MB),
        "total_chunks": "1",
        "requirement_id": str(reqs["video"].id),
    }
    assert client.post("/api/v1/uploads/chunk-start/", start_payload).status_code == 403
    started = client.post("/api/v1/uploads/chunk-start/", start_payload, HTTP_X_CSRFTOKEN=token)
    assert started.status_code == 201
    upload_id = started.json()["upload_id"]

    part = {"upload_id": upload_id, "chunk_index": "0"}
    chunk = SimpleUploadedFile("blob", MP4_HEAD + b"v" * 64)
    assert (
        client.post("/api/v1/uploads/chunk-upload/", {**part, "chunk": chunk}).status_code == 403
    )

    # 4) 断网重拖续传（重发同一片）依旧要过 CSRF
    first = client.post(
        "/api/v1/uploads/chunk-upload/",
        {**part, "chunk": SimpleUploadedFile("blob", MP4_HEAD + b"v" * 64)},
        HTTP_X_CSRFTOKEN=token,
    )
    assert first.status_code == 200
    replay_without_token = client.post(
        "/api/v1/uploads/chunk-upload/",
        {**part, "chunk": SimpleUploadedFile("blob", MP4_HEAD + b"v" * 64)},
    )
    assert replay_without_token.status_code == 403


# =====================================================================
# 六、日志与审计
# =====================================================================

@pytest.mark.django_db
def test_rejection_audit_row_carries_user_target_ip_reason_time(client, student, other_student, activity):
    reqs = _reqs(activity)
    upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("报告.pdf", b"%PDF-1.7\n" + b"0" * 16),
    )
    client.force_login(other_student)
    assert client.get(f"/api/v1/uploads/{upload.id}/file/").status_code == 403

    row = AuditLog.objects.get(action="upload.file_access.rejected")
    assert row.actor_id == other_student.id
    assert row.target_id == str(upload.id)
    assert row.ip == "127.0.0.1"
    assert row.reason
    assert row.created_at is not None


@pytest.mark.django_db
def test_oversize_and_magic_rejections_are_audited_with_reason(client, student, activity):
    reqs = _reqs(activity)
    client.force_login(student)

    oversize = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": "video",
            "filename": "超大视频.mp4",
            "file_size": str(301 * MB),
            "total_chunks": "61",
            "requirement_id": str(reqs["video"].id),
        },
    )
    assert oversize.status_code == 400
    row = AuditLog.objects.get(action="upload.chunk_start.rejected")
    assert "300MB" in row.reason
    assert row.ip == "127.0.0.1"

    bad_magic = _upload(client, reqs["application_form"], "申报表.docx", FAKE_DOCX_EXE)
    assert bad_magic.status_code == 400
    assert AuditLog.objects.filter(action="upload.direct.rejected", reason__contains="魔数").exists()


@pytest.mark.django_db
def test_admin_can_list_and_filter_audit_records(rf, client, db):
    staff = User.objects.create_user(
        username="audit_admin", password="pwd", role=User.Role.ADMIN, is_staff=True, is_superuser=True
    )
    AuditLog.objects.create(
        actor=staff, action="upload.direct.rejected", target_type="raw", target_id="abc",
        ip="127.0.0.1", reason="文件头魔数校验失败", metadata={"code": "invalid_request"},
    )
    AuditLog.objects.create(
        actor=None, action="upload.file_access.rejected", target_type="raw", target_id="def",
        ip="10.0.0.9", reason="无权访问此文件", metadata={},
    )

    admin_view = AuditLogAdmin(AuditLog, __import__("django.contrib.admin.sites", fromlist=["AdminSite"]).AdminSite())
    assert "action" in admin_view.list_filter
    assert "ip" in admin_view.list_display and "reason" in admin_view.list_display

    client.force_login(staff)
    listing = client.get("/admin/audit/auditlog/")
    assert listing.status_code == 200

    filtered = client.get("/admin/audit/auditlog/", {"action": "upload.direct.rejected"})
    assert filtered.status_code == 200
    body = filtered.content.decode("utf-8")
    assert "文件头魔数校验失败" in body
    assert "无权访问此文件" not in body

    student = User.objects.create_user(username="audit_student", password="pwd")
    client.force_login(student)
    assert client.get("/admin/audit/auditlog/").status_code == 302
