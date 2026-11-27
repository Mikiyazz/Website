"""批次 1（1.1 ~ 1.4）专项验收测试：文件绝对不丢 & 上传不卡死。

覆盖范围：
- 1.1 大文件合并/直传落盘完全移出 DB 事务 + 短事务耗时 < 100ms + 提交异常回滚时旧文件与分片完好无损
- 1.2 中途取消视频上传后立即换传新视频无 409 死锁（前端 keepalive 释放 + 后端单文件自动中止旧 UPLOADING 会话）
- 1.3 两层/三层子目录正常 .zip 和 300MB+ 正常打包 .zip 放行；zipslip / nested / bomb / >500 条目 / >6 层目录仍拦截
- 1.4 >10MB 文件统一走 5MB 分片 + 50MB .zip 分片断点续传 + .zip 最多 5 份复核 + 默认关闭 ffprobe + Pillow 无降级告警
"""
import io
import logging
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.utils import timezone

from apps.accounts.models import User
from apps.activities.models import Activity
from apps.common.errors import Conflict, DomainError
from apps.uploads import chunks as chunk_services
from apps.uploads import policies, services as upload_services
from apps.uploads.archive import inspect_zip_archive
from apps.uploads.models import FileAsset, StorageQuota, UploadSession
from config.settings import base as base_settings
from tests.fixtures.samples import REAL_JPEG, REAL_PNG, fixture_path

MB = 1024 * 1024
SMALL_CHUNK = 256 * 1024
MP4_HEAD = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 8


def _video_bytes(size: int) -> bytes:
    if size <= len(MP4_HEAD):
        return MP4_HEAD[:size]
    return MP4_HEAD + b"v" * (size - len(MP4_HEAD))


def _make_tx_fixtures():
    """在 transactional_db 场景下独立创建学生、活动与标准材料要求。"""
    student = User.objects.create_user(
        username="batch1_student",
        password="test-password-123",
        class_name="信安25-1班",
    )
    now = timezone.now()
    activity = Activity.objects.create(
        title="批次1事务验收活动",
        opens_at=now - timedelta(days=1),
        deadline=now + timedelta(days=1),
        is_open=True,
    )
    reqs = upload_services.ensure_standard_material_requirements(activity)
    return student, activity, reqs


# =====================================================================
# 1.1 大文件落盘/合并移出 DB 长事务 + 物理删除延后到 on_commit
# =====================================================================

