"""All writes use a short transaction. No HTTP/cloud calls inside these locks."""
import copy
import json
import uuid
from django.db import transaction
from apps.accounts.permissions import require_student
from django.utils import timezone
from apps.activities.models import Activity
from apps.activities.selectors import ensure_activity_open
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError
from .models import Submission, SubmissionRevision

DECLARATION_SCHEMA_KEYS = ("project_name", "category", "declaration_type", "team_members")


def normalize_and_validate_draft_data(data, *, actor):
    if not isinstance(data, dict) or len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 32_768:
        raise DomainError("草稿必须为 JSON 对象且不能超过 32KiB。")

    if not any(key in data for key in DECLARATION_SCHEMA_KEYS):
        return data

    normalized = copy.deepcopy(data)

    if "project_name" in normalized:
        if not isinstance(normalized["project_name"], str) or not normalized["project_name"].strip():
            raise DomainError("project_name 必须为非空字符串。")
        normalized["project_name"] = normalized["project_name"].strip()
    else:
        normalized["project_name"] = ""

    if "category" in normalized:
        if not isinstance(normalized["category"], str):
            raise DomainError("category 必须为字符串。")
        normalized["category"] = normalized["category"].strip()
    else:
        normalized["category"] = ""

    raw_members = normalized.get("team_members")
    if raw_members is None:
        raw_members = []
    if not isinstance(raw_members, list):
        raise DomainError("team_members 必须为列表。")

    raw_decl_type = normalized.get("declaration_type")
    if raw_decl_type is None or str(raw_decl_type).strip() == "":
        declaration_type = (
            Submission.DeclarationType.TEAM
            if len(raw_members) > 0
            else Submission.DeclarationType.INDIVIDUAL
        )
    else:
        declaration_type = str(raw_decl_type).strip()
        if declaration_type not in (
            Submission.DeclarationType.INDIVIDUAL,
            Submission.DeclarationType.TEAM,
        ):
            raise DomainError('declaration_type 必须为 "individual" 或 "team"。')

    normalized_members = []
    seen_member_numbers = set()
    for idx, item in enumerate(raw_members):
        if not isinstance(item, dict):
            raise DomainError(f"team_members 第 {idx + 1} 项必须为对象。")
        sn = str(item.get("student_number") or "").strip()
        rn = str(item.get("real_name") or "").strip()
        cn = str(item.get("class_name") or "").strip()
        if not sn or not rn or not cn:
            raise DomainError("team_members 每项必须包含非空的 student_number、real_name、class_name。")
        if sn in seen_member_numbers:
            raise Conflict(f"同一申报内团队成员学号重复：{sn}")
        seen_member_numbers.add(sn)
        member_entry = dict(item)
        member_entry["student_number"] = sn
        member_entry["real_name"] = rn
        member_entry["class_name"] = cn
        normalized_members.append(member_entry)

    if declaration_type == Submission.DeclarationType.INDIVIDUAL:
        other_members = [m["student_number"] for m in normalized_members if m["student_number"] != actor.username]
        if other_members:
            raise DomainError("个人申报（individual）不能包含其他团队成员。")

    normalized["declaration_type"] = declaration_type
    normalized["team_members"] = normalized_members
    return normalized


def is_late_submission_attempt(activity: Activity, student_number, *, now=None) -> bool:
    """活动截止后、命中补交白名单且补交通道仍开放 -> 本次提交属于迟交。

    白名单查询集中在服务层，视图与模板只消费 Submission.is_late_submission，
    避免每个页面各自去读 Activity.late_whitelist_student_numbers。
    """
    moment = now or timezone.now()
    target = str(student_number or "").strip()
    if not target or moment < activity.deadline:
        return False
    return activity.is_in_late_whitelist(target) and activity.late_submission_allowed_at(moment)


