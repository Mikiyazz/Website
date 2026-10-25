from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class UploadedPart:
    number: int
    etag: str
    size: int


@dataclass(frozen=True)
class PartsPage:
    parts: tuple[UploadedPart, ...]
    next_marker: str | None


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    checksum_algorithm: str
    checksum_value: str


class MultipartStorage(Protocol):
    """Implement with an official SDK; all operations need bounded timeouts."""
    def ensure_available(self) -> None: ...
    def initiate(self, *, key: str, content_type: str) -> str: ...
    def sign_part(self, *, key: str, upload_id: str, number: int, expires: int) -> str: ...
    def list_parts(self, *, key: str, upload_id: str, marker: str | None = None) -> PartsPage: ...
    def complete(self, *, key: str, upload_id: str, parts: tuple[UploadedPart, ...]) -> None: ...
    def head(self, *, key: str) -> ObjectInfo: ...
    def abort(self, *, key: str, upload_id: str) -> None: ...
    def sign_download(self, *, key: str, filename: str, expires: int) -> str: ...
