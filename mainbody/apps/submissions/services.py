"""All writes use a short transaction. No HTTP/cloud calls inside these locks."""
import copy
import json
import uuid
from django.db import transaction
from django.db.models import Q
from apps.accounts.models import User
from apps.accounts.permissions import require_student
from django.utils import timezone
from apps.activities.models import Activity
from apps.activities.selectors import ensure_activity_open
from apps.audit.services import record
from apps.common.errors import Conflict, DomainError
from .models import Submission, SubmissionRevision

DECLARATION_SCHEMA_KEYS = ("project_name", "category", "declaration_type", "team_members")

STUDENT_LOCKED_STATUSES = frozenset({
    Submission.Status.PENDING_FINAL_REVIEW,
    Submission.Status.FINAL_APPROVED,
})


def is_submission_locked_for_student(submission: Submission | None) -> bool:
    """初审通过（待终审）或终审通过状态下，学生不得自行修改；被打回后恢复可修改。"""
    if submission is None:
        return False
    return submission.status in STUDENT_LOCKED_STATUSES


def ensure_submission_editable_by_student(submission: Submission | None) -> None:
    if is_submission_locked_for_student(submission):
        raise Conflict(
            f"该申报当前状态为【{submission.get_status_display()}】，初审通过后不可自行修改，需由审核人员打回后再进行修改。"
        )


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


def _is_effective_team_submission(submission: Submission) -> bool:
    data = submission.draft_data if isinstance(submission.draft_data, dict) else {}
    if not data and submission.status == Submission.Status.DRAFT and not submission.declaration_submitted:
        return False
    team_members = data.get("team_members")
    if not isinstance(team_members, list) or not team_members:
        return False
    decl_type = str(data.get("declaration_type") or "").strip()
    return decl_type == Submission.DeclarationType.TEAM or not decl_type


def _team_submission_priority(submission: Submission) -> tuple[int, int, object]:
    status_rank = {
        Submission.Status.FINAL_APPROVED: 5,
        Submission.Status.PENDING_FINAL_REVIEW: 4,
        Submission.Status.PENDING_FIRST_REVIEW: 3,
        Submission.Status.RETURNED: 2,
        Submission.Status.DRAFT: 1,
    }.get(submission.status, 0)
    return (status_rank, 1 if submission.declaration_submitted else 0, submission.updated_at)


def is_member_of_submission(submission: Submission | None, student_number: str) -> bool:
    """判断指定学号是否为某条有效团队申报的队员（不含队长本人）。"""
    target = str(student_number or "").strip()
    if submission is None or not target or not _is_effective_team_submission(submission):
        return False
    owner_sn = str(getattr(submission.owner, "username", "") or "").strip()
    if target == owner_sn:
        return False
    for member in submission.draft_data.get("team_members") or []:
        if isinstance(member, dict) and str(member.get("student_number") or "").strip() == target:
            return True
    return False


def map_activity_team_memberships_all(activity_id) -> dict[str, list[Submission]]:
    """返回指定活动下「队员学号 -> 参与的全部有效团队申报列表（按优先级与更新时间降序）」。"""
    if not activity_id:
        return {}
    candidates = list(
        Submission.objects.select_related("owner", "activity")
        .filter(activity_id=activity_id)
        .order_by("-updated_at")
    )
    membership: dict[str, list[Submission]] = {}
    for sub in candidates:
        if not _is_effective_team_submission(sub):
            continue
        owner_sn = str(sub.owner.username or "").strip()
        seen_in_sub: set[str] = set()
        for member in sub.draft_data.get("team_members") or []:
            if not isinstance(member, dict):
                continue
            sn = str(member.get("student_number") or "").strip()
            if not sn or sn == owner_sn or sn in seen_in_sub:
                continue
            seen_in_sub.add(sn)
            membership.setdefault(sn, []).append(sub)
    for sn, subs in membership.items():
        subs.sort(key=_team_submission_priority, reverse=True)
    return membership


