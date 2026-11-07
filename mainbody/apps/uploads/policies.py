"""Material category specs, filename/extension checks, and 32-byte magic header validation."""
from django.conf import settings
from apps.common.errors import DomainError

MB = 1024 * 1024

STANDARD_MATERIAL_RULES = {
    "application_form": {
        "code": "application_form",
        "title": "申报表",
        "required": True,
        "allowed_extensions": [".docx"],
        "max_file_bytes": 20 * MB,
        "min_count": 1,
        "max_count": 1,
    },
    "practice_report": {
        "code": "practice_report",
        "title": "实践报告",
        "required": True,
        "allowed_extensions": [".pdf"],
        "max_file_bytes": 20 * MB,
        "min_count": 1,
        "max_count": 1,
    },
    "scene_photos": {
        "code": "scene_photos",
        "title": "现场照片",
        "required": True,
        "allowed_extensions": [".jpg", ".jpeg", ".png"],
        "max_file_bytes": 10 * MB,
        "min_count": 3,
        "max_count": 5,
    },
    "video": {
        "code": "video",
        "title": "宣讲视频",
        "required": False,
        "allowed_extensions": [".mp4"],
        "max_file_bytes": 300 * MB,
        "min_count": 0,
        "max_count": 1,
    },
    "other_materials": {
        "code": "other_materials",
        "title": "其他证明材料",
        "required": False,
        "allowed_extensions": [".pdf", ".jpg", ".jpeg", ".png"],
        "max_file_bytes": 20 * MB,
        "min_count": 0,
        "max_count": 10,
    },
}

STANDARD_MATERIAL_SPECS = STANDARD_MATERIAL_RULES

SINGLE_FILE_MATERIAL_CODES = frozenset({"application_form", "practice_report", "video"})
INLINE_PREVIEW_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".png"})

EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
    ".zip": "application/zip",
}

MP4_BOX_SIGNATURES = frozenset({
    b"ftyp",
    b"moov",
    b"mdat",
    b"wide",
    b"free",
    b"skip",
    b"styp",
})


def validate_filename_and_extension(filename, allowed_extensions=None) -> str:
    """Validate filename safety (no path traversal, control chars, or >255 chars) and lowercase extension whitelist."""
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise DomainError("文件名长度必须为 1～255 个字符。")
    if (
        any(c in filename for c in ("/", "\\", ":"))
        or ".." in filename
        or any(ord(c) < 32 or ord(c) == 127 for c in filename)
    ):
        raise DomainError("文件名不能包含路径穿越符或控制字符。")
    if "." not in filename:
        raise DomainError("文件名必须包含合法扩展名。")
    stem, ext_part = filename.rsplit(".", 1)
    if not stem.strip() or not ext_part:
        raise DomainError("文件名或扩展名不能为空。")
    extension = "." + ext_part.lower()
    if allowed_extensions is not None:
        normalized_whitelist = [str(x).lower() for x in allowed_extensions if isinstance(x, str)]
        if not normalized_whitelist or extension not in normalized_whitelist:
            raise DomainError(f"材料类别未配置此文件扩展名（{extension}）。")
    return extension


def _read_first_32_bytes(file_or_bytes) -> bytes:
    if isinstance(file_or_bytes, (bytes, bytearray, memoryview)):
        return bytes(file_or_bytes[:32])
    if hasattr(file_or_bytes, "read"):
        pos = file_or_bytes.tell() if hasattr(file_or_bytes, "tell") else None
        header = file_or_bytes.read(32)
        if pos is not None and hasattr(file_or_bytes, "seek"):
            file_or_bytes.seek(pos)
        elif hasattr(file_or_bytes, "seek"):
            file_or_bytes.seek(0)
        if isinstance(header, str):
            header = header.encode("latin1")
        return bytes(header)
    raise DomainError("无法读取文件流内容。")


def validate_magic_bytes(file_or_bytes, extension: str) -> str:
    """Read first 32 bytes and verify real file header magic bytes match the lowercase extension."""
    ext = extension.lower() if extension.startswith(".") else "." + extension.lower()
    header = _read_first_32_bytes(file_or_bytes)
    if not header:
        raise DomainError("上传文件内容为空。")

    if ext == ".pdf":
        if not header.startswith(b"%PDF-"):
            raise DomainError("文件头魔数校验失败：非真实的 PDF 文件（缺少 %PDF- 签名）。")
    elif ext == ".docx":
        if not header.startswith(b"PK\x03\x04"):
            raise DomainError("文件头魔数校验失败：非真实的 DOCX 文件（缺少 PK\\x03\\x04 ZIP 头签名）。")
    elif ext in (".jpg", ".jpeg"):
        if not header.startswith(b"\xff\xd8\xff"):
            raise DomainError("文件头魔数校验失败：非真实的 JPG/JPEG 图片（缺少 \\xff\\xd8\\xff 签名）。")
    elif ext == ".png":
        if not header.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DomainError("文件头魔数校验失败：非真实的 PNG 图片（缺少 PNG 魔数签名）。")
    elif ext == ".mp4":
        if len(header) < 8 or header[4:8] not in MP4_BOX_SIGNATURES:
            raise DomainError("文件头魔数校验失败：非真实的 MP4 视频文件（偏移 4~8 字节缺少 ftyp 等合法 box 签名）。")
    elif ext == ".zip":
        if not header.startswith(b"PK\x03\x04"):
            raise DomainError("文件头魔数校验失败：非真实的 ZIP 压缩包。")
    else:
        raise DomainError(f"不支持的文件扩展名魔数校验：{ext}")

    return EXTENSION_CONTENT_TYPES.get(ext, "application/octet-stream")


def validate_metadata(*, filename, declared_bytes, requirement):
    spec = STANDARD_MATERIAL_RULES.get(getattr(requirement, "code", ""))
    allowed_extensions = requirement.allowed_extensions
    if spec and not allowed_extensions:
        allowed_extensions = spec["allowed_extensions"]
    extension = validate_filename_and_extension(filename, allowed_extensions)
    if spec and extension not in spec["allowed_extensions"]:
        raise DomainError(f"材料类别【{spec['title']}】仅允许上传 {', '.join(spec['allowed_extensions'])} 格式。")

    max_allowed = min(settings.MAX_FILE_BYTES, requirement.max_file_bytes)
    if spec:
        max_allowed = min(max_allowed, spec["max_file_bytes"])

    if type(declared_bytes) is not int or not (0 < declared_bytes <= max_allowed):
        raise DomainError("文件大小超出允许范围。")
    return extension


def validate_uploaded_file(file_obj, *, requirement, filename=None, declared_bytes=None):
    """Double hard validation: 1) filename & extension & size limit, 2) 32-byte magic header check."""
    resolved_name = filename if filename is not None else getattr(file_obj, "name", "")
    resolved_size = declared_bytes if declared_bytes is not None else getattr(file_obj, "size", None)
    if resolved_size is None and hasattr(file_obj, "seek") and hasattr(file_obj, "tell"):
        pos = file_obj.tell()
        file_obj.seek(0, 2)
        resolved_size = file_obj.tell()
        file_obj.seek(pos)
    extension = validate_metadata(
        filename=resolved_name,
        declared_bytes=resolved_size,
        requirement=requirement,
    )
    detected_type = validate_magic_bytes(file_obj, extension)
    return resolved_name, extension, resolved_size, detected_type


def part_count(size, part_size):
    if size <= 0 or part_size <= 0:
        raise ValueError("Sizes must be positive")
    return (size + part_size - 1) // part_size
