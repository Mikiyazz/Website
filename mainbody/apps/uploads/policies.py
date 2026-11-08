"""Material category specs, filename/extension checks, and layered content verification.

安全分层（每一层都可能单独拦下一个伪造样本，任一层失败即拒绝入库）：

1. 文件名消毒：不允许路径分隔符、控制字符、..，长度上限 255；扩展名只从白名单反查，
   大小写统一 lowercase。
2. 32 字节魔数：PDF / DOCX / JPG / PNG / MP4 / MOV / AVI / MKV / ZIP 的真实文件头。
3. 声明 MIME 一致性：浏览器上报的 Content-Type 不能与扩展名所代表的家族矛盾。
4. 容器结构：PNG 的 IHDR/IEND 与 CRC、JPEG 的 SOI/SOF/EOI；装了 Pillow 再走一次
   Image.open().verify()。按运维要求不再扫描图片尾部附加数据（图片马特征），安全性由
   "图片必须真实可解码" + "下载/预览一律 attachment 或受限 inline" 两条保证。
5. 视频容器：优先 ffprobe 解析；环境没有 ffprobe 时降级为扩展名+大小校验并打 warning。
6. ZIP：条目数、解压总大小、条目路径（ZipSlip）、嵌套 zip 全部在 apps.uploads.archive 校验。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from pathlib import Path

from django.conf import settings

from apps.common.errors import DomainError

logger = logging.getLogger(__name__)

MB = 1024 * 1024

# ---------------------------------------------------------------------------
# 分片上传（chunked upload）：单片固定 5MB，最终大小以服务端下发为准。
# 300MB 宣讲视频由此变成 60 次约 5MB 的短请求，Gunicorn worker 不会被独占。
# UPLOAD_CHUNK_BYTES 只允许向下调整（测试/运维），上限仍是 5MB 硬顶。
# ---------------------------------------------------------------------------
CHUNK_SIZE_BYTES = 5 * MB
CHUNK_SIZE_HARD_CAP = 5 * MB
CHUNK_SIZE_FLOOR = 64 * 1024
MAX_CHUNKS_PER_UPLOAD = 10_000

# 魔数常量：全部用 fromhex 声明，避免转义写法在代码里埋坑。
MAGIC_PDF = b"%PDF-"
MAGIC_ZIP = b"PK" + bytes((3, 4))
MAGIC_JPEG = bytes.fromhex("ffd8ff")
MAGIC_PNG = bytes.fromhex("89504e470d0a1a0a")
MAGIC_EBML = bytes.fromhex("1a45dfa3")
MAGIC_AVI_BODY = b"AVI "
MAGIC_RIFF = b"RIFF"
JPEG_EOI = bytes.fromhex("ffd9")
# PNG 的 IEND 数据块整体：长度 0 + 类型 IEND + CRC ae426082。
PNG_IEND_CHUNK = bytes.fromhex("0000000049454e44ae426082")

# 落盘文件名的唯一来源：uuid4().hex + 白名单扩展名，绝不拼 original_filename。
OBJECT_KEY_HEX_LENGTH = 32

# 深度校验的读取窗口：头部 128KB 足够覆盖所有容器头，尾部 4KB 用于 EOI/IEND 断言。
HEAD_SCAN_BYTES = 128 * 1024
TAIL_SCAN_BYTES = 4 * 1024
# 单张图片允许的最大像素数（防像素炸弹）。
MAX_IMAGE_PIXELS = 60_000_000
FFPROBE_TIMEOUT_SECONDS = 25


def chunk_size_bytes() -> int:
    """Server-authoritative part size (bytes) for new chunked uploads."""
    configured = getattr(settings, "UPLOAD_CHUNK_BYTES", CHUNK_SIZE_BYTES)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = CHUNK_SIZE_BYTES
    return max(CHUNK_SIZE_FLOOR, min(value, CHUNK_SIZE_HARD_CAP))


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
        # 宣讲视频：mp4 / mov / avi / mkv 四类容器格式，单份 ≤300MB，选交。
        "allowed_extensions": [".mp4", ".mov", ".avi", ".mkv"],
        "max_file_bytes": 300 * MB,
        "min_count": 0,
        "max_count": 1,
    },
    "other_materials": {
        "code": "other_materials",
        "title": "其他证明材料",
        "required": False,
        "allowed_extensions": [".pdf", ".jpg", ".jpeg", ".png", ".zip"],
        "max_file_bytes": 50 * MB,
        # 按扩展名特批的单文件上限：打包材料（.zip）体积远大于普通附件，
        # 该值覆盖类别上限，但仍受 settings.MAX_FILE_BYTES 硬顶约束。
        "extension_max_bytes": {".zip": 2000 * MB},
        # 按扩展名特批的份数上限：5 份 2000MB 打包件合计 10,485,760,000 字节，
        # 活动默认额度 12,884,901,888 字节（12 GiB）能装下并留约 2.4GB 给普通材料。
        # 其余扩展名仍受类别 max_count=10 约束。
        "extension_max_count": {".zip": 5},
        "min_count": 0,
        "max_count": 10,
    },
}

# 落库的五个标准类别，顺序即前端展示顺序。
STANDARD_MATERIAL_CODES = (
    "application_form",
    "practice_report",
    "scene_photos",
    "video",
    "other_materials",
)

# 落盘目录名与类别 code 解耦：宣讲视频固定落在 <activity>/<student_number>/promotional_video/ 下。
MATERIAL_DIRECTORY_NAMES = {"video": "promotional_video"}

# 前端/第三方可能沿用业务说法而非内部 code，这里只做有限别名归一，不做模糊匹配。
MATERIAL_TYPE_ALIASES = {"promotional_video": "video", "宣讲视频": "video"}

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv"})
ARCHIVE_EXTENSIONS = frozenset({".zip"})


def material_directory(code: str) -> str:
    """Directory segment (relative to MEDIA_ROOT) that stores one material code."""
    return MATERIAL_DIRECTORY_NAMES.get(str(code or ""), str(code or ""))


def canonical_material_type(value) -> str | None:
    """Normalize an incoming material_type to a rule code, or None when it is not a known category."""
    code = str(value or "").strip().lower()
    code = MATERIAL_TYPE_ALIASES.get(code, code)
    return code if code in STANDARD_MATERIAL_RULES else None


def seeded_material_rules():
    """Yield (code, spec) for the five standard categories that own a database row."""
    for code in STANDARD_MATERIAL_CODES:
        spec = STANDARD_MATERIAL_RULES.get(code)
        if spec is not None:
            yield code, spec


def effective_max_file_bytes(code, extension, requirement_max_bytes=None):
    """Single source of truth for "how big may this one file be".

    Order of precedence: settings.MAX_FILE_BYTES ceiling, then the activity row's own
    limit (when provided), then the category limit, and finally a per-extension override
    from the rule (for example the 2000MB .zip pack in other_materials) which replaces
    the category limit for that extension only.
    """
    spec = STANDARD_MATERIAL_RULES.get(code) or {}
    ceiling = int(settings.MAX_FILE_BYTES)
    category_cap = int(spec.get("max_file_bytes") or 0)
    cap = category_cap
    if requirement_max_bytes is not None:
        row_cap = int(requirement_max_bytes)
        cap = min(cap, row_cap) if cap else row_cap
    override = (spec.get("extension_max_bytes") or {}).get(extension)
    if override:
        cap = int(override)
    if not cap:
        return ceiling
    return min(ceiling, cap)


def extension_max_count(code, extension):
    """Per-extension file-count cap for one code (for example 5 .zip packs), or None when uncapped."""
    spec = STANDARD_MATERIAL_RULES.get(code) or {}
    return (spec.get("extension_max_count") or {}).get(extension)


def rule_max_file_bytes(code, extension):
    """Effective single-file cap (bytes) for one code/extension pair, ignoring row overrides."""
    return effective_max_file_bytes(code, extension)


SINGLE_FILE_MATERIAL_CODES = frozenset({"application_form", "practice_report", "video"})
INLINE_PREVIEW_EXTENSIONS = frozenset({".pdf", ".jpg", ".jpeg", ".png"})

EXTENSION_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
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

# 声明 MIME 白名单：浏览器/第三方客户端上报的 Content-Type 必须落在对应家族里，
# 出现 image/jpeg 配 .pdf 这种"说的和做的不一致"时直接拒绝。
# 通用二进制类型不做断言，因为各浏览器对未知扩展名的上报并不统一。
EXTENSION_MIME_ALLOWLIST = {
    ".pdf": frozenset({"application/pdf", "application/x-pdf"}),
    ".docx": frozenset({
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/x-zip-compressed",
        "application/msword",
    }),
    ".jpg": frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}),
    ".jpeg": frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}),
    ".png": frozenset({"image/png", "image/x-png"}),
    ".mp4": frozenset({"video/mp4", "video/x-m4v", "application/mp4"}),
    ".mov": frozenset({"video/quicktime", "video/mov"}),
    ".avi": frozenset({"video/x-msvideo", "video/avi", "video/msvideo"}),
    ".mkv": frozenset({"video/x-matroska", "video/matroska", "application/x-matroska"}),
    ".zip": frozenset({
        "application/zip",
        "application/x-zip-compressed",
        "application/x-zip",
        "multipart/x-zip",
    }),
}

# 这些声明值不携带语义，跳过一致性断言：
# - 空 / octet-stream：curl、requests 等客户端的"未知类型"默认值；
# - text/plain：Django 上传助手与部分浏览器对未知扩展名的默认值。
# 它们既不是承诺也不是证据，真正的裁决权在魔数与容器结构两层。
MIME_WILDCARDS = frozenset({
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/unknown",
    "application/x-www-form-urlencoded",
    "text/plain",
})

_SAFE_SEGMENT_PATTERN = None


def safe_path_segment(value, fallback: str = "unknown") -> str:
    """Reduce an arbitrary value to a single path segment that cannot traverse or escape.

    Used for {activity_id}/{student_number}/{material_type}/... object keys: even a
    hand-crafted username such as ../../etc collapses to etc.
    """
    global _SAFE_SEGMENT_PATTERN
    if _SAFE_SEGMENT_PATTERN is None:
        import re

        _SAFE_SEGMENT_PATTERN = re.compile(r"[^0-9A-Za-z_-]+")
    cleaned = _SAFE_SEGMENT_PATTERN.sub("_", str(value or "")).strip("._-")
    return cleaned or fallback


def normalize_extension(extension) -> str:
    text = str(extension or "").strip().lower()
    if not text:
        raise DomainError("缺少文件扩展名。")
    return text if text.startswith(".") else "." + text


def authoritative_extension(filename, allowed_extensions) -> str:
    """反向查白名单得到权威扩展名（lowercase），original_filename 只在这里用一次。

    白名单条目本身要求小写；这里再统一 lowercase 比较一次，避免 .JPG 之类的大小写
    变体绕过类别限制。
    """
    name = str(filename or "").strip().lower()
    candidates = sorted(
        (str(item).lower() for item in (allowed_extensions or []) if isinstance(item, str) and str(item).strip()),
        key=len,
        reverse=True,
    )
    for candidate in candidates:
        if name.endswith(candidate):
            return candidate
    raise DomainError("文件扩展名不在该材料类别的白名单内。")


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
        # 白名单反查：返回白名单里的权威写法，而不是客户端拼出来的字符串。
        return authoritative_extension(filename, normalized_whitelist)
    return extension


def validate_declared_mime(declared_content_type, extension: str) -> str:
    """Reject a declared Content-Type that contradicts the extension's MIME family."""
    ext = normalize_extension(extension)
    declared = str(declared_content_type or "").split(";", 1)[0].strip().lower()
    if declared in MIME_WILDCARDS:
        return ""
    allowed = EXTENSION_MIME_ALLOWLIST.get(ext)
    if allowed and declared not in allowed:
        raise DomainError(
            f"文件声明的 MIME 类型（{declared}）与扩展名（{ext}）不一致，已拒绝入库。"
        )
    return declared


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
    ext = normalize_extension(extension)
    header = _read_first_32_bytes(file_or_bytes)
    if not header:
        raise DomainError("上传文件内容为空。")

    if ext == ".pdf":
        if not header.startswith(MAGIC_PDF):
            raise DomainError("文件头魔数校验失败：非真实的 PDF 文件（缺少 %PDF- 签名）。")
    elif ext == ".docx":
        if not header.startswith(MAGIC_ZIP):
            raise DomainError("文件头魔数校验失败：非真实的 DOCX 文件（缺少 PK 0304 ZIP 头签名）。")
    elif ext in (".jpg", ".jpeg"):
        if not header.startswith(MAGIC_JPEG):
            raise DomainError("文件头魔数校验失败：非真实的 JPG/JPEG 图片（缺少 ffd8ff 签名）。")
    elif ext == ".png":
        if not header.startswith(MAGIC_PNG):
            raise DomainError("文件头魔数校验失败：非真实的 PNG 图片（缺少 PNG 魔数签名）。")
    elif ext in (".mp4", ".mov"):
        # MP4 与 MOV 同属 ISO Base Media File Format，偏移 4~8 字节是 box 类型。
        if len(header) < 8 or header[4:8] not in MP4_BOX_SIGNATURES:
            label = "MP4" if ext == ".mp4" else "MOV"
            raise DomainError(
                f"文件头魔数校验失败：非真实的 {label} 视频文件（偏移 4~8 字节缺少 ftyp 等合法 box 签名）。"
            )
    elif ext == ".avi":
        # RIFF....AVI<space>，前 4 字节 RIFF，8~12 字节 AVI 标识。
        if len(header) < 12 or not header.startswith(MAGIC_RIFF) or header[8:12] != MAGIC_AVI_BODY:
            raise DomainError("文件头魔数校验失败：非真实的 AVI 视频文件（缺少 RIFF....AVI 签名）。")
    elif ext == ".mkv":
        # Matroska 容器以 EBML 头 1A 45 DF A3 起始。
        if not header.startswith(MAGIC_EBML):
            raise DomainError("文件头魔数校验失败：非真实的 MKV 视频文件（缺少 EBML 头 1a45dfa3）。")
    elif ext == ".zip":
        if not header.startswith(MAGIC_ZIP):
            raise DomainError("文件头魔数校验失败：非真实的 ZIP 压缩包。")
    else:
        raise DomainError(f"不支持的文件扩展名魔数校验：{ext}")

    return EXTENSION_CONTENT_TYPES.get(ext, "application/octet-stream")


