"""分片上传（300MB 宣讲视频）：chunk-start / chunk-upload / chunk-complete。

验收对应关系：
- 300MB 文件完整落盘 -> test_300mb_promotional_video_completes_under_real_chunk_size
- 断点续传            -> test_chunk_start_resumes_after_browser_reconnect
- 最终目录结构        -> test_chunk_complete_writes_promotional_video_path
- 单片 5MB 立即返回   -> test_chunk_upload_rejects_oversized_part / 直接断言响应体与落盘大小
"""
import hashlib
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.submissions.models import Submission
from apps.submissions.services import create_draft
from apps.uploads import chunks as chunk_services
from apps.uploads.models import FileAsset, StorageQuota, UploadSession
from apps.uploads.services import (
    direct_upload,
    ensure_standard_material_requirements,
    resolve_file_disk_path,
)

MB = 1024 * 1024
SMALL_CHUNK = 256 * 1024  # 测试用小分片，逻辑与 5MB 完全一致
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8


def _reqs(activity):
    return ensure_standard_material_requirements(activity)


def _start(client, requirement, filename, file_size, total_chunks, material_type="video"):
    return client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": material_type,
            "filename": filename,
            "file_size": str(file_size),
            "total_chunks": str(total_chunks),
            "requirement_id": str(requirement.id),
        },
    )


def _upload_part(client, upload_id, index, payload):
    return client.post(
        "/api/v1/uploads/chunk-upload/",
        {
            "upload_id": str(upload_id),
            "chunk_index": str(index),
            "chunk": SimpleUploadedFile("blob", payload, content_type="application/octet-stream"),
        },
    )


def _video_bytes(size):
    """A body of exactly size bytes that starts with a valid MP4 ftyp box."""
    head = MP4_HEAD
    if size <= len(head):
        return head[:size]
    return head + b"v" * (size - len(head))


# =====================================================================
# 1. chunk_start：服务端权威校验
# =====================================================================

@pytest.mark.django_db
def test_chunk_start_creates_uploading_session_and_returns_chunk_size(client, student, activity):
    reqs = _reqs(activity)
    client.force_login(student)

    resp = _start(client, reqs["video"], "宣讲视频.mp4", 3 * 5 * MB, 3)
    assert resp.status_code == 201
    data = resp.json()
    assert data["chunk_size"] == 5 * MB  # 服务端下发为准
    assert data["total_chunks"] == 3
    assert data["resumed"] is False
    assert data["received_chunks"] == []

    upload = UploadSession.objects.get(pk=data["upload_id"])
    assert upload.status == UploadSession.Status.UPLOADING
    assert upload.upload_mode == UploadSession.Mode.CHUNKED
    assert upload.requirement.code == "video"
    assert upload.original_name == "宣讲视频.mp4"
    assert upload.declared_bytes == 3 * 5 * MB
    assert upload.total_chunks == 3
    assert upload.object_key == "tmp/" + str(upload.id)
    assert chunk_services.chunk_tmp_dir(upload).is_dir()

    quota = StorageQuota.objects.get(owner=student, activity=activity)
    assert quota.reserved_bytes == 3 * 5 * MB
    assert quota.used_bytes == 0


@pytest.mark.django_db
def test_chunk_start_rejects_illegal_material_type_filename_and_size(client, student, activity):
    reqs = _reqs(activity)
    client.force_login(student)

    bad_type = _start(client, reqs["video"], "宣讲视频.mp4", MB, 1, material_type="unknown_material")
    assert bad_type.status_code == 400
    assert bad_type.json()["error"]["code"] == "invalid_request"

    # 扩展名不在 video 白名单里
    bad_ext = _start(client, reqs["video"], "宣讲视频.exe", MB, 1)
    assert bad_ext.status_code == 400

    # 路径穿越文件名
    traversal = _start(client, reqs["video"], "../evil.mp4", MB, 1)
    assert traversal.status_code == 400

    # 超过 300MB 类别上限（不信任前端声明的 file_size）
    oversize = _start(client, reqs["video"], "宣讲视频.mp4", 300 * MB + 1, 61)
    assert oversize.status_code == 400
    assert "300MB" in oversize.json()["error"]["message"]

    # total_chunks 与 file_size/chunk_size 不自洽
    mismatch = _start(client, reqs["video"], "宣讲视频.mp4", 3 * 5 * MB, 2)
    assert mismatch.status_code == 400
    assert "分片数量" in mismatch.json()["error"]["message"]

    assert UploadSession.objects.count() == 0


