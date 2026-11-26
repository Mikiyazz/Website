"""Export services: Excel summary generation and streaming class-based ZIP packaging."""
from pathlib import Path
import logging
import re
import shutil
import tempfile
import time
import uuid
import zipfile
import openpyxl
from django.conf import settings
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.permissions import (
    is_college_admin,
    is_reviewer,
    reviewer_allowed_class_names,
    reviewer_user_scope_q,
)
from apps.activities.models import Activity
from apps.audit.services import record
from apps.common.errors import DomainError, Forbidden
from apps.submissions.models import Submission
from apps.uploads.models import UploadSession
from apps.uploads.policies import SINGLE_FILE_MATERIAL_CODES, STANDARD_MATERIAL_CODES, STANDARD_MATERIAL_RULES
from apps.uploads.services import STREAM_CHUNK_BYTES, map_uploads_by_submission, resolve_file_disk_path
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

    if is_reviewer(actor):
        allowed_classes = reviewer_allowed_class_names(actor, activity_id=activity.id)
        if not allowed_classes:
            raise Forbidden("当前账号尚未绑定负责班级，无法导出申报材料。")
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


def build_filtered_submissions_queryset(
    *,
    actor,
    activity: Activity,
    class_name: str | None = None,
    status: str | None = None,
    category: str | None = None,
    declaration_type: str | None = None,
):
    """Build permission-checked and filtered Submission queryset for an activity."""
    from apps.reviews.selectors import declaration_type_filter_q
    from apps.submissions.services import redundant_member_draft_ids

    normalized_class = str(class_name).strip() if class_name is not None and str(class_name).strip() else None
    normalized_category = str(category).strip() if category is not None and str(category).strip() else None
    allowed_classes = require_export_permission(actor, activity, class_name=normalized_class)
    resolved_status = resolve_status_filter(status)

    qs = Submission.objects.filter(activity=activity).select_related("owner")
    redundant_ids = redundant_member_draft_ids([activity.id])
    if redundant_ids:
        qs = qs.exclude(pk__in=redundant_ids)
    if not is_college_admin(actor):
        qs = qs.filter(reviewer_user_scope_q(actor, prefix="owner__", activity_id=activity.id))
    elif allowed_classes is not None:
        qs = qs.filter(owner__class_name__in=allowed_classes)
    if normalized_class is not None:
        qs = qs.filter(owner__class_name=normalized_class)
    if resolved_status is not None:
        qs = qs.filter(status=resolved_status)
    if normalized_category is not None:
        qs = qs.filter(draft_data__category=normalized_category)
    decl_q = declaration_type_filter_q(declaration_type)
    if decl_q is not None:
        qs = qs.filter(decl_q)

    return qs.order_by("owner__class_name", "owner__username", "created_at")


def list_activity_declared_classes(*, actor, activity_id) -> list[str]:
    """Return sorted distinct class names that have submissions under the given activity."""
    act_uuid = _parse_activity_uuid(activity_id)
    activity = Activity.objects.get(pk=act_uuid)
    allowed_classes = require_export_permission(actor, activity)

    qs = Submission.objects.filter(activity=activity).exclude(owner__class_name="")
    if not is_college_admin(actor):
        qs = qs.filter(reviewer_user_scope_q(actor, prefix="owner__", activity_id=activity.id))
    elif allowed_classes is not None:
        qs = qs.filter(owner__class_name__in=allowed_classes)

    return list(qs.order_by("owner__class_name").values_list("owner__class_name", flat=True).distinct())


