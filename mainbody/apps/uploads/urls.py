from django.urls import path
from .views import delete_upload_view, direct_upload_view, file_access_view, initialize

urlpatterns = [
    path("init/", initialize, name="upload-init"),
    path("direct-upload/", direct_upload_view, name="upload-direct"),
    path("<uuid:upload_id>/file/", file_access_view, name="upload-file"),
    path("<uuid:upload_id>/delete/", delete_upload_view, name="upload-delete"),
    path("<uuid:upload_id>/", delete_upload_view, name="upload-detail"),
]
