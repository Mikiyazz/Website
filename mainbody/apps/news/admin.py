from django.contrib import admin

from apps.common.admin import ReadOnlyAdmin
from .models import NewsDraft, NewsRevision


class NewsRevisionInline(admin.TabularInline):
    model = NewsRevision
    extra = 0
    can_delete = False
    readonly_fields = ("number", "snapshot", "request_key", "created_at")
    ordering = ("number",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(NewsDraft)
class NewsDraftAdmin(ReadOnlyAdmin):
    list_display = ("submission", "title", "status", "version", "submitted_at", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = ("title", "submission__owner__username", "submission__owner__real_name")
    ordering = ("-updated_at",)
    readonly_fields = ("submitted_at", "reviewed_version", "returned_comment", "version")
    inlines = (NewsRevisionInline,)


@admin.register(NewsRevision)
class NewsRevisionAdmin(ReadOnlyAdmin):
    list_display = ("news", "number", "created_at")
    ordering = ("-created_at",)
