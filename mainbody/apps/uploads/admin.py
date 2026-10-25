from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import FileAsset, StorageQuota, UploadSession

for model in (FileAsset, StorageQuota, UploadSession):
    admin.site.register(model, ReadOnlyAdmin)
