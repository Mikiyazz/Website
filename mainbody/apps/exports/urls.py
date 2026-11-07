"""Exports URL routes."""
from django.urls import path
from . import views

urlpatterns = [
    path("activities/<uuid:activity_id>/excel/", views.export_activity_excel_view, name="export-activity-excel"),
    path("activities/<uuid:activity_id>/zip/", views.export_activity_class_zip_view, name="export-activity-zip"),
    path("activities/<uuid:activity_id>/classes/", views.activity_classes_view, name="export-activity-classes"),
    path("activities/<uuid:activity_id>/class-list/", views.activity_classes_view, name="export-activity-class-list"),
]