# ---------------------------------------------------------------------------
# 第 3、4、5 层：落盘后的容器结构与内容校验
# ---------------------------------------------------------------------------

def _read_head_and_tail(path: Path) -> tuple[bytes, bytes]:
    try:
        size = path.stat().st_size
    except OSError:
        raise DomainError("无法读取已落盘文件。") from None
    if size <= 0:
        raise DomainError("上传文件内容为空。")
    with open(path, "rb") as handle:
        head = handle.read(HEAD_SCAN_BYTES)
        if size > TAIL_SCAN_BYTES:
            handle.seek(max(0, size - TAIL_SCAN_BYTES))
            tail = handle.read(TAIL_SCAN_BYTES)
        else:
            tail = head
    return head, tail


def png_structure_ok(head: bytes, tail: bytes) -> bool:
    """PNG: 8 字节签名 + 紧跟 IHDR（并校验 IHDR CRC）+ 尾部窗口内能找到 IEND 块。"""
    import zlib

    if not head.startswith(MAGIC_PNG):
        return False
    if len(head) < 33 or head[12:16] != b"IHDR":
        return False
    length = int.from_bytes(head[8:12], "big")
    if length != 13 or len(head) < 8 + 12 + length:
        return False
    chunk_end = 8 + 12 + length
    expected_crc = int.from_bytes(head[chunk_end - 4:chunk_end], "big")
    # CRC 覆盖范围是"类型 + 数据"，不含 4 字节长度字段，因此从 offset 12 开始。
    if (zlib.crc32(head[12:chunk_end - 4]) & 0xFFFFFFFF) != expected_crc:
        return False
    # 出现过合法的 IEND 块即视为结构完整；IEND 之后允许存在附加数据（不扫描图片马）。
    return PNG_IEND_CHUNK in tail


