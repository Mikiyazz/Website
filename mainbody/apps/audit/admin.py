from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    """管理端只读审计视图：可按动作/对象/时间筛选，可搜学号、原因与目标 ID。"""

    list_display = ("created_at", "action", "actor", "target_type", "target_id", "ip", "reason")
    list_filter = ("action", "target_type", "created_at")
    search_fields = ("action", "target_id", "reason", "actor__username", "ip")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 50
    list_select_related = ("actor",)
    fields = ("created_at", "updated_at", "actor", "action", "target_type", "target_id",
              "request_id", "ip", "reason", "metadata")
    readonly_fields = fields
