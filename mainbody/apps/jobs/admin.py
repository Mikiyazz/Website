from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import OutboxEvent


@admin.register(OutboxEvent)
class OutboxAdmin(ReadOnlyAdmin):
    list_display = ("id", "topic", "state", "attempts", "available_at", "last_error_code")
    list_filter = ("state", "topic")
