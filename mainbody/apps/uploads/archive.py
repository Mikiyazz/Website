"""ZIP 安全校验与安全解包：ZipSlip、解压炸弹、嵌套压缩包、符号链接、加密条目。

设计要点
--------
* 上传阶段只做"压缩包本身是否安全"的静态检查（apps.uploads.policies 调用
  inspect_zip_archive），违规时上层负责删除已落盘文件。
* 真正解包一律走 safe_extract_zip：先整体体检，再逐条目流式写盘，写入过程中再次
  累计字节数（ZIP 头里的 file_size 可以伪造），最后确保真实路径仍在 MEDIA_ROOT 内。
* 任何一步失败都会清掉本次已经落盘的条目，不在磁盘上留半个解包目录。
"""
from __future__ import annotations

import logging
import os
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from apps.common.errors import DomainError

logger = logging.getLogger(__name__)

MB = 1024 * 1024

# 解压炸弹的三道闸：解压后总大小、条目数、条目路径与嵌套层级。
MAX_ARCHIVE_TOTAL_BYTES = 200 * MB
MAX_ARCHIVE_ENTRIES = 200
MAX_ARCHIVE_DEPTH = 1  # 只允许"一层"：包内再出现 .zip 即等于 2 层，直接拒绝
MAX_ENTRY_NAME_LENGTH = 255
MAX_ENTRY_DIRECTORY_DEPTH = 6
STREAM_CHUNK_BYTES = 64 * 1024
SYMLINK_MODE = 0o120000
FILE_TYPE_MASK = 0o170000

NESTED_ARCHIVE_SUFFIXES = frozenset({".zip"})


@dataclass(frozen=True)
class ZipEntry:
    """一个已通过体检的压缩包条目（名字已规范化，无 ..、无绝对路径）。"""

    name: str
    size: int
    compressed_size: int
    depth: int


@dataclass(frozen=True)
class ZipInspection:
    entries: tuple[ZipEntry, ...]
    total_uncompressed_bytes: int
    compressed_bytes: int
    max_depth: int

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def compression_ratio(self) -> float:
        if self.compressed_bytes <= 0:
            return float(self.total_uncompressed_bytes)
        return self.total_uncompressed_bytes / self.compressed_bytes

    def as_dict(self) -> dict:
        return {
            "entry_count": self.entry_count,
            "total_uncompressed_bytes": self.total_uncompressed_bytes,
            "compressed_bytes": self.compressed_bytes,
            "max_depth": self.max_depth,
            "compression_ratio": round(self.compression_ratio, 2),
            "names": [entry.name for entry in self.entries],
        }