def _extract_participant_student_numbers(*, owner_student_number, draft_data):
    participants = []
    seen = set()
    owner_sn = str(owner_student_number or "").strip()
    if owner_sn:
        participants.append(owner_sn)
        seen.add(owner_sn)
    if isinstance(draft_data, dict):
        team_members = draft_data.get("team_members")
        if isinstance(team_members, list):
            for member in team_members:
                if isinstance(member, dict):
                    sn = str(member.get("student_number") or "").strip()
                    if sn and sn not in seen:
                        participants.append(sn)
                        seen.add(sn)
    return participants


def ensure_no_duplicate_participants(*, activity, submission, owner, draft_data):
    current_participants = _extract_participant_student_numbers(
        owner_student_number=owner.username,
        draft_data=draft_data,
    )
    if not current_participants:
        return

    other_submissions = (
        Submission.objects.filter(activity=activity)
        .exclude(pk=submission.pk)
        .select_related("owner")
    )

    occupied_student_numbers = set()
    for other in other_submissions:
        # 有效申报：已保存草稿数据或已进入提交流转状态
        if not other.draft_data and other.status == Submission.Status.DRAFT:
            continue
        for sn in _extract_participant_student_numbers(
            owner_student_number=other.owner.username,
            draft_data=other.draft_data,
        ):
            occupied_student_numbers.add(sn)

    conflicts = [sn for sn in current_participants if sn in occupied_student_numbers]
    if conflicts:
        conflict_str = ", ".join(conflicts)
        raise Conflict(f"同一活动内学生不能重复出现在多个有效申报中，冲突学号：{conflict_str}")


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
    normalized_data = normalize_and_validate_draft_data(data, actor=actor)
    submission = Submission.objects.select_for_update().get(pk=submission_id, owner=actor)
    ensure_activity_open(submission.activity, actor)
    if submission.status not in (Submission.Status.DRAFT, Submission.Status.RETURNED):
        raise Conflict("当前状态不允许直接修改草稿。")
    if submission.version != expected_version:
        raise Conflict("草稿已被其他页面更新，请重新获取后再保存。")
    ensure_no_duplicate_participants(
        activity=submission.activity,
        submission=submission,
        owner=actor,
        draft_data=normalized_data,
    )
    submission.draft_data = normalized_data
    submission.version += 1
    submission.save(update_fields=["draft_data", "version", "updated_at"])
    record(actor=actor, action="submission.draft_saved", target=submission, metadata={"version": submission.version})
    return submission


@transaction.atomic
def submit_for_review(*, actor, submission_id, request_key=None):
    require_student(actor)
    submission = Submission.objects.select_for_update().get(pk=submission_id, owner=actor)
    ensure_activity_open(submission.activity, actor)
    if submission.status not in (Submission.Status.DRAFT, Submission.Status.RETURNED):
        raise Conflict(f"当前材料状态为【{submission.get_status_display()}】，无需重复提交。")
    if not submission.draft_data or not isinstance(submission.draft_data, dict):
        raise DomainError("提交材料不能为空。")

    normalized_data = normalize_and_validate_draft_data(submission.draft_data, actor=actor)
    ensure_no_duplicate_participants(
        activity=submission.activity,
        submission=submission,
        owner=actor,
        draft_data=normalized_data,
    )
    if normalized_data != submission.draft_data:
        submission.draft_data = normalized_data

    from apps.uploads.services import validate_required_materials

    validate_required_materials(actor=actor, activity=submission.activity)

    revision_number = submission.revisions.count() + 1
    key = request_key or uuid.uuid4()
    revision = SubmissionRevision.objects.create(
        submission=submission,
        number=revision_number,
        snapshot=copy.deepcopy(submission.draft_data),
        request_key=key,
    )
    submission.status = Submission.Status.PENDING_FIRST_REVIEW
    submission.is_late_submission = is_late_submission_attempt(submission.activity, actor.username)
    submission.version += 1
    submission.save(update_fields=["draft_data", "status", "is_late_submission", "version", "updated_at"])
    record(
        actor=actor,
        action="submission.submitted",
        target=submission,
        metadata={
            "revision_id": str(revision.id),
            "revision_number": revision.number,
            "status": submission.status,
            "is_late_submission": submission.is_late_submission,
        },
    )
    return submission, revision
