"""过期上传回收：purge_stale_uploads 命令 + dispatch_outbox 常驻循环里的顺带清扫。

覆盖的是真实故障场景：学生传 300MB 宣讲视频，60 片传了 50 片就关页面，
从此 media/tmp/<upload_id>/ 与 reserved_bytes 无人回收。
"""
import io
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.submissions.services import create_draft
from apps.uploads import retention
from apps.uploads.models import UploadSession
from apps.uploads.services import (
    ensure_standard_material_requirements,
    resolve_file_disk_path,
)

MB = 1024 * 1024
SMALL_CHUNK = 256 * 1024
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8


def _body(size):
    if size <= len(MP4_HEAD):
        return MP4_HEAD[:size]
    return MP4_HEAD + b"v" * (size - len(MP4_HEAD))


def _start(client, requirement, *, filename, file_size, total_chunks, code=None):
    response = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": code or requirement.code,
            "filename": filename,
            "file_size": str(file_size),
            "total_chunks": str(total_chunks),
            "requirement_id": str(requirement.id),
        },
    )
    assert response.status_code in (200, 201), response.content
    return response.json()


def _part(client, upload_id, index, payload):
    return client.post(
        "/api/v1/uploads/chunk-upload/",
        {
            "upload_id": str(upload_id),
            "chunk_index": str(index),
            "chunk": SimpleUploadedFile("part", payload, content_type="application/octet-stream"),
        },
    )


def _expire(upload_id, hours=1):
    UploadSession.objects.filter(pk=upload_id).update(expires_at=timezone.now() - timedelta(hours=hours))


def _abandoned_upload(client, requirement, *, filename="宣讲视频.mp4", parts=1, hours=1):
    """开一个分片任务、只传一部分，然后把 expires_at 推到过去。"""
    size = SMALL_CHUNK * 3
    started = _start(client, requirement, filename=filename, file_size=size, total_chunks=3)
    for index in range(parts):
        assert _part(client, started["upload_id"], index, _body(SMALL_CHUNK)).status_code == 200
    _expire(started["upload_id"], hours=hours)
    return UploadSession.objects.get(pk=started["upload_id"])