def generate_activity_excel_workbook(
    *,
    actor,
    activity_id,
    class_name: str | None = None,
    status: str | None = None,
    category: str | None = None,
    declaration_type: str | None = None,
):
    """Generate an openpyxl Workbook with the 6 standard summary columns."""
    act_uuid = _parse_activity_uuid(activity_id)
    activity = Activity.objects.get(pk=act_uuid)
    qs = build_filtered_submissions_queryset(
        actor=actor,
        activity=activity,
        class_name=class_name,
        status=status,
        category=category,
        declaration_type=declaration_type,
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


def export_activity_excel_stream(
    *,
    actor,
    activity_id,
    class_name: str | None = None,
    status: str | None = None,
    category: str | None = None,
    declaration_type: str | None = None,
):
    """Write the Excel summary workbook to a temporary disk file and return (temp_file, download_filename)."""
    wb, activity = generate_activity_excel_workbook(
        actor=actor,
        activity_id=activity_id,
        class_name=class_name,
        status=status,
        category=category,
        declaration_type=declaration_type,
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


def export_class_zip_stream(
    *,
    actor,
    activity_id,
    class_name: str,
    status: str | None = None,
    category: str | None = None,
    declaration_type: str | None = None,
):
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
            category=category,
            declaration_type=declaration_type,
        )
    )

    uploads_by_submission = map_uploads_by_submission(submissions, activity=activity)

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
            student_uploads = uploads_by_submission.get(sub.id, [])

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

# =====================================================================
# 异步导出（Celery）：整包单 ZIP，按 年级/班级/姓名+学号/材料文件 分类归档
# =====================================================================

EXPORT_ROOT_DIRNAME = "exports"
# 单个文件超过 20MB（典型是宣讲视频）改用 STORED，避免 worker 把 CPU 花在 deflate 上；
# 无论哪种压缩方式，拷贝都按 STREAM_CHUNK_BYTES 分块，绝不整文件 read()。
EXPORT_STORED_THRESHOLD_BYTES = 20 * 1024 * 1024
EXPORT_PROGRESS_FLUSH_BYTES = 4 * 1024 * 1024
EXPORT_PROGRESS_FLUSH_SECONDS = 0.5

EXPORT_MODE_DECLARATION_ONLY = "declaration_only"
EXPORT_MODE_DECLARATION_AND_OUTCOMES = "declaration_and_outcomes"
DEFAULT_EXPORT_MODE = EXPORT_MODE_DECLARATION_AND_OUTCOMES

EXPORT_MODE_CHOICES = (
    (EXPORT_MODE_DECLARATION_ONLY, "导出申报表"),
    (EXPORT_MODE_DECLARATION_AND_OUTCOMES, "导出申报表和成果"),
)
EXPORT_MODE_LABELS = dict(EXPORT_MODE_CHOICES)
EXPORT_MODE_ARCHIVE_SUFFIX = {
    EXPORT_MODE_DECLARATION_ONLY: "申报表归档",
    EXPORT_MODE_DECLARATION_AND_OUTCOMES: "申报表和成果归档",
}
DECLARATION_MATERIAL_CODE = "application_form"


def normalize_export_mode(raw) -> str:
    """规范化导出模式；留空回退默认模式，非法值抛出 DomainError。"""
    if raw is None or str(raw).strip() == "":
        return DEFAULT_EXPORT_MODE
    mode = str(raw).strip()
    if mode not in EXPORT_MODE_LABELS:
        raise DomainError("无效的导出模式，请选择「导出申报表」或「导出申报表和成果」。")
    return mode


def export_relative_dir(activity_id) -> str:
    return f"{EXPORT_ROOT_DIRNAME}/{activity_id}"


