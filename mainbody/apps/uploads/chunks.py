"""Browser-side chunked upload for large materials (宣讲视频 300MB).

Flow: chunk_start -> chunk_upload x N -> chunk_complete.

Design notes
------------
* One part request carries at most chunk_size_bytes (5MB hard cap), so a 300MB
  video is 60 short requests instead of one 300MB request that parks a Gunicorn
  worker for minutes on a campus uplink.
* Parts land in MEDIA_ROOT/tmp/<upload_id>/<index:06d>.part. Every part is
  written to a staging file and os.replace'd into place, so an interrupted
  request can never leave a truncated .part that later "looks complete".
* Re-posting a part that already exists with the same length returns 200 without
  rewriting it: that is what makes FilePond resume after a dropped connection.
* The merged file name never derives from the client filename: the directory is
  <activity_id>/<student_number>/<material directory>/<uuid4().hex><ext>.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import require_student
from apps.activities.models import Activity, MaterialRequirement
from apps.activities.selectors import (
    eligible_activities,
    ensure_activity_open,
    is_activity_open_for_user,
)
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError, Forbidden, QuotaExceeded

from .models import FileAsset, StorageQuota, UploadSession
from .policies import (
    MAX_CHUNKS_PER_UPLOAD,
    SINGLE_FILE_MATERIAL_CODES,
    STANDARD_MATERIAL_RULES,
    build_object_key,
    canonical_material_type,
    chunk_size_bytes,
    effective_max_file_bytes,
    part_count,
    validate_magic_bytes,
    validate_metadata,
    validate_stored_file_content,
)
from .services import (
    ACTIVE_STATES,
    STREAM_CHUNK_BYTES,
    _remove_physical_file,
    assert_extension_count_available,
    ensure_standard_material_requirements,
    resolve_file_disk_path,
)

logger = logging.getLogger(__name__)

TMP_PREFIX = "tmp"
MB = 1024 * 1024

_PART_NAME = re.compile(r"^(\d{6})\.part$")


# ---------------------------------------------------------------------------
# 路径与小工具
# ---------------------------------------------------------------------------

def chunk_tmp_dir(upload) -> Path:
    """Absolute MEDIA_ROOT/tmp/<upload_id>/ for one in-flight chunked upload."""
    return resolve_file_disk_path(f"{TMP_PREFIX}/{upload.id}")


def _part_path(upload, index: int) -> Path:
    return chunk_tmp_dir(upload) / f"{int(index):06d}.part"


def received_parts(upload) -> dict[int, int]:
    """Map chunk_index -> byte size for the parts already on disk (sorted ascending)."""
    directory = chunk_tmp_dir(upload)
    if not directory.is_dir():
        return {}
    sizes: dict[int, int] = {}
    for entry in directory.iterdir():
        match = _PART_NAME.match(entry.name)
        if not match or not entry.is_file():
            continue
        try:
            sizes[int(match.group(1))] = entry.stat().st_size
        except FileNotFoundError:  # pragma: no cover - concurrent cleanup
            continue
    return dict(sorted(sizes.items()))


def received_chunk_indexes(upload) -> list[int]:
    return sorted(received_parts(upload))


def _owned_upload(actor, upload_id, *, lock: bool = False) -> UploadSession:
    """Load one chunked session and prove the caller owns it (403 otherwise)."""
    try:
        target = uuid.UUID(str(upload_id))
    except (ValueError, TypeError, AttributeError):
        raise DomainError("upload_id 必须为 UUID。") from None

    queryset = UploadSession.objects.select_related("requirement__activity", "owner")
    if lock:
        queryset = queryset.select_for_update()
    upload = queryset.filter(pk=target).first()
    if upload is None:
        raise UploadSession.DoesNotExist("上传任务不存在或已失效。")
    if upload.owner_id != actor.pk:
        raise Forbidden("无权操作此上传任务。")
    if upload.upload_mode != UploadSession.Mode.CHUNKED:
        raise DomainError("该上传任务不是分片上传任务，请使用直传接口。")
    return upload


def _resolve_requirement(actor, *, code: str, requirement_id=None, activity_id=None) -> MaterialRequirement:
    """Find the MaterialRequirement row the chunk upload belongs to, server-side."""
    if requirement_id:
        try:
            requirement_uuid = uuid.UUID(str(requirement_id))
        except (ValueError, TypeError):
            raise DomainError("requirement_id 必须为 UUID。") from None
        requirement = MaterialRequirement.objects.select_related("activity").get(pk=requirement_uuid)
        if requirement.code != code:
            raise DomainError("material_type 与 requirement_id 不匹配。")
        return requirement

    if activity_id:
        try:
            activity_uuid = uuid.UUID(str(activity_id))
        except (ValueError, TypeError):
            raise DomainError("activity_id 必须为 UUID。") from None
        activity = Activity.objects.get(pk=activity_uuid)
    else:
        candidates = [
            act
            for act in eligible_activities(actor).order_by("-created_at")[:50]
            if is_activity_open_for_user(act, actor)[0]
        ]
        if len(candidates) != 1:
            raise DomainError("请提供 activity_id 或 requirement_id：当前开放中的活动不唯一。")
        activity = candidates[0]

    requirement = MaterialRequirement.objects.filter(activity=activity, code=code).first()
    if requirement is None:
        requirement = ensure_standard_material_requirements(activity)[code]
    return requirement


def _submission_locked(owner, activity) -> str:
    """Return a display label when the material may no longer be replaced, else ''."""
    from apps.submissions.models import Submission

    existing = Submission.objects.filter(owner=owner, activity=activity).first()
    if existing and existing.status not in (Submission.Status.DRAFT, Submission.Status.RETURNED):
        return existing.get_status_display()
    return ""


# ---------------------------------------------------------------------------
# chunk_start
# ---------------------------------------------------------------------------

@transaction.atomic
def start_chunked_upload(
    *,
    actor,
    material_type,
    filename,
    file_size,
    total_chunks,
    requirement_id=None,
    activity_id=None,
) -> tuple[UploadSession, bool]:
    """Open (or resume) one chunked upload session.

    Returns (upload, resumed). resumed is True when an identical in-flight
    session was reused, which is what lets a reloaded page continue the same file.
    """
    require_student(actor)

    code = canonical_material_type(material_type)
    if code is None:
        raise DomainError(
            "material_type 非法，仅支持：" + "、".join(sorted(STANDARD_MATERIAL_RULES)) + "。"
        )
    requirement = _resolve_requirement(
        actor, code=code, requirement_id=requirement_id, activity_id=activity_id
    )
    activity = requirement.activity
    ensure_activity_open(activity, actor)

    # 服务端权威校验：文件名字符集/扩展名白名单/大小上限全部按 policies 规则判定。
    filename = str(filename or "").strip()
    extension = validate_metadata(
        filename=filename,
        declared_bytes=file_size,
        requirement=requirement,
    )

    part_bytes = chunk_size_bytes()
    expected_chunks = part_count(file_size, part_bytes)
    if expected_chunks > MAX_CHUNKS_PER_UPLOAD:
        raise DomainError(f"分片数量超出上限（{expected_chunks} > {MAX_CHUNKS_PER_UPLOAD}）。")
    if total_chunks != expected_chunks:
        raise DomainError(
            f"分片数量与文件大小不匹配：{file_size:,} 字节按每片 {part_bytes:,} 字节应为 "
            f"{expected_chunks} 片，收到 {total_chunks} 片。"
        )

    owner = User.objects.select_for_update().get(pk=actor.pk)
    require_student(owner)

    locked_label = _submission_locked(owner, activity)
    if locked_label:
        raise Conflict(f"当前申报状态为【{locked_label}】，不允许上传或替换材料。")

    # 断点续传：同名同大小的进行中任务直接复用，分片保留在磁盘上。
    resumable = (
        UploadSession.objects.filter(
            owner=owner,
            requirement=requirement,
            upload_mode=UploadSession.Mode.CHUNKED,
            status=UploadSession.Status.UPLOADING,
            original_name=filename,
            declared_bytes=file_size,
            total_chunks=total_chunks,
            expires_at__gt=timezone.now(),
        )
        .order_by("-created_at")
        .first()
    )
    if resumable is not None:
        chunk_tmp_dir(resumable).mkdir(parents=True, exist_ok=True)
        return resumable, True

    active_count = UploadSession.objects.filter(owner=owner, status__in=ACTIVE_STATES).count()
    if active_count >= settings.MAX_ACTIVE_UPLOADS_PER_USER:
        raise Conflict("同时上传任务过多，请先完成或取消已有任务。")

    siblings = list(
        UploadSession.objects.filter(owner=owner, requirement=requirement).exclude(
            status=UploadSession.Status.ABORTED
        )
    )
    if any(item.status in ACTIVE_STATES for item in siblings):
        raise Conflict("此材料类别已有正在进行的上传任务，请先完成或取消后再试。")
    if len(siblings) >= requirement.max_count and requirement.max_count != 1:
        raise Conflict(f"此材料类别已达到文件数量上限（最多 {requirement.max_count} 份）。")
    assert_extension_count_available(siblings, requirement, extension)

    quota, _ = StorageQuota.objects.get_or_create(
        owner=owner,
        activity=activity,
        defaults={"limit_bytes": activity.quota_bytes_per_student},
    )
    quota = StorageQuota.objects.select_for_update().get(pk=quota.pk)
    if quota.used_bytes + quota.reserved_bytes + file_size > quota.limit_bytes:
        raise QuotaExceeded(
            "该活动存储额度不足：已占用 "
            f"{quota.used_bytes + quota.reserved_bytes:,} 字节 / 上限 {quota.limit_bytes:,} 字节，"
            f"本次还需 {file_size:,} 字节。"
        )
    quota.reserved_bytes += file_size
    quota.save(update_fields=["reserved_bytes", "updated_at"])

    upload_id = uuid.uuid4()
    upload = UploadSession.objects.create(
        id=upload_id,
        owner=owner,
        requirement=requirement,
        quota=quota,
        request_key=uuid.uuid4(),
        object_key=f"{TMP_PREFIX}/{upload_id}",
        original_name=filename,
        declared_bytes=file_size,
        status=UploadSession.Status.UPLOADING,
        upload_mode=UploadSession.Mode.CHUNKED,
        total_chunks=total_chunks,
        chunk_size_bytes=part_bytes,
        expires_at=timezone.now() + timedelta(days=3),
    )
    chunk_tmp_dir(upload).mkdir(parents=True, exist_ok=True)

    record(
        actor=owner,
        action="upload.chunk_started",
        target=upload,
        metadata={
            "requirement_code": requirement.code,
            "original_name": filename,
            "declared_bytes": file_size,
            "total_chunks": total_chunks,
            "chunk_size_bytes": part_bytes,
        },
    )
    return upload, False


# ---------------------------------------------------------------------------
# chunk_upload
# ---------------------------------------------------------------------------

def store_chunk(*, actor, upload_id, chunk_index, chunk_file) -> tuple[UploadSession, int, bool]:
    """Store one part. Returns (upload, chunk_index, skipped_existing)."""
    require_student(actor)
    # 归属校验在读取任何分片内容之前完成：非本人 upload_id 一律 403。
    upload = _owned_upload(actor, upload_id)

    if upload.status == UploadSession.Status.COMPLETE:
        raise Conflict("该上传任务已完成，无需继续上传分片。")
    if upload.status != UploadSession.Status.UPLOADING:
        raise Conflict("该上传任务已取消或正在合并，无法继续上传分片。")

    try:
        index = int(str(chunk_index).strip())
    except (TypeError, ValueError):
        raise DomainError("chunk_index 必须为整数。") from None
    if not 0 <= index < upload.total_chunks:
        raise DomainError(
            f"chunk_index 越界：应在 0 ～ {upload.total_chunks - 1} 之间，收到 {index}。"
        )

    if chunk_file is None:
        raise DomainError("缺少分片内容（字段名 chunk）。")
    size = int(getattr(chunk_file, "size", 0) or 0)
    if size <= 0:
        raise DomainError("分片内容为空。")
    if size > upload.chunk_size_bytes:
        raise DomainError(
            f"单个分片不得超过 {upload.chunk_size_bytes // MB}MB，收到 {size:,} 字节。"
        )

    directory = chunk_tmp_dir(upload)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{index:06d}.part"

    # 幂等：同名分片已存在且长度一致，直接返回，支持断点续传。
    if target.exists() and target.stat().st_size == size:
        return upload, index, True

    staging = directory / f".{index:06d}.part.{uuid.uuid4().hex}.tmp"
    written = 0
    try:
        with open(staging, "wb") as destination:
            if hasattr(chunk_file, "chunks"):
                for piece in chunk_file.chunks(chunk_size=STREAM_CHUNK_BYTES):
                    if isinstance(piece, str):  # pragma: no cover - defensive
                        piece = piece.encode("utf-8")
                    destination.write(piece)
                    written += len(piece)
            else:  # pragma: no cover - plain file objects
                while True:
                    piece = chunk_file.read(STREAM_CHUNK_BYTES)
                    if not piece:
                        break
                    destination.write(piece)
                    written += len(piece)
            destination.flush()
            os.fsync(destination.fileno())
        if written != size:
            raise DomainError("分片写入字节数与声明长度不一致，请重试。")
        os.replace(staging, target)
    except Exception:
        staging.unlink(missing_ok=True)
        raise

    return upload, index, False


# ---------------------------------------------------------------------------
# chunk_complete
# ---------------------------------------------------------------------------

def _merge_parts(upload, final_path: Path, total_bytes: int) -> tuple[int, str]:
    """Stream parts into final_path in index order; returns (bytes, sha256 hex)."""
    staging = final_path.parent / f".{uuid.uuid4().hex}.merge.tmp"
    hasher = hashlib.sha256()
    written = 0
    try:
        with open(staging, "wb") as destination:
            for index in range(upload.total_chunks):
                with open(_part_path(upload, index), "rb") as source:
                    while True:
                        piece = source.read(STREAM_CHUNK_BYTES)
                        if not piece:
                            break
                        destination.write(piece)
                        hasher.update(piece)
                        written += len(piece)
            destination.flush()
            os.fsync(destination.fileno())
        if written != total_bytes:
            raise DomainError("合并结果与分片总大小不一致，已回滚，请重试。")
        os.replace(staging, final_path)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return written, hasher.hexdigest()


def _schedule_validation(asset: FileAsset) -> bool:
    """Hand the merged file to apps.validation when a task exists; no-op today."""
    try:
        from apps.validation.tasks import validate_file_asset
    except ModuleNotFoundError:
        return False
    try:
        validate_file_asset.delay(str(asset.pk))
    except Exception:  # broker down must never fail a finished upload
        logger.warning("validation enqueue failed for file asset %s", asset.pk, exc_info=True)
        return False
    return True


@transaction.atomic
def complete_chunked_upload(*, actor, upload_id, material_type=None) -> tuple[UploadSession, FileAsset, list]:
    """Verify every part, merge in index order, then mark the session complete.

    Returns (upload, file_asset, replaced_upload_ids).
    """
    require_student(actor)
    upload = _owned_upload(actor, upload_id, lock=True)

    requirement = upload.requirement
    if requirement is None:  # pragma: no cover - FK is PROTECT
        raise DomainError("上传任务缺少材料类别。")

    if material_type:
        code = canonical_material_type(material_type)
        if code is None:
            raise DomainError("material_type 非法。")
        if code != requirement.code:
            raise DomainError("material_type 与本次上传的材料类别不一致。")

    if upload.status == UploadSession.Status.COMPLETE:
        asset = FileAsset.objects.filter(upload=upload).first()
        if asset is None:
            raise Conflict("该上传任务已完成但缺少文件记录。")
        return upload, asset, []
    if upload.status != UploadSession.Status.UPLOADING:
        raise Conflict("该上传任务已取消或正在合并，无法完成合并。")

    extension = validate_metadata(
        filename=upload.original_name,
        declared_bytes=upload.declared_bytes,
        requirement=requirement,
    )

    parts = received_parts(upload)
    missing = [index for index in range(upload.total_chunks) if index not in parts]
    if missing:
        raise DomainError(
            f"分片不完整：缺少 {len(missing)} 片（首个缺失为第 {missing[0] + 1} 片），请继续上传后再合并。"
        )
    empty = [index for index, size in parts.items() if size <= 0]
    if empty:
        raise DomainError(f"分片内容为空：第 {empty[0] + 1} 片为 0 字节。")

    total_bytes = sum(parts.values())
    tolerance = upload.chunk_size_bytes  # ±1 片噪声
    if abs(total_bytes - upload.declared_bytes) > tolerance:
        raise DomainError(
            f"分片总大小 {total_bytes:,} 字节与声明的 {upload.declared_bytes:,} 字节不一致"
            f"（允许误差 ±{tolerance:,} 字节）。"
        )
    max_allowed = effective_max_file_bytes(requirement.code, extension, requirement.max_file_bytes)
    if total_bytes <= 0 or total_bytes > max_allowed:
        raise DomainError(f"合并后文件大小超出允许范围（上限 {max_allowed // MB}MB）。")

    # 魔数校验只读第 1 片的头 32 字节，与直传路径使用同一套规则。
    with open(_part_path(upload, 0), "rb") as first_part:
        header = first_part.read(32)
    detected_type = validate_magic_bytes(header, extension)

    # 目录结构固定 <activity_id>/<student_number>/<material_type>/<uuid4().hex><ext>；
    # 原始文件名只在 DB 的 original_name 里，永远不进物理路径。
    final_key = build_object_key(
        activity_id=requirement.activity_id,
        student_number=getattr(upload.owner, "username", "") or f"user-{upload.owner_id}",
        material_code=requirement.code,
        extension=extension,
    )
    final_path = resolve_file_disk_path(final_key)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    written, checksum = _merge_parts(upload, final_path, total_bytes)

    # 合并后的整份文件再走一次结构校验（容器结构 / ZipSlip / ffprobe），失败即删文件。
    try:
        detected_type = validate_stored_file_content(final_path, extension)
    except Exception:
        final_path.unlink(missing_ok=True)
        raise

    replace_all = requirement.max_count == 1 or requirement.code in SINGLE_FILE_MATERIAL_CODES
    siblings = list(
        UploadSession.objects.select_for_update()
        .filter(owner=upload.owner, requirement=requirement)
        .exclude(pk=upload.pk)
        .exclude(status=UploadSession.Status.ABORTED)
        .order_by("created_at")
    )
    if not replace_all and len(siblings) + 1 > requirement.max_count:
        final_path.unlink(missing_ok=True)
        raise Conflict(f"此材料类别已达到文件数量上限（最多 {requirement.max_count} 份）。")
    replaced = siblings if replace_all else []

    old_assets = (
        {
            asset.upload_id: asset
            for asset in FileAsset.objects.select_for_update().filter(upload__in=replaced)
        }
        if replaced
        else {}
    )
    release_used = 0
    release_reserved = 0
    for old in replaced:
        if old.status == UploadSession.Status.COMPLETE:
            old_asset = old_assets.get(old.id)
            release_used += old_asset.actual_bytes if old_asset is not None else old.declared_bytes
        else:
            release_reserved += old.declared_bytes

    quota = StorageQuota.objects.select_for_update().get(pk=upload.quota_id)
    projected_used = max(0, quota.used_bytes - release_used) + written
    # 本次自己的预留（declared_bytes）在合并完成时转为已用。
    projected_reserved = max(0, quota.reserved_bytes - upload.declared_bytes - release_reserved)
    if projected_used + projected_reserved > quota.limit_bytes:
        final_path.unlink(missing_ok=True)
        raise QuotaExceeded(
            "该活动存储额度不足：本次合并后需 "
            f"{projected_used + projected_reserved:,} 字节 / 上限 {quota.limit_bytes:,} 字节。"
        )

    quota.used_bytes = projected_used
    quota.reserved_bytes = projected_reserved
    quota.save(update_fields=["used_bytes", "reserved_bytes", "updated_at"])

    replaced_ids = []
    old_keys_to_delete = []
    for old in replaced:
        old.status = UploadSession.Status.ABORTED
        old.save(update_fields=["status", "updated_at"])
        old_asset = old_assets.get(old.id)
        if old_asset is not None and old_asset.status != FileAsset.Status.INVALID:
            old_asset.status = FileAsset.Status.INVALID
            old_asset.save(update_fields=["status", "updated_at"])
        replaced_ids.append(old.id)
        old_keys_to_delete.append(old.object_key)

    upload.status = UploadSession.Status.COMPLETE
    upload.object_key = final_key
    upload.save(update_fields=["status", "object_key", "updated_at"])

    asset = FileAsset.objects.create(
        upload=upload,
        actual_bytes=written,
        detected_type=detected_type,
        checksum_algorithm="sha256",
        checksum_value=checksum,
        status=FileAsset.Status.VALID,
    )

    # 分片读完后整目录删除，磁盘上不留 tmp/<upload_id>/。
    shutil.rmtree(chunk_tmp_dir(upload), ignore_errors=True)

    for old_key in old_keys_to_delete:
        key_text = str(old_key)
        if key_text.startswith(TMP_PREFIX + "/"):
            try:
                shutil.rmtree(resolve_file_disk_path(key_text), ignore_errors=True)
            except DomainError:  # pragma: no cover - defensive
                pass
        else:
            _remove_physical_file(old_key)

    record(
        actor=upload.owner,
        action="upload.chunk_completed",
        target=upload,
        metadata={
            "requirement_code": requirement.code,
            "original_name": upload.original_name,
            "actual_bytes": written,
            "total_chunks": upload.total_chunks,
            "replaced_upload_ids": [str(item) for item in replaced_ids],
        },
    )
    _schedule_validation(asset)
    return upload, asset, replaced_ids
