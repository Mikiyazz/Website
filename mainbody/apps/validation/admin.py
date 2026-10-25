from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import ValidationResult

admin.site.register(ValidationResult, ReadOnlyAdmin)