def map_activity_team_memberships(activity_id) -> dict[str, Submission]:
    """返回指定活动下「队员学号 -> 优先级最高的所属有效团队申报」映射（不含队长本人）。"""
    all_map = map_activity_team_memberships_all(activity_id)
    return {sn: subs[0] for sn, subs in all_map.items() if subs}


def find_team_membership_submissions(*, activity, student_number: str) -> list[Submission]:
    """查找某学生在指定活动下作为队员归属的全部团队申报列表。"""
    target = str(student_number or "").strip()
    if activity is None or not target:
        return []
    activity_id = getattr(activity, "id", activity)
    return list(map_activity_team_memberships_all(activity_id).get(target, []))


def find_team_membership_submission(*, activity, student_number: str) -> Submission | None:
    """查找某学生在指定活动下作为队员归属的首条团队申报；未归属任何团队时返回 None。"""
    subs = find_team_membership_submissions(activity=activity, student_number=student_number)
    return subs[0] if subs else None


def find_team_membership_activity_ids(student_number: str) -> set[uuid.UUID]:
    """查找某学生作为队员归属团队申报的所有活动 ID。"""
    target = str(student_number or "").strip()
    if not target:
        return set()
    act_ids: set[uuid.UUID] = set()
    candidates = (
        Submission.objects.select_related("owner")
        .exclude(owner__username=target)
        .exclude(draft_data={})
    )
    for sub in candidates:
        if not _is_effective_team_submission(sub):
            continue
        for member in sub.draft_data.get("team_members") or []:
            if isinstance(member, dict) and str(member.get("student_number") or "").strip() == target:
                act_ids.add(sub.activity_id)
                break
    return act_ids


def is_empty_draft_submission(submission: Submission | None) -> bool:
    if submission is None:
        return True
    if submission.status != Submission.Status.DRAFT or submission.declaration_submitted:
        return False
    data = submission.draft_data if isinstance(submission.draft_data, dict) else {}
    return not bool(data)


def redundant_member_draft_ids(activity_ids) -> list[uuid.UUID]:
    """返回冗余空白占位草稿 ID（已被纳入团队或同人同活动已有其他有效申报时，避免审核队列出现空白占位行）。"""
    from apps.uploads.models import FileAsset, UploadSession

    act_id_list = [aid for aid in (activity_ids or []) if aid]
    if not act_id_list:
        return []
    subs = list(
        Submission.objects.select_related("owner")
        .filter(activity_id__in=act_id_list)
    )
    uploaded_sub_ids = set(
        UploadSession.objects.filter(
            submission_id__in=[sub.id for sub in subs],
            status=UploadSession.Status.COMPLETE,
            fileasset__status=FileAsset.Status.VALID,
        ).values_list("submission_id", flat=True)
    )
    members_by_act: dict[object, set[str]] = {}
    effective_owners_by_act: dict[object, set[str]] = {}
    for sub in subs:
        owner_sn = str(sub.owner.username or "").strip()
        if not is_empty_draft_submission(sub) or sub.id in uploaded_sub_ids:
            effective_owners_by_act.setdefault(sub.activity_id, set()).add(owner_sn)
        if not _is_effective_team_submission(sub):
            continue
        bucket = members_by_act.setdefault(sub.activity_id, set())
        for member in sub.draft_data.get("team_members") or []:
            if isinstance(member, dict):
                sn = str(member.get("student_number") or "").strip()
                if sn and sn != owner_sn:
                    bucket.add(sn)
    redundant: list[uuid.UUID] = []
    for sub in subs:
        if is_empty_draft_submission(sub) and sub.id not in uploaded_sub_ids:
            owner_sn = str(sub.owner.username or "").strip()
            if owner_sn in members_by_act.get(sub.activity_id, ()) or owner_sn in effective_owners_by_act.get(sub.activity_id, ()):
                redundant.append(sub.id)
    return redundant


