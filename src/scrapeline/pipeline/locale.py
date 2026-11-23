"""How a source writes dates, times, numbers and prices. Pure, no I/O.

Strict by default: a `date_order` of None accepts only unambiguous dates (`03/04/2026`
becomes a reject, not a guess), and a `timezone` of None rejects times without an offset.
Naive times are interpreted in `timezone` and stored as UTC.
"""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

DateOrder = Literal["dmy", "mdy", "ymd"]


class Locale(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
    timezone: str | None = None
    date_order: DateOrder | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    decimal_separator: Literal[".", ","] = "."

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @property
    def tzinfo(self) -> ZoneInfo | None:
        return ZoneInfo(self.timezone) if self.timezone else None