def export_absolute_dir(activity_id) -> Path:
    target = Path(settings.MEDIA_ROOT) / EXPORT_ROOT_DIRNAME / str(activity_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _material_title(code: str, fallback: str) -> str:
    spec = STANDARD_MATERIAL_RULES.get(str(code or ""))
    raw = (spec or {}).get("title") or fallback or code or "附件"
    return sanitize_filename(raw, default="附件")


def _resolve_grade_dir(owner) -> str:
    return sanitize_filename(getattr(owner, "grade", ""), default="未填年级")


def _resolve_class_dir(owner) -> str:
    from apps.accounts.models import CLASS_NAME_RE

    raw_class = str(getattr(owner, "class_name", "") or "").strip()
    raw_major = str(getattr(owner, "major", "") or "").strip()
    if raw_class:
        if raw_major and CLASS_NAME_RE.match(raw_class):
            return sanitize_filename(f"{raw_major}{raw_class}", default="未分班")
        return sanitize_filename(raw_class, default="未分班")
    if raw_major:
        return sanitize_filename(raw_major, default="未分班")
    return "未分班"


def _student_dir_name(submission: Submission, *, multi_for_owner: bool = False) -> str:
    grade_dir = _resolve_grade_dir(submission.owner)
    class_dir = _resolve_class_dir(submission.owner)
    real_name = sanitize_filename(submission.owner.real_name or submission.owner.username, default="未知姓名")
    student_number = sanitize_filename(submission.owner.username, default="未知学号")
    student_leaf = f"{real_name}+{student_number}"
    if multi_for_owner:
        raw_project_name, _ = extract_submission_project_info(submission)
        clean_project = sanitize_filename(raw_project_name, default=str(submission.id)[:8])
        student_leaf = f"{student_leaf}-{clean_project}"
    return f"{grade_dir}/{class_dir}/{student_leaf}"


def _has_submitted_declaration(submission: Submission) -> bool:
    """判定是否已提交立项申报（含兼容已进入审核流转的历史记录）。"""
    return bool(submission.declaration_submitted) or submission.status != Submission.Status.DRAFT


def _has_submitted_outcomes(submission: Submission) -> bool:
    """判定是否已提交成果审核（草稿状态均视为未提交成果）。"""
    return submission.status != Submission.Status.DRAFT


def _ordered_requirement_codes(grouped: dict[str, list]) -> list[str]:
    standard_order = {code: idx for idx, code in enumerate(STANDARD_MATERIAL_CODES)}
    return sorted(grouped.keys(), key=lambda code: (standard_order.get(code, 999), code))


def build_export_entries(*, activity: Activity, allowed_classes=None, mode: str | None = None, actor=None) -> list[dict]:
    """Collect (source file, archive name, size) tuples for one activity without touching bytes."""
    from apps.submissions.services import redundant_member_draft_ids

    resolved_mode = normalize_export_mode(mode)
    qs = (
        Submission.objects.filter(activity=activity)
        .select_related("owner")
        .order_by("owner__grade", "owner__class_name", "owner__username", "created_at")
    )
    redundant_ids = redundant_member_draft_ids([activity.id])
    if redundant_ids:
        qs = qs.exclude(pk__in=redundant_ids)
    if actor is not None and not is_college_admin(actor):
        qs = qs.filter(reviewer_user_scope_q(actor, prefix="owner__", activity_id=activity.id))

    submissions = list(qs)
    if allowed_classes is not None:
        allowed = {str(item).strip() for item in allowed_classes if str(item).strip()}
        submissions = [sub for sub in submissions if str(sub.owner.class_name or "").strip() in allowed]

    uploads_by_submission = map_uploads_by_submission(submissions, activity=activity)

    # 先按导出模式严格筛选符合提交状态且存在有效磁盘文件的申报及附件集合
    selected_records: list[tuple[Submission, list[tuple[UploadSession, Path, int]]]] = []
    for submission in submissions:
        valid_items: list[tuple[UploadSession, Path, int]] = []
        for upload in uploads_by_submission.get(submission.id, []):
            try:
                source_path = resolve_file_disk_path(upload.object_key)
            except DomainError:
                continue
            if not source_path.exists() or not source_path.is_file():
                continue
            try:
                size = source_path.stat().st_size
            except OSError:
                continue
            valid_items.append((upload, source_path, size))

        decl_items = [item for item in valid_items if item[0].requirement.code == DECLARATION_MATERIAL_CODE]
        outcome_items = [item for item in valid_items if item[0].requirement.code != DECLARATION_MATERIAL_CODE]

        if resolved_mode == EXPORT_MODE_DECLARATION_ONLY:
            if not _has_submitted_declaration(submission) or not decl_items:
                continue
            chosen_items = decl_items
        else:
            if not _has_submitted_outcomes(submission) or not outcome_items:
                continue
            chosen_items = decl_items + outcome_items

        selected_records.append((submission, chosen_items))

    owner_sub_counts: dict[int, int] = {}
    for submission, _ in selected_records:
        owner_sub_counts[submission.owner_id] = owner_sub_counts.get(submission.owner_id, 0) + 1

    entries: list[dict] = []
    used_arcnames: set[str] = set()
    for submission, chosen_items in selected_records:
        student_dir = _student_dir_name(
            submission,
            multi_for_owner=owner_sub_counts.get(submission.owner_id, 0) > 1,
        )
        prefix = "late_" if submission.is_late_submission else ""

        grouped: dict[str, list[tuple[UploadSession, Path, int]]] = {}
        for item in chosen_items:
            grouped.setdefault(item[0].requirement.code, []).append(item)

        for req_code in _ordered_requirement_codes(grouped):
            items = grouped[req_code]
            total = len(items)
            for idx, (upload, source_path, size) in enumerate(items, start=1):
                ext = Path(upload.original_name).suffix.lower() or source_path.suffix.lower()
                file_label = _build_material_arcname_filename(
                    req_code=req_code,
                    req_title=upload.requirement.title,
                    ext=ext,
                    index=idx,
                    total_in_category=total,
                    max_count=upload.requirement.max_count,
                )
                filename = sanitize_filename(f"{prefix}{file_label}", default="附件")
                arcname = f"{student_dir}/{filename}"
                if arcname in used_arcnames:
                    stem = Path(filename).stem
                    suffix_ext = Path(filename).suffix
                    dedup = 2
                    while f"{student_dir}/{stem}_{dedup}{suffix_ext}" in used_arcnames:
                        dedup += 1
                    arcname = f"{student_dir}/{stem}_{dedup}{suffix_ext}"
                used_arcnames.add(arcname)
                entries.append(
                    {
                        "arcname": arcname,
                        "source_path": source_path,
                        "size": size,
                        "submission_id": str(submission.id),
                        "is_late": submission.is_late_submission,
                    }
                )

    entries.sort(key=lambda item: item["arcname"])
    return entries


class _ExportProgress:
    """Throttled progress writer: at most one UPDATE per 4MB or 0.5s."""

    def __init__(self, job_id, total_bytes: int):
        self.job_id = job_id
        self.total_bytes = total_bytes
        self.current_bytes = 0
        self._pending_bytes = 0
        self._last_flush = time.monotonic()

    def add(self, byte_count: int) -> None:
        self.current_bytes += byte_count
        self._pending_bytes += byte_count
        if (
            self._pending_bytes >= EXPORT_PROGRESS_FLUSH_BYTES
            or time.monotonic() - self._last_flush >= EXPORT_PROGRESS_FLUSH_SECONDS
        ):
            self.flush()

    def flush(self) -> None:
        ExportJob.objects.filter(pk=self.job_id).update(
            current=self.current_bytes,
            total=self.total_bytes,
            updated_at=timezone.now(),
        )
        self._pending_bytes = 0
        self._last_flush = time.monotonic()


def _write_zip_entry(zf: zipfile.ZipFile, entry: dict, progress: _ExportProgress) -> None:
    """Stream one file into the archive; large media goes in STORED to keep worker memory flat."""
    source_path: Path = entry["source_path"]
    info = zipfile.ZipInfo.from_file(source_path, entry["arcname"])
    info.compress_type = (
        zipfile.ZIP_STORED if entry["size"] >= EXPORT_STORED_THRESHOLD_BYTES else zipfile.ZIP_DEFLATED
    )
    info.flag_bits |= 0x800  # UTF-8 file name flag
    with open(source_path, "rb") as source_stream, zf.open(info, "w") as dest_stream:
        while True:
            chunk = source_stream.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            dest_stream.write(chunk)
            progress.add(len(chunk))


def create_activity_export_job(*, actor, activity_id, mode: str | None = None) -> ExportJob:
    """Create a pending ExportJob after permission checks; celery task owns the heavy lifting."""
    try:
        activity = Activity.objects.get(pk=_parse_activity_uuid(activity_id))
    except Activity.DoesNotExist:
        raise DomainError("导出失败：活动不存在或已被删除。") from None
    allowed_classes = require_export_permission(actor, activity)
    resolved_mode = normalize_export_mode(mode)
    mode_label = EXPORT_MODE_LABELS[resolved_mode]
    job = ExportJob.objects.create(
        requested_by=actor,
        activity=activity,
        status=ExportJob.Status.PENDING,
        filters={
            "type": "activity_zip",
            "mode": resolved_mode,
            "mode_label": mode_label,
            "classes": list(allowed_classes or []),
            "scope": "activity",
        },
    )
    record(
        actor=actor,
        action="export.activity.started",
        target=job,
        metadata={
            "activity_id": str(activity.id),
            "job_id": str(job.id),
            "mode": resolved_mode,
            "classes": list(allowed_classes or []),
        },
    )
    return job


def run_activity_export_job(job_id) -> ExportJob:
    """Build MEDIA_ROOT/exports/{activity_id}/{uuid}.zip with DB-backed progress."""
    job = ExportJob.objects.select_related("activity", "requested_by").get(pk=job_id)
    activity = job.activity
    now = timezone.now()
    ExportJob.objects.filter(pk=job.pk).update(
        status=ExportJob.Status.RUNNING, started_at=now, total=0, current=0, updated_at=now
    )

    try:
        filters = dict(job.filters or {})
        raw_classes = filters.get("classes")
        if is_college_admin(job.requested_by):
            allowed_classes = raw_classes if raw_classes else None
        else:
            allowed_classes = list(raw_classes) if raw_classes is not None else []
        resolved_mode = normalize_export_mode(filters.get("mode"))
        mode_label = EXPORT_MODE_LABELS[resolved_mode]
        entries = build_export_entries(
            activity=activity,
            allowed_classes=allowed_classes,
            mode=resolved_mode,
            actor=job.requested_by,
        )
        total_bytes = sum(int(entry["size"]) for entry in entries)
        submission_count = len({entry["submission_id"] for entry in entries})
        ExportJob.objects.filter(pk=job.pk).update(
            total=total_bytes,
            current=0,
            filters={
                **filters,
                "mode": resolved_mode,
                "mode_label": mode_label,
                "file_count": len(entries),
                "submission_count": submission_count,
            },
            updated_at=timezone.now(),
        )

        export_dir = export_absolute_dir(activity.id)
        zip_name = f"{uuid.uuid4().hex}.zip"
        zip_path = export_dir / zip_name
        relative_path = f"{export_relative_dir(activity.id)}/{zip_name}"

        progress = _ExportProgress(job.pk, total_bytes)
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for entry in entries:
                _write_zip_entry(zf, entry, progress)
        progress.flush()

        download_name = sanitize_filename(activity.title, default="活动")
        archive_suffix = EXPORT_MODE_ARCHIVE_SUFFIX.get(resolved_mode, "归档材料")
        finished = timezone.now()
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.DONE,
            file_path=relative_path,
            result_key=f"{download_name}-{archive_suffix}.zip",
            current=total_bytes,
            total=total_bytes,
            finished_at=finished,
            error_code="",
            updated_at=finished,
        )
        record(
            actor=job.requested_by,
            action="export.activity.done",
            target=job,
            metadata={
                "activity_id": str(activity.id),
                "mode": resolved_mode,
                "submission_count": submission_count,
                "file_count": len(entries),
                "total_bytes": total_bytes,
                "file_path": relative_path,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败也必须落库，页面才能显示原因
        logging.getLogger(__name__).exception("export_activity_failed", extra={"job_id": str(job.pk)})
        failed_at = timezone.now()
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.FAILED,
            error_code=type(exc).__name__[:100],
            finished_at=failed_at,
            updated_at=failed_at,
        )
        record(
            actor=job.requested_by,
            action="export.activity.failed",
            target=job,
            metadata={"activity_id": str(activity.id), "error_type": type(exc).__name__},
        )

    return ExportJob.objects.get(pk=job.pk)


def serialize_export_job(job: ExportJob) -> dict:
    filters = job.filters or {}
    return {
        "job_id": str(job.id),
        "activity_id": str(job.activity_id),
        "activity_title": job.activity.title if job.activity_id else "",
        "mode": job.export_mode,
        "mode_label": job.export_mode_label,
        "status": job.status,
        "status_display": job.get_status_display(),
        "total": job.total,
        "current": job.current,
        "percent": job.percent,
        "submission_count": int(filters.get("submission_count") or 0),
        "file_count": int(filters.get("file_count") or 0),
        "file_path": job.file_path,
        "error_code": job.error_code,
        "result_key": job.result_key,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
        "download_url": f"/api/v1/exports/{job.id}/download/" if job.status == ExportJob.Status.DONE else "",
    }
