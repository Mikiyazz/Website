from urllib.parse import quote
from django.http import FileResponse, JsonResponse
from apps.accounts.permissions import require_student
from apps.audit.decorators import audit_rejections
from apps.common.api import endpoint
from apps.common.errors import DomainError, FeatureUnavailable
from integrations.storage import get_storage
from .chunks import (
    complete_chunked_upload,
    received_chunk_indexes,
    start_chunked_upload,
    store_chunk,
)
from .policies import EXTENSION_CONTENT_TYPES, INLINE_PREVIEW_EXTENSIONS
from .services import delete_uploaded_file, direct_upload, get_accessible_upload_for_user


def serialize_upload(upload, asset, replaced_ids=()):
    return {
        "id": str(upload.id),
        "upload_id": str(upload.id),
        "file_asset_id": str(asset.id),
        "requirement_id": str(upload.requirement_id),
        "requirement_code": upload.requirement.code,
        "original_name": upload.original_name,
        "object_key": upload.object_key,
        "declared_bytes": upload.declared_bytes,
        "actual_bytes": asset.actual_bytes,
        "detected_type": asset.detected_type,
        "checksum_algorithm": asset.checksum_algorithm,
        "checksum_value": asset.checksum_value,
        "status": upload.status,
        "file_status": asset.status,
        "file_url": f"/api/v1/uploads/{upload.id}/file/",
        "replaced_upload_ids": [str(uid) for uid in replaced_ids],
    }


@endpoint("POST")
def initialize(request):
    require_student(request.user)
    get_storage().ensure_available()
    raise FeatureUnavailable("分片上传业务编排尚未实现。")


@endpoint("POST")
@audit_rejections("upload.direct.rejected")
def direct_upload_view(request):
    require_student(request.user)
    requirement_id = request.POST.get("requirement_id") or request.GET.get("requirement_id")
    if not requirement_id:
        raise DomainError("缺少必要参数 requirement_id。")
    uploaded_file = request.FILES.get("file") or next(iter(request.FILES.values()), None)
    if uploaded_file is None:
        raise DomainError("请上传文件（字段名 file）。")
    replace_upload_id = request.POST.get("replace_upload_id") or request.POST.get("replace_id")
    request_key = request.POST.get("request_key")

    upload, asset, replaced_ids = direct_upload(
        actor=request.user,
        requirement_id=requirement_id,
        file_obj=uploaded_file,
        replace_upload_id=replace_upload_id,
        request_key=request_key,
    )
    return JsonResponse(serialize_upload(upload, asset, replaced_ids), status=201)


# =====================================================================
# 分片上传（300MB 宣讲视频）：chunk-start / chunk-upload / chunk-complete
# 每个请求最多处理一片 5MB，立即返回，Gunicorn worker 不会被大文件独占。
# =====================================================================

def _payload(request) -> dict:
    """Accept the same fields as JSON or as multipart/form-data."""
    if request.content_type and "application/json" in request.content_type:
        from apps.common.api import json_body

        return json_body(request)
    return request.POST.dict()


