from django.urls import path
from .views import activity_list, manage_activity, manage_whitelist

urlpatterns = [
    path("", activity_list, name="activity-list"),
    path("manage/", manage_activity, name="activity-manage"),
    path("<uuid:activity_id>/whitelist/", manage_whitelist, name="activity-whitelist"),
]