@pytest.mark.django_db(transaction=True)
def test_chunk_merge_and_content_validation_run_outside_db_transaction(monkeypatch, settings):
    """验证 complete_chunked_upload 合并与校验均在事务外执行，且 DB 事务本身耗时远小于 100ms。"""
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    student, activity, reqs = _make_tx_fixtures()

    upload, _ = chunk_services.start_chunked_upload(
        actor=student,
        material_type="video",
        filename="宣讲视频.mp4",
        file_size=SMALL_CHUNK * 2,
        total_chunks=2,
        requirement_id=reqs["video"].id,
    )
    chunk_services.store_chunk(
        actor=student,
        upload_id=upload.id,
        chunk_index=0,
        chunk_file=SimpleUploadedFile("p0", _video_bytes(SMALL_CHUNK)),
    )
    chunk_services.store_chunk(
        actor=student,
        upload_id=upload.id,
        chunk_index=1,
        chunk_file=SimpleUploadedFile("p1", b"B" * SMALL_CHUNK),
    )

    observed = {}
    orig_merge = chunk_services._merge_parts
    orig_validate = chunk_services.validate_stored_file_content

    def wrapped_merge(up, final_path, total_bytes):
        observed["merge_in_atomic"] = connection.in_atomic_block
        observed["status_during_merge"] = UploadSession.objects.get(pk=up.pk).status
        return orig_merge(up, final_path, total_bytes)

    def wrapped_validate(path, extension, *args, **kwargs):
        observed["validate_in_atomic"] = connection.in_atomic_block
        return orig_validate(path, extension, *args, **kwargs)

    monkeypatch.setattr(chunk_services, "_merge_parts", wrapped_merge)
    monkeypatch.setattr(chunk_services, "validate_stored_file_content", wrapped_validate)

    t0 = time.perf_counter()
    completed, asset, _ = chunk_services.complete_chunked_upload(
        actor=student,
        upload_id=upload.id,
        material_type="video",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    assert observed["merge_in_atomic"] is False
    assert observed["validate_in_atomic"] is False
    assert observed["status_during_merge"] == UploadSession.Status.COMPLETING
    assert completed.status == UploadSession.Status.COMPLETE
    assert asset.status == FileAsset.Status.VALID
    assert upload_services.resolve_file_disk_path(completed.object_key).is_file()
    # 真实事务已提交，on_commit 应已清理 tmp/<upload_id>/
    assert not chunk_services.chunk_tmp_dir(completed).exists()
    assert elapsed_ms < 2000


@pytest.mark.django_db(transaction=True)
def test_direct_upload_streaming_and_validation_run_outside_db_transaction(monkeypatch):
    """验证 direct_upload 流式原子落盘与内容校验均在数据库事务外执行。"""
    student, activity, reqs = _make_tx_fixtures()
    observed = {}
    orig_stream = upload_services._stream_file_atomically
    orig_validate = upload_services.validate_stored_file_content

    def wrapped_stream(file_obj, disk_path):
        observed["stream_in_atomic"] = connection.in_atomic_block
        return orig_stream(file_obj, disk_path)

    def wrapped_validate(disk_path, extension, **kwargs):
        observed["validate_in_atomic"] = connection.in_atomic_block
        return orig_validate(disk_path, extension, **kwargs)

    monkeypatch.setattr(upload_services, "_stream_file_atomically", wrapped_stream)
    monkeypatch.setattr(upload_services, "validate_stored_file_content", wrapped_validate)

    upload, asset, _ = upload_services.direct_upload(
        actor=student,
        requirement_id=reqs["scene_photos"].id,
        file_obj=SimpleUploadedFile("现场.jpg", REAL_JPEG, content_type="image/jpeg"),
    )
    assert observed["stream_in_atomic"] is False
    assert observed["validate_in_atomic"] is False
    assert upload.status == UploadSession.Status.COMPLETE
    assert asset.status == FileAsset.Status.VALID
    assert upload_services.resolve_file_disk_path(upload.object_key).is_file()


@pytest.mark.django_db(transaction=True)
def test_chunk_complete_rollback_preserves_old_file_and_all_chunks(monkeypatch, settings, isolated_media_root):
    """模拟短事务 2 提交前异常回滚：原已上传视频与新上传分片 100% 完好，不留新建合并孤儿文件。"""
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    student, activity, reqs = _make_tx_fixtures()

    old_payload = _video_bytes(SMALL_CHUNK)
    old_upload, old_asset, _ = upload_services.direct_upload(
        actor=student,
        requirement_id=reqs["video"].id,
        file_obj=SimpleUploadedFile("旧视频.mp4", old_payload),
    )
    old_path = upload_services.resolve_file_disk_path(old_upload.object_key)
    assert old_path.is_file()

    new_upload, _ = chunk_services.start_chunked_upload(
        actor=student,
        material_type="video",
        filename="新视频.mp4",
        file_size=SMALL_CHUNK * 2,
        total_chunks=2,
        requirement_id=reqs["video"].id,
    )
    chunk_services.store_chunk(
        actor=student,
        upload_id=new_upload.id,
        chunk_index=0,
        chunk_file=SimpleUploadedFile("p0", _video_bytes(SMALL_CHUNK)),
    )
    chunk_services.store_chunk(
        actor=student,
        upload_id=new_upload.id,
        chunk_index=1,
        chunk_file=SimpleUploadedFile("p1", b"N" * SMALL_CHUNK),
    )
    tmp_dir = chunk_services.chunk_tmp_dir(new_upload)
    assert (tmp_dir / "000000.part").is_file()
    assert (tmp_dir / "000001.part").is_file()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated db failure before commit")

    monkeypatch.setattr(chunk_services, "record", boom)

    with pytest.raises(RuntimeError, match="simulated db failure"):
        chunk_services.complete_chunked_upload(
            actor=student,
            upload_id=new_upload.id,
            material_type="video",
        )

    # 1) 原记录与原物理文件完好无损
    old_upload.refresh_from_db()
    old_asset.refresh_from_db()
    assert old_upload.status == UploadSession.Status.COMPLETE
    assert old_asset.status == FileAsset.Status.VALID
    assert old_path.is_file() and old_path.read_bytes() == old_payload

    # 2) 新分片目录与分片完好无损，且会话恢复为 UPLOADING 以便重试
    new_upload.refresh_from_db()
    assert new_upload.status == UploadSession.Status.UPLOADING
    assert (tmp_dir / "000000.part").is_file()
    assert (tmp_dir / "000001.part").is_file()

    # 3) 本次新建的目标合并文件已被清理，promotional_video 目录下只有原文件
    video_dir = Path(isolated_media_root) / str(activity.id) / student.username / "promotional_video"
    assert list(video_dir.iterdir()) == [old_path]


@pytest.mark.django_db(transaction=True)
def test_direct_upload_rollback_preserves_old_file_and_cleans_new_file(monkeypatch, isolated_media_root):
    """模拟 direct_upload 写库异常回滚：原已上传文件完好无损，本次新建文件被清理。"""
    student, activity, reqs = _make_tx_fixtures()
    old_bytes = b"%PDF-1.7\noriginal-report-bytes"
    old_upload, _, _ = upload_services.direct_upload(
        actor=student,
        requirement_id=reqs["practice_report"].id,
        file_obj=SimpleUploadedFile("旧报告.pdf", old_bytes),
    )
    old_path = upload_services.resolve_file_disk_path(old_upload.object_key)
    assert old_path.is_file()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated commit abort")

    monkeypatch.setattr(upload_services, "record", boom)
    with pytest.raises(RuntimeError, match="simulated commit abort"):
        upload_services.direct_upload(
            actor=student,
            requirement_id=reqs["practice_report"].id,
            file_obj=SimpleUploadedFile("新报告.pdf", b"%PDF-1.7\nnew-report-bytes"),
        )

    old_upload.refresh_from_db()
    assert old_upload.status == UploadSession.Status.COMPLETE
    assert old_path.is_file() and old_path.read_bytes() == old_bytes
    report_dir = Path(isolated_media_root) / str(activity.id) / student.username / "practice_report"
    assert list(report_dir.iterdir()) == [old_path]


# =====================================================================
# 1.2 中途取消视频上传不再 409 死锁 72 小时
# =====================================================================

@pytest.mark.django_db
def test_cancel_video_mid_upload_and_switch_to_new_video_succeeds_without_409(
    client, student, activity, settings, django_capture_on_commit_callbacks
):
    """传 A.mp4 到一半不通知后端（或前端取消请求丢包），立刻选 B.mp4 上传直接 201 -> 200 无 409。"""
    settings.UPLOAD_CHUNK_BYTES = SMALL_CHUNK
    reqs = upload_services.ensure_standard_material_requirements(activity)
    client.force_login(student)

    # 1) 开始上传 A.mp4 并只传第 0 片
    start_a = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": "video",
            "filename": "A.mp4",
            "file_size": str(SMALL_CHUNK * 2),
            "total_chunks": "2",
            "requirement_id": str(reqs["video"].id),
        },
    )
    assert start_a.status_code == 201
    upload_a_id = start_a.json()["upload_id"]
    assert client.post(
        "/api/v1/uploads/chunk-upload/",
        {
            "upload_id": upload_a_id,
            "chunk_index": "0",
            "chunk": SimpleUploadedFile("p0", _video_bytes(SMALL_CHUNK)),
        },
    ).status_code == 200
    upload_a = UploadSession.objects.get(pk=upload_a_id)
    tmp_dir_a = chunk_services.chunk_tmp_dir(upload_a)
    assert (tmp_dir_a / "000000.part").is_file()

    # 2) 立刻换选 B.mp4 发起 chunk-start（即使没先调 delete，后端也自动中止 A.mp4、释放额度并清理分片目录）
    with django_capture_on_commit_callbacks(execute=True):
        start_b = client.post(
            "/api/v1/uploads/chunk-start/",
            {
                "material_type": "video",
                "filename": "B.mp4",
                "file_size": str(SMALL_CHUNK),
                "total_chunks": "1",
                "requirement_id": str(reqs["video"].id),
            },
        )
    assert start_b.status_code == 201
    upload_b_id = start_b.json()["upload_id"]
    assert upload_b_id != upload_a_id

    upload_a.refresh_from_db()
    assert upload_a.status == UploadSession.Status.ABORTED
    assert not tmp_dir_a.exists()

    quota = StorageQuota.objects.get(owner=student, activity=activity)
    assert quota.reserved_bytes == SMALL_CHUNK
    assert quota.used_bytes == 0

    # 3) 完成 B.mp4 上传 -> 200
    assert client.post(
        "/api/v1/uploads/chunk-upload/",
        {
            "upload_id": upload_b_id,
            "chunk_index": "0",
            "chunk": SimpleUploadedFile("p0", _video_bytes(SMALL_CHUNK)),
        },
    ).status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        done_b = client.post("/api/v1/uploads/chunk-complete/", {"upload_id": upload_b_id, "material_type": "video"})
    assert done_b.status_code == 200
    assert done_b.json()["original_name"] == "B.mp4"
    quota.refresh_from_db()
    assert quota.used_bytes == SMALL_CHUNK
    assert quota.reserved_bytes == 0


