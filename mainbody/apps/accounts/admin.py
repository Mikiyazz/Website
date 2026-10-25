from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import StudentRoster, User


@admin.register(User)
class PlatformUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("学院信息", {"fields": ("real_name", "class_name", "role")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("学院信息", {"fields": ("real_name", "class_name", "role")}),)


@admin.register(StudentRoster)
class RosterAdmin(admin.ModelAdmin):
    list_display = ("student_number", "real_name", "class_name", "active")
    list_filter = ("class_name", "active")
    search_fields = ("student_number", "real_name")
