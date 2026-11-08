import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.jobs.dispatcher import dispatch_one
from apps.uploads.retention import purge_stale_uploads

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Publish durable events and reclaim expired uploads; run as one independently supervised process."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process up to 20 due events, then exit.")
        parser.add_argument(
            "--purge-interval",
            type=int,
            default=3600,
            help="顺带回收过期上传任务的间隔秒数；0 关闭（默认 3600）。",
        )
        parser.add_argument(
            "--no-purge",
            action="store_true",
            help="本次运行不做上传回收，等价于 --purge-interval 0。",
        )

    def handle(self, *args, **options):
        self.stopping = False
        signal.signal(signal.SIGTERM, lambda *args: setattr(self, "stopping", True))
        interval = 0 if options["no_purge"] else max(0, int(options["purge_interval"]))
        # 启动先扫一次：进程重启后能立刻清掉停机期间攒下的过期上传
        next_purge_at = time.monotonic()
        while not self.stopping:
            close_old_connections()
            try:
                for _ in range(20):
                    if self.stopping or not dispatch_one():
                        break
            except Exception as exc:
                logger.error("dispatcher_iteration_failed", extra={"error_type": type(exc).__name__})
                if options["once"]:
                    raise
            if interval and time.monotonic() >= next_purge_at:
                self._purge_stale_uploads()
                next_purge_at = time.monotonic() + interval
            if options["once"]:
                return
            time.sleep(2)

    def _purge_stale_uploads(self):
        """Housekeeping must never take the outbox loop down: swallow and log."""
        try:
            result = purge_stale_uploads()
        except Exception as exc:
            logger.error("stale_upload_purge_failed", extra={"error_type": type(exc).__name__})
            return
        if result.purged or result.failed:
            logger.info("stale_upload_purge_done", extra=result.as_dict())