def ensure_no_duplicate_participants(*, activity, submission, owner, draft_data):
    """不同社会实践申报之间团队成员完全相互独立，仅由 normalize_and_validate_draft_data 校验单申报内部成员不重复。"""
    return None


def validate_category_for_activity(*, activity: Activity, draft_data):
    """当活动已配置自定义项目类别时，校验申报类别必须属于当前活动允许的列表。"""
    if not activity.project_categories or not isinstance(draft_data, dict):
        return
    if not any(key in draft_data for key in DECLARATION_SCHEMA_KEYS):
        return
    allowed = activity.get_project_categories()
    category = str(draft_data.get("category") or "").strip()
    if not category:
        raise DomainError("请选择当前活动允许的项目类别。")
    if category not in allowed:
        raise DomainError(f"当前活动不支持项目类别「{category}」，请从以下类别中选择：{'、'.join(allowed)}。")


@transaction.atomic
def create_draft(*, actor, activity_id, force_new: bool = False):
    require_student(actor)
    owner = User.objects.select_for_update().get(pk=actor.pk)
    require_student(owner)
    activity = Activity.objects.get(pk=activity_id)
    ensure_activity_open(activity, owner)
    if not force_new:
        existing = (
            Submission.objects.filter(owner=owner, activity=activity)
            .order_by("-updated_at", "-created_at")
            .first()
        )
        if existing is not None:
            return existing
    submission = Submission.objects.create(owner=owner, activity=activity)
    record(
        actor=owner,
        action="submission.draft_created",
        target=submission,
        metadata={"force_new": bool(force_new)},
    )
    return submission


@transaction.atomic
def save_draft(*, actor, submission_id, expected_version, data):
    require_student(actor)
    if type(expected_version) is not int or expected_version < 0:
        raise DomainError("expected_version 必须为非负整数。")
    normalized_data = normalize_and_validate_draft_data(data, actor=actor)
    submission = Submission.objects.select_for_update().get(pk=submission_id, owner=actor)
    ensure_activity_open(submission.activity, actor)
    ensure_submission_editable_by_student(submission)
    if submission.version != expected_version:
        raise Conflict("草稿已被其他页面更新，请重新获取后再保存。")
    validate_category_for_activity(activity=submission.activity, draft_data=normalized_data)
    submission.draft_data = normalized_data
    submission.version += 1
    submission.save(update_fields=["draft_data", "version", "updated_at"])
    record(actor=actor, action="submission.draft_saved", target=submission, metadata={"version": submission.version})
    return submission


@transaction.atomic
def submit_declaration(*, actor, submission_id):
    """项目立项申报提交/更新覆盖：校验项目信息、团队成员与 .docx 申报表并覆盖原记录。"""
    require_student(actor)
    submission = Submission.objects.select_for_update().get(pk=submission_id, owner=actor)
    ensure_activity_open(submission.activity, actor)
    ensure_submission_editable_by_student(submission)
    if not submission.draft_data or not isinstance(submission.draft_data, dict):
        raise DomainError("请先填写项目信息后再提交申报。")

    normalized_data = normalize_and_validate_draft_data(submission.draft_data, actor=actor)
    if not normalized_data.get("project_name"):
        raise DomainError("请填写项目名称后再提交申报。")
    if normalized_data.get("declaration_type") == Submission.DeclarationType.TEAM and not normalized_data.get("team_members"):
        raise DomainError("团队申报至少需要添加 1 名团队成员。")
    validate_category_for_activity(activity=submission.activity, draft_data=normalized_data)

    from apps.uploads.services import validate_declaration_materials

    validate_declaration_materials(actor=actor, activity=submission.activity, submission=submission)

    now = timezone.now()
    was_submitted_before = bool(submission.declaration_submitted)
    submission.draft_data = normalized_data
    submission.declaration_submitted = True
    submission.declaration_submitted_at = now
    update_fields = [
        "draft_data",
        "declaration_submitted",
        "declaration_submitted_at",
        "version",
        "updated_at",
    ]
    if submission.status in (
        Submission.Status.PENDING_FIRST_REVIEW,
        Submission.Status.PENDING_FINAL_REVIEW,
        Submission.Status.FINAL_APPROVED,
    ):
        revision_number = submission.revisions.count() + 1
        SubmissionRevision.objects.create(
            submission=submission,
            number=revision_number,
            snapshot=copy.deepcopy(normalized_data),
            request_key=uuid.uuid4(),
        )
        submission.status = Submission.Status.PENDING_FIRST_REVIEW
        submission.is_late_submission = is_late_submission_attempt(submission.activity, actor.username)
        update_fields.extend(["status", "is_late_submission"])

    submission.version += 1
    submission.save(update_fields=update_fields)
    record(
        actor=actor,
        action="submission.declaration_submitted",
        target=submission,
        metadata={
            "project_name": normalized_data.get("project_name", ""),
            "declaration_type": normalized_data.get("declaration_type", ""),
            "declaration_submitted_at": now.isoformat(),
            "is_overwrite": was_submitted_before,
        },
    )
    return submission


