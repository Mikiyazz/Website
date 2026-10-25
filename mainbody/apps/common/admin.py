from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Business transitions must not bypass services via an editable admin form."""
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