@pytest.mark.django_db
def test_chunk_start_requires_student_role(client, activity):
    reqs = _reqs(activity)
    assert _start(client, reqs["video"], "宣讲视频.mp4", MB, 1).status_code == 401

    guest = User.objects.create_user(username="rev_chunk", password="pwd", role="external_guest")
    client.force_login(guest)
    assert _start(client, reqs["video"], "宣讲视频.mp4", MB, 1).status_code == 403


@pytest.mark.django_db
def test_chunk_start_blocked_after_submission_locked(client, student, activity):
    reqs = _reqs(activity)
    submission = create_draft(actor=student, activity_id=activity.id)
    submission.status = Submission.Status.PENDING_FIRST_REVIEW
    submission.save(update_fields=["status", "updated_at"])

    client.force_login(student)
    resp = _start(client, reqs["video"], "宣讲视频.mp4", MB, 1)
    assert resp.status_code == 409


# =====================================================================
# 2. chunk_upload：单片落盘、幂等、越权
# =====================================================================

@pytest.mark.django_db
def test_chunk_upload_writes_part_and_is_idempotent(client, student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)

    started = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK * 2, 2).json()
    upload_id = started["upload_id"]
    assert started["chunk_size"] == SMALL_CHUNK

    first = _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK))
    assert first.status_code == 200
    assert first.json() == {
        "received": True,
        "upload_id": str(upload_id),
        "chunk_index": 0,
        "skipped": False,
    }
    upload = UploadSession.objects.get(pk=upload_id)
    part = chunk_services.chunk_tmp_dir(upload) / "000000.part"
    assert part.is_file() and part.stat().st_size == SMALL_CHUNK

    # 断线重连后 FilePond 会重发同一片：已存在的同长分片直接 200，不重复写盘
    mtime = part.stat().st_mtime_ns
    again = _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK))
    assert again.status_code == 200
    assert again.json()["skipped"] is True
    assert part.stat().st_mtime_ns == mtime

    # 续传提示：chunk_start 再跑一次能看到已收到的分片
    resumed = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK * 2, 2).json()
    assert resumed["resumed"] is True
    assert resumed["received_chunks"] == [0]


@pytest.mark.django_db
def test_chunk_upload_rejects_out_of_range_index_and_oversized_part(client, student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)
    upload_id = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK * 2, 2).json()["upload_id"]

    assert _upload_part(client, upload_id, 2, _video_bytes(SMALL_CHUNK)).status_code == 400
    assert _upload_part(client, upload_id, -1, _video_bytes(SMALL_CHUNK)).status_code == 400
    oversized = _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK + 1))
    assert oversized.status_code == 400
    assert "不得超过" in oversized.json()["error"]["message"]
    assert _upload_part(client, upload_id, "abc", _video_bytes(SMALL_CHUNK)).status_code == 400


@pytest.mark.django_db
def test_chunk_upload_rejects_other_users_upload_with_403(client, student, other_student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)
    upload_id = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK, 1).json()["upload_id"]

    client.force_login(other_student)
    forbidden = _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK))
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    # 非本人的 chunk_complete 同样 403
    completed = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id})
    assert completed.status_code == 403

    # 匿名访问先被拦在认证层
    client.logout()
    assert _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK)).status_code == 401


# =====================================================================
# 3. chunk_complete：按序合并、目录结构、清理与配额
# =====================================================================

