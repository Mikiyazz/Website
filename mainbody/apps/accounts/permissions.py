from .models import User
from apps.common.errors import Forbidden


def require_student(user):
    if not user.is_authenticated or not user.is_active or user.role != User.Role.STUDENT:
        raise Forbidden("此操作只对有效学生账号开放。")


def is_college_admin(user):
    return user.is_authenticated and user.is_active and (user.is_superuser or user.role == User.Role.ADMIN)