def _limit(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_member_name(raw_name) -> tuple[str, int]:
    """把条目名归一成 posix 相对路径，并算出它的目录层级。

    拒绝：空名、NUL、绝对路径、Windows 盘符、.. 片段、超长名、层级过深。
    """
    name = str(raw_name or "")
    if not name or chr(0) in name:
        raise DomainError("压缩包内存在非法条目名（空名或含 NUL 字节），已拒绝。")
    name = name.replace(chr(92), "/")
    if len(name) > MAX_ENTRY_NAME_LENGTH:
        raise DomainError(f"压缩包条目名超过 {MAX_ENTRY_NAME_LENGTH} 字符，已拒绝。")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise DomainError("压缩包条目使用绝对路径，已拒绝（ZipSlip）。")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts:
        raise DomainError("压缩包内存在空路径条目，已拒绝。")
    if any(part == ".." for part in parts):
        raise DomainError("压缩包条目包含 .. 路径穿越（ZipSlip），已拒绝。")
    if len(parts) > MAX_ENTRY_DIRECTORY_DEPTH + 1:
        raise DomainError(f"压缩包条目目录层级超过 {MAX_ENTRY_DIRECTORY_DEPTH} 层，已拒绝。")
    normalized = "/".join(parts)
    return normalized, len(parts) - 1


def _resolve_within(relative: Path, root: Path) -> Path:
    """在 root 之内解析路径，逃逸（含符号链接）一律拒绝。"""
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise DomainError("压缩包条目路径逃逸出 MEDIA_ROOT（ZipSlip），已拒绝。")
    return candidate


def inspect_zip_archive(
    source,
    *,
    destination_root=None,
    max_total_bytes: int | None = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> ZipInspection:
    """静态体检一个 ZIP：条目数、解压总大小、条目名、嵌套与符号链接。

    source 可以是路径，也可以是可 seek 的文件对象（Django 的 TemporaryUploadedFile）。
    """
    total_cap = int(max_total_bytes or _limit("ARCHIVE_MAX_TOTAL_BYTES", MAX_ARCHIVE_TOTAL_BYTES))
    entry_cap = int(max_entries or _limit("ARCHIVE_MAX_ENTRIES", MAX_ARCHIVE_ENTRIES))
    depth_cap = int(max_depth or _limit("ARCHIVE_MAX_DEPTH", MAX_ARCHIVE_DEPTH))
    root = Path(destination_root or settings.MEDIA_ROOT)

    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError):
        raise DomainError("压缩包已损坏或不是合法的 ZIP 文件。") from None

    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos:
            raise DomainError("压缩包内没有任何文件，已拒绝。")
        if len(infos) > entry_cap:
            raise DomainError(f"压缩包条目数 {len(infos)} 超过上限 {entry_cap}，已拒绝（解压炸弹）。")

        entries: list[ZipEntry] = []
        total = 0
        compressed_total = 0
        deepest = 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise DomainError("压缩包包含加密条目，已拒绝。")
            file_type = (info.external_attr >> 16) & FILE_TYPE_MASK
            if file_type == SYMLINK_MODE:
                raise DomainError("压缩包包含符号链接条目，已拒绝。")
            name, depth = _normalize_member_name(info.filename)
            if Path(name).suffix.lower() in NESTED_ARCHIVE_SUFFIXES:
                raise DomainError(
                    f"压缩包内含压缩包（{name}），嵌套层级超过 {depth_cap}，已拒绝。"
                )
            if depth > depth_cap:
                raise DomainError(f"压缩包条目嵌套层级超过 {depth_cap}，已拒绝。")
            _resolve_within(Path(name), root)
            size = int(info.file_size)
            if size < 0:
                raise DomainError("压缩包条目声明了非法大小，已拒绝。")
            total += size
            if total > total_cap:
                raise DomainError(
                    f"压缩包解压后总大小超过 {total_cap // MB}MB 上限，已拒绝（解压炸弹）。"
                )
            compressed_total += int(info.compress_size or 0)
            deepest = max(deepest, depth)
            entries.append(
                ZipEntry(
                    name=name,
                    size=size,
                    compressed_size=int(info.compress_size or 0),
                    depth=depth,
                )
            )

    inspection = ZipInspection(
        entries=tuple(entries),
        total_uncompressed_bytes=total,
        compressed_bytes=compressed_total,
        max_depth=deepest,
    )
    logger.info(
        "zip_inspected",
        extra={
            "entry_count": inspection.entry_count,
            "total_uncompressed_bytes": inspection.total_uncompressed_bytes,
            "compression_ratio": round(inspection.compression_ratio, 2),
        },
    )
    return inspection


def _remove_path(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 清理失败不掩盖原始错误
        logger.warning("zip_cleanup_failed", extra={"path_name": path.name})


def _cleanup_extracted(paths: list[Path], root: Path) -> None:
    root = root.resolve()
    for path in reversed(paths):
        _remove_path(path)
        parent = path.parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def safe_extract_zip(
    source,
    destination_root=None,
    *,
    max_total_bytes: int | None = None,
    max_entries: int | None = None,
    max_depth: int | None = None,
) -> list[Path]:
    """体检 + 流式解包。任何违规都会删掉本次已落盘的条目后抛 DomainError。"""
    root = Path(destination_root or settings.MEDIA_ROOT)
    total_cap = int(max_total_bytes or _limit("ARCHIVE_MAX_TOTAL_BYTES", MAX_ARCHIVE_TOTAL_BYTES))
    inspection = inspect_zip_archive(
        source,
        destination_root=root,
        max_total_bytes=total_cap,
        max_entries=max_entries,
        max_depth=max_depth,
    )
    root_resolved = root.resolve()
    extracted: list[Path] = []
    written_total = 0
    try:
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name, _ = _normalize_member_name(info.filename)
                target = _resolve_within(Path(name), root_resolved)
                target.parent.mkdir(parents=True, exist_ok=True)
                staging = target.parent / ("." + uuid.uuid4().hex + ".part")
                try:
                    with archive.open(info) as source_stream, open(staging, "wb") as sink:
                        while True:
                            piece = source_stream.read(STREAM_CHUNK_BYTES)
                            if not piece:
                                break
                            written_total += len(piece)
                            if written_total > total_cap:
                                raise DomainError(
                                    f"压缩包实际解压字节数超过 {total_cap // MB}MB 上限，已拒绝（解压炸弹）。"
                                )
                            sink.write(piece)
                    os.replace(staging, target)
                except Exception:
                    staging.unlink(missing_ok=True)
                    raise
                extracted.append(target)
    except Exception:
        _cleanup_extracted(extracted, root_resolved)
        raise

    if written_total > inspection.total_uncompressed_bytes:
        # ZIP 头低报大小：真实字节数不可能超过条目声明之和，超过说明头被伪造过。
        _cleanup_extracted(extracted, root_resolved)
        raise DomainError("压缩包条目声明大小与实际内容不一致，已拒绝。")
    return extracted
