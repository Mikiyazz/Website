from django.urls import path
from .views import (
    chunk_complete,
    chunk_start,
    chunk_upload,
    delete_upload_view,
    direct_upload_view,
    file_access_view,
    initialize,
)

urlpatterns = [
    path("init/", initialize, name="upload-init"),
    path("direct-upload/", direct_upload_view, name="upload-direct"),
    # 分片上传（300MB 宣讲视频）：先开任务，再逐片 5MB 上传，最后合并。
    path("chunk-start/", chunk_start, name="upload-chunk-start"),
    path("chunk-upload/", chunk_upload, name="upload-chunk-upload"),
    path("chunk-complete/", chunk_complete, name="upload-chunk-complete"),
    path("<uuid:upload_id>/file/", file_access_view, name="upload-file"),
    path("<uuid:upload_id>/delete/", delete_upload_view, name="upload-delete"),
    path("<uuid:upload_id>/", delete_upload_view, name="upload-detail"),
]