@transaction.atomic
def submit_for_review(*, actor, submission_id, request_key=None):
    require_student(actor)
    submission = Submission.objects.select_for_update().get(pk=submission_id, owner=actor)
    ensure_activity_open(submission.activity, actor)
    ensure_submission_editable_by_student(submission)
    if not submission.draft_data or not isinstance(submission.draft_data, dict):
        raise DomainError("提交材料不能为空。")

    normalized_data = normalize_and_validate_draft_data(submission.draft_data, actor=actor)
    validate_category_for_activity(activity=submission.activity, draft_data=normalized_data)
    if normalized_data != submission.draft_data:
        submission.draft_data = normalized_data

    from apps.uploads.services import validate_required_materials

    validate_required_materials(actor=actor, activity=submission.activity, submission=submission)

    is_overwrite = submission.status in (
        Submission.Status.PENDING_FIRST_REVIEW,
        Submission.Status.PENDING_FINAL_REVIEW,
        Submission.Status.FINAL_APPROVED,
    )
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
    if not submission.declaration_submitted:
        submission.declaration_submitted = True
        submission.declaration_submitted_at = timezone.now()
    submission.version += 1
    submission.save(
        update_fields=[
            "draft_data",
            "status",
            "declaration_submitted",
            "declaration_submitted_at",
            "is_late_submission",
            "version",
            "updated_at",
        ]
    )
    record(
        actor=actor,
        action="submission.submitted",
        target=submission,
        metadata={
            "revision_id": str(revision.id),
            "revision_number": revision.number,
            "status": submission.status,
            "is_late_submission": submission.is_late_submission,
            "is_overwrite": is_overwrite,
        },
    )
    return submission, revision


def first_approved_submission_ids(submission_ids) -> set[uuid.UUID]:
    """批量返回已经通过初审（包含初审通过记录）的申报 ID 集合。"""
    id_list = [sid for sid in (submission_ids or []) if sid]
    if not id_list:
        return set()
    from apps.reviews.models import ReviewRecord

    return set(
        ReviewRecord.objects.filter(
            submission_id__in=id_list,
            stage=ReviewRecord.Stage.FIRST,
            decision=ReviewRecord.Decision.APPROVED,
        ).values_list("submission_id", flat=True)
    )


def can_delete_submission(submission: Submission | None, *, has_passed_first_review: bool | None = None) -> bool:
    """申报在未通过初审前允许删除；已通过初审（待终审、终审通过或含初审通过记录）禁止删除。"""
    if submission is None:
        return False
    if submission.status not in (
        Submission.Status.DRAFT,
        Submission.Status.PENDING_FIRST_REVIEW,
        Submission.Status.RETURNED,
    ):
        return False
    if has_passed_first_review is None:
        from apps.reviews.models import ReviewRecord

        has_passed_first_review = ReviewRecord.objects.filter(
            submission=submission,
            stage=ReviewRecord.Stage.FIRST,
            decision=ReviewRecord.Decision.APPROVED,
        ).exists()
    return not has_passed_first_review


