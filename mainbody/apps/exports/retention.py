"""历史导出归档 ZIP 自动清理：回收超过保留期（默认 72 小时）的导出包与孤儿半成品，防止写爆磁盘。

由两处触发（幂等、单条异常隔离）：
- `manage.py purge_expired_exports`：人工或 cron 触发，支持 --dry-run / --hours / --json；
- `manage.py dispatch_outbox` 常驻循环：默认每小时与过期上传回收同步执行。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.audit.services import record
from apps.exports.models import ExportJob
from apps.exports.services import DEFAULT_EXPORT_RETENTION_HOURS, EXPORT_ROOT_DIRNAME

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 200
ORPHAN_PART_TTL_HOURS = 4


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


@dataclass
class ExportPurgeResult:
    scanned: int = 0
    purged: int = 0
    orphans_removed: int = 0
    failed: int = 0
    freed_bytes: int = 0
    dry_run: bool = False
    job_ids: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        prefix = "预演（未改动任何数据）" if self.dry_run else "已回收"
        parts = [
            f"扫描 {self.scanned} 条导出记录",
            f"{prefix} {self.purged} 个过期包",
            f"孤儿文件 {self.orphans_removed} 个",
        ]
        if self.failed:
            parts.append(f"失败 {self.failed} 条")
        parts.append(f"释放磁盘 {_format_bytes(self.freed_bytes)}")
        return "，".join(parts) + "。"

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "purged": self.purged,
            "orphans_removed": self.orphans_removed,
            "failed": self.failed,
            "freed_bytes": self.freed_bytes,
            "dry_run": self.dry_run,
            "job_ids": list(self.job_ids),
            "orphan_files": list(self.orphan_files),
            "failures": [{"target": target, "error": err} for target, err in self.failures],
        }


def _resolve_retention_hours(retention_hours) -> int:
    raw = retention_hours if retention_hours is not None else getattr(
        settings, "EXPORT_RETENTION_HOURS", DEFAULT_EXPORT_RETENTION_HOURS
    )
    return max(1, int(raw))


def _resolve_export_disk_path(relative_path: str) -> Path | None:
    rel = str(relative_path or "").strip()
    if not rel:
        return None
    media_root = Path(settings.MEDIA_ROOT).resolve()
    exports_root = (media_root / EXPORT_ROOT_DIRNAME).resolve()
    target = (media_root / rel).resolve()
    if exports_root not in target.parents:
        return None
    return target


def expired_export_queryset(*, now=None, retention_hours=None):
    current = now or timezone.now()
    hours = _resolve_retention_hours(retention_hours)
    cutoff = current - timedelta(hours=hours)
    return ExportJob.objects.filter(
        status__in=(ExportJob.Status.DONE, ExportJob.Status.FAILED),
    ).exclude(file_path="").filter(
        Q(expires_at__isnull=False, expires_at__lt=current)
        | Q(expires_at__isnull=True, finished_at__isnull=False, finished_at__lt=cutoff)
        | Q(expires_at__isnull=True, finished_at__isnull=True, created_at__lt=cutoff)
    )


def purge_expired_exports(
    *,
    now=None,
    retention_hours=None,
    limit: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> ExportPurgeResult:
    """Delete expired export ZIP archives (default >72h) and orphaned partial export files."""
    current = now or timezone.now()
    hours = _resolve_retention_hours(retention_hours)
    cutoff = current - timedelta(hours=hours)
    result = ExportPurgeResult(dry_run=dry_run)
    batch = max(1, int(limit))

    job_ids = list(
        expired_export_queryset(now=current, retention_hours=hours)
        .order_by("created_at")
        .values_list("pk", flat=True)[:batch]
    )
    result.scanned = len(job_ids)

    for job_id in job_ids:
        try:
            outcome = _purge_one_job(job_id, now=current, cutoff=cutoff, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.failures.append((str(job_id), type(exc).__name__))
            logger.warning(
                "expired_export_purge_failed",
                extra={"job_id": str(job_id), "error_type": type(exc).__name__},
            )
            continue
        if outcome is None:
            continue
        result.purged += 1
        result.job_ids.append(str(job_id))
        result.freed_bytes += outcome["freed_bytes"]

    _sweep_orphan_export_files(
        result,
        now=current,
        zip_cutoff=cutoff,
        part_cutoff=current - timedelta(hours=ORPHAN_PART_TTL_HOURS),
        dry_run=dry_run,
    )

    if result.purged or result.orphans_removed or result.failed:
        logger.info("expired_export_purge_done", extra=result.as_dict())
    return result


def _purge_one_job(job_id, *, now, cutoff, dry_run: bool) -> dict | None:
    with transaction.atomic():
        job = ExportJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None or not job.file_path:
            return None
        if job.status not in (ExportJob.Status.DONE, ExportJob.Status.FAILED):
            return None
        is_expired = (
            (job.expires_at is not None and job.expires_at < now)
            or (job.expires_at is None and job.finished_at is not None and job.finished_at < cutoff)
            or (job.expires_at is None and job.finished_at is None and job.created_at < cutoff)
        )
        if not is_expired:
            return None

        rel_path = str(job.file_path)
        target = _resolve_export_disk_path(rel_path)
        size = 0
        if target is not None and target.is_file():
            try:
                size = target.stat().st_size
            except OSError:
                size = 0

        if dry_run:
            return {"freed_bytes": size}

        job.file_path = ""
        job.save(update_fields=["file_path", "updated_at"])
        record(
            actor=None,
            action="export.purged",
            target=job,
            metadata={
                "reason": "expired_export_archive",
                "file_path": rel_path,
                "freed_bytes": size,
            },
        )

    if target is not None:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.warning("expired_export_unlink_failed", extra={"path": str(target)})
    return {"freed_bytes": size}


def _sweep_orphan_export_files(
    result: ExportPurgeResult,
    *,
    now,
    zip_cutoff,
    part_cutoff,
    dry_run: bool,
) -> None:
    exports_root = (Path(settings.MEDIA_ROOT) / EXPORT_ROOT_DIRNAME).resolve()
    if not exports_root.is_dir():
        return

    media_root = Path(settings.MEDIA_ROOT).resolve()
    referenced = {
        str(fp).strip().replace("\\", "/")
        for fp in ExportJob.objects.exclude(file_path="").values_list("file_path", flat=True)
        if str(fp or "").strip()
    }

    for path in exports_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            rel_posix = path.resolve().relative_to(media_root).as_posix()
        except (OSError, ValueError):
            continue
        if rel_posix in referenced:
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=dt_timezone.utc)
        is_part = path.name.endswith(".part")
        threshold = part_cutoff if is_part else zip_cutoff
        if mtime >= threshold:
            continue
        result.orphans_removed += 1
        result.freed_bytes += stat.st_size
        result.orphan_files.append(rel_posix)
        if not dry_run:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                result.failed += 1
                result.failures.append((rel_posix, type(exc).__name__))
