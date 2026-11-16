"""Pure data types shared by runtime, adapters and the offline sandbox (no I/O imports here)."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Target(BaseModel):
    """Something to fetch. `meta` travels to the RawDoc so `parse` knows what it is looking at."""

    model_config = ConfigDict(frozen=True)

    url: str
    kind: str = "page"
    meta: dict[str, Any] = Field(default_factory=dict)
    render: bool = False  # True = fetch through BrowserPool (Playwright)


class RawDoc(BaseModel):
    """One fetched response, exactly as stored in the raw store."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    url: str
    final_url: str
    status: int
    content_type: str | None
    content: bytes
    sha256: str
    fetched_at: datetime
    kind: str = "page"
    meta: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def hash_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class Reject(BaseModel):
    """A candidate record that failed validation. Lands in pipeline.rejects with its reason."""

    record_type: str
    data: dict[str, Any]
    reason: str


class ParseResult(BaseModel):
    """Output of `Adapter.parse`. Pure data: records, rejects, follow-up targets, watermark."""

    records: list[Any] = Field(default_factory=list)  # list[pipeline.models.Record]
    rejects: list[Reject] = Field(default_factory=list)
    next_targets: list[Target] = Field(default_factory=list)
    watermark: datetime | None = None  # highest source-side modification time seen

    def add(self, model: type[Any], data: dict[str, Any]) -> None:
        """Validate `data` as `model`; append to records, or to rejects with the reason."""
        try:
            self.records.append(model.model_validate(data))
        except ValidationError as exc:
            reason = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<record>'}: {err['msg']}"
                for err in exc.errors()
            )
            safe = {
                k: (v if isinstance(v, str | int | float | bool | type(None)) else str(v))
                for k, v in data.items()
            }
            self.rejects.append(Reject(record_type=model.record_type, data=safe, reason=reason))

    def bump_watermark(self, value: datetime | None) -> None:
        if value is not None and (self.watermark is None or value > self.watermark):
            self.watermark = value
