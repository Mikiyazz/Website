"""Exports URL routes."""
from django.urls import path
from . import views

urlpatterns = [
    path("activities/<uuid:activity_id>/excel/", views.export_activity_excel_view, name="export-activity-excel"),
    path("activities/<uuid:activity_id>/zip/", views.export_activity_class_zip_view, name="export-activity-zip"),
    path("activities/<uuid:activity_id>/classes/", views.activity_classes_view, name="export-activity-classes"),
    path("activities/<uuid:activity_id>/class-list/", views.activity_classes_view, name="export-activity-class-list"),
    # 异步导出：发起后立即返回 job_id，页面每 2 秒轮询 status/<job_id>/ 直到 done。
    path("start/", views.start_export, name="export-start"),
    path("status/<uuid:job_id>/", views.export_status, name="export-status"),
    path("<uuid:job_id>/download/", views.download_export, name="export-download"),
]
