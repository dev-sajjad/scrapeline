"""Identity and change detection.

Identity = (source_id, external_id). When a source has no stable id, parsers call
`meeting_natural_key` (jurisdiction + canonical URL, else body + date + folded title).
Change detection = sha256 over the record's content fields, never identity alone.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from typing import Any

from scrapeline.pipeline.models import Record
from scrapeline.pipeline.normalise import fold


def _default(value: Any) -> str:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    raise TypeError(f"not serialisable: {type(value).__name__}")


def content_hash(record: Record) -> str:
    payload = record.model_dump(exclude=set(record.hash_exclude))
    payload["__type"] = record.record_type
    payload["__schema_version"] = record.schema_version
    blob = json.dumps(payload, sort_keys=True, default=_default, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def meeting_natural_key(
    *,
    canonical_url: str | None = None,
    body: str | None = None,
    meeting_date: date | None = None,
    title: str | None = None,
) -> str:
    if canonical_url:
        basis = f"url|{canonical_url.strip()}"
    else:
        basis = f"bdt|{fold(body)}|{meeting_date.isoformat() if meeting_date else ''}|{fold(title)}"
    return "nk:" + hashlib.sha256(basis.encode()).hexdigest()[:32]
