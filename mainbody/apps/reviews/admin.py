from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import ReviewAssignment, ReviewRecord

admin.site.register(ReviewAssignment)
admin.site.register(ReviewRecord, ReadOnlyAdmin)
