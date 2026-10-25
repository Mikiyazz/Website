import uuid
from django.core.management.base import BaseCommand
from django.db import transaction
from apps.jobs.services import enqueue


class Command(BaseCommand):
    help = "Create a harmless pipeline probe; inspect its status in admin/jobs/outboxevent/."

    def handle(self, *args, **options):
        with transaction.atomic():
            event = enqueue(topic="system.probe", dedup_key=f"probe:{uuid.uuid4()}", payload={})
        self.stdout.write(str(event.pk))
