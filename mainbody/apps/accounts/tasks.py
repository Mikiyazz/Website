"""Accounts Celery tasks: batch account initialization and password reset off the web request thread."""
import logging
from celery import shared_task

from apps.common.errors import DomainError
from .models import StudentRoster, User
from .services import register_student, reset_password_to_default

logger = logging.getLogger(__name__)

DEFAULT_TASK_BATCH_SIZE = 100


def _chunked(seq, size: int):
    step = max(1, int(size or DEFAULT_TASK_BATCH_SIZE))
    for idx in range(0, len(seq), step):
        yield seq[idx : idx + step]


@shared_task(
    name="apps.accounts.batch_initialize_student_accounts",
    soft_time_limit=1800,
    time_limit=1860,
)
def batch_initialize_student_accounts_task(roster_ids: list[str], actor_id: int | None = None, batch_size: int = DEFAULT_TASK_BATCH_SIZE) -> dict:
    """分批从 StudentRoster 初始化学生账号（每批独立事务，避免长事务与 HTTP 超时）。"""
    clean_ids = [str(rid).strip() for rid in (roster_ids or []) if str(rid).strip()]
    created_count = 0
    skipped_count = 0
    failed_items: list[dict[str, str]] = []

    for chunk in _chunked(clean_ids, batch_size):
        rosters = list(
            StudentRoster.objects.filter(pk__in=chunk, active=True, user__isnull=True).order_by("student_number")
        )
        found_ids = {str(r.id) for r in rosters}
        skipped_count += len(chunk) - len(found_ids)
        for roster in rosters:
            try:
                register_student(
                    student_number=roster.student_number,
                    real_name=roster.real_name,
                    class_name=roster.class_name,
                    password=None,
                    grade=roster.grade,
                    major=roster.major,
                    phone=roster.phone,
                )
                created_count += 1
            except DomainError as exc:
                skipped_count += 1
                failed_items.append({"student_number": roster.student_number, "reason": str(exc)})
            except Exception as exc:  # pragma: no cover - 单条异常不得拖垮整批任务
                logger.exception("batch_init_student_failed", extra={"student_number": roster.student_number})
                failed_items.append({"student_number": roster.student_number, "reason": str(exc)})

    return {
        "requested": len(clean_ids),
        "created": created_count,
        "skipped": skipped_count,
        "failed_count": len(failed_items),
        "failed": failed_items[:50],
        "actor_id": actor_id,
    }


@shared_task(
    name="apps.accounts.batch_reset_passwords",
    soft_time_limit=1800,
    time_limit=1860,
)
def batch_reset_passwords_task(user_ids: list[int], actor_id: int | None = None, batch_size: int = DEFAULT_TASK_BATCH_SIZE) -> dict:
    """分批重置用户密码为学号后 6 位（脱离 Web 请求线程执行 PBKDF2）。"""
    clean_ids = [int(uid) for uid in (user_ids or [])]
    actor = User.objects.filter(pk=actor_id).first() if actor_id else None
    reset_count = 0
    failed_items: list[dict[str, str]] = []

    for chunk in _chunked(clean_ids, batch_size):
        users = list(User.objects.filter(pk__in=chunk).order_by("username"))
        for user in users:
            try:
                reset_password_to_default(user=user, actor=actor)
                reset_count += 1
            except Exception as exc:  # pragma: no cover
                logger.exception("batch_reset_password_failed", extra={"username": user.username})
                failed_items.append({"username": user.username, "reason": str(exc)})

    return {
        "requested": len(clean_ids),
        "reset": reset_count,
        "failed_count": len(failed_items),
        "failed": failed_items[:50],
        "actor_id": actor_id,
    }
