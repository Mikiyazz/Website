from urllib.parse import quote
from django.http import FileResponse, JsonResponse
from apps.accounts.permissions import require_student
from apps.common.api import endpoint
from apps.common.errors import DomainError, FeatureUnavailable
from integrations.storage import get_storage
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


@endpoint("GET")
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
def delete_upload_view(request, upload_id):
    require_student(request.user)
    upload = delete_uploaded_file(actor=request.user, upload_id=upload_id)
    return JsonResponse({"id": str(upload.id), "status": upload.status}, status=200)
