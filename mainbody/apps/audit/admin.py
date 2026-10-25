from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import AuditLog

admin.site.register(AuditLog, ReadOnlyAdmin)