def _run(*args):
    out = io.StringIO()
    call_command("purge_stale_uploads", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


@pytest.mark.django_db
def test_purge_reclaims_staging_dir_and_quota(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)

    upload = _abandoned_upload(client, requirement, parts=1)
    staging = resolve_file_disk_path(f"tmp/{upload.id}")
    assert staging.is_dir()
    assert staging.joinpath("000000.part").is_file()
    quota = upload.quota
    quota.refresh_from_db()
    assert quota.reserved_bytes == SMALL_CHUNK * 3

    output = _run()
    assert "已回收 1 条" in output

    upload.refresh_from_db()
    assert upload.status == UploadSession.Status.ABORTED
    assert not staging.exists()
    quota.refresh_from_db()
    assert quota.reserved_bytes == 0
    assert quota.used_bytes == 0

    log = AuditLog.objects.get(action="upload.purged", target_id=str(upload.id))
    assert log.actor_id is None  # 系统任务没有操作人
    assert log.metadata["reason"] == "expired_upload_session"
    assert log.metadata["released_bytes"] == SMALL_CHUNK * 3
    assert log.metadata["upload_mode"] == UploadSession.Mode.CHUNKED


@pytest.mark.django_db
def test_purge_leaves_unexpired_uploads_alone(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)

    started = _start(client, requirement, filename="还在传.mp4", file_size=SMALL_CHUNK * 2, total_chunks=2)
    assert _part(client, started["upload_id"], 0, _body(SMALL_CHUNK)).status_code == 200

    assert "已回收 0 条" in _run()
    upload = UploadSession.objects.get(pk=started["upload_id"])
    assert upload.status == UploadSession.Status.UPLOADING
    assert resolve_file_disk_path(f"tmp/{upload.id}").is_dir()
    upload.quota.refresh_from_db()
    assert upload.quota.reserved_bytes == SMALL_CHUNK * 2


@pytest.mark.django_db
def test_completed_upload_is_never_purged(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)

    started = _start(client, requirement, filename="已完成.mp4", file_size=SMALL_CHUNK, total_chunks=1)
    assert _part(client, started["upload_id"], 0, _body(SMALL_CHUNK)).status_code == 200
    done = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": started["upload_id"]}).json()
    final_path = resolve_file_disk_path(done["final_path"])
    assert final_path.is_file()

    _expire(started["upload_id"], hours=24 * 400)  # 连"完成一年后"也不动
    assert "已回收 0 条" in _run()

    upload = UploadSession.objects.get(pk=started["upload_id"])
    assert upload.status == UploadSession.Status.COMPLETE
    assert final_path.is_file()
    upload.quota.refresh_from_db()
    assert upload.quota.used_bytes == SMALL_CHUNK


@pytest.mark.django_db
def test_dry_run_reports_without_touching_anything(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)
    upload = _abandoned_upload(client, requirement, parts=1)

    output = _run("--dry-run")
    assert "预演（未改动任何数据） 1 条" in output
    assert "768.0KB" in output  # 按声明的 3 片总量估算，而不是按已落盘的 1 片
    assert "删除分片目录 1 个" in output  # 预演也要说清"将会删掉哪个目录"

    upload.refresh_from_db()
    assert upload.status == UploadSession.Status.UPLOADING
    assert resolve_file_disk_path(f"tmp/{upload.id}").is_dir()
    upload.quota.refresh_from_db()
    assert upload.quota.reserved_bytes == SMALL_CHUNK * 3
    assert not AuditLog.objects.filter(action="upload.purged").exists()


@pytest.mark.django_db
def test_second_run_is_idempotent(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)
    upload = _abandoned_upload(client, requirement)

    assert "已回收 1 条" in _run()
    assert "已回收 0 条" in _run()

    upload.quota.refresh_from_db()
    assert upload.quota.reserved_bytes == 0
    assert AuditLog.objects.filter(action="upload.purged").count() == 1


@pytest.mark.django_db
def test_limit_bounds_one_batch(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = ensure_standard_material_requirements(activity)
    client.force_login(student)

    first = _abandoned_upload(client, reqs["video"], filename="宣讲视频.mp4")
    second = _abandoned_upload(client, reqs["other_materials"], filename="补充材料.zip")

    assert "已回收 1 条" in _run("--limit", "1")
    remaining = UploadSession.objects.filter(
        pk__in=[first.id, second.id], status=UploadSession.Status.ABORTED
    ).count()
    assert remaining == 1
    assert "已回收 1 条" in _run()


@pytest.mark.django_db
def test_grace_hours_defers_the_sweep(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)
    upload = _abandoned_upload(client, requirement, hours=1)

    assert "已回收 0 条" in _run("--grace-hours", "2")
    upload.refresh_from_db()
    assert upload.status == UploadSession.Status.UPLOADING

    assert "已回收 1 条" in _run("--grace-hours", "0")


@pytest.mark.django_db
def test_quota_release_never_goes_negative(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)
    upload = _abandoned_upload(client, requirement)

    quota = upload.quota
    quota.reserved_bytes = 0  # 模拟别处已经把预留清掉
    quota.save(update_fields=["reserved_bytes", "updated_at"])

    assert "已回收 1 条" in _run()
    quota.refresh_from_db()
    assert quota.reserved_bytes == 0
    log = AuditLog.objects.get(action="upload.purged", target_id=str(upload.id))
    assert log.metadata["released_bytes"] == 0


@pytest.mark.django_db
def test_one_broken_row_does_not_stop_the_sweep(
    client, student, activity, isolated_media_root, settings, monkeypatch
):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = ensure_standard_material_requirements(activity)
    client.force_login(student)
    first = _abandoned_upload(client, reqs["video"], filename="宣讲视频.mp4")
    second = _abandoned_upload(client, reqs["other_materials"], filename="补充材料.zip")

    original = retention.remove_chunk_staging_dir

    def flaky(upload):
        if str(upload.pk) == str(first.pk):
            raise OSError("disk busy")
        return original(upload)

    monkeypatch.setattr(retention, "remove_chunk_staging_dir", flaky)

    with pytest.raises(CommandError):
        _run()

    second.refresh_from_db()
    assert second.status == UploadSession.Status.ABORTED
    assert not resolve_file_disk_path(f"tmp/{second.id}").exists()
    # 失败那条的额度也已经释放（数据库是对的），只是磁盘没清干净，需要人工或下轮处理
    first.quota.refresh_from_db()
    assert first.quota.reserved_bytes == 0


@pytest.mark.django_db
def test_dispatch_outbox_reclaims_expired_uploads_on_start(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)
    upload = _abandoned_upload(client, requirement)

    call_command("dispatch_outbox", "--once", stdout=io.StringIO(), stderr=io.StringIO())

    upload.refresh_from_db()
    assert upload.status == UploadSession.Status.ABORTED
    assert not resolve_file_disk_path(f"tmp/{upload.id}").exists()


@pytest.mark.django_db
def test_dispatch_outbox_can_skip_purge(client, student, activity, isolated_media_root, settings):
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)
    upload = _abandoned_upload(client, requirement)

    call_command("dispatch_outbox", "--once", "--no-purge", stdout=io.StringIO(), stderr=io.StringIO())

    upload.refresh_from_db()
    assert upload.status == UploadSession.Status.UPLOADING
    assert resolve_file_disk_path(f"tmp/{upload.id}").is_dir()


@pytest.mark.django_db
def test_purged_session_no_longer_blocks_a_fresh_upload(client, student, activity, isolated_media_root, settings):
    """回收后同一类别应能重新开分片任务，而不是继续撞"已有进行中的上传任务"。"""
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    requirement = ensure_standard_material_requirements(activity)["video"]
    client.force_login(student)

    abandoned = _abandoned_upload(client, requirement)
    _run()
    abandoned.refresh_from_db()

    resumed = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": "video",
            "filename": "宣讲视频.mp4",
            "file_size": str(SMALL_CHUNK * 3),
            "total_chunks": "3",
            "requirement_id": str(requirement.id),
        },
    )
    assert resumed.status_code == 201
    assert resumed.json()["upload_id"] != str(abandoned.id)
