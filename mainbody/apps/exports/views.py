"""Export views: Excel summary download, class list query, and per-class ZIP archive download."""
from urllib.parse import quote
from django.http import FileResponse, JsonResponse
from apps.common.api import endpoint
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
