"""手动或 cron 触发：回收超过保留期（默认 72 小时）的历史导出 ZIP 包与孤儿半成品文件。"""
import json

from django.core.management.base import BaseCommand

from apps.exports.retention import DEFAULT_BATCH_SIZE, purge_expired_exports


class Command(BaseCommand):
    help = "Reclaim expired activity export ZIP archives (default > 72 hours) and orphan partial files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只扫描统计不修改数据库也不删文件。",
        )
        parser.add_argument(
            "--hours",
            type=int,
            default=None,
            help="覆盖默认保留时长（小时，默认读取 EXPORT_RETENTION_HOURS=72）。",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"单次最多处理多少条导出记录（默认 {DEFAULT_BATCH_SIZE}）。",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="以 JSON 格式输出，便于监控脚本解析。",
        )

    def handle(self, *args, **options):
        result = purge_expired_exports(
            retention_hours=options["hours"],
            limit=options["limit"],
            dry_run=options["dry_run"],
        )
        if options["as_json"]:
            self.stdout.write(json.dumps(result.as_dict(), ensure_ascii=False))
        else:
            self.stdout.write(result.summary())
        if result.failed:
            raise SystemExit(1)
