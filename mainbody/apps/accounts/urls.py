"""Accounts URL routes."""
from django.urls import path
from . import views

urlpatterns = [
    path("register/", views.register, name="account-register"),
    path("change-password/", views.change_password_view, name="account-change-password"),
    # 学生端申报向导的队员学号占位查询（正式前端入口见 config/urls.py 的 /api/students/lookup/）
    path("students/lookup/", views.student_lookup_view, name="account-student-lookup"),
]
