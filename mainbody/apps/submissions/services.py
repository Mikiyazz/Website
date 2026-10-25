"""All writes use a short transaction. No HTTP/cloud calls inside these locks."""
import json
from django.db import transaction
from apps.accounts.permissions import require_student
from apps.activities.models import Activity
from apps.activities.selectors import ensure_activity_open
from apps.common.errors import Conflict, DomainError, FeatureUnavailable
from apps.audit.services import record
from .models import Submission


@transaction.atomic
def create_draft(*, actor, activity_id):
    require_student(actor)
    activity = Activity.objects.get(pk=activity_id)
    ensure_activity_open(activity, actor)
    # Unique constraint + get_or_create safely resolves two simultaneous first clicks.
    submission, created = Submission.objects.get_or_create(owner=actor, activity=activity)
    if created:
        record(actor=actor, action="submission.draft_created", target=submission)
    return submission


@transaction.atomic
def save_draft(*, actor, submission_id, expected_version, data):
    require_student(actor)
    if type(expected_version) is not int or expected_version < 0:
        raise DomainError("expected_version 必须为非负整数。")
    if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 32_768:
        raise DomainError("草稿必须为 JSON 对象且不能超过 32KiB。")
    submission = Submission.objects.select_for_update().get(pk=submission_id, owner=actor)
    ensure_activity_open(submission.activity, actor)
    if submission.status not in (Submission.Status.DRAFT, Submission.Status.RETURNED):
        raise Conflict("已提交版本不能直接修改。")
    if submission.version != expected_version:
        raise Conflict("草稿已被其他页面更新，请重新获取后再保存。")
    submission.draft_data = data
    submission.version += 1
    submission.save(update_fields=["draft_data", "version", "updated_at"])
    record(actor=actor, action="submission.draft_saved", target=submission, metadata={"version": submission.version})
    return submission


def submit_for_review(*, actor, submission_id):
    require_student(actor)
    Submission.objects.get(pk=submission_id, owner=actor)
    raise FeatureUnavailable("正式提交尚未接入材料完整性检查和不可变文件快照。")
