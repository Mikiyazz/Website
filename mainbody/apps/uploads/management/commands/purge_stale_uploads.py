"""回收过期上传任务：释放额度预留、删除 media/tmp/<upload_id>/、把会话置为 ABORTED。

日常由 dispatch_outbox 常驻循环按 --purge-interval 顺带执行；这个命令用于人工排查、
cron 兜底和预演（--dry-run）。
"""
import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.uploads.retention import purge_stale_uploads


class Command(BaseCommand):
    help = "Reclaim disk space and quota reservations left behind by expired upload sessions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只统计将回收多少，不改数据库也不删文件。",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="单次最多处理多少条（默认 200），避免一次锁太多行。",
        )
        parser.add_argument(
            "--grace-hours",
            type=int,
            default=0,
            help="额外宽限期（小时）：expires_at 过期后再等这么久才清，给慢网络留余地。",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="输出机器可读的 JSON，便于监控采集。",
        )

    def handle(self, *args, **options):
        grace_hours = max(0, int(options["grace_hours"]))
        result = purge_stale_uploads(
            now=timezone.now(),
            grace=timedelta(hours=grace_hours),
            limit=max(1, int(options["limit"])),
            dry_run=bool(options["dry_run"]),
        )

        if options["json"]:
            self.stdout.write(json.dumps(result.as_dict(), ensure_ascii=False))
        else:
            style = self.style.WARNING if result.dry_run else self.style.SUCCESS
            self.stdout.write(style(result.summary()))
            for upload_id in result.upload_ids[:10]:
                self.stdout.write(f"  - {upload_id}")
            if len(result.upload_ids) > 10:
                self.stdout.write(f"  ... 其余 {len(result.upload_ids) - 10} 条略")
            for upload_id, error in result.failures:
                self.stderr.write(f"  失败 {upload_id}: {error}")

        if result.failed:
            raise CommandError(f"{result.failed} 条过期上传回收失败，详见错误输出与日志。")