@pytest.mark.django_db
def test_chunk_complete_writes_promotional_video_path(client, student, activity, settings, isolated_media_root):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)

    size = SMALL_CHUNK * 3
    upload_id = _start(client, reqs["video"], "宣讲视频.mp4", size, 3).json()["upload_id"]

    payloads = [_video_bytes(SMALL_CHUNK), b"A" * SMALL_CHUNK, b"B" * SMALL_CHUNK]
    # 故意乱序上传，验证合并严格按 chunk_index 升序
    for index in (2, 0, 1):
        assert _upload_part(client, upload_id, index, payloads[index]).status_code == 200

    resp = client.post(
        "/api/v1/uploads/chunk-complete/",
        {"upload_id": upload_id, "material_type": "video"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "uploaded"
    assert data["material_type"] == "video"
    assert data["actual_bytes"] == size

    final_path = data["final_path"]
    relative = Path(final_path)
    assert relative.parts[0] == str(activity.id)
    assert relative.parts[1] == student.username
    assert relative.parts[2] == "promotional_video"
    assert relative.suffix == ".mp4"
    assert len(relative.stem) == 32 and int(relative.stem, 16) >= 0  # uuid4().hex，不含原始文件名
    assert "宣讲视频" not in final_path

    disk_path = resolve_file_disk_path(final_path)
    assert disk_path.is_file()
    assert disk_path.parent == (Path(isolated_media_root) / str(activity.id) / student.username / "promotional_video")
    assert disk_path.read_bytes() == b"".join(payloads)
    assert hashlib.sha256(disk_path.read_bytes()).hexdigest() == data["checksum_value"]

    # tmp/<upload_id>/ 整目录删除
    assert not chunk_services.chunk_tmp_dir(UploadSession.objects.get(pk=upload_id)).exists()

    upload = UploadSession.objects.get(pk=upload_id)
    assert upload.status == UploadSession.Status.COMPLETE
    assert upload.object_key == final_path
    assert upload.final_path == final_path
    asset = FileAsset.objects.get(upload=upload)
    assert asset.status == FileAsset.Status.VALID
    assert asset.actual_bytes == size

    quota = StorageQuota.objects.get(owner=student, activity=activity)
    assert quota.used_bytes == size
    assert quota.reserved_bytes == 0


@pytest.mark.django_db
def test_chunk_start_resumes_after_browser_reconnect(client, student, activity, settings):
    """模拟浏览器中途关闭：重新 chunk_start 复用同一 upload_id，已上传分片被跳过。"""
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)

    size = SMALL_CHUNK * 4
    first = _start(client, reqs["video"], "宣讲视频.mp4", size, 4).json()
    upload_id = first["upload_id"]
    assert _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK)).status_code == 200
    assert _upload_part(client, upload_id, 1, b"X" * SMALL_CHUNK).status_code == 200

    # 断线重连：FilePond 重新调用 chunkStart
    resumed = _start(client, reqs["video"], "宣讲视频.mp4", size, 4)
    assert resumed.status_code == 200
    assert resumed.json()["upload_id"] == upload_id
    assert resumed.json()["resumed"] is True
    assert resumed.json()["received_chunks"] == [0, 1]
    assert UploadSession.objects.filter(owner=student).count() == 1

    # 前端跳过 0/1，只补 2/3
    for index in (2, 3):
        assert _upload_part(client, upload_id, index, b"Y" * SMALL_CHUNK).status_code == 200

    done = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id}).json()
    merged = resolve_file_disk_path(done["final_path"]).read_bytes()
    assert len(merged) == size
    assert merged[:SMALL_CHUNK] == _video_bytes(SMALL_CHUNK)
    assert merged[SMALL_CHUNK:2 * SMALL_CHUNK] == b"X" * SMALL_CHUNK


