import logging
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from .models import StudentRoster, User
from .services import batch_initialize_students, reset_password_to_default
from .tasks import batch_initialize_student_accounts_task, batch_reset_passwords_task

logger = logging.getLogger(__name__)

# Admin 页面单次同步执行 PBKDF2 的最大人数阈值：超过该数量转交 Celery 异步队列，避免 HTTP 请求超时。
ADMIN_SYNC_PASSWORD_BATCH_LIMIT = 50


def _enqueue_or_reject_large_batch(
    *,
    request,
    model_admin,
    total_count: int,
    action_label: str,
    task,
    task_args: tuple,
    cli_hint: str,
) -> bool:
    """尝试将超阈值批量密码任务投递至 Celery；若 Broker 未启用或不可达则拒绝同步执行并给出明确指引。"""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or not getattr(settings, "CELERY_BROKER_URL", ""):
        if request:
            model_admin.message_user(
                request,
                f"本次勾选 {total_count} 条记录，超过后台同步安全上限（{ADMIN_SYNC_PASSWORD_BATCH_LIMIT} 条），"
                f"且当前未启用异步队列，已阻止同步执行以避免超时。请分批勾选（每次不超过 {ADMIN_SYNC_PASSWORD_BATCH_LIMIT} 条）"
                f"或使用命令行执行：{cli_hint}",
                messages.WARNING,
            )
        return False

    try:
        async_result = task.delay(*task_args)
    except Exception:
        logger.warning("admin_batch_task_enqueue_failed", extra={"action": action_label, "count": total_count}, exc_info=True)
        if request:
            model_admin.message_user(
                request,
                f"本次勾选 {total_count} 条记录，超过同步上限（{ADMIN_SYNC_PASSWORD_BATCH_LIMIT} 条），"
                f"且异步队列暂不可用，已取消执行以防请求超时。请分批操作或使用命令行执行：{cli_hint}",
                messages.ERROR,
            )
        return False

    if request:
        model_admin.message_user(
            request,
            f"已将 {total_count} 条{action_label}任务提交至 Celery 后台队列异步执行（任务 ID：{async_result.id}），请稍后刷新查看结果。",
            messages.INFO,
        )
    return True


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
        user_ids = list(queryset.values_list("pk", flat=True))
        total = len(user_ids)
        if total == 0:
            if request:
                self.message_user(request, "未选中任何可重置密码的用户。", messages.INFO)
            return
        if total > ADMIN_SYNC_PASSWORD_BATCH_LIMIT:
            actor_id = getattr(getattr(request, "user", None), "pk", None)
            _enqueue_or_reject_large_batch(
                request=request,
                model_admin=self,
                total_count=total,
                action_label="密码重置",
                task=batch_reset_passwords_task,
                task_args=(user_ids, actor_id),
                cli_hint="python manage.py batch_reset_passwords --all-students",
            )
            return

        actor = getattr(request, "user", None)
        count = 0
        for user in queryset:
            reset_password_to_default(user=user, actor=actor)
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
        pending_qs = queryset.filter(active=True, user__isnull=True)
        roster_ids = [str(rid) for rid in pending_qs.values_list("pk", flat=True)]
        total = len(roster_ids)
        if total == 0:
            if request:
                self.message_user(request, "选中的花名册记录均已初始化账号或已停用。", messages.INFO)
            return
        if total > ADMIN_SYNC_PASSWORD_BATCH_LIMIT:
            actor_id = getattr(getattr(request, "user", None), "pk", None)
            _enqueue_or_reject_large_batch(
                request=request,
                model_admin=self,
                total_count=total,
                action_label="学生账号初始化",
                task=batch_initialize_student_accounts_task,
                task_args=(roster_ids, actor_id),
                cli_hint="python manage.py batch_init_students --all-roster",
            )
            return

        created = batch_initialize_students(pending_qs)
        if request:
            self.message_user(request, f"成功批量初始化 {len(created)} 个学生账号。", messages.SUCCESS)
