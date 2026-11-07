from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import ReviewAssignment, ReviewRecord


@admin.register(ReviewAssignment)
class ReviewAssignmentAdmin(admin.ModelAdmin):
    list_display = ("reviewer", "activity", "class_name", "created_at")
    list_filter = ("activity", "class_name")
    search_fields = ("reviewer__username", "reviewer__real_name", "class_name")


@admin.register(ReviewRecord)
class ReviewRecordAdmin(ReadOnlyAdmin):
    list_display = ("submission", "stage", "reviewer", "decision", "comment", "created_at")
    list_filter = ("stage", "decision")
    search_fields = ("submission__owner__username", "submission__owner__real_name", "comment")
