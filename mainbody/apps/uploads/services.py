"""Upload services: local streaming storage, quota management, overwrite cleanup, and mandatory material checks.

Lock order: user -> quota -> upload.
"""
import hashlib
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
import uuid
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin, is_target_in_reviewer_scope, require_student
from apps.activities.models import MaterialRequirement
from apps.activities.selectors import ensure_activity_open
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError, Forbidden, InsufficientStorage, QuotaExceeded
from .models import FileAsset, StorageQuota, UploadSession
from .policies import (
    SINGLE_FILE_MATERIAL_CODES,
    build_object_key,
    effective_max_file_bytes,
    extension_max_count,
    seeded_material_rules,
    validate_metadata,
    validate_stored_file_content,
    validate_uploaded_file,
)

logger = logging.getLogger(__name__)

ACTIVE_STATES = [
    UploadSession.Status.RESERVED,
    UploadSession.Status.UPLOADING,
    UploadSession.Status.COMPLETING,
    UploadSession.Status.ABORTING,
]

STREAM_CHUNK_BYTES = 64 * 1024
DEFAULT_MIN_FREE_DISK_BYTES = 5 * 1024 * 1024 * 1024


def ensure_disk_space_available(required_bytes: int = 0, target_dir: Path | None = None) -> int:
    """Verify target filesystem retains at least UPLOAD_MIN_FREE_DISK_BYTES (default 5 GiB) after writing.

    Returns current free bytes on success; logs ERROR and raises InsufficientStorage (HTTP 503) when below threshold.
    """
    min_free = int(getattr(settings, "UPLOAD_MIN_FREE_DISK_BYTES", DEFAULT_MIN_FREE_DISK_BYTES) or 0)
    if min_free <= 0:
        return 0

    probe = Path(target_dir or settings.MEDIA_ROOT)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    needed = max(0, int(required_bytes or 0))
    if usage.free < min_free or (usage.free - needed) < min_free:
        logger.error(
            "disk_space_low",
            extra={
                "path": str(probe),
                "free_bytes": usage.free,
                "required_bytes": needed,
                "min_free_bytes": min_free,
            },
        )
        raise InsufficientStorage(
            "服务器存储空间不足（剩余空间低于安全阈值），已暂停文件写入，请联系管理员清理或扩容。"
        )
    return usage.free


def ensure_standard_material_requirements(activity, *, sync_existing: bool = False) -> dict[str, MaterialRequirement]:
    """Ensure the five standard material requirements exist on the given activity.

    默认只读查询已有 5 条规则（全部存在时 0 写 SQL，避免高并发 GET 页面争抢 PostgreSQL 行锁）；
    仅当数据库确实缺少记录，或调用方显式传 sync_existing=True（如管理员保存活动）时才写入。
    """
    rules = list(seeded_material_rules())
    expected_codes = [code for code, _ in rules]
    existing = {
        req.code: req
        for req in MaterialRequirement.objects.filter(activity=activity, code__in=expected_codes)
    }
    if not sync_existing and len(existing) == len(expected_codes):
        return existing

    result: dict[str, MaterialRequirement] = {}
    for code, spec in rules:
        if not sync_existing and code in existing:
            result[code] = existing[code]
            continue
        defaults = {
            "title": spec["title"],
            "required": spec["required"],
            "allowed_extensions": list(spec["allowed_extensions"]),
            "max_file_bytes": spec["max_file_bytes"],
            "min_count": spec["min_count"],
            "max_count": spec["max_count"],
        }
        if sync_existing:
            req, _ = MaterialRequirement.objects.update_or_create(
                activity=activity,
                code=code,
                defaults=defaults,
            )
        else:
            try:
                req, _ = MaterialRequirement.objects.get_or_create(
                    activity=activity,
                    code=code,
                    defaults=defaults,
                )
            except IntegrityError:
                req = MaterialRequirement.objects.get(activity=activity, code=code)
        result[code] = req
    return result


configure_standard_requirements = ensure_standard_material_requirements
init_standard_requirements = ensure_standard_material_requirements


