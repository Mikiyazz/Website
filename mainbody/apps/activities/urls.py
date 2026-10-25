from django.urls import path
from .views import activity_list

urlpatterns = [path("", activity_list, name="activity-list")]