def jpeg_structure_ok(head: bytes, tail: bytes) -> bool:
    """JPEG: SOI + 头部段里至少一个 SOF + 尾部窗口内出现 EOI 标记。"""
    if not head.startswith(MAGIC_JPEG[:2]):
        return False
    saw_sof = False
    index = 2
    limit = len(head)
    while index + 1 < limit:
        if head[index] != 0xFF:
            index += 1
            continue
        marker = head[index + 1]
        if marker in (0xFF, 0x00):
            index += 1
            continue
        if marker == 0xD9:  # 头部就已 EOI，说明没有图像数据
            break
        if marker == 0xDA:  # SOS：后面是熵编码数据，标志段扫描到此为止
            break
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            saw_sof = True
        if index + 4 > limit:
            return False
        segment_length = int.from_bytes(head[index + 2:index + 4], "big")
        if segment_length < 2:
            return False
        index += 2 + segment_length
    if not saw_sof:
        return False
    # EOI 之后允许跟随附加数据（图片马常见做法），这里只要求真实的图像结束标记存在。
    return JPEG_EOI in tail


def _verify_with_pillow(path: Path, ext: str) -> None:
    """第二重防线：Pillow 真实解码一次。未安装时降级为纯结构校验并打 warning。"""
    try:
        from PIL import Image  # 延迟导入：Pillow 是可选依赖
    except ImportError:  # pragma: no cover - 取决于运行环境
        logger.warning(
            "image_verify_degraded",
            extra={"reason": "pillow_not_installed", "extension": ext, "path_name": path.name},
        )
        return
    expected = {"JPEG"} if ext in (".jpg", ".jpeg") else {"PNG"}
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            detected = (image.format or "").upper()
    except Exception:
        raise DomainError("图片内容校验失败：Pillow 无法解析该图片，疑似伪造或已损坏。") from None
    if detected not in expected:
        raise DomainError(f"图片内容校验失败：实际格式为 {detected or '未知'}，与扩展名 {ext} 不符。")
    if width <= 0 or height <= 0:
        raise DomainError("图片内容校验失败：像素尺寸非法。")
    if width * height > MAX_IMAGE_PIXELS:
        raise DomainError(f"图片内容校验失败：像素总量超过 {MAX_IMAGE_PIXELS} 上限。")


