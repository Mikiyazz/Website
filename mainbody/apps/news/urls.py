"""JSON API 路由，挂载于 /api/v1/news/（与其余 6 个模块同一约定）。"""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.api_collection, name="news-list"),
    path("<uuid:news_id>/", views.api_detail, name="news-detail"),
    path("<uuid:news_id>/submit/", views.api_submit, name="news-submit"),
    path("<uuid:news_id>/document/", views.api_delete_document, name="news-document-delete"),
]