def assert_extension_count_available(upload_sessions, rule, extension: str) -> None:
    """Enforce a per-extension count cap (other_materials keeps at most 5 .zip packs).

    Counts existing sessions of the same category whose original name carries the
    extension; callers pass a queryset (reservation path) or a locked list (completion path).
    """
    cap = extension_max_count(rule.code, extension)
    if not cap:
        return
    queryset = upload_sessions
    if isinstance(upload_sessions, (list, tuple)):
        current = sum(1 for up in upload_sessions if str(up.original_name).lower().endswith(extension))
    else:
        current = queryset.filter(original_name__iendswith=extension).count()
    if current >= cap:
        raise Conflict(f"此材料类别中 {extension} 文件最多 {cap} 份，请先删除已有文件或改用其他格式。")


def resolve_file_disk_path(object_key_or_upload) -> Path:
    """Resolve the absolute local disk path inside settings.MEDIA_ROOT for an UploadSession or object_key."""
    object_key = (
        object_key_or_upload.object_key
        if hasattr(object_key_or_upload, "object_key")
        else str(object_key_or_upload)
    )
    media_root = Path(settings.MEDIA_ROOT).resolve()
    full_path = (media_root / object_key).resolve()
    if media_root not in full_path.parents and full_path != media_root:
        raise DomainError("非法的文件存储路径。")
    return full_path


def _remove_physical_file(object_key: str) -> None:
    if not object_key:
        return
    try:
        disk_path = resolve_file_disk_path(object_key)
        os.remove(disk_path)
    except FileNotFoundError:
        pass


def remove_upload_object(object_key: str) -> None:
    """Public wrapper: remove one object under MEDIA_ROOT by key (no-op when already gone)."""
    _remove_physical_file(object_key)


def remove_chunk_staging_dir(upload) -> None:
    """Drop MEDIA_ROOT/tmp/<upload_id>/ for a chunked session (idempotent, never raises)."""
    object_key = str(getattr(upload, "object_key", "") or "")
    if not object_key.startswith("tmp/"):
        return
    try:
        shutil.rmtree(resolve_file_disk_path(object_key), ignore_errors=True)
    except DomainError:
        return


def cleanup_upload_disk_artifacts(object_keys) -> None:
    """Best-effort post-commit removal of stored files and tmp/<upload_id>/ directories."""
    for raw_key in object_keys or ():
        key_text = str(raw_key or "").strip()
        if not key_text:
            continue
        try:
            if key_text.startswith("tmp/"):
                shutil.rmtree(resolve_file_disk_path(key_text), ignore_errors=True)
            else:
                _remove_physical_file(key_text)
        except Exception:  # pragma: no cover - post-commit cleanup must never break caller
            logger.warning("upload_disk_cleanup_failed", extra={"object_key": key_text}, exc_info=True)


def _stream_file_atomically(file_obj, disk_path: Path) -> tuple[int, str]:
    """Stream file_obj to a sibling temp file, fsync, and os.replace into disk_path."""
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = disk_path.parent / f".{uuid.uuid4().hex}.direct.tmp"
    hasher = hashlib.sha256()
    written_bytes = 0
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    try:
        with open(staging_path, "wb") as dst:
            if hasattr(file_obj, "chunks"):
                for chunk in file_obj.chunks(chunk_size=STREAM_CHUNK_BYTES):
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    dst.write(chunk)
                    hasher.update(chunk)
                    written_bytes += len(chunk)
            else:
                while True:
                    chunk = file_obj.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    dst.write(chunk)
                    hasher.update(chunk)
                    written_bytes += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(staging_path, disk_path)
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise
    return written_bytes, hasher.hexdigest()


def resolve_upload_submission(*, owner, activity, submission_id=None):
    """Resolve and verify the Submission an upload belongs to (optional for legacy callers)."""
    from apps.submissions.models import Submission

    if submission_id is not None and str(submission_id).strip() != "":
        try:
            sub_uuid = uuid.UUID(str(submission_id).strip())
        except ValueError:
            raise DomainError("submission_id 必须为有效 UUID。") from None
        sub = Submission.objects.filter(pk=sub_uuid, owner=owner, activity=activity).first()
        if sub is None:
            raise DomainError("目标申报记录不存在或无权操作。")
        return sub
    return (
        Submission.objects.filter(owner=owner, activity=activity)
        .order_by("-updated_at", "-created_at")
        .first()
    )


