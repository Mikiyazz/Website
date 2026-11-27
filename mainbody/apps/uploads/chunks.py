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
    _submission_upload_scope_q,
    assert_extension_count_available,
    cleanup_upload_disk_artifacts,
    ensure_disk_space_available,
    ensure_standard_material_requirements,
    resolve_file_disk_path,
    resolve_upload_submission,
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
    submission_id=None,
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

    target_submission = resolve_upload_submission(
        owner=owner,
        activity=activity,
        submission_id=submission_id,
    )
    from apps.submissions.services import ensure_submission_editable_by_student

    ensure_submission_editable_by_student(target_submission)
    scope_q = _submission_upload_scope_q(
        owner_id=owner.pk,
        activity_id=activity.id,
        submission=target_submission,
    )

    # 断点续传：同一申报下同名同大小的进行中任务直接复用，分片保留在磁盘上。
    resumable = (
        UploadSession.objects.filter(
            scope_q,
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

    # 严格锁顺序：user -> quota -> upload。
    quota, _ = StorageQuota.objects.get_or_create(
        owner=owner,
        activity=activity,
        defaults={"limit_bytes": activity.quota_bytes_per_student},
    )
    quota = StorageQuota.objects.select_for_update().get(pk=quota.pk)

    siblings = list(
        UploadSession.objects.select_for_update()
        .filter(scope_q, owner=owner, requirement=requirement)
        .exclude(status=UploadSession.Status.ABORTED)
        .order_by("created_at")
    )

    is_single_file_rule = requirement.max_count == 1 or requirement.code in SINGLE_FILE_MATERIAL_CODES
    if is_single_file_rule:
        # 单文件类别（如宣讲视频）换传新文件时，自动中止同申报同类别下残留的旧 UPLOADING 会话、
        # 释放预留额度并在 on_commit 清理旧分片目录，彻底避免取消/换选导致 409 死锁 72 小时。
        stale_uploading = [item for item in siblings if item.status == UploadSession.Status.UPLOADING]
        if stale_uploading:
            aborted_keys: list[str] = []
            quota_dirty = False
            for old in stale_uploading:
                if old.quota_id == quota.pk:
                    release = min(int(quota.reserved_bytes or 0), int(old.declared_bytes or 0))
                    if release:
                        quota.reserved_bytes -= release
                        quota_dirty = True
                elif old.quota_id:
                    other_quota = StorageQuota.objects.select_for_update().get(pk=old.quota_id)
                    release = min(int(other_quota.reserved_bytes or 0), int(old.declared_bytes or 0))
                    if release:
                        other_quota.reserved_bytes -= release
                        other_quota.save(update_fields=["reserved_bytes", "updated_at"])
                old.status = UploadSession.Status.ABORTED
                old.save(update_fields=["status", "updated_at"])
                aborted_keys.append(str(old.object_key or ""))
                record(
                    actor=owner,
                    action="upload.chunk_superseded",
                    target=old,
                    metadata={
                        "requirement_code": requirement.code,
                        "superseded_name": old.original_name,
                        "replaced_by_name": filename,
                        "released_reserved_bytes": int(old.declared_bytes or 0),
                    },
                )
            if quota_dirty:
                quota.save(update_fields=["reserved_bytes", "updated_at"])
            stale_ids = {old.pk for old in stale_uploading}
            siblings = [item for item in siblings if item.pk not in stale_ids]
            keys_snapshot = tuple(aborted_keys)
            transaction.on_commit(lambda keys=keys_snapshot: cleanup_upload_disk_artifacts(keys))

    active_count = UploadSession.objects.filter(owner=owner, status__in=ACTIVE_STATES).count()
    if active_count >= settings.MAX_ACTIVE_UPLOADS_PER_USER:
        raise Conflict("同时上传任务过多，请先完成或取消已有任务。")

    if is_single_file_rule:
        if any(item.status in ACTIVE_STATES for item in siblings):
            raise Conflict("此材料类别已有正在进行的上传任务，请先完成或取消后再试。")
    else:
        if len(siblings) >= requirement.max_count:
            raise Conflict(f"此材料类别已达到文件数量上限（最多 {requirement.max_count} 份）。")
        assert_extension_count_available(siblings, requirement, extension)

    if quota.used_bytes + quota.reserved_bytes + file_size > quota.limit_bytes:
        raise QuotaExceeded(
            "该活动存储额度不足：已占用 "
            f"{quota.used_bytes + quota.reserved_bytes:,} 字节 / 上限 {quota.limit_bytes:,} 字节，"
            f"本次还需 {file_size:,} 字节。"
        )
    ensure_disk_space_available(required_bytes=file_size)
    quota.reserved_bytes += file_size
    quota.save(update_fields=["reserved_bytes", "updated_at"])

    upload_id = uuid.uuid4()
    upload = UploadSession.objects.create(
        id=upload_id,
        owner=owner,
        submission=target_submission,
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
    from apps.submissions.services import ensure_submission_editable_by_student

    ensure_activity_open(upload.requirement.activity, actor)
    ensure_submission_editable_by_student(upload.submission)

    if upload.status == UploadSession.Status.COMPLETE:
        raise Conflict("该上传任务已完成，无需继续上传分片。")
    if upload.status != UploadSession.Status.UPLOADING:
        raise Conflict("该上传任务已取消或正在合并，无法继续上传分片。")
    if upload.expires_at and upload.expires_at <= timezone.now():
        raise Conflict("该上传任务已过期，请重新发起上传。")

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

    ensure_disk_space_available(required_bytes=size, target_dir=directory)

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


def _restore_uploading_if_completing(upload_pk) -> None:
    """Restore COMPLETING -> UPLOADING only when still in COMPLETING (never overwrite ABORTED/COMPLETE)."""
    UploadSession.objects.filter(
        pk=upload_pk,
        status=UploadSession.Status.COMPLETING,
    ).update(status=UploadSession.Status.UPLOADING, updated_at=timezone.now())


def complete_chunked_upload(*, actor, upload_id, material_type=None) -> tuple[UploadSession, FileAsset, list]:
    """Verify every part, merge in index order outside DB transactions, then commit quota and records.

    Three-phase protocol:
    1. Short DB tx 1: lock upload row, validate metadata/state, transition UPLOADING -> COMPLETING.
    2. Outside DB tx: verify parts & magic header, stream-merge + fsync + os.replace, run content check.
    3. Short DB tx 2: settle quota, validate counts/extension caps, mark COMPLETE, create FileAsset,
       mark replaced uploads ABORTED, and defer all physical/tmp deletions to transaction.on_commit.

    Returns (upload, file_asset, replaced_upload_ids).
    """
    require_student(actor)
    from apps.submissions.services import ensure_submission_editable_by_student

    # -----------------------------------------------------------------------
    # 阶段 1（短事务 1）：锁行校验并切为 COMPLETING，阻断并发分片写入或重复合并。
    # -----------------------------------------------------------------------
    with transaction.atomic():
        upload = _owned_upload(actor, upload_id, lock=True)
        ensure_submission_editable_by_student(upload.submission)

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
        ensure_activity_open(requirement.activity, actor)
        if upload.expires_at and upload.expires_at <= timezone.now():
            raise Conflict("该上传任务已过期，请重新发起上传。")

        extension = validate_metadata(
            filename=upload.original_name,
            declared_bytes=upload.declared_bytes,
            requirement=requirement,
        )

        upload.status = UploadSession.Status.COMPLETING
        upload.save(update_fields=["status", "updated_at"])

        upload_pk = upload.pk
        owner_id = upload.owner_id
        quota_id = upload.quota_id
        student_number = getattr(upload.owner, "username", "") or f"user-{upload.owner_id}"
        activity_id = requirement.activity_id
        requirement_code = requirement.code
        requirement_max_file_bytes = requirement.max_file_bytes
        tmp_dir = chunk_tmp_dir(upload)

    # -----------------------------------------------------------------------
    # 阶段 2（事务外）：校验分片、流式合并 + fsync + os.replace、容器与 ZIP 深度校验。
    # -----------------------------------------------------------------------
    final_path: Path | None = None
    try:
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
        max_allowed = effective_max_file_bytes(requirement_code, extension, requirement_max_file_bytes)
        if total_bytes <= 0 or total_bytes > max_allowed:
            raise DomainError(f"合并后文件大小超出允许范围（上限 {max_allowed // MB}MB）。")

        # 魔数校验只读第 1 片的头 32 字节，与直传路径使用同一套规则。
        with open(_part_path(upload, 0), "rb") as first_part:
            header = first_part.read(32)
        detected_type = validate_magic_bytes(header, extension)

        # 目录结构固定 <activity_id>/<student_number>/<material_type>/<uuid4().hex><ext>；
        # 原始文件名只在 DB 的 original_name 里，永远不进物理路径。
        final_key = build_object_key(
            activity_id=activity_id,
            student_number=student_number,
            material_code=requirement_code,
            extension=extension,
        )
        final_path = resolve_file_disk_path(final_key)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_disk_space_available(required_bytes=total_bytes, target_dir=final_path.parent)

        written, checksum = _merge_parts(upload, final_path, total_bytes)

        # 合并后的整份文件再走一次结构校验（容器结构 / ZipSlip / Pillow / 可选 ffprobe）。
        detected_type = validate_stored_file_content(final_path, extension)
    except Exception:
        # 仅删除本次新建的合并文件，原文件与分片目录完好保留，并将状态恢复回 UPLOADING 以便重试。
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        _restore_uploading_if_completing(upload_pk)
        raise

    # -----------------------------------------------------------------------
    # 阶段 3（短事务 2）：按 user -> quota -> upload 锁序结算配额、落库并延后物理清理至 on_commit。
    # -----------------------------------------------------------------------
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=owner_id)
            quota = StorageQuota.objects.select_for_update().get(pk=quota_id)
            upload = (
                UploadSession.objects.select_related("requirement__activity", "owner", "submission")
                .select_for_update()
                .get(pk=upload_pk)
            )
            if upload.status != UploadSession.Status.COMPLETING:
                raise Conflict("该上传任务状态已变更，无法完成合并。")
            ensure_activity_open(upload.requirement.activity, upload.owner)
            if upload.expires_at and upload.expires_at <= timezone.now():
                raise Conflict("该上传任务已过期，请重新发起上传。")
            ensure_submission_editable_by_student(upload.submission)

            requirement = upload.requirement
            replace_all = requirement.max_count == 1 or requirement.code in SINGLE_FILE_MATERIAL_CODES
            scope_q = _submission_upload_scope_q(
                owner_id=upload.owner_id,
                activity_id=requirement.activity_id,
                submission=upload.submission,
            )
            siblings = list(
                UploadSession.objects.select_for_update()
                .filter(scope_q, owner=upload.owner, requirement=requirement)
                .exclude(pk=upload.pk)
                .exclude(status=UploadSession.Status.ABORTED)
                .order_by("created_at")
            )
            if not replace_all:
                if len(siblings) + 1 > requirement.max_count:
                    raise Conflict(f"此材料类别已达到文件数量上限（最多 {requirement.max_count} 份）。")
                assert_extension_count_available(siblings, requirement, extension)
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

            projected_used = max(0, quota.used_bytes - release_used) + written
            # 本次自己的预留（declared_bytes）在合并完成时转为已用。
            projected_reserved = max(0, quota.reserved_bytes - upload.declared_bytes - release_reserved)
            if projected_used + projected_reserved > quota.limit_bytes:
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
                old_keys_to_delete.append(str(old.object_key or ""))

            old_tmp_key = str(upload.object_key or "")
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

            cleanup_keys = tuple(old_keys_to_delete)
            tmp_dir_snapshot = tmp_dir
            old_tmp_key_snapshot = old_tmp_key

            def _after_commit():
                # 100% 延后至 DB 提交成功后删除分片目录与旧物理文件，回滚绝不丢原文件与分片。
                shutil.rmtree(tmp_dir_snapshot, ignore_errors=True)
                if old_tmp_key_snapshot.startswith(TMP_PREFIX + "/"):
                    cleanup_upload_disk_artifacts((old_tmp_key_snapshot,))
                if cleanup_keys:
                    cleanup_upload_disk_artifacts(cleanup_keys)
                _schedule_validation(asset)

            transaction.on_commit(_after_commit)
    except Exception:
        # 提交失败/回滚：仅删除本次新建的合并文件并恢复 UPLOADING，旧文件与分片目录毫发无损。
        final_path.unlink(missing_ok=True)
        _restore_uploading_if_completing(upload_pk)
        raise

    return upload, asset, replaced_ids