def verify_image_file(path, extension: str) -> None:
    """图片三段式校验：魔数 -> 容器结构（SOI/SOF/EOI、IHDR/IEND+CRC）-> Pillow 解码。"""
    ext = normalize_extension(extension)
    if ext not in IMAGE_EXTENSIONS:
        raise DomainError(f"非图片扩展名不适用图片校验：{ext}")
    disk_path = Path(path)
    head, tail = _read_head_and_tail(disk_path)
    validate_magic_bytes(head, ext)
    if ext == ".png":
        if not png_structure_ok(head, tail):
            raise DomainError("图片结构校验失败：PNG 缺少合法 IHDR/IEND 块或 CRC 校验不通过。")
    else:
        if not jpeg_structure_ok(head, tail):
            raise DomainError("图片结构校验失败：JPEG 缺少 SOI/SOF/EOI 标记，疑似伪造图片。")
    _verify_with_pillow(disk_path, ext)


def _ffprobe_binary() -> str:
    return str(getattr(settings, "UPLOAD_FFPROBE_BIN", "ffprobe") or "").strip()


def verify_video_file(path, extension: str, *, declared_bytes: int | None = None) -> str:
    """视频容器校验：能跑 ffprobe 就用它；跑不了就降级并打 warning。

    返回 probed 或 degraded，便于调用方把降级事实写进日志/审计。
    """
    ext = normalize_extension(extension)
    if ext not in VIDEO_EXTENSIONS:
        raise DomainError(f"非视频扩展名不适用视频校验：{ext}")
    disk_path = Path(path)
    head, _ = _read_head_and_tail(disk_path)
    validate_magic_bytes(head, ext)

    binary = _ffprobe_binary()
    resolved = shutil.which(binary) if binary else None
    if not resolved:
        logger.warning(
            "video_probe_degraded",
            extra={
                "reason": "ffprobe_not_available",
                "extension": ext,
                "declared_bytes": declared_bytes,
                "path_name": disk_path.name,
            },
        )
        # 降级路径：魔数 + 扩展名 + 大小已由上层校验，这里只确认文件非空。
        if disk_path.stat().st_size <= 0:
            raise DomainError("视频文件内容为空。")
        return "degraded"

    command = [
        resolved,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(disk_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=FFPROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning(
            "video_probe_degraded",
            extra={"reason": "ffprobe_exec_failed", "extension": ext, "path_name": disk_path.name},
        )
        return "degraded"
    if completed.returncode != 0:
        raise DomainError("视频容器校验失败：ffprobe 无法解析该文件，疑似伪造或已损坏。")
    try:
        probe = json.loads(completed.stdout or b"{}")
    except ValueError:
        logger.warning(
            "video_probe_degraded",
            extra={"reason": "ffprobe_output_unparsable", "path_name": disk_path.name},
        )
        return "degraded"
    streams = probe.get("streams") or []
    if not any(str(stream.get("codec_type")) == "video" for stream in streams):
        raise DomainError("视频容器校验失败：文件中没有可识别的视频流。")
    return "probed"


def validate_stored_file_content(disk_path, extension: str, *, declared_content_type=None) -> str:
    """落盘后的最终防线，返回规范化 MIME 类型。

    - 声明 MIME 与扩展名不一致 -> DomainError
    - 图片 -> 魔数 + 容器结构 + Pillow（不做尾部载荷扫描）
    - 视频 -> ffprobe（不可用则降级 warning）
    - ZIP  -> ZipSlip / 解压炸弹（apps.uploads.archive）
    """
    ext = normalize_extension(extension)
    validate_declared_mime(declared_content_type, ext)
    path = Path(disk_path)
    if ext in IMAGE_EXTENSIONS:
        verify_image_file(path, ext)
    elif ext in VIDEO_EXTENSIONS:
        verify_video_file(path, ext)
    elif ext in ARCHIVE_EXTENSIONS:
        from .archive import inspect_zip_archive

        inspect_zip_archive(path)
    return EXTENSION_CONTENT_TYPES.get(ext, "application/octet-stream")


def validate_metadata(*, filename, declared_bytes, requirement):
    spec = STANDARD_MATERIAL_RULES.get(getattr(requirement, "code", ""))
    allowed_extensions = requirement.allowed_extensions
    if spec and not allowed_extensions:
        allowed_extensions = spec["allowed_extensions"]
    extension = validate_filename_and_extension(filename, allowed_extensions)
    if spec and extension not in spec["allowed_extensions"]:
        raise DomainError(f"材料类别【{spec['title']}】仅允许上传 {', '.join(spec['allowed_extensions'])} 格式。")

    max_allowed = effective_max_file_bytes(
        getattr(requirement, "code", ""),
        extension,
        getattr(requirement, "max_file_bytes", None),
    )

    if type(declared_bytes) is not int or not (0 < declared_bytes <= max_allowed):
        raise DomainError(f"文件大小超出允许范围（上限 {max_allowed // MB}MB）。")
    return extension


def validate_uploaded_file(file_obj, *, requirement, filename=None, declared_bytes=None):
    """Double hard validation: 1) filename & extension & size limit, 2) 32-byte magic header check.

    The third layer (container structure / image-horse / zip-slip / ffprobe) runs after the
    bytes hit the disk, see validate_stored_file_content.
    """
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
    validate_declared_mime(getattr(file_obj, "content_type", ""), extension)
    detected_type = validate_magic_bytes(file_obj, extension)
    return resolved_name, extension, resolved_size, detected_type


def part_count(size, part_size):
    if size <= 0 or part_size <= 0:
        raise ValueError("Sizes must be positive")
    return (size + part_size - 1) // part_size


def build_object_key(*, activity_id, student_number, material_code, extension) -> str:
    """物理路径唯一生成器：{activity_id}/{student_number}/{material_type}/{uuid4().hex}{ext}。

    调用方不允许把 original_filename 拼进这里；扩展名必须是 authoritative_extension
    反查白名单后的 lowercase 版本。
    """
    ext = normalize_extension(extension)
    stem = uuid.uuid4().hex
    if len(stem) != OBJECT_KEY_HEX_LENGTH:  # pragma: no cover - uuid4().hex 恒为 32 位
        raise DomainError("内部错误：无法生成合法存储标识。")
    return (
        f"{safe_path_segment(activity_id)}/"
        f"{safe_path_segment(student_number)}/"
        f"{material_directory(material_code)}/"
        f"{stem}{ext}"
    )