def _submission_upload_scope_q(*, owner_id, activity_id, submission):
    """Build queryset filter scoping uploads to one submission (legacy null fallback only when unique)."""
    from apps.submissions.models import Submission

    if submission is None:
        return Q(submission__isnull=True)
    sub_count = Submission.objects.filter(owner_id=owner_id, activity_id=activity_id).count()
    if sub_count <= 1:
        return Q(submission_id=submission.id) | Q(submission__isnull=True)
    return Q(submission_id=submission.id)


def uploads_for_submission(submission, *, requirement_code: str | None = None) -> list[UploadSession]:
    """Return valid completed UploadSessions belonging exclusively to the given Submission."""
    if submission is None:
        return []
    scope_q = _submission_upload_scope_q(
        owner_id=submission.owner_id,
        activity_id=submission.activity_id,
        submission=submission,
    )
    qs = (
        UploadSession.objects.select_related("requirement", "fileasset")
        .filter(
            scope_q,
            owner_id=submission.owner_id,
            requirement__activity_id=submission.activity_id,
            status=UploadSession.Status.COMPLETE,
            fileasset__status=FileAsset.Status.VALID,
        )
        .order_by("created_at", "id")
    )
    if requirement_code:
        qs = qs.filter(requirement__code=requirement_code)
    return list(qs)


def map_uploads_by_submission(
    submissions,
    *,
    activity=None,
    requirement_code: str | None = None,
) -> dict[uuid.UUID, list[UploadSession]]:
    """Batch-fetch valid completed uploads keyed by submission.id without N+1 queries."""
    from apps.submissions.models import Submission

    sub_list = list(submissions or [])
    result: dict[uuid.UUID, list[UploadSession]] = {sub.id: [] for sub in sub_list}
    if not sub_list:
        return result

    owner_ids = {sub.owner_id for sub in sub_list}
    activity_ids = {sub.activity_id for sub in sub_list}
    if activity is not None:
        activity_ids.add(getattr(activity, "id", activity))

    qs = (
        UploadSession.objects.select_related("requirement", "fileasset")
        .filter(
            owner_id__in=owner_ids,
            requirement__activity_id__in=activity_ids,
            status=UploadSession.Status.COMPLETE,
            fileasset__status=FileAsset.Status.VALID,
        )
        .order_by("created_at", "id")
    )
    if requirement_code:
        qs = qs.filter(requirement__code=requirement_code)

    uploads = list(qs)
    legacy_null_uploads = [up for up in uploads if up.submission_id is None]
    single_sub_by_pair: dict[tuple, uuid.UUID] = {}
    if legacy_null_uploads:
        count_rows = (
            Submission.objects.filter(owner_id__in=owner_ids, activity_id__in=activity_ids)
            .values("owner_id", "activity_id")
            .annotate(total=Count("id"))
        )
        counts_by_pair = {(row["owner_id"], row["activity_id"]): row["total"] for row in count_rows}
        for sub in sub_list:
            pair = (sub.owner_id, sub.activity_id)
            if counts_by_pair.get(pair, 0) <= 1:
                single_sub_by_pair[pair] = sub.id

    for up in uploads:
        if up.submission_id in result:
            result[up.submission_id].append(up)
        elif up.submission_id is None:
            fallback_sub_id = single_sub_by_pair.get((up.owner_id, up.requirement.activity_id))
            if fallback_sub_id is not None:
                result[fallback_sub_id].append(up)
    return result


