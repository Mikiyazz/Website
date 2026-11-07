from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from .models import StudentRoster, User
from .services import batch_initialize_students


@admin.register(User)
class PlatformUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (
            "学院信息",
            {
                "fields": (
                    "real_name",
                    "class_name",
                    "grade",
                    "major",
                    "phone",
                    "role",
                    "must_change_password",
                )
            },
        ),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            "学院信息",
            {
                "fields": (
                    "real_name",
                    "class_name",
                    "grade",
                    "major",
                    "phone",
                    "role",
                    "must_change_password",
                )
            },
        ),
    )

    # 搜索学号（username）、姓名、班级与专业
    search_fields = ("username", "real_name", "class_name", "major", "phone")
    list_display = (
        "username",
        "real_name",
        "class_name",
        "grade",
        "major",
        "role",
        "must_change_password",
        "is_active",
        "is_staff",
    )
    list_filter = ("role", "grade", "class_name", "must_change_password", "is_active", "is_staff")
    ordering = ("username",)

    actions = [
        "promote_to_reviewer",
        "promote_to_admin",
        "demote_to_student",
        "reset_password_to_last_six",
    ]

    @admin.action(description="一键提权为审核人员（团支书）")
    def promote_to_reviewer(self, request, queryset):
        updated = queryset.update(role=User.Role.REVIEWER)
        if request:
            self.message_user(request, f"成功将 {updated} 名用户提权为审核人员（团支书）。", messages.SUCCESS)

    @admin.action(description="一键提权为学院管理员")
    def promote_to_admin(self, request, queryset):
        updated = queryset.update(role=User.Role.ADMIN, is_staff=True)
        if request:
            self.message_user(request, f"成功将 {updated} 名用户提权为学院管理员并开通后台权限。", messages.SUCCESS)

    @admin.action(description="一键重置为普通学生")
    def demote_to_student(self, request, queryset):
        updated = queryset.update(role=User.Role.STUDENT, is_staff=False)
        if request:
            self.message_user(request, f"成功将 {updated} 名用户重置为普通学生角色。", messages.SUCCESS)

    @admin.action(description="一键重置密码为学号后 6 位")
    def reset_password_to_last_six(self, request, queryset):
        count = 0
        for user in queryset:
            user.reset_password_to_default(save=True)
            count += 1
        if request:
            self.message_user(request, f"成功将 {count} 名用户的密码重置为学号后 6 位。", messages.SUCCESS)


@admin.register(StudentRoster)
class RosterAdmin(admin.ModelAdmin):
    list_display = ("student_number", "real_name", "class_name", "grade", "major", "active")
    list_filter = ("grade", "class_name", "active")
    search_fields = ("student_number", "real_name", "major")
    actions = ["initialize_student_accounts"]

    @admin.action(description="批量初始化学生账号（默认密码为学号后 6 位）")
    def initialize_student_accounts(self, request, queryset):
        created = batch_initialize_students(queryset.filter(active=True, user__isnull=True))
        if request:
            self.message_user(request, f"成功批量初始化 {len(created)} 个学生账号。", messages.SUCCESS)
