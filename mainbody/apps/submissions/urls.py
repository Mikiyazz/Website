from django.urls import path
from . import views

urlpatterns = [
    path("", views.collection, name="submission-list"),
    path("<uuid:submission_id>/", views.detail, name="submission-detail"),
    path("<uuid:submission_id>/submit/", views.submit, name="submission-submit"),
]
