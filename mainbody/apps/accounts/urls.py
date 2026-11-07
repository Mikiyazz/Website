"""Accounts URL routes."""
from django.urls import path
from . import views

urlpatterns = [
    path("register/", views.register, name="account-register"),
    path("change-password/", views.change_password_view, name="account-change-password"),
]
