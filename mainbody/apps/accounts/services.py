"""Accounts service layer: registration, student verification, and credential handling."""
from django.db import transaction
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError, Forbidden
from .models import (
    CLASS_NAME_RE,
    GRADE_RE,
    MAJOR_CHOICES,
    PHONE_RE,
    STUDENT_NUMBER_RE,
    StudentRoster,
    User,
    default_password_from_student_number,
)


def get_default_password(student_number: str) -> str:
    student_number = str(student_number or "").strip()
    if len(student_number) < 6:
        raise DomainError("学号长度至少为 6 位。")
    return default_password_from_student_number(student_number)


def validate_student_registration_fields(
    *,
    student_number: str,
    real_name: str,
    grade: str,
    major: str,
    class_name: str,
    phone: str,
    password=None,
) -> dict[str, str]:
    """逐字段校验学生注册信息，返回 {field_name: error_message}。"""
    errors: dict[str, str] = {}

    if not student_number:
        errors["student_number"] = "请输入学号。"
    elif not STUDENT_NUMBER_RE.match(student_number):
        errors["student_number"] = "学号必须为 8 位数字。"

    if not real_name:
        errors["real_name"] = "请输入姓名。"

    if not grade:
        errors["grade"] = "请输入年级。"
    elif not GRADE_RE.match(grade):
        errors["grade"] = "年级格式须为 202X级（如 2025级）。"

    if not major:
        errors["major"] = "请选择专业。"
    elif major not in MAJOR_CHOICES:
        errors["major"] = "请从可选专业列表中选择，不支持自定义专业。"

    if not class_name:
        errors["class_name"] = "请输入班级。"
    elif not CLASS_NAME_RE.match(class_name):
        errors["class_name"] = "班级格式须为 XX班，个位数须补零（如 01班、12班）。"

    if not phone:
        errors["phone"] = "请输入手机号。"
    elif not PHONE_RE.match(phone):
        errors["phone"] = "手机号必须为 11 位数字。"

    if password is not None and str(password) != "" and len(str(password)) < 6:
        errors["password"] = "自定义密码长度至少为 6 位。"

    return errors


@transaction.atomic
def register_student(
    *,
    student_number,
    real_name,
    class_name,
    password=None,
    grade="",
    major="",
    phone="",
):
    student_number = str(student_number or "").strip()
    real_name = str(real_name or "").strip()
    class_name = str(class_name or "").strip()
    grade = str(grade or "").strip()
    major = str(major or "").strip()
    phone = str(phone or "").strip()

    roster = (
        StudentRoster.objects.filter(student_number=student_number, user__isnull=True).first()
        if student_number
        else None
    )
    if roster:
        grade = grade or roster.grade
        major = major or roster.major
        phone = phone or roster.phone

    field_errors = validate_student_registration_fields(
        student_number=student_number,
        real_name=real_name,
        grade=grade,
        major=major,
        class_name=class_name,
        phone=phone,
        password=password,
    )
    if field_errors:
        first_message = next(iter(field_errors.values()))
        err = DomainError(first_message)
        err.field_errors = field_errors
        raise err

    # 若未显式传入自定义密码，默认初始密码设为学号后 6 位
    if password is None or str(password) == "":
        raw_password = get_default_password(student_number)
    else:
        raw_password = str(password)

    # 学号查重：username 保存学号字符串，保留可能的前导零
    if User.objects.filter(username=student_number).exists():
        err = Conflict("该学号已被注册。")
        err.field_errors = {"student_number": "该学号已被注册。"}
        raise err

    # 后端强制写死角色为普通学生 (student)，严禁从外部读取权限参数；首次登录必须修改密码
    user = User.objects.create_user(
        username=student_number,
        password=raw_password,
        real_name=real_name,
        class_name=class_name,
        grade=grade,
        major=major,
        phone=phone,
        must_change_password=True,
        role=User.Role.STUDENT,
    )

    # 若花名册中已有该学号记录，建立关联
    if roster:
        roster.user = user
        roster.save(update_fields=["user", "updated_at"])

    # 业务审计记录
    record(
        actor=user,
        action="account.registered",
        target=user,
        metadata={
            "student_number": student_number,
            "class_name": class_name,
            "grade": grade,
            "major": major,
        },
    )

    return user


@transaction.atomic
def batch_initialize_students(items):
    """后台批量初始化学生账号：默认初始密码设为学号后 6 位，must_change_password=True。"""
    created_users = []
    for item in items:
        if isinstance(item, StudentRoster):
            if item.user_id:
                continue
            user = register_student(
                student_number=item.student_number,
                real_name=item.real_name,
                class_name=item.class_name,
                password=None,
                grade=item.grade,
                major=item.major,
                phone=item.phone,
            )
            created_users.append(user)
        elif isinstance(item, dict):
            user = register_student(
                student_number=item.get("student_number") or item.get("username"),
                real_name=item.get("real_name"),
                class_name=item.get("class_name"),
                password=item.get("password"),
                grade=item.get("grade", ""),
                major=item.get("major", ""),
                phone=item.get("phone", ""),
            )
            created_users.append(user)
        else:
            raise DomainError("批量初始化数据项格式不正确。")
    return created_users


@transaction.atomic
def change_password(*, user, new_password, old_password=None):
    """修改用户密码，成功后将 must_change_password 置为 False。"""
    if not user or not user.is_authenticated or not user.is_active:
        raise Forbidden("请先登录有效账号。")

    if old_password is not None and str(old_password) != "":
        if not user.check_password(str(old_password)):
            raise DomainError("原密码不正确。")

    new_password_str = str(new_password or "")
    if not new_password_str:
        raise DomainError("新密码不能为空。")
    if len(new_password_str) < 6:
        raise DomainError("新密码长度至少为 6 位。")
    if user.check_password(new_password_str):
        raise DomainError("新密码不能与原密码相同。")

    user.set_password(new_password_str)
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password"])

    record(
        actor=user,
        action="account.password_changed",
        target=user,
        metadata={"student_number": user.username, "must_change_password": False},
    )
    return user


@transaction.atomic
def reset_password_to_default(*, user, actor=None):
    """重置指定用户密码为学号后 6 位，并将 must_change_password 置为 True。"""
    default_pwd = get_default_password(user.username)
    user.set_password(default_pwd)
    user.must_change_password = True
    user.save(update_fields=["password", "must_change_password"])

    record(
        actor=actor or user,
        action="account.password_reset",
        target=user,
        metadata={"student_number": user.username, "must_change_password": True},
    )
    return user
