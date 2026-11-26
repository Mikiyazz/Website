import re
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from apps.common.models import BaseModel

STUDENT_NUMBER_RE = re.compile(r"^\d{8}$")
GRADE_RE = re.compile(r"^202\d级$")
CLASS_NAME_RE = re.compile(r"^(?:0[1-9]|[1-9]\d)班$")
PHONE_RE = re.compile(r"^\d{11}$")

MAJOR_CHOICES = (
    "计算机类",
    "人工智能",
    "计算机科学与技术",
    "软件工程",
    "信息安全",
    "数据科学与大数据技术",
)


def default_password_from_student_number(student_number: str) -> str:
    raw = str(student_number or "").strip()
    return raw[-6:] if len(raw) >= 6 else raw


class PlatformUserManager(UserManager):
    def create_user(self, username, email=None, password=None, **extra_fields):
        username_str = str(username or "").strip()
        if password is None or str(password) == "":
            password = default_password_from_student_number(username_str)
            extra_fields.setdefault("must_change_password", True)
        else:
            extra_fields.setdefault("must_change_password", True)
        return super().create_user(username_str, email=email, password=password, **extra_fields)

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("must_change_password", False)
        return super().create_superuser(username, email=email, password=password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        STUDENT = "student", "学生"
        REVIEWER = "reviewer", "审核人员"
        ADMIN = "college_admin", "学院管理员"

    class Major(models.TextChoices):
        COMPUTER_CATEGORY = "计算机类", "计算机类"
        AI = "人工智能", "人工智能"
        CS = "计算机科学与技术", "计算机科学与技术"
        SE = "软件工程", "软件工程"
        INFOSEC = "信息安全", "信息安全"
        DATA_SCIENCE = "数据科学与大数据技术", "数据科学与大数据技术"

    real_name = models.CharField("姓名", max_length=100, blank=True)
    class_name = models.CharField("班级", max_length=100, blank=True, db_index=True)
    grade = models.CharField("年级", max_length=32, blank=True, default="")
    major = models.CharField("专业", max_length=100, choices=Major.choices, blank=True, default="")
    phone = models.CharField("手机号", max_length=32, blank=True, default="")
    must_change_password = models.BooleanField("需修改初始密码", default=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STUDENT)
    # username remains a string, preserving leading zeros in student numbers.

    objects = PlatformUserManager()

    def get_default_password(self) -> str:
        return default_password_from_student_number(self.username)

    def reset_password_to_default(self, *, save: bool = True) -> str:
        default_pwd = self.get_default_password()
        self.set_password(default_pwd)
        self.must_change_password = True
        if save:
            self.save(update_fields=["password", "must_change_password"])
        return default_pwd


class StudentRoster(BaseModel):
    student_number = models.CharField(max_length=32, unique=True)
    real_name = models.CharField(max_length=100)
    class_name = models.CharField(max_length=100, db_index=True)
    grade = models.CharField("年级", max_length=32, blank=True, default="")
    major = models.CharField("专业", max_length=100, choices=User.Major.choices, blank=True, default="")
    phone = models.CharField("手机号", max_length=32, blank=True, default="")
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
