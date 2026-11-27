import re
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from apps.common.models import BaseModel

DEFAULT_PROJECT_CATEGORIES = (
    "社会调查",
    "红色寻访",
    "科普宣讲",
    "乡村振兴",
    "科技发明制作",
    "文化传承",
    "志愿服务",
    "学术论文",
)
MAX_PROJECT_CATEGORIES = 50
MAX_PROJECT_CATEGORY_LENGTH = 50
_PROJECT_CATEGORY_SEPARATOR = re.compile(r"[\r\n,，;；、]+")


def parse_project_categories(raw) -> list[str]:
    """将文本（换行/逗号/顿号/分号分隔）或列表规范化为保序去重的项目类别列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        tokens = [str(item) for item in raw]
    elif isinstance(raw, str):
        tokens = _PROJECT_CATEGORY_SEPARATOR.split(raw)
    else:
        raise ValueError("项目类别格式不正确，请使用列表或文本。")

    categories: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        value = str(token or "").strip()
        if not value:
            continue
        if len(value) > MAX_PROJECT_CATEGORY_LENGTH:
            raise ValueError(f"单个项目类别名称不能超过 {MAX_PROJECT_CATEGORY_LENGTH} 个字符：{value[:20]}…")
        if value in seen:
            continue
        seen.add(value)
        categories.append(value)
    if len(categories) > MAX_PROJECT_CATEGORIES:
        raise ValueError(f"单个活动最多配置 {MAX_PROJECT_CATEGORIES} 个项目类别。")
    return categories


class Activity(BaseModel):
    title = models.CharField(max_length=200)
    opens_at = models.DateTimeField()
    deadline = models.DateTimeField()
    is_open = models.BooleanField(default=False)
    # Empty list means all active students. Explicit class list restricts access.
    allowed_classes = models.JSONField(default=list, blank=True)
    # 留空表示使用系统默认 8 个项目类别；非空时学生申报只能从该活动设定的类别中选择。
    project_categories = models.JSONField("申报项目类别", default=list, blank=True)
    # Late submission whitelist containing student numbers allowed to save/submit after deadline.
    late_whitelist_student_numbers = models.JSONField("补交白名单", default=list, blank=True)
    # 补交设置卡片的总开关与补交截止时间：关闭时白名单整体失效；
    # 补交截止为空表示“活动截止后长期开放给白名单学生”。
    late_submission_enabled = models.BooleanField("启用补交白名单", default=True)
    late_submission_deadline = models.DateTimeField("补交截止时间", null=True, blank=True)
    # 单生默认配额 12 GiB = 12,884,901,888 B：容纳业务上限 5 个 2000MB 打包件（10,485,760,000 B）
    # + 约 2.4 GB（2.23 GiB）给申报表/报告/照片/宣讲视频。
    # 矿大服务器部署核定建议：请结合数据盘容量按活动调整（例：500GB 数据盘 2000 人建议设为 1–2 GiB；
    # 2TB 数据盘建议设为 4–6 GiB），并由 UPLOAD_MIN_FREE_DISK_BYTES（默认 5 GiB）兜底防写爆磁盘。
    quota_bytes_per_student = models.PositiveBigIntegerField(default=12_884_901_888)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(deadline__gt=F("opens_at")), name="activity_positive_window"),
            models.CheckConstraint(condition=Q(quota_bytes_per_student__gt=0), name="activity_positive_quota"),
        ]
        ordering = ["-created_at"]

    def clean(self):
        if not isinstance(self.allowed_classes, list) or any(not isinstance(x, str) for x in self.allowed_classes):
            raise ValidationError({"allowed_classes": "必须填写班级名称的 JSON 列表。"})
        if not isinstance(self.project_categories, list) or any(
            not isinstance(x, str) or not x.strip() or len(x.strip()) > MAX_PROJECT_CATEGORY_LENGTH
            for x in self.project_categories
        ):
            raise ValidationError(
                {"project_categories": f"必须填写非空项目类别字符串列表，单项不超过 {MAX_PROJECT_CATEGORY_LENGTH} 字。"}
            )
        if len(self.project_categories) > MAX_PROJECT_CATEGORIES:
            raise ValidationError({"project_categories": f"单个活动最多配置 {MAX_PROJECT_CATEGORIES} 个项目类别。"})
        if not isinstance(self.late_whitelist_student_numbers, list) or any(
            not isinstance(x, str) or not x.strip() for x in self.late_whitelist_student_numbers
        ):
            raise ValidationError({"late_whitelist_student_numbers": "必须填写补交学生学号字符串的 JSON 列表。"})

    def get_project_categories(self) -> list[str]:
        """返回当前活动生效的申报项目类别列表；未单独配置时回退默认 8 项。"""
        custom: list[str] = []
        seen: set[str] = set()
        for item in self.project_categories or []:
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                custom.append(value)
        return custom if custom else list(DEFAULT_PROJECT_CATEGORIES)

    def late_submission_allowed_at(self, moment=None) -> bool:
        """白名单通道当前是否仍开放：开关打开且未超过补交截止时间。"""
        if not self.late_submission_enabled:
            return False
        if self.late_submission_deadline is None:
            return True
        from django.utils import timezone

        return (moment or timezone.now()) < self.late_submission_deadline

    def is_in_late_whitelist(self, student_number: str) -> bool:
        target = str(student_number or "").strip()
        if not target:
            return False
        whitelist = {str(x).strip() for x in (self.late_whitelist_student_numbers or []) if str(x).strip()}
        return target in whitelist

    def add_to_late_whitelist(self, *student_numbers, save: bool = True):
        current = [str(x).strip() for x in (self.late_whitelist_student_numbers or []) if str(x).strip()]
        seen = set(current)
        for sn in student_numbers:
            normalized = str(sn or "").strip()
            if normalized and normalized not in seen:
                current.append(normalized)
                seen.add(normalized)
        self.late_whitelist_student_numbers = current
        if save:
            self.save(update_fields=["late_whitelist_student_numbers", "updated_at"])
        return self.late_whitelist_student_numbers

    def remove_from_late_whitelist(self, *student_numbers, save: bool = True):
        to_remove = {str(sn or "").strip() for sn in student_numbers if str(sn or "").strip()}
        current = [
            str(x).strip()
            for x in (self.late_whitelist_student_numbers or [])
            if str(x).strip() and str(x).strip() not in to_remove
        ]
        self.late_whitelist_student_numbers = current
        if save:
            self.save(update_fields=["late_whitelist_student_numbers", "updated_at"])
        return self.late_whitelist_student_numbers

    def __str__(self):
        return self.title


class MaterialRequirement(BaseModel):
    activity = models.ForeignKey(Activity, on_delete=models.PROTECT, related_name="requirements")
    code = models.SlugField(max_length=64)
    title = models.CharField(max_length=100)
    required = models.BooleanField(default=True)
    allowed_extensions = models.JSONField(default=list)
    max_file_bytes = models.PositiveBigIntegerField(default=10_000_000_000)
    min_count = models.PositiveIntegerField(default=0)
    max_count = models.PositiveIntegerField(default=1)
    rule_version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["activity", "code"], name="unique_activity_material_code"),
            models.CheckConstraint(condition=Q(max_file_bytes__gt=0) & Q(max_file_bytes__lte=10_000_000_000), name="material_file_cap"),
            models.CheckConstraint(condition=Q(max_count__gt=0), name="material_positive_count"),
        ]

    def clean(self):
        extensions = self.allowed_extensions
        if not isinstance(extensions, list) or not extensions or any(
            not isinstance(x, str) or not x.startswith(".") or x != x.lower() or "/" in x or "\\" in x
            for x in extensions
        ):
            raise ValidationError({"allowed_extensions": '例如 [".pdf", ".docx"]，不能为空。'})