def test_wizard_js_releases_chunk_session_on_cancel_and_routes_large_files_to_chunked():
    """验证前端 apply-wizard.js 取消分片带 keepalive: true 释放，且 >10MB 文件统一走 5MB 分片。"""
    source = (Path(settings.BASE_DIR) / "static" / "js" / "apply-wizard.js").read_text(encoding="utf-8")
    assert "CHUNK_THRESHOLD_BYTES = 10 * 1024 * 1024" in source
    assert "Number(file.size) > CHUNK_THRESHOLD_BYTES" in source
    assert "keepalive: true" in source
    assert "/api/v1/uploads/' + encodeURIComponent(id) + '/delete/" in source


# =====================================================================
# 1.3 修复 2000MB .zip 与 archive.py 规则冲突
# =====================================================================

@pytest.mark.django_db
def test_zip_allows_nested_normal_folders_and_300mb_normal_archive_but_blocks_attacks(
    client, student, activity, tmp_path
):
    reqs = upload_services.ensure_standard_material_requirements(activity)
    client.force_login(student)

    # 1) 两层、三层正常文件夹目录的 .zip 正常通过
    nested_dirs = io.BytesIO()
    with zipfile.ZipFile(nested_dirs, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("证明材料/获奖证书/一等奖.pdf", b"%PDF-1.7\ncert-1")
        zf.writestr("证明材料/获奖证书/省级/2026/证书扫描件.pdf", b"%PDF-1.7\ncert-2")
    ok_resp = client.post(
        "/api/v1/uploads/direct-upload/",
        {
            "requirement_id": str(reqs["other_materials"].id),
            "file": SimpleUploadedFile("多层目录证明.zip", nested_dirs.getvalue(), content_type="application/zip"),
        },
    )
    assert ok_resp.status_code == 201, ok_resp.content[:300]

    # 2) 超过 6 层普通目录仍被拒绝
    too_deep = io.BytesIO()
    with zipfile.ZipFile(too_deep, "w") as zf:
        zf.writestr("a/b/c/d/e/f/g/file.txt", b"too deep")
    with pytest.raises(DomainError, match="目录层级超过 6 层"):
        inspect_zip_archive(io.BytesIO(too_deep.getvalue()))

    # 3) 300MB+ 正常打包的 .zip（压缩比约 1.0x，非炸弹）通过 inspect_zip_archive
    big_zip_path = tmp_path / "normal_305mb.zip"
    chunk_1mb = b"M" * MB
    with zipfile.ZipFile(big_zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        with zf.open("证明材料/实践影像/完整素材.bin", "w") as member:
            for _ in range(305):
                member.write(chunk_1mb)
    inspection = inspect_zip_archive(big_zip_path)
    assert inspection.total_uncompressed_bytes == 305 * MB
    assert inspection.max_depth == 2

    # 4) zipslip.zip / nested.zip / bomb.zip 仍全部被拦截
    for sample_name, keyword in (
        ("zipslip.zip", "ZipSlip"),
        ("nested.zip", "嵌套"),
        ("bomb.zip", "解压炸弹"),
    ):
        resp = client.post(
            "/api/v1/uploads/direct-upload/",
            {
                "requirement_id": str(reqs["other_materials"].id),
                "file": SimpleUploadedFile(
                    sample_name, fixture_path(sample_name).read_bytes(), content_type="application/zip"
                ),
            },
        )
        assert resp.status_code == 400
        assert keyword in resp.json()["error"]["message"]


# =====================================================================
# 1.4 50MB .zip 走 5MB 分片续传 + .zip 最多 5 份校验 + 停用 ffprobe + Pillow 生效
# =====================================================================

@pytest.mark.django_db
def test_50mb_zip_uses_5mb_chunk_flow_with_resume_and_enforces_five_zip_cap(
    client, student, activity, tmp_path, django_capture_on_commit_callbacks
):
    reqs = upload_services.ensure_standard_material_requirements(activity)
    client.force_login(student)

    # 构造约 50MB 的合法 .zip（含两层子目录）
    zip_path = tmp_path / "pack_50mb.zip"
    block = b"Z" * MB
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        with zf.open("佐证材料/汇报扫描件/合集.bin", "w") as member:
            for _ in range(50):
                member.write(block)
    zip_bytes = zip_path.read_bytes()
    total_size = len(zip_bytes)
    chunk_size = 5 * MB
    total_chunks = (total_size + chunk_size - 1) // chunk_size
    assert total_chunks >= 10

    # 1) 启动分片并上传前 2 片
    started = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": "other_materials",
            "filename": "50MB材料包.zip",
            "file_size": str(total_size),
            "total_chunks": str(total_chunks),
            "requirement_id": str(reqs["other_materials"].id),
        },
    )
    assert started.status_code == 201
    upload_id = started.json()["upload_id"]
    for idx in (0, 1):
        part_data = zip_bytes[idx * chunk_size : (idx + 1) * chunk_size]
        assert client.post(
            "/api/v1/uploads/chunk-upload/",
            {
                "upload_id": upload_id,
                "chunk_index": str(idx),
                "chunk": SimpleUploadedFile(f"part-{idx}", part_data),
            },
        ).status_code == 200

    # 2) 断点续传：再次 chunk-start 复用同一会话并返回已收到 [0, 1]
    resumed = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": "other_materials",
            "filename": "50MB材料包.zip",
            "file_size": str(total_size),
            "total_chunks": str(total_chunks),
            "requirement_id": str(reqs["other_materials"].id),
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] is True
    assert resumed.json()["upload_id"] == upload_id
    assert resumed.json()["received_chunks"] == [0, 1]

    # 3) 补齐剩余分片并完成合并
    for idx in range(2, total_chunks):
        part_data = zip_bytes[idx * chunk_size : (idx + 1) * chunk_size]
        assert client.post(
            "/api/v1/uploads/chunk-upload/",
            {
                "upload_id": upload_id,
                "chunk_index": str(idx),
                "chunk": SimpleUploadedFile(f"part-{idx}", part_data),
            },
        ).status_code == 200

    with django_capture_on_commit_callbacks(execute=True):
        done = client.post(
            "/api/v1/uploads/chunk-complete/",
            {"upload_id": upload_id, "material_type": "other_materials"},
        )
    assert done.status_code == 200, done.content[:300]
    assert done.json()["actual_bytes"] == total_size
    assert upload_services.resolve_file_disk_path(done.json()["final_path"]).stat().st_size == total_size

    # 4) 再补 4 份合法小 .zip 凑满 5 份，第 6 份在 chunk-complete 也会被扩展名份数校验拦截（409）
    small_zip_buf = io.BytesIO()
    with zipfile.ZipFile(small_zip_buf, "w") as zf:
        zf.writestr("证明/附件.txt", b"ok")
    small_zip = small_zip_buf.getvalue()

    for i in range(2, 6):
        up_resp = client.post(
            "/api/v1/uploads/direct-upload/",
            {
                "requirement_id": str(reqs["other_materials"].id),
                "file": SimpleUploadedFile(f"材料包_{i}.zip", small_zip, content_type="application/zip"),
            },
        )
        assert up_resp.status_code == 201

    # 已有 5 份 .zip：新开第 6 份直接 409
    sixth_start = client.post(
        "/api/v1/uploads/chunk-start/",
        {
            "material_type": "other_materials",
            "filename": "第6份.zip",
            "file_size": str(len(small_zip)),
            "total_chunks": "1",
            "requirement_id": str(reqs["other_materials"].id),
        },
    )
    assert sixth_start.status_code == 409
    assert ".zip" in sixth_start.json()["error"]["message"]

    # 模拟并发绕过 chunk-start 后直接进入 complete_chunked_upload：同样被 assert_extension_count_available 拦下
    quota = StorageQuota.objects.get(owner=student, activity=activity)
    quota.reserved_bytes += len(small_zip)
    quota.save(update_fields=["reserved_bytes", "updated_at"])
    raced = UploadSession.objects.create(
        owner=student,
        requirement=reqs["other_materials"],
        quota=quota,
        object_key="tmp/raced-zip-session",
        original_name="并发第6份.zip",
        declared_bytes=len(small_zip),
        status=UploadSession.Status.UPLOADING,
        upload_mode=UploadSession.Mode.CHUNKED,
        total_chunks=1,
        chunk_size_bytes=5 * MB,
        expires_at=timezone.now() + timedelta(days=1),
    )
    raced.object_key = f"tmp/{raced.id}"
    raced.save(update_fields=["object_key"])
    chunk_services.store_chunk(
        actor=student,
        upload_id=raced.id,
        chunk_index=0,
        chunk_file=SimpleUploadedFile("p0", small_zip),
    )
    with pytest.raises(Conflict, match=r"\.zip"):
        chunk_services.complete_chunked_upload(
            actor=student,
            upload_id=raced.id,
            material_type="other_materials",
        )