@transaction.atomic
def reserve_upload(*, actor, requirement_id, filename, declared_bytes, request_key, submission_id=None):
    require_student(actor)
    try:
        request_key = uuid.UUID(str(request_key))
    except ValueError:
        raise DomainError("request_key 必须为 UUID。") from None
    # One user's competing reservations serialize; different users do not share this lock.
    owner = User.objects.select_for_update().get(pk=actor.pk)
    require_student(owner)
    existing = UploadSession.objects.filter(owner=owner, request_key=request_key).first()
    if existing:
        if (str(existing.requirement_id), existing.original_name, existing.declared_bytes) != (
            str(requirement_id),
            filename,
            declared_bytes,
        ):
            raise Conflict("同一幂等键不能用于不同的文件。")
        return existing
    rule = MaterialRequirement.objects.select_related("activity").get(pk=requirement_id)
    ensure_activity_open(rule.activity, owner)
    target_submission = resolve_upload_submission(
        owner=owner,
        activity=rule.activity,
        submission_id=submission_id,
    )
    from apps.submissions.services import ensure_submission_editable_by_student

    ensure_submission_editable_by_student(target_submission)
    extension = validate_metadata(filename=filename, declared_bytes=declared_bytes, requirement=rule)
    if UploadSession.objects.filter(owner=owner, status__in=ACTIVE_STATES).count() >= settings.MAX_ACTIVE_UPLOADS_PER_USER:
        raise Conflict("同时上传任务过多，请先完成或取消已有任务。")
    scope_q = _submission_upload_scope_q(
        owner_id=owner.pk,
        activity_id=rule.activity_id,
        submission=target_submission,
    )
    siblings = UploadSession.objects.filter(scope_q, owner=owner, requirement=rule).exclude(
        status=UploadSession.Status.ABORTED
    )
    if siblings.count() >= rule.max_count:
        raise Conflict("此材料类别已达到文件数量限制。")
    assert_extension_count_available(siblings, rule, extension)
    quota, _ = StorageQuota.objects.get_or_create(
        owner=owner,
        activity=rule.activity,
        defaults={"limit_bytes": rule.activity.quota_bytes_per_student},
    )
    quota = StorageQuota.objects.select_for_update().get(pk=quota.pk)
    if quota.used_bytes + quota.reserved_bytes + declared_bytes > quota.limit_bytes:
        raise QuotaExceeded(
            "该活动存储额度不足：已占用 "
            f"{quota.used_bytes + quota.reserved_bytes:,} 字节 / 上限 {quota.limit_bytes:,} 字节，"
            f"本次还需 {declared_bytes:,} 字节。"
        )
    quota.reserved_bytes += declared_bytes
    quota.save(update_fields=["reserved_bytes", "updated_at"])
    return UploadSession.objects.create(
        owner=owner,
        submission=target_submission,
        requirement=rule,
        quota=quota,
        request_key=request_key,
        original_name=filename,
        declared_bytes=declared_bytes,
        object_key=f"activities/{rule.activity_id}/{uuid.uuid4().hex}",
        expires_at=timezone.now() + timedelta(days=3),
    )


@transaction.atomic
def cancel_unstarted_reservation(*, actor, upload_id):
    """Only a purely local reservation can be released without a cloud API call."""
    require_student(actor)
    User.objects.select_for_update().get(pk=actor.pk)
    upload = UploadSession.objects.get(pk=upload_id, owner=actor)
    quota = StorageQuota.objects.select_for_update().get(pk=upload.quota_id)
    upload = UploadSession.objects.select_for_update().get(pk=upload.pk)
    if upload.status == UploadSession.Status.ABORTED:
        return upload
    if upload.status != UploadSession.Status.RESERVED or upload.storage_upload_id:
        raise Conflict("任务已涉及远程存储，必须先确认云端取消。")
    quota.reserved_bytes -= upload.declared_bytes
    quota.save(update_fields=["reserved_bytes", "updated_at"])
    upload.status = UploadSession.Status.ABORTED
    upload.save(update_fields=["status", "updated_at"])
    return upload


