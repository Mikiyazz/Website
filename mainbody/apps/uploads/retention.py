"""过期上传任务的回收：释放额度预留、删除分片目录、把会话置为 ABORTED。

为什么需要它：分片上传把 300MB 拆成 60 个 5MB 请求，用户中途关页面、切网络或直接放弃都很常见。
这些任务之后再没有请求会碰到，于是 `MEDIA_ROOT/tmp/<upload_id>/` 一直占着磁盘，
`StorageQuota.reserved_bytes` 也一直挂着不放，最后表现为"额度莫名不足"。

本模块提供可重复执行（幂等）的清扫函数，给两处调用：
- `manage.py purge_stale_uploads`：人工或 cron 触发，支持 --dry-run；
- `manage.py dispatch_outbox` 常驻循环：默认每小时顺带扫一次。

边界：只处理 ACTIVE_STATES（reserved/uploading/completing/aborting）且 `expires_at` 已过的会话；
已完成（complete）的材料属于正常数据，expires_at 再久也不动它 —— 那是保留策略，不是垃圾回收。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.audit.services import record
from apps.common.errors import DomainError
from apps.uploads.models import FileAsset, StorageQuota, UploadSession
from apps.uploads.services import (
    ACTIVE_STATES,
    remove_chunk_staging_dir,
    remove_upload_object,
    resolve_file_disk_path,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 200


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


@dataclass
class PurgeResult:
    """一次清扫的结果。dry_run 时只统计"将会发生什么"，不动数据库也不动磁盘。"""

    scanned: int = 0
    purged: int = 0
    skipped: int = 0
    failed: int = 0
    released_bytes: int = 0
    removed_staging_dirs: int = 0
    dry_run: bool = False
    upload_ids: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        prefix = "预演（未改动任何数据）" if self.dry_run else "已回收"
        parts = [
            f"扫描 {self.scanned} 条",
            f"{prefix} {self.purged} 条",
            f"跳过 {self.skipped} 条",
        ]
        if self.failed:
            parts.append(f"失败 {self.failed} 条")
        parts.append(f"释放额度 {_format_bytes(self.released_bytes)}")
        parts.append(f"删除分片目录 {self.removed_staging_dirs} 个")
        return "，".join(parts) + "。"

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "purged": self.purged,
            "skipped": self.skipped,
            "failed": self.failed,
            "released_bytes": self.released_bytes,
            "removed_staging_dirs": self.removed_staging_dirs,
            "dry_run": self.dry_run,
            "upload_ids": list(self.upload_ids),
            "failures": [{"upload_id": uid, "error": err} for uid, err in self.failures],
        }


def stale_upload_queryset(*, now=None, grace: timedelta = timedelta(0)):
    """Active upload sessions whose expires_at already passed (grace 可再往后推一段)。"""
    cutoff = (now or timezone.now()) - grace
    return UploadSession.objects.filter(status__in=ACTIVE_STATES, expires_at__lt=cutoff)


def purge_stale_uploads(
    *,
    now=None,
    grace: timedelta = timedelta(0),
    limit: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> PurgeResult:
    """Reclaim abandoned upload sessions, newest-expired first.

    Idempotent: a purged session becomes ABORTED, so a second run finds nothing to do.
    A single broken row must not stop the sweep — failures are collected and reported.
    """
    now = now or timezone.now()
    result = PurgeResult(dry_run=dry_run)
    batch = max(1, int(limit))
    upload_ids = list(
        stale_upload_queryset(now=now, grace=grace)
        .order_by("expires_at")
        .values_list("pk", flat=True)[:batch]
    )
    result.scanned = len(upload_ids)

    for upload_id in upload_ids:
        try:
            outcome = _purge_one(upload_id, now=now, grace=grace, dry_run=dry_run)
        except Exception as exc:  # 单条失败不影响整批，下一轮会再试
            result.failed += 1
            result.failures.append((str(upload_id), type(exc).__name__))
            logger.warning(
                "stale_upload_purge_failed",
                extra={"upload_id": str(upload_id), "error_type": type(exc).__name__},
            )
            continue
        if outcome is None:
            result.skipped += 1
            continue
        result.purged += 1
        result.upload_ids.append(str(upload_id))
        result.released_bytes += outcome["released_bytes"]
        if outcome["removed_staging_dir"]:
            result.removed_staging_dirs += 1

    if result.purged or result.failed:
        logger.info(
            "stale_upload_purge_done",
            extra={
                "scanned": result.scanned,
                "purged": result.purged,
                "failed": result.failed,
                "released_bytes": result.released_bytes,
                "dry_run": dry_run,
            },
        )
    return result


def _staging_exists(object_key: str) -> bool:
    try:
        return resolve_file_disk_path(object_key).is_dir()
    except DomainError:  # pragma: no cover - 非法键按"没有目录"处理
        return False


def _purge_one(upload_id, *, now, grace, dry_run) -> dict | None:
    """Handle one session. Returns None when it turned out to be ineligible."""
    with transaction.atomic():
        upload = UploadSession.objects.select_for_update().filter(pk=upload_id).first()
        if upload is None or upload.status not in ACTIVE_STATES:
            # 并发场景：另一个请求刚好删掉了它，或者它已经完成合并
            return None
        if upload.expires_at is None or upload.expires_at > now - grace:
            return None

        object_key = str(upload.object_key or "")
        expires_at = upload.expires_at
        declared_bytes = int(upload.declared_bytes or 0)
        plan = {
            "upload_id": str(upload.id),
            "object_key": object_key,
            "expires_at": expires_at,
            "released_bytes": 0,
            "removed_staging_dir": False,
        }

        if dry_run:
            # 只读估算：预览时也要给出"本可释放多少"，否则运维看不出这次清扫值不值得
            reserved = 0
            if upload.quota_id:
                reserved = (
                    StorageQuota.objects.filter(pk=upload.quota_id)
                    .values_list("reserved_bytes", flat=True)
                    .first()
                    or 0
                )
            plan["released_bytes"] = min(int(reserved), declared_bytes)
            plan["removed_staging_dir"] = bool(object_key.startswith("tmp/") and _staging_exists(object_key))
            return plan

        released = 0
        if upload.quota_id:
            quota = StorageQuota.objects.select_for_update().get(pk=upload.quota_id)
            # 预留是共享池子，只能扣回本会话声明的那部分，且不能扣成负数
            released = min(int(quota.reserved_bytes or 0), declared_bytes)
            if released:
                quota.reserved_bytes = quota.reserved_bytes - released
                quota.save(update_fields=["reserved_bytes", "updated_at"])

        asset = FileAsset.objects.select_for_update().filter(upload=upload).first()
        if asset is not None and asset.status != FileAsset.Status.INVALID:
            asset.status = FileAsset.Status.INVALID
            asset.save(update_fields=["status", "updated_at"])

        upload.status = UploadSession.Status.ABORTED
        upload.save(update_fields=["status", "updated_at"])

        record(
            actor=None,  # 系统任务，没有操作人
            action="upload.purged",
            target=upload,
            metadata={
                "reason": "expired_upload_session",
                "upload_mode": upload.upload_mode,
                "total_chunks": int(upload.total_chunks or 1),
                "declared_bytes": declared_bytes,
                "released_bytes": released,
                "object_key": object_key,
                "expires_at": expires_at.isoformat(),
            },
        )
        plan["released_bytes"] = released

    # 事务提交后再动磁盘：即使提交后崩溃，也只会留下孤儿文件，
    # 不会出现"会话还活着但文件已经没了"这种无法续传的状态。
    if object_key.startswith("tmp/"):
        remove_chunk_staging_dir(upload)
        plan["removed_staging_dir"] = True
    else:  # pragma: no cover - 目前只有预留态会走这里
        remove_upload_object(object_key)
    return plan