@transaction.atomic
def delete_submission(*, actor, submission_id) -> dict:
    """删除本人名下尚未通过初审的申报，并级联清理关联附件、配额、版本快照与审核记录。"""
    require_student(actor)
    owner = User.objects.select_for_update().get(pk=actor.pk)
    require_student(owner)
    submission = (
        Submission.objects.select_for_update()
        .select_related("activity")
        .get(pk=submission_id, owner=owner)
    )
    ensure_activity_open(submission.activity, owner)

    if not can_delete_submission(submission):
        raise Conflict("该申报已通过初审，不可删除。")

    from apps.news.models import NewsDraft
    from apps.reviews.models import ReviewRecord
    from apps.uploads.models import FileAsset, StorageQuota, UploadSession
    from apps.uploads.services import remove_chunk_staging_dir, remove_upload_object

    sub_count = Submission.objects.filter(owner=owner, activity=submission.activity).count()
    upload_filter = Q(submission=submission)
    if sub_count <= 1:
        upload_filter |= Q(submission__isnull=True, owner=owner, requirement__activity=submission.activity)

    uploads = list(
        UploadSession.objects.select_for_update()
        .filter(upload_filter)
        .order_by("created_at")
    )
    upload_ids = [up.id for up in uploads]
    assets_by_upload_id = {
        asset.upload_id: asset
        for asset in FileAsset.objects.select_for_update().filter(upload_id__in=upload_ids)
    } if upload_ids else {}

    release_used = 0
    release_reserved = 0
    object_keys_to_remove: list[str] = []
    chunk_uploads_to_clean: list[str] = []

    for up in uploads:
        if up.status != UploadSession.Status.ABORTED:
            if up.status == UploadSession.Status.COMPLETE:
                asset = assets_by_upload_id.get(up.id)
                release_used += asset.actual_bytes if asset is not None else up.declared_bytes
            else:
                release_reserved += up.declared_bytes
        if up.object_key:
            if str(up.object_key).startswith("tmp/"):
                chunk_uploads_to_clean.append(str(up.object_key))
            else:
                object_keys_to_remove.append(str(up.object_key))

    if release_used > 0 or release_reserved > 0:
        quota = (
            StorageQuota.objects.select_for_update()
            .filter(owner=owner, activity=submission.activity)
            .first()
        )
        if quota is not None:
            quota.used_bytes = max(0, quota.used_bytes - release_used)
            quota.reserved_bytes = max(0, quota.reserved_bytes - release_reserved)
            quota.save(update_fields=["used_bytes", "reserved_bytes", "updated_at"])

    if upload_ids:
        FileAsset.objects.filter(upload_id__in=upload_ids).delete()
        UploadSession.objects.filter(id__in=upload_ids).delete()

    NewsDraft.objects.filter(submission=submission).delete()
    ReviewRecord.objects.filter(Q(submission=submission) | Q(revision__submission=submission)).delete()
    SubmissionRevision.objects.filter(submission=submission).delete()

    deleted_info = {
        "id": str(submission.id),
        "activity_id": str(submission.activity_id),
        "project_name": (
            str(submission.draft_data.get("project_name") or "").strip()
            if isinstance(submission.draft_data, dict)
            else ""
        ),
        "status": submission.status,
        "deleted_uploads": len(upload_ids),
    }

    record(
        actor=owner,
        action="submission.deleted",
        target=submission,
        metadata=deleted_info,
    )
    submission.delete()

    def _cleanup_disk():
        for key in object_keys_to_remove:
            remove_upload_object(key)
        for tmp_key in chunk_uploads_to_clean:
            remove_chunk_staging_dir(type("_TmpUpload", (), {"object_key": tmp_key})())

    transaction.on_commit(_cleanup_disk)
    return deleted_info