def direct_upload(*, actor, requirement_id, file_obj, submission_id=None, replace_upload_id=None, request_key=None):
    """Stream an uploaded file to MEDIA_ROOT outside DB transactions, then settle quota/records in a short transaction.

    All physical deletions of superseded files are deferred to transaction.on_commit so a DB rollback
    can never destroy previously committed files.
    """
    require_student(actor)
    if file_obj is None:
        raise DomainError("未提供上传文件。")

    has_client_request_key = request_key is not None and str(request_key).strip() != ""
    if has_client_request_key:
        try:
            resolved_request_key = uuid.UUID(str(request_key))
        except ValueError:
            raise DomainError("request_key 必须为 UUID。") from None
    else:
        resolved_request_key = uuid.uuid4()

    try:
        req_uuid = uuid.UUID(str(requirement_id))
    except ValueError:
        raise DomainError("requirement_id 必须为 UUID。") from None

    rep_uuid = None
    if replace_upload_id is not None and str(replace_upload_id).strip() != "":
        try:
            rep_uuid = uuid.UUID(str(replace_upload_id))
        except ValueError:
            raise DomainError("replace_upload_id 必须为 UUID。") from None

    def _resolve_idempotent_direct_upload(owner_obj, expected_filename: str):
        existing = (
            UploadSession.objects.select_related("requirement", "fileasset")
            .filter(owner=owner_obj, request_key=resolved_request_key)
            .first()
        )
        if existing is None:
            return None
        if str(existing.requirement_id) != str(req_uuid) or existing.original_name != expected_filename:
            raise Conflict("同一幂等键不能用于不同的文件。")
        if existing.status == UploadSession.Status.COMPLETE:
            existing_asset = getattr(existing, "fileasset", None)
            if existing_asset is not None and existing_asset.status == FileAsset.Status.VALID:
                return existing, existing_asset, []
        raise Conflict("该幂等键已被其他上传任务占用。")

    # 阶段 1（事务外）：只读预检 -> (owner, request_key) 幂等快查 -> 原子流式落盘 -> 深度内容校验。
    rule = MaterialRequirement.objects.select_related("activity").get(pk=req_uuid)
    ensure_activity_open(rule.activity, actor)

    from apps.submissions.services import ensure_submission_editable_by_student

    precheck_submission = resolve_upload_submission(
        owner=actor,
        activity=rule.activity,
        submission_id=submission_id,
    )
    ensure_submission_editable_by_student(precheck_submission)

    filename, extension, declared_bytes, detected_type = validate_uploaded_file(
        file_obj,
        requirement=rule,
    )

    if has_client_request_key:
        idempotent_hit = _resolve_idempotent_direct_upload(actor, filename)
        if idempotent_hit is not None:
            return idempotent_hit

    ensure_disk_space_available(required_bytes=declared_bytes)

    # 物理路径只由 build_object_key 生成：<activity_id>/<student_number>/<material_type>/<uuid4().hex><ext>，
    # original_filename 只进数据库 original_name 字段，永不参与路径拼接。
    object_key = build_object_key(
        activity_id=rule.activity_id,
        student_number=actor.username,
        material_code=rule.code,
        extension=extension,
    )
    disk_path = resolve_file_disk_path(object_key)
    written_bytes, checksum_value = _stream_file_atomically(file_obj, disk_path)

    max_allowed = effective_max_file_bytes(rule.code, extension, rule.max_file_bytes)
    if written_bytes <= 0 or written_bytes > max_allowed:
        disk_path.unlink(missing_ok=True)
        raise DomainError("文件实际写入字节数超出材料类别允许范围。")

    # 第三层内容校验（容器结构 / ZipSlip / Pillow / 可选 ffprobe）：失败必须把新建落盘文件清掉，
    # 绝不动任何已存在的旧文件。
    try:
        detected_type = validate_stored_file_content(
            disk_path,
            extension,
            declared_content_type=getattr(file_obj, "content_type", ""),
        )
    except Exception:
        disk_path.unlink(missing_ok=True)
        raise

    # 阶段 2（短事务）：加锁复查幂等键、用 written_bytes 复核配额与份数、写库并延后物理删除至 on_commit。
    try:
        with transaction.atomic():
            owner = User.objects.select_for_update().get(pk=actor.pk)
            require_student(owner)

            if has_client_request_key:
                locked_hit = _resolve_idempotent_direct_upload(owner, filename)
                if locked_hit is not None:
                    disk_path.unlink(missing_ok=True)
                    return locked_hit

            rule = MaterialRequirement.objects.select_related("activity").get(pk=req_uuid)
            ensure_activity_open(rule.activity, owner)

            target_submission = resolve_upload_submission(
                owner=owner,
                activity=rule.activity,
                submission_id=submission_id,
            )
            ensure_submission_editable_by_student(target_submission)

            quota, _ = StorageQuota.objects.get_or_create(
                owner=owner,
                activity=rule.activity,
                defaults={"limit_bytes": rule.activity.quota_bytes_per_student},
            )
            quota = StorageQuota.objects.select_for_update().get(pk=quota.pk)

            scope_q = _submission_upload_scope_q(
                owner_id=owner.pk,
                activity_id=rule.activity_id,
                submission=target_submission,
            )
            active_uploads = list(
                UploadSession.objects.select_for_update()
                .filter(scope_q, owner=owner, requirement=rule)
                .exclude(status=UploadSession.Status.ABORTED)
                .order_by("created_at")
            )

            is_single_file_rule = rule.max_count == 1 or rule.code in SINGLE_FILE_MATERIAL_CODES
            uploads_to_replace: list[UploadSession] = []

            if is_single_file_rule:
                uploads_to_replace = active_uploads
            elif rep_uuid is not None:
                matched = [u for u in active_uploads if u.id == rep_uuid]
                if not matched:
                    raise UploadSession.DoesNotExist("待替换的原文件记录不存在。")
                remaining = [u for u in active_uploads if u.id != rep_uuid]
                assert_extension_count_available(remaining, rule, extension)
                uploads_to_replace = matched
            else:
                if len(active_uploads) >= rule.max_count:
                    raise Conflict(f"此材料类别已达到文件数量上限（最多 {rule.max_count} 份）。")
                assert_extension_count_available(active_uploads, rule, extension)

            release_used_bytes = 0
            release_reserved_bytes = 0
            old_assets_by_upload_id: dict[uuid.UUID, FileAsset] = {}
            if uploads_to_replace:
                for asset in FileAsset.objects.select_for_update().filter(upload__in=uploads_to_replace):
                    old_assets_by_upload_id[asset.upload_id] = asset
                for old_up in uploads_to_replace:
                    if old_up.status == UploadSession.Status.COMPLETE:
                        old_asset = old_assets_by_upload_id.get(old_up.id)
                        release_used_bytes += (
                            old_asset.actual_bytes if old_asset is not None else old_up.declared_bytes
                        )
                    else:
                        release_reserved_bytes += old_up.declared_bytes

            projected_used = max(0, quota.used_bytes - release_used_bytes) + written_bytes
            projected_reserved = max(0, quota.reserved_bytes - release_reserved_bytes)
            if projected_used + projected_reserved > quota.limit_bytes:
                raise QuotaExceeded(
                    "该活动存储额度不足：本次写入后需 "
                    f"{projected_used + projected_reserved:,} 字节 / 上限 {quota.limit_bytes:,} 字节。"
                )

            replaced_ids: list[uuid.UUID] = []
            old_object_keys_to_delete: list[str] = []
            for old_up in uploads_to_replace:
                old_up.status = UploadSession.Status.ABORTED
                old_up.save(update_fields=["status", "updated_at"])
                old_asset = old_assets_by_upload_id.get(old_up.id)
                if old_asset is not None and old_asset.status != FileAsset.Status.INVALID:
                    old_asset.status = FileAsset.Status.INVALID
                    old_asset.save(update_fields=["status", "updated_at"])
                replaced_ids.append(old_up.id)
                old_object_keys_to_delete.append(str(old_up.object_key or ""))

            quota.used_bytes = projected_used
            quota.reserved_bytes = projected_reserved
            quota.save(update_fields=["used_bytes", "reserved_bytes", "updated_at"])

            upload_session = UploadSession.objects.create(
                owner=owner,
                submission=target_submission,
                requirement=rule,
                quota=quota,
                request_key=resolved_request_key,
                object_key=object_key,
                original_name=filename,
                declared_bytes=written_bytes,
                status=UploadSession.Status.COMPLETE,
                expires_at=timezone.now() + timedelta(days=365),
            )

            file_asset = FileAsset.objects.create(
                upload=upload_session,
                actual_bytes=written_bytes,
                detected_type=detected_type,
                checksum_algorithm="sha256",
                checksum_value=checksum_value,
                status=FileAsset.Status.VALID,
            )

            record(
                actor=owner,
                action="upload.direct_completed",
                target=upload_session,
                metadata={
                    "requirement_code": rule.code,
                    "original_name": filename,
                    "actual_bytes": written_bytes,
                    "replaced_upload_ids": [str(uid) for uid in replaced_ids],
                },
            )

            if old_object_keys_to_delete:
                keys_snapshot = tuple(old_object_keys_to_delete)
                transaction.on_commit(lambda keys=keys_snapshot: cleanup_upload_disk_artifacts(keys))
    except Exception:
        # 仅清理本次新建的落盘文件，绝不动任何已提交的旧文件。
        disk_path.unlink(missing_ok=True)
        raise

    return upload_session, file_asset, replaced_ids