@pytest.mark.django_db
def test_chunk_complete_rejects_incomplete_or_mismatched_parts(client, student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)

    upload_id = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK * 3, 3).json()["upload_id"]
    assert _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK)).status_code == 200

    incomplete = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id})
    assert incomplete.status_code == 400
    assert "分片不完整" in incomplete.json()["error"]["message"]

    # 补齐但每片都严重偏短：总大小与声明相差近 2 片，超出 ±1 片容差
    for index in (1, 2):
        assert _upload_part(client, upload_id, index, b"Z" * 1024).status_code == 200
    mismatched = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id})
    assert mismatched.status_code == 400
    assert "不一致" in mismatched.json()["error"]["message"]

    upload = UploadSession.objects.get(pk=upload_id)
    assert upload.status == UploadSession.Status.UPLOADING
    assert chunk_services.chunk_tmp_dir(upload).is_dir()  # 失败不清分片，允许重试


@pytest.mark.django_db
def test_chunk_complete_rejects_forged_magic_bytes(client, student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)

    upload_id = _start(client, reqs["video"], "假视频.mp4", SMALL_CHUNK, 1).json()["upload_id"]
    assert _upload_part(client, upload_id, 0, b"plain text pretending to be a video" * 10).status_code == 200

    resp = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id})
    assert resp.status_code == 400
    assert "魔数" in resp.json()["error"]["message"]
    assert UploadSession.objects.get(pk=upload_id).status == UploadSession.Status.UPLOADING


@pytest.mark.django_db
def test_chunk_complete_replaces_previous_video_and_releases_quota(
    client, student, activity, settings, isolated_media_root
):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    old_upload, _, _ = direct_upload(
        actor=student,
        requirement_id=reqs["video"].id,
        file_obj=SimpleUploadedFile("旧宣讲视频.mp4", _video_bytes(SMALL_CHUNK)),
    )
    old_path = resolve_file_disk_path(old_upload.object_key)
    assert old_path.is_file()

    client.force_login(student)
    upload_id = _start(client, reqs["video"], "新宣讲视频.mp4", SMALL_CHUNK, 1).json()["upload_id"]
    assert _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK)).status_code == 200
    done = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id}).json()

    old_upload.refresh_from_db()
    assert old_upload.status == UploadSession.Status.ABORTED
    assert str(old_upload.id) in done["replaced_upload_ids"]
    assert not old_path.exists()  # 旧物理文件已删

    quota = StorageQuota.objects.get(owner=student, activity=activity)
    assert quota.used_bytes == SMALL_CHUNK
    assert quota.reserved_bytes == 0


@pytest.mark.django_db
def test_chunk_complete_is_idempotent(client, student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)

    upload_id = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK, 1).json()["upload_id"]
    assert _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK)).status_code == 200
    first = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id})
    second = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["final_path"] == first.json()["final_path"]
    assert UploadSession.objects.filter(owner=student).count() == 1
    assert FileAsset.objects.count() == 1


# =====================================================================
# 4. 真实 300MB 宣讲视频：默认 5MB 分片、60 片完整落盘
# =====================================================================

@pytest.mark.django_db
def test_300mb_promotional_video_completes_under_real_chunk_size(client, student, activity, isolated_media_root):
    chunk = 5 * MB
    total = 60
    size = chunk * total  # 300MB

    reqs = _reqs(activity)
    client.force_login(student)

    started = _start(client, reqs["video"], "300MB宣讲视频.mp4", size, total)
    assert started.status_code == 201
    assert started.json()["chunk_size"] == chunk
    upload_id = started.json()["upload_id"]

    filler = b"\x00" * chunk
    for index in range(total):
        payload = _video_bytes(chunk) if index == 0 else filler
        part = _upload_part(client, upload_id, index, payload)
        assert part.status_code == 200, part.content[:200]
        assert part.json()["chunk_index"] == index

    done = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_id, "material_type": "video"})
    assert done.status_code == 200, done.content[:200]
    data = done.json()
    assert data["status"] == "uploaded"
    assert data["actual_bytes"] == size

    disk_path = resolve_file_disk_path(data["final_path"])
    assert disk_path.is_file()
    assert disk_path.stat().st_size == size
    assert disk_path.parent.name == "promotional_video"
    assert disk_path.parent.parent.name == student.username
    assert disk_path.parent.parent.parent.name == str(activity.id)
    with open(disk_path, "rb") as handle:
        assert handle.read(len(MP4_HEAD)) == MP4_HEAD

    upload = UploadSession.objects.get(pk=upload_id)
    assert upload.status == UploadSession.Status.COMPLETE
    assert not chunk_services.chunk_tmp_dir(upload).exists()
    assert StorageQuota.objects.get(owner=student, activity=activity).used_bytes == size

