"""Legistar (Granicus) Web API platform adapter. One adapter, many cities: tenant comes from YAML.

API: https://webapi.legistar.com/Help  (public, read-only, OData query options).
Incremental by EventLastModifiedUtc (source-side change time), not by meeting date, so edits to
past meetings (minutes published, title corrected) are picked up.
Never reads Body contact fields (names, phones, emails): out of scope by policy.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from scrapeline.adapters.base import BaseAdapter, Window
from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Target
from scrapeline.errors import ParseError
from scrapeline.pipeline.models import Body, Document, Meeting
from scrapeline.pipeline.normalise import clean_text, parse_date, parse_datetime_utc, parse_time

PAGE_SIZE = 1000  # Legistar's maximum $top


def _odata_dt(value: datetime) -> str:
    return f"datetime'{value.astimezone(UTC):%Y-%m-%dT%H:%M:%S}'"


def _url(base: str, path: str, params: dict[str, str | int]) -> str:
    query = urlencode(params, quote_via=quote, safe="$'():,")
    return f"{base.rstrip('/')}/{path}?{query}" if query else f"{base.rstrip('/')}/{path}"


class LegistarAdapter(BaseAdapter):
    page_size = PAGE_SIZE

    def discover(self, window: Window) -> Iterator[Target]:
        yield Target(url=_url(self.source.entry, "bodies", {}), kind="bodies")
        since = window.since or datetime.now(UTC) - timedelta(
            days=self.source.watermark.initial_lookback_days
        )
        if window.backfill:
            until = window.until or datetime.now(UTC)
            flt = f"EventDate ge {_odata_dt(since)} and EventDate lt {_odata_dt(until)}"
        else:
            flt = f"EventLastModifiedUtc ge {_odata_dt(since)}"
        yield self._events_target(flt, skip=0)

    def _events_target(self, flt: str, skip: int) -> Target:
        params: dict[str, str | int] = {
            "$filter": flt,
            "$orderby": "EventId",
            "$top": self.page_size,
            "$skip": skip,
        }
        return Target(
            url=_url(self.source.entry, "events", params),
            kind="events",
            meta={"filter": flt, "skip": skip},
        )

    # -- pure -------------------------------------------------------------------------------

    def parse(self, raw: RawDoc) -> ParseResult:
        try:
            payload: Any = json.loads(raw.content)
        except json.JSONDecodeError as exc:
            raise ParseError(f"legistar: response is not JSON ({raw.url})") from exc
        if not isinstance(payload, list):
            raise ParseError(f"legistar: expected a JSON array, got {type(payload).__name__}")
        result = ParseResult()
        if raw.kind == "bodies":
            for item in payload:
                self._parse_body(item, result)
        elif raw.kind == "events":
            for item in payload:
                self._parse_event(item, result)
            if len(payload) >= self.page_size and "filter" in raw.meta:
                skip = int(raw.meta.get("skip", 0)) + self.page_size
                result.next_targets.append(self._events_target(str(raw.meta["filter"]), skip))
        else:
            raise ParseError(f"legistar: unknown raw kind {raw.kind!r}")
        return result

    @staticmethod
    def _parse_body(item: dict[str, Any], result: ParseResult) -> None:
        result.add(
            Body,
            {
                "external_id": str(item.get("BodyId", "")),
                "name": item.get("BodyName"),
                "body_type": item.get("BodyTypeName"),
                "active": item.get("BodyActiveFlag") == 1 if "BodyActiveFlag" in item else None,
            },
        )

    @staticmethod
    def _parse_event(item: dict[str, Any], result: ParseResult) -> None:
        event_id = str(item.get("EventId", ""))
        agenda_status = (item.get("EventAgendaStatusName") or "").lower()
        comment = (item.get("EventComment") or "").lower()
        cancelled = "cancel" in agenda_status or "cancel" in comment
        postponed = "postpone" in agenda_status or "postpone" in comment
        modified = parse_datetime_utc(item.get("EventLastModifiedUtc"))
        agenda_url = clean_text(item.get("EventAgendaFile"))
        minutes_url = clean_text(item.get("EventMinutesFile"))
        result.add(
            Meeting,
            {
                "external_id": event_id,
                "canonical_url": clean_text(item.get("EventInSiteURL")),
                "title": item.get("EventBodyName"),
                "meeting_date": parse_date(item.get("EventDate")),
                "start_time": parse_time(item.get("EventTime")),
                "body_external_id": str(item["EventBodyId"]) if item.get("EventBodyId") else None,
                "body_name": item.get("EventBodyName"),
                "location": item.get("EventLocation"),
                "status": "cancelled" if cancelled else "postponed" if postponed else "scheduled",
                "agenda_url": agenda_url,
                "minutes_url": minutes_url,
                "source_modified_at": modified,
            },
        )
        result.bump_watermark(modified)
        for kind, url in (("agenda", agenda_url), ("minutes", minutes_url)):
            if url:
                result.add(
                    Document,
                    {
                        "external_id": url,
                        "meeting_external_id": event_id,
                        "kind": kind,
                        "title": f"{item.get('EventBodyName') or 'Meeting'} {kind}",
                        "url": url,
                    },
                )
