"""Reviews URL routes."""
from django.urls import path
from . import views

urlpatterns = [
    path("", views.review_list, name="review-list"),
    path("<uuid:submission_id>/", views.review, name="review-submission"),
    path("<uuid:submission_id>/detail/", views.review_detail, name="review-submission-detail-api"),
]
