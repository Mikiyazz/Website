"""Pure metadata checks; passing these checks does NOT mean bytes were verified."""
from django.conf import settings
from apps.common.errors import DomainError


def validate_metadata(*, filename, declared_bytes, requirement):
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        raise DomainError("文件名长度必须为 1～255 个字符。")
    if any(c in filename for c in ("/", "\\")) or any(ord(c) < 32 for c in filename):
        raise DomainError("文件名不能包含路径或控制字符。")
    if type(declared_bytes) is not int or not (0 < declared_bytes <= min(settings.MAX_FILE_BYTES, requirement.max_file_bytes)):
        raise DomainError("文件大小超出允许范围（全局最大 10GB）。")
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if not requirement.allowed_extensions or extension not in requirement.allowed_extensions:
        raise DomainError("材料类别未配置此文件扩展名。")


def part_count(size, part_size):
    if size <= 0 or part_size <= 0:
        raise ValueError("Sizes must be positive")
    return (size + part_size - 1) // part_size