def _is_activity_reviewer(actor, activity_id, owner) -> bool:
    """团支书（初审人员）是否有权访问/处理本班同学的该份材料。"""
    return is_target_in_reviewer_scope(actor, owner, activity_id=activity_id)


@transaction.atomic
def delete_uploaded_file(*, actor, upload_id):
    """删除附件：本人可删；该活动的管理员/本班团支书也可删（纠错用），全部落审计。

    归属校验顺序：先取上传记录 -> 再判"本人 or 学院管理员 or 本班团支书" -> 才动配额与磁盘。
    非本人且非管理端一律 Forbidden，绝不因为"记录存在"就放行。
    """
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise Forbidden("请先登录。")

    target_upload = (
        UploadSession.objects.select_related("requirement__activity", "owner")
        .filter(pk=upload_id)
        .first()
    )
    if target_upload is None:
        raise UploadSession.DoesNotExist("文件不存在。")

    acting_admin = is_college_admin(actor)
    acting_reviewer = _is_activity_reviewer(
        actor, target_upload.requirement.activity_id, target_upload.owner
    )
    if target_upload.owner_id != actor.pk and not (acting_admin or acting_reviewer):
        raise Forbidden("无权删除他人的上传文件。")
    managed = acting_admin or acting_reviewer

    owner = User.objects.select_for_update().get(pk=target_upload.owner_id)
    require_student(owner)
    upload = UploadSession.objects.select_related("requirement__activity").select_for_update().get(
        pk=target_upload.pk,
    )

    if not managed:
        # 学生自助删除受活动窗口与审核锁约束；管理端纠错不受限制。
        ensure_activity_open(upload.requirement.activity, owner)
        from apps.submissions.services import ensure_submission_editable_by_student

        target_sub = upload.submission or resolve_upload_submission(
            owner=owner,
            activity=upload.requirement.activity,
        )
        ensure_submission_editable_by_student(target_sub)

    quota = StorageQuota.objects.select_for_update().get(pk=upload.quota_id)
    asset = FileAsset.objects.select_for_update().filter(upload=upload).first()

    if upload.status != UploadSession.Status.ABORTED:
        if upload.status == UploadSession.Status.COMPLETE:
            bytes_to_release = asset.actual_bytes if asset is not None else upload.declared_bytes
            quota.used_bytes = max(0, quota.used_bytes - bytes_to_release)
        else:
            quota.reserved_bytes = max(0, quota.reserved_bytes - upload.declared_bytes)
        quota.save(update_fields=["used_bytes", "reserved_bytes", "updated_at"])
        upload.status = UploadSession.Status.ABORTED
        upload.save(update_fields=["status", "updated_at"])

    if asset is not None and asset.status != FileAsset.Status.INVALID:
        asset.status = FileAsset.Status.INVALID
        asset.save(update_fields=["status", "updated_at"])

    # 物理文件与分片临时目录的删除统一延后至数据库提交之后，防止事务回滚造成文件永久丢失。
    cleanup_key = str(upload.object_key or "")
    if cleanup_key:
        transaction.on_commit(lambda key=cleanup_key: cleanup_upload_disk_artifacts((key,)))
    record(
        actor=actor,
        action="upload.deleted_by_staff" if managed else "upload.deleted",
        target=upload,
        metadata={
            "owner_id": str(upload.owner_id),
            "object_key": upload.object_key,
            "requirement_code": upload.requirement.code,
            "managed_by_staff": managed,
        },
    )
    return upload


