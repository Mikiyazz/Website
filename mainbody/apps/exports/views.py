"""Export views: Excel summary download, class list query, and per-class ZIP archive download."""
from urllib.parse import quote
from django.http import FileResponse, JsonResponse
from apps.accounts.models import User
from apps.accounts.permissions import is_college_admin
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError, Forbidden
from .services import (
    export_activity_excel_stream,
    export_class_zip_stream,
    list_activity_declared_classes,
)


class StreamableFileResponse(FileResponse):
    """FileResponse that streams from disk in production while also supporting .content in test clients."""

    @property
    def content(self) -> bytes:
        if not hasattr(self, "_cached_content"):
            chunks = list(self.streaming_content)
            self._cached_content = b"".join(
                chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                for chunk in chunks
            )
            self.streaming_content = iter([self._cached_content])
        return self._cached_content


def _build_attachment_response(file_handle, filename: str, content_type: str) -> StreamableFileResponse:
    response = StreamableFileResponse(
        file_handle,
        as_attachment=True,
        filename=filename,
        content_type=content_type,
    )
    encoded_name = quote(filename)
    if not response.headers.get("Content-Disposition", "").lower().startswith("attachment"):
        response["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_name}"
    return response


@endpoint("GET")
def export_activity_excel_view(request, activity_id):
    class_name = request.GET.get("class_name") or request.GET.get("class")
    status = request.GET.get("status")
    tmp_file, filename = export_activity_excel_stream(
        actor=request.user,
        activity_id=activity_id,
        class_name=class_name,
        status=status,
    )
    return _build_attachment_response(
        tmp_file,
        filename=filename,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@endpoint("GET")
def activity_classes_view(request, activity_id):
    classes = list_activity_declared_classes(
        actor=request.user,
        activity_id=activity_id,
    )
    return JsonResponse(
        {
            "activity_id": str(activity_id),
            "classes": classes,
            "items": classes,
        },
        status=200,
    )


@endpoint("GET")
def export_activity_class_zip_view(request, activity_id):
    class_name = request.GET.get("class_name") or request.GET.get("class")
    status = request.GET.get("status")
    tmp_file, filename = export_class_zip_stream(
        actor=request.user,
        activity_id=activity_id,
        class_name=class_name,
        status=status,
    )
    return _build_attachment_response(
        tmp_file,
        filename=filename,
        content_type="application/zip",
    )

# =====================================================================
# 异步导出接口：POST /api/v1/exports/start/、GET /api/v1/exports/status/<job_id>/
# =====================================================================

def _require_export_role(user):
    if is_college_admin(user) or getattr(user, "role", None) == User.Role.REVIEWER:
        return
    raise Forbidden("仅管理员或审核老师可发起与查看导出任务。")


def list_export_jobs_for_user(user, *, limit: int = 12):
    """Workbench '正在进行的导出任务' list: own jobs, newest first, running/pending first."""
    from .models import ExportJob

    jobs = list(
        ExportJob.objects.select_related("activity")
        .filter(requested_by=user)
        .order_by("-created_at")[:limit]
    )
    active = [job for job in jobs if job.status in (ExportJob.Status.PENDING, ExportJob.Status.RUNNING)]
    finished = [job for job in jobs if job.status not in (ExportJob.Status.PENDING, ExportJob.Status.RUNNING)]
    return active + finished


@endpoint("POST")
def start_export(request):
    from .models import ExportJob
    from .services import create_activity_export_job, serialize_export_job
    from .tasks import export_activity_submissions

    _require_export_role(request.user)
    data = json_body(request)
    activity_id = data.get("activity_id")
    if not activity_id:
        raise DomainError("缺少必要参数：activity_id。")

    job = create_activity_export_job(actor=request.user, activity_id=activity_id)
    try:
        export_activity_submissions.delay(str(job.activity_id), str(request.user.pk), str(job.pk))
    except Exception as exc:  # noqa: BLE001 - broker 不可用要落库，页面才能提示重试
        ExportJob.objects.filter(pk=job.pk).update(
            status=ExportJob.Status.FAILED, error_code=type(exc).__name__[:100]
        )
        raise DomainError("导出任务派发失败，请稍后重试。") from None

    job.refresh_from_db()
    return JsonResponse({"job": serialize_export_job(job), "job_id": str(job.id)}, status=202)


@endpoint("GET")
def export_status(request, job_id):
    from .models import ExportJob
    from .services import serialize_export_job

    _require_export_role(request.user)
    job = ExportJob.objects.select_related("activity").filter(pk=job_id).first()
    if job is None:
        raise DomainError("导出任务不存在或已被清理。")
    if not is_college_admin(request.user) and job.requested_by_id != request.user.pk:
        raise Forbidden("无权查看该导出任务。")
    return JsonResponse({"job": serialize_export_job(job)}, status=200)


@endpoint("GET")
def download_export(request, job_id):
    from pathlib import Path

    from django.conf import settings

    from .models import ExportJob

    _require_export_role(request.user)
    job = ExportJob.objects.select_related("activity").filter(pk=job_id).first()
    if job is None:
        raise DomainError("导出任务不存在或已被清理。")
    if not is_college_admin(request.user) and job.requested_by_id != request.user.pk:
        raise Forbidden("无权下载该导出任务。")
    if job.status != ExportJob.Status.DONE or not job.file_path:
        raise DomainError("导出尚未完成，请稍后再试。")

    media_root = Path(settings.MEDIA_ROOT).resolve()
    target = (media_root / job.file_path).resolve()
    if media_root not in target.parents or not target.is_file():
        raise DomainError("导出文件已失效，请重新发起导出。")

    filename = job.result_key or f"{job.activity.title}-归档材料.zip"
    return FileResponse(open(target, "rb"), as_attachment=True, filename=filename, content_type="application/zip")
