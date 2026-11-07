"""Export services: Excel summary generation and streaming class-based ZIP packaging."""
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import zipfile
import openpyxl
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin
from apps.activities.models import Activity
from apps.audit.services import record
from apps.common.errors import DomainError, Forbidden
from apps.reviews.models import ReviewAssignment
from apps.submissions.models import Submission
from apps.uploads.models import FileAsset, UploadSession
from apps.uploads.policies import SINGLE_FILE_MATERIAL_CODES, STANDARD_MATERIAL_RULES
from apps.uploads.services import STREAM_CHUNK_BYTES, resolve_file_disk_path
from .models import ExportJob

EXCEL_HEADERS = ["学号", "姓名", "班级", "项目名", "类别", "审核状态"]

STATUS_DISPLAY_MAP = {
    Submission.Status.DRAFT: "草稿",
    Submission.Status.PENDING_FIRST_REVIEW: "待初审",
    Submission.Status.RETURNED: "打回修改",
    Submission.Status.PENDING_FINAL_REVIEW: "待终审",
    Submission.Status.FINAL_APPROVED: "终审通过",
}

DISPLAY_TO_STATUS_MAP = {
    **{label: code for code, label in STATUS_DISPLAY_MAP.items()},
    "已通过": Submission.Status.FINAL_APPROVED,
    "approved": Submission.Status.FINAL_APPROVED,
    "已退回": Submission.Status.RETURNED,
}

ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


def sanitize_filename(name: str, default: str = "未命名") -> str:
    r"""Replace illegal filesystem characters (/  : * ? " < > | and control chars) with underscores."""
    raw = str(name or "").strip()
    if not raw:
        return default
    cleaned = ILLEGAL_FILENAME_CHARS_RE.sub("_", raw).strip().rstrip(".")
    return cleaned if cleaned else default


def _parse_activity_uuid(activity_id) -> uuid.UUID:
    try:
        return uuid.UUID(str(activity_id))
    except ValueError:
        raise DomainError("activity_id 必须为有效 UUID。") from None


def require_export_permission(actor, activity: Activity, class_name: str | None = None) -> list[str] | None:
    """Verify actor is college_admin or reviewer; return allowed class list or None if unrestricted."""
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise Forbidden("请先登录。")

    if is_college_admin(actor):
        return None

    if getattr(actor, "role", None) == User.Role.REVIEWER:
        assignments = list(
            ReviewAssignment.objects.filter(reviewer=actor, activity=activity).values_list("class_name", flat=True)
        )
        if not assignments:
            return None
        if "" in assignments:
            return None
        allowed_classes = [c.strip() for c in assignments if c and c.strip()]
        if class_name is not None and str(class_name).strip() != "":
            target_cls = str(class_name).strip()
            if target_cls not in allowed_classes:
                raise Forbidden("您无权导出该班级的申报材料。")
        return allowed_classes

    raise Forbidden("仅限管理员或审核人员访问导出功能。")


def resolve_status_filter(status_param: str | None) -> str | None:
    """Normalize status query parameter (accepts status code or Chinese display name)."""
    if status_param is None:
        return None
    raw = str(status_param).strip()
    if not raw:
        return None
    if raw in STATUS_DISPLAY_MAP:
        return raw
    if raw in DISPLAY_TO_STATUS_MAP:
        return DISPLAY_TO_STATUS_MAP[raw]
    raise DomainError(f"无效的审核状态筛选参数：{raw}")


def extract_submission_project_info(submission: Submission) -> tuple[str, str]:
    """Extract (project_name, category) from submission.draft_data (or latest revision snapshot)."""
    data = submission.draft_data if isinstance(submission.draft_data, dict) else {}
    project_name = str(data.get("project_name") or data.get("title") or "").strip()
    category = str(data.get("category") or "").strip()
    if not project_name and not category:
        latest_rev = submission.revisions.order_by("-number").first()
        if latest_rev and isinstance(latest_rev.snapshot, dict):
            project_name = str(latest_rev.snapshot.get("project_name") or latest_rev.snapshot.get("title") or "").strip()
            category = str(latest_rev.snapshot.get("category") or "").strip()
    return project_name, category


