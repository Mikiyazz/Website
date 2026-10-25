from django.contrib.auth.models import AbstractUser
from django.db import models
from apps.common.models import BaseModel


class User(AbstractUser):
    class Role(models.TextChoices):
        STUDENT = "student", "学生"
        REVIEWER = "reviewer", "审核人员"
        ADMIN = "college_admin", "学院管理员"

    real_name = models.CharField("姓名", max_length=100, blank=True)
    class_name = models.CharField("班级", max_length=100, blank=True, db_index=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STUDENT)
    # username remains a string, preserving leading zeros in student numbers.


class StudentRoster(BaseModel):
    student_number = models.CharField(max_length=32, unique=True)
    real_name = models.CharField(max_length=100)
    class_name = models.CharField(max_length=100, db_index=True)
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
