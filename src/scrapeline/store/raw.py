"""Immutable, content-addressed raw store. Key: <source>/<yyyy>/<mm>/<dd>/<sha256>.<ext>.

Bytes are written once and never modified; writing the same bytes again is a no-op.
Filesystem backend now; an S3 backend implements the same protocol later (step 6).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Protocol

_SAFE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")

_EXT_BY_TYPE = {
    "application/json": "json",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/csv": "csv",
}


def ext_for(content_type: str | None, url: str = "") -> str:
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _EXT_BY_TYPE:
            return _EXT_BY_TYPE[base]
    suffix = Path(url.split("?")[0]).suffix.lower().lstrip(".")
    return suffix if suffix in {"json", "html", "pdf", "txt", "xml", "csv"} else "bin"


class RawStore(Protocol):
    def put(self, source_id: str, content: bytes, ext: str, fetched_at: datetime) -> RawRef: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class RawRef:
    __slots__ = ("created", "key", "sha256")

    def __init__(self, sha256: str, key: str, created: bool) -> None:
        self.sha256 = sha256
        self.key = key
        self.created = created

    def __repr__(self) -> str:
        return f"RawRef({self.key!r}, created={self.created})"


class FilesystemRawStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"raw key escapes store root: {key}")
        return path

    def put(self, source_id: str, content: bytes, ext: str, fetched_at: datetime) -> RawRef:
        if not _SAFE.match(source_id) or not ext.isalnum():
            raise ValueError(f"unsafe source_id/ext: {source_id!r} {ext!r}")
        sha = hashlib.sha256(content).hexdigest()
        key = f"{source_id}/{fetched_at:%Y/%m/%d}/{sha}.{ext}"
        path = self._path(key)
        if path.exists():
            return RawRef(sha, key, created=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)  # atomic; concurrent writers of same bytes are harmless
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return RawRef(sha, key, created=True)

    def get(self, key: str) -> bytes:
        data = self._path(key).read_bytes()
        expected = Path(key).stem
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"raw object corrupted (hash mismatch): {key}")
        return data

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