def build_filtered_submissions_queryset(*, actor, activity: Activity, class_name: str | None = None, status: str | None = None):
    """Build permission-checked and filtered Submission queryset for an activity."""
    normalized_class = str(class_name).strip() if class_name is not None and str(class_name).strip() else None
    allowed_classes = require_export_permission(actor, activity, class_name=normalized_class)
    resolved_status = resolve_status_filter(status)

    qs = Submission.objects.filter(activity=activity).select_related("owner")
    if allowed_classes is not None:
        qs = qs.filter(owner__class_name__in=allowed_classes)
    if normalized_class is not None:
        qs = qs.filter(owner__class_name=normalized_class)
    if resolved_status is not None:
        qs = qs.filter(status=resolved_status)

    return qs.order_by("owner__class_name", "owner__username", "created_at")


def list_activity_declared_classes(*, actor, activity_id) -> list[str]:
    """Return sorted distinct class names that have submissions under the given activity."""
    act_uuid = _parse_activity_uuid(activity_id)
    activity = Activity.objects.get(pk=act_uuid)
    allowed_classes = require_export_permission(actor, activity)

    qs = Submission.objects.filter(activity=activity).exclude(owner__class_name="")
    if allowed_classes is not None:
        qs = qs.filter(owner__class_name__in=allowed_classes)

    return list(qs.order_by("owner__class_name").values_list("owner__class_name", flat=True).distinct())


