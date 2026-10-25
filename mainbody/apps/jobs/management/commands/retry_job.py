import uuid
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from apps.jobs.models import OutboxEvent


class Command(BaseCommand):
    help = "After fixing the root cause, reset a failed job for bounded redelivery."

    def add_arguments(self, parser):
        parser.add_argument("event_id", type=uuid.UUID)

    def handle(self, *args, **options):
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update().get(pk=options["event_id"])
            if event.state != OutboxEvent.State.FAILED:
                raise CommandError("Only FAILED jobs may be reset.")
            event.state = OutboxEvent.State.PENDING
            event.attempts = 0
            event.available_at = timezone.now()
            event.lease_until = None
            event.lease_token = None
            event.last_error_code = ""
            event.save()
        self.stdout.write("Job reset; dispatcher will deliver it.")