# =====================================================================
# 5. 前端接线：宣讲视频走分片，其余四类保持直传；顶部 Tabler 进度条
# =====================================================================

@pytest.mark.django_db
def test_apply_page_exposes_chunk_endpoints_and_progress_bar(client, student, activity):
    client.force_login(student)
    html = client.get("/submissions/apply/").content.decode("utf-8")

    assert 'data-chunk-start-url="/api/v1/uploads/chunk-start/"' in html
    assert 'data-chunk-upload-url="/api/v1/uploads/chunk-upload/"' in html
    assert 'data-chunk-complete-url="/api/v1/uploads/chunk-complete/"' in html
    assert 'data-direct-upload-url="/api/v1/uploads/direct-upload/"' in html
    assert 'data-upload-locked="false"' in html

    # Tabler 进度条组件（页面顶部，上传中常显）
    assert 'id="upload-progress-bar"' in html
    assert 'class="progress progress-sm"' in html
    assert 'id="upload-progress-fill"' in html
    assert 'id="upload-progress-percent"' in html
    assert "断点续传" in html

    # 旧骨架提示不再出现
    assert "下一次迭代接入" not in html

    # FilePond 五个实例仍然都在（本任务只换 video 的上传通道）
    assert html.count('class="filepond-zone"') == 5


def test_wizard_js_splits_video_into_5mb_chunks_and_guards_unload():
    source = (Path(settings.BASE_DIR) / "static" / "js" / "apply-wizard.js").read_text(encoding="utf-8")

    assert "var CHUNK_SIZE = 5 * 1024 * 1024;" in source
    assert "CHUNK_START_URL" in source
    assert "CHUNK_UPLOAD_URL" in source
    assert "CHUNK_COMPLETE_URL" in source
    assert "file.slice(start, end)" in source  # 浏览器原生 Blob 分片
    assert "new FormData()" in source
    assert "beforeunload" in source  # 上传中勿关闭页面
    assert "makeUploadProcess" in source
    assert "received[current]" in source  # 断点续传时跳过服务端已有的分片
    assert "instantUpload: !UPLOAD_LOCKED" in source


@pytest.mark.django_db
def test_locked_application_disables_upload_zones(client, student, activity):
    client.force_login(student)
    submission = create_draft(actor=student, activity_id=activity.id)
    submission.status = Submission.Status.PENDING_FIRST_REVIEW
    submission.save(update_fields=["status", "updated_at"])

    html = client.get("/submissions/apply/").content.decode("utf-8")
    assert 'data-upload-locked="true"' in html


# =====================================================================
# 6. 错误响应格式与 validation 回调
# =====================================================================

@pytest.mark.django_db
def test_chunk_errors_follow_domain_error_envelope(client, other_student, student, activity, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = _reqs(activity)
    client.force_login(student)
    upload_id = _start(client, reqs["video"], "宣讲视频.mp4", SMALL_CHUNK, 1).json()["upload_id"]

    client.force_login(other_student)
    body = _upload_part(client, upload_id, 0, _video_bytes(SMALL_CHUNK)).json()
    assert set(body) == {"error", "request_id"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == "forbidden"

    client.force_login(student)
    bad = client.post(
        "/api/v1/uploads/chunk-start/",
        {"material_type": "video", "filename": "x.mp4", "file_size": "abc", "total_chunks": "1"},
    ).json()
    assert bad["error"]["code"] == "invalid_request"
    assert "file_size" in bad["error"]["message"]


@pytest.mark.django_db
def test_validation_hook_is_a_safe_noop_until_a_task_exists():
    assert chunk_services._schedule_validation(FileAsset()) is False
