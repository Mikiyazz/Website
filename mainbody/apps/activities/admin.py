from django.contrib import admin
from .models import Activity, MaterialRequirement


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("title", "is_open", "opens_at", "deadline")
    list_filter = ("is_open",)


@admin.register(MaterialRequirement)
class RequirementAdmin(admin.ModelAdmin):
    list_display = ("title", "activity", "code", "rule_version", "required")
    list_filter = ("activity",)