def generate_activity_excel_workbook(*, actor, activity_id, class_name: str | None = None, status: str | None = None):
    """Generate an openpyxl Workbook with the 6 standard summary columns."""
    act_uuid = _parse_activity_uuid(activity_id)
    activity = Activity.objects.get(pk=act_uuid)
    qs = build_filtered_submissions_queryset(
        actor=actor,
        activity=activity,
        class_name=class_name,
        status=status,
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "申报汇总表"
    ws.append(list(EXCEL_HEADERS))

    for sub in qs.iterator(chunk_size=500):
        student_number = sub.owner.username or ""
        real_name = sub.owner.real_name or ""
        student_class = sub.owner.class_name or ""
        project_name, category = extract_submission_project_info(sub)
        status_label = STATUS_DISPLAY_MAP.get(sub.status, sub.get_status_display())
        ws.append([
            student_number,
            real_name,
            student_class,
            project_name,
            category,
            status_label,
        ])

    return wb, activity


def export_activity_excel_stream(*, actor, activity_id, class_name: str | None = None, status: str | None = None):
    """Write the Excel summary workbook to a temporary disk file and return (temp_file, download_filename)."""
    wb, activity = generate_activity_excel_workbook(
        actor=actor,
        activity_id=activity_id,
        class_name=class_name,
        status=status,
    )
    tmp_file = tempfile.TemporaryFile(suffix=".xlsx")
    wb.save(tmp_file)
    wb.close()
    tmp_file.seek(0)

    clean_title = sanitize_filename(activity.title, default="活动")
    if class_name and str(class_name).strip():
        clean_cls = sanitize_filename(str(class_name).strip(), default="班级")
        filename = f"{clean_title}-{clean_cls}-汇总表.xlsx"
    else:
        filename = f"{clean_title}-汇总表.xlsx"

    job = ExportJob.objects.create(
        requested_by=actor,
        activity=activity,
        status="done",
        filters={
            "type": "excel",
            "class_name": str(class_name or "").strip(),
            "status": str(status or "").strip(),
        },
        result_key=filename,
        expires_at=timezone.now(),
    )
    record(
        actor=actor,
        action="export.excel.downloaded",
        target=job,
        metadata={"activity_id": str(activity.id), "class_name": str(class_name or "").strip(), "status": str(status or "").strip()},
    )
    return tmp_file, filename


def _build_material_arcname_filename(req_code: str, req_title: str, ext: str, index: int, total_in_category: int, max_count: int) -> str:
    """Format standardized attachment filename inside student project directory."""
    spec = STANDARD_MATERIAL_RULES.get(req_code)
    base_title = spec["title"] if spec else sanitize_filename(req_title, default=req_code or "附件")
    normalized_ext = ext.lower() if ext.startswith(".") else f".{ext.lower()}" if ext else ""

    is_single = (req_code in SINGLE_FILE_MATERIAL_CODES or max_count == 1) and req_code not in ("scene_photos", "other_materials")
    if is_single and total_in_category == 1:
        return f"{base_title}{normalized_ext}"
    return f"{base_title}_{index}{normalized_ext}"


def export_class_zip_stream(*, actor, activity_id, class_name: str, status: str | None = None):
    """Stream all submissions and attachments of a class into a temporary ZIP file on disk."""
    act_uuid = _parse_activity_uuid(activity_id)
    normalized_class = str(class_name or "").strip()
    if not normalized_class:
        raise DomainError("缺少必要参数 class_name。")

    activity = Activity.objects.get(pk=act_uuid)
    submissions = list(
        build_filtered_submissions_queryset(
            actor=actor,
            activity=activity,
            class_name=normalized_class,
            status=status,
        )
    )

    owner_ids = [sub.owner_id for sub in submissions]
    uploads_by_owner: dict[int, list[UploadSession]] = {}
    if owner_ids:
        valid_uploads = (
            UploadSession.objects.select_related("requirement", "fileasset")
            .filter(
                owner_id__in=owner_ids,
                requirement__activity=activity,
                status=UploadSession.Status.COMPLETE,
                fileasset__status=FileAsset.Status.VALID,
            )
            .order_by("created_at", "id")
        )
        for up in valid_uploads:
            uploads_by_owner.setdefault(up.owner_id, []).append(up)

    clean_class_dir = sanitize_filename(normalized_class, default="未分班")
    tmp_file = tempfile.TemporaryFile(suffix=".zip")
    used_arcnames: set[str] = set()

    with zipfile.ZipFile(tmp_file, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for sub in submissions:
            clean_student_class = sanitize_filename(sub.owner.class_name or normalized_class, default=clean_class_dir)
            clean_name = sanitize_filename(sub.owner.real_name or sub.owner.username, default="未知姓名")
            clean_sn = sanitize_filename(sub.owner.username, default="未知学号")
            raw_project_name, _ = extract_submission_project_info(sub)
            clean_project_name = sanitize_filename(raw_project_name, default="未命名项目")

            student_folder = f"{clean_student_class}/{clean_name}-{clean_sn}-{clean_project_name}"
            student_uploads = uploads_by_owner.get(sub.owner_id, [])

            grouped: dict[str, list[ tuple[UploadSession, Path] ]] = {}
            for up in student_uploads:
                try:
                    disk_path = resolve_file_disk_path(up.object_key)
                except DomainError:
                    continue
                if not disk_path.exists() or not disk_path.is_file():
                    continue
                grouped.setdefault(up.requirement.code, []).append((up, disk_path))

            for req_code, items in grouped.items():
                total = len(items)
                for idx, (up, disk_path) in enumerate(items, start=1):
                    ext = Path(up.original_name).suffix.lower() or disk_path.suffix.lower()
                    file_label = _build_material_arcname_filename(
                        req_code=req_code,
                        req_title=up.requirement.title,
                        ext=ext,
                        index=idx,
                        total_in_category=total,
                        max_count=up.requirement.max_count,
                    )
                    arcname = f"{student_folder}/{file_label}"
                    if arcname in used_arcnames:
                        stem = Path(file_label).stem
                        suffix = Path(file_label).suffix
                        dedup_idx = idx
                        while arcname in used_arcnames:
                            dedup_idx += 1
                            arcname = f"{student_folder}/{stem}_{dedup_idx}{suffix}"
                    used_arcnames.add(arcname)

                    with open(disk_path, "rb") as src_stream, zf.open(arcname, "w") as dst_stream:
                        shutil.copyfileobj(src_stream, dst_stream, length=STREAM_CHUNK_BYTES)

    tmp_file.seek(0)
    clean_title = sanitize_filename(activity.title, default="活动")
    download_filename = f"{clean_title}-{clean_class_dir}-归档材料.zip"

    job = ExportJob.objects.create(
        requested_by=actor,
        activity=activity,
        status="done",
        filters={
            "type": "zip",
            "class_name": normalized_class,
            "status": str(status or "").strip(),
        },
        result_key=download_filename,
        expires_at=timezone.now(),
    )
    record(
        actor=actor,
        action="export.zip.downloaded",
        target=job,
        metadata={
            "activity_id": str(activity.id),
            "class_name": normalized_class,
            "submission_count": len(submissions),
            "file_count": len(used_arcnames),
        },
    )
    return tmp_file, download_filename
