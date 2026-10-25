from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import ExportJob

admin.site.register(ExportJob, ReadOnlyAdmin)
