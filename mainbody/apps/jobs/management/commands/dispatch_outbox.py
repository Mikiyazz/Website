import logging
import signal
import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from apps.jobs.dispatcher import dispatch_one

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Publish durable events; run as one independently supervised process."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process up to 20 due events, then exit.")

    def handle(self, *args, **options):
        self.stopping = False
        signal.signal(signal.SIGTERM, lambda *args: setattr(self, "stopping", True))
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
            if options["once"]:
                return
            time.sleep(2)