def _positive_int(value, field_name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        raise DomainError(f"{field_name} 必须为整数。") from None
    if parsed <= 0:
        raise DomainError(f"{field_name} 必须为正整数。")
    return parsed


def serialize_chunk_session(upload, *, received=None) -> dict:
    received_chunks = received_chunk_indexes(upload) if received is None else received
    return {
        "upload_id": str(upload.id),
        "chunk_size": int(upload.chunk_size_bytes),
        "total_chunks": int(upload.total_chunks),
        "material_type": upload.requirement.code,
        "requirement_id": str(upload.requirement_id),
        "activity_id": str(upload.requirement.activity_id),
        "filename": upload.original_name,
        "file_size": int(upload.declared_bytes),
        "status": upload.status,
        "received_chunks": received_chunks,
        "expires_at": upload.expires_at.isoformat(),
    }


@endpoint("POST")
@audit_rejections("upload.chunk_start.rejected")
def chunk_start(request):
    require_student(request.user)
    data = _payload(request)
    material_type = str(data.get("material_type") or "").strip()
    if not material_type:
        raise DomainError("缺少必要参数 material_type。")
    filename = str(data.get("filename") or "").strip()
    if not filename:
        raise DomainError("缺少必要参数 filename。")
    file_size = _positive_int(data.get("file_size"), "file_size")
    total_chunks = _positive_int(data.get("total_chunks"), "total_chunks")

    upload, resumed = start_chunked_upload(
        actor=request.user,
        material_type=material_type,
        filename=filename,
        file_size=file_size,
        total_chunks=total_chunks,
        requirement_id=data.get("requirement_id") or None,
        activity_id=data.get("activity_id") or None,
    )
    payload = serialize_chunk_session(upload)
    payload["resumed"] = resumed
    return JsonResponse(payload, status=200 if resumed else 201)


@endpoint("POST")
@audit_rejections("upload.chunk_upload.rejected")
def chunk_upload(request):
    # 第一行即校验登录态与账号角色，随后在服务层校验 upload 归属（非本人 403）。
    require_student(request.user)
    upload_id = request.POST.get("upload_id") or request.GET.get("upload_id")
    if not upload_id:
        raise DomainError("缺少必要参数 upload_id。")
    chunk_index = request.POST.get("chunk_index")
    if chunk_index is None or str(chunk_index).strip() == "":
        raise DomainError("缺少必要参数 chunk_index。")
    chunk_file = request.FILES.get("chunk") or next(iter(request.FILES.values()), None)
    if chunk_file is None:
        raise DomainError("请上传分片内容（字段名 chunk）。")

    upload, index, skipped = store_chunk(
        actor=request.user,
        upload_id=upload_id,
        chunk_index=chunk_index,
        chunk_file=chunk_file,
    )
    return JsonResponse(
        {
            "received": True,
            "upload_id": str(upload.id),
            "chunk_index": index,
            "skipped": skipped,
        },
        status=200,
    )


@endpoint("POST")
@audit_rejections("upload.chunk_complete.rejected")
def chunk_complete(request):
    require_student(request.user)
    data = _payload(request)
    upload_id = data.get("upload_id")
    if not upload_id:
        raise DomainError("缺少必要参数 upload_id。")

    upload, asset, replaced_ids = complete_chunked_upload(
        actor=request.user,
        upload_id=upload_id,
        material_type=data.get("material_type") or None,
    )
    return JsonResponse(
        {
            "status": "uploaded",
            "upload_id": str(upload.id),
            "final_path": upload.object_key,
            "material_type": upload.requirement.code,
            "original_name": upload.original_name,
            "actual_bytes": asset.actual_bytes,
            "detected_type": asset.detected_type,
            "checksum_algorithm": asset.checksum_algorithm,
            "checksum_value": asset.checksum_value,
            "file_url": f"/api/v1/uploads/{upload.id}/file/",
            "replaced_upload_ids": [str(item) for item in replaced_ids],
        },
        status=200,
    )


@endpoint("GET")
@audit_rejections("upload.file_access.rejected")
def file_access_view(request, upload_id):
    upload, asset, disk_path = get_accessible_upload_for_user(
        actor=request.user,
        upload_or_asset_id=upload_id,
    )
    ext = "." + upload.original_name.rsplit(".", 1)[-1].lower() if "." in upload.original_name else ""
    inline_requested = str(request.GET.get("inline", "")).strip().lower() in ("1", "true", "yes")
    can_inline = ext in INLINE_PREVIEW_EXTENSIONS and inline_requested
    as_attachment = not can_inline
    content_type = EXTENSION_CONTENT_TYPES.get(ext, asset.detected_type or "application/octet-stream")

    response = FileResponse(
        open(disk_path, "rb"),
        as_attachment=as_attachment,
        filename=upload.original_name,
        content_type=content_type,
    )
    disposition_type = "inline" if can_inline else "attachment"
    encoded_name = quote(upload.original_name)
    if not response.headers.get("Content-Disposition", "").lower().startswith(disposition_type):
        response["Content-Disposition"] = f"{disposition_type}; filename*=UTF-8''{encoded_name}"
    return response


@endpoint("DELETE", "POST")
@audit_rejections("upload.delete.rejected")
def delete_upload_view(request, upload_id):
    # 归属校验在服务层完成：本人 or 该活动管理员/审核老师，其余一律 403 并落审计。
    upload = delete_uploaded_file(actor=request.user, upload_id=upload_id)
    return JsonResponse({"id": str(upload.id), "status": upload.status}, status=200)
