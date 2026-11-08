"""补交白名单设置服务：批量解析学号、StudentRoster 存在性校验、开关与补交截止保存。

设计约定：
- 解析只做"切分 + 去重"，不做模糊匹配；同一份粘贴文本重复提交结果稳定。
- 存在性校验以 StudentRoster 为主，已注册的 User（role=student）作为兜底，
  避免名册尚未导入时把真实学生全部标红。
- 非法学号不写入白名单，但原样回传，供页面标红展示。
"""
import re
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import StudentRoster, User
from apps.audit.services import record
from apps.common.errors import DomainError

from .models import Activity

STUDENT_NUMBER_SEPARATOR = re.compile(r"[\s,，;；、|/]+")
MAX_WHITELIST_SIZE = 5000


def parse_student_numbers(raw) -> list[str]:
    """把 textarea / 逗号串 / 列表统一解析成去重后的学号列表（保持首次出现顺序）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        tokens = [str(item) for item in raw]
    else:
        tokens = STUDENT_NUMBER_SEPARATOR.split(str(raw))
    parsed: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        value = str(token or "").strip()
        if not value:
            continue
        if len(value) > 32:
            # 超长 token 不截断，直接当作非法学号返回，交由调用方标红。
            parsed.append(value)
            seen.add(value)
            continue
        if value in seen:
            continue
        seen.add(value)
        parsed.append(value)
    if len(parsed) > MAX_WHITELIST_SIZE:
        raise DomainError(f"一次最多处理 {MAX_WHITELIST_SIZE} 个学号。")
    return parsed


def resolve_known_student_numbers(numbers: list[str]) -> dict:
    """Split numbers into known / unknown / duplicated entries."""
    if not numbers:
        return {"valid": [], "invalid": [], "duplicates": [], "total": 0}
    roster = set(
        StudentRoster.objects.filter(student_number__in=numbers, active=True).values_list(
            "student_number", flat=True
        )
    )
    accounts = set(
        User.objects.filter(username__in=numbers, role=User.Role.STUDENT, is_active=True).values_list(
            "username", flat=True
        )
    )
    known = roster | accounts
    valid = [sn for sn in numbers if sn in known]
    invalid = [sn for sn in numbers if sn not in known]
    return {"valid": valid, "invalid": invalid, "duplicates": [], "total": len(numbers)}


def _parse_deadline(raw):
    if raw is None or str(raw).strip() == "":
        return None
    value = parse_datetime(str(raw).strip())
    if value is None:
        raise DomainError("补交截止时间格式不正确，请使用 datetime-local 格式。")
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


@transaction.atomic
def update_late_submission_settings(
    *,
    actor,
    activity: Activity,
    enabled: bool,
    deadline_raw,
    raw_numbers,
) -> dict:
    """一次性保存补交开关、补交截止时间与白名单（全量替换，自动去重）。"""
    deadline = _parse_deadline(deadline_raw)
    if deadline is not None and deadline < activity.opens_at:
        raise DomainError("补交截止时间不能早于活动开始时间。")

    numbers = parse_student_numbers(raw_numbers)
    resolved = resolve_known_student_numbers(numbers)

    activity.late_submission_enabled = bool(enabled)
    activity.late_submission_deadline = deadline
    activity.late_whitelist_student_numbers = resolved["valid"]
    try:
        activity.full_clean(exclude=["allowed_classes", "quota_bytes_per_student"])
    except ValidationError as exc:
        raise DomainError("补交设置未通过校验：" + "；".join(exc.messages)) from None
    activity.save(
        update_fields=[
            "late_submission_enabled",
            "late_submission_deadline",
            "late_whitelist_student_numbers",
            "updated_at",
        ]
    )

    record(
        actor=actor,
        action="activity.late_settings.updated",
        target=activity,
        metadata={
            "enabled": activity.late_submission_enabled,
            "deadline": activity.late_submission_deadline.isoformat() if activity.late_submission_deadline else "",
            "valid_count": len(resolved["valid"]),
            "invalid": resolved["invalid"],
        },
    )
    return resolved