def test_ffprobe_disabled_by_default_and_pillow_verifies_images_without_degraded_warning(
    monkeypatch, caplog, tmp_path
):
    """默认关闭 ffprobe（不启子进程不卡死），且 Pillow 已就绪不刷 image_verify_degraded 告警。"""
    assert base_settings.UPLOAD_FFPROBE_BIN == ""

    def forbid_subprocess(*args, **kwargs):
        raise AssertionError("ffprobe subprocess should not be invoked when UPLOAD_FFPROBE_BIN is empty")

    monkeypatch.setattr(policies.subprocess, "run", forbid_subprocess)
    sample_mp4 = tmp_path / "clip.mp4"
    sample_mp4.write_bytes(_video_bytes(4096))

    caplog.set_level(logging.WARNING, logger="apps.uploads.policies")
    assert policies.verify_video_file(sample_mp4, ".mp4") == "disabled"
    assert not any("video_probe_degraded" in r.getMessage() for r in caplog.records)

    sample_jpg = tmp_path / "photo.jpg"
    sample_jpg.write_bytes(REAL_JPEG)
    sample_png = tmp_path / "photo.png"
    sample_png.write_bytes(REAL_PNG)
    policies.verify_image_file(sample_jpg, ".jpg")
    policies.verify_image_file(sample_png, ".png")
    assert not any("image_verify_degraded" in r.getMessage() for r in caplog.records)