def validate_declaration_materials(*, actor, activity, submission=None) -> None:
    """校验假期前立项申报必交附件：仅要求申报表（application_form）1 份有效 .docx 文件。"""
    if submission is not None:
        valid_uploads = uploads_for_submission(submission, requirement_code="application_form")
    else:
        valid_uploads = list(
            UploadSession.objects.select_related("requirement", "fileasset").filter(
                owner=actor,
                requirement__activity=activity,
                requirement__code="application_form",
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
        )
    app_form_count = sum(1 for up in valid_uploads if resolve_file_disk_path(up.object_key).exists())
    if app_form_count != 1:
        raise DomainError(f"项目申报必交附件【申报表】需上传 1 份 .docx 文件（当前有效份数：{app_form_count}）。")


def validate_required_materials(*, actor, activity, submission=None) -> None:
    """Verify all mandatory materials (application_form=1, practice_report=1, scene_photos=3..5) are validly uploaded."""
    if submission is not None:
        valid_uploads = uploads_for_submission(submission)
    else:
        valid_uploads = list(
            UploadSession.objects.select_related("requirement", "fileasset").filter(
                owner=actor,
                requirement__activity=activity,
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
        )
    counts_by_code: dict[str, int] = {}
    for up in valid_uploads:
        disk_path = resolve_file_disk_path(up.object_key)
        if not disk_path.exists():
            continue
        code = up.requirement.code
        counts_by_code[code] = counts_by_code.get(code, 0) + 1

    # Hard check on the 3 mandatory standard categories:
    # 1) 申报表 (application_form): 1份
    app_form_count = counts_by_code.get("application_form", 0)
    if app_form_count != 1:
        raise DomainError(f"必交材料【申报表（application_form）】需上传 1 份 .docx 文件（当前有效份数：{app_form_count}）。")

    # 2) 实践报告 (practice_report): 1份
    report_count = counts_by_code.get("practice_report", 0)
    if report_count != 1:
        raise DomainError(f"必交材料【实践报告（practice_report）】需上传 1 份 .pdf 文件（当前有效份数：{report_count}）。")

    # 3) 现场照片 (scene_photos): 3~5张
    photo_count = counts_by_code.get("scene_photos", 0)
    if not (3 <= photo_count <= 5):
        raise DomainError(f"必交材料【现场照片（scene_photos）】需上传 3～5 张图片（当前有效张数：{photo_count}）。")

    # Also check any additional required MaterialRequirement rows configured on the activity
    for req in MaterialRequirement.objects.filter(activity=activity, required=True):
        c = counts_by_code.get(req.code, 0)
        min_required = max(1, req.min_count)
        if not (min_required <= c <= req.max_count):
            raise DomainError(
                f"必交材料【{req.title}（{req.code}）】上传数量不符合要求（需 {min_required}～{req.max_count} 份，当前：{c} 份）。"
            )


def get_accessible_upload_for_user(*, actor, upload_or_asset_id) -> tuple[UploadSession, FileAsset, Path]:
    """Verify user permission (owner, activity reviewer, or college_admin) and return (UploadSession, FileAsset, Path)."""
    if not actor.is_authenticated or not actor.is_active:
        raise Forbidden("请先登录。")

    try:
        target_uuid = uuid.UUID(str(upload_or_asset_id))
    except ValueError:
        raise DomainError("文件 ID 必须为有效 UUID。") from None

    upload = (
        UploadSession.objects.select_related("requirement__activity", "owner", "fileasset", "submission")
        .filter(pk=target_uuid)
        .first()
    )
    if upload is None:
        asset_match = (
            FileAsset.objects.select_related("upload__requirement__activity", "upload__owner", "upload__submission")
            .filter(pk=target_uuid)
            .first()
        )
        if asset_match is not None:
            upload = asset_match.upload

    if upload is None:
        raise UploadSession.DoesNotExist("文件不存在。")

    # Permission check: owner, teammate in the same team submission, reviewer for this activity, or college admin
    has_access = False
    if upload.owner_id == actor.id:
        has_access = True
    elif is_college_admin(actor):
        has_access = True
    elif _is_activity_reviewer(actor, upload.requirement.activity_id, upload.owner):
        has_access = True
    else:
        from apps.submissions.services import find_team_membership_submissions, is_member_of_submission

        if upload.submission is not None:
            has_access = is_member_of_submission(upload.submission, actor.username)
        else:
            team_subs = find_team_membership_submissions(
                activity=upload.requirement.activity,
                student_number=actor.username,
            )
            if any(sub.owner_id == upload.owner_id for sub in team_subs):
                has_access = True

    if not has_access:
        raise Forbidden("无权访问此文件。")

    if upload.status != UploadSession.Status.COMPLETE:
        raise UploadSession.DoesNotExist("该文件已被覆盖或作废。")

    asset = getattr(upload, "fileasset", None) or FileAsset.objects.filter(upload=upload).first()
    if asset is None or asset.status != FileAsset.Status.VALID:
        raise UploadSession.DoesNotExist("该文件未通过校验或已失效。")

    disk_path = resolve_file_disk_path(upload.object_key)
    if not disk_path.exists():
        raise UploadSession.DoesNotExist("文件在磁盘上不存在。")

    return upload, asset, disk_path
