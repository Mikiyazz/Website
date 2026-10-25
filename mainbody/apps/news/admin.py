from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import NewsDraft

admin.site.register(NewsDraft, ReadOnlyAdmin)
