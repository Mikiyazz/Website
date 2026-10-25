from django.contrib import admin
from apps.common.admin import ReadOnlyAdmin
from .models import Submission, SubmissionRevision

admin.site.register(Submission, ReadOnlyAdmin)
admin.site.register(SubmissionRevision, ReadOnlyAdmin)
