"""Source config schema and loader for config/sources/*.yaml."""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from scrapeline.errors import ConfigError, StatusTransitionError
from scrapeline.pipeline.locale import Locale

Status = Literal["draft", "active", "broken", "retired"]
Pagination = Literal["none", "next-link", "page-number", "cursor", "load-more", "offset"]

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"active", "broken", "retired"}),
    "active": frozenset({"broken", "retired", "draft"}),
    "broken": frozenset({"draft", "active", "retired"}),
    "retired": frozenset({"draft"}),
}


def check_transition(current: str, new: str) -> None:
    if new == current:
        return
    if new not in TRANSITIONS.get(current, frozenset()):
        raise StatusTransitionError(f"illegal status transition {current} -> {new}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Jurisdiction(_Strict):
    id: str = Field(pattern=r"^[a-z]{2}(-[a-z0-9]+)+$")
    name: str
    state: str | None = None
    country: str = Field(default="US", pattern=r"^[A-Z]{2}$")


class Politeness(_Strict):
    rps: float = Field(default=1.0, gt=0, le=5)
    concurrency: int = Field(default=1, ge=1, le=2)
    respect_robots: bool = True


class Legal(_Strict):
    terms_checked: date
    notes: str


class UsageRights(_Strict):
    """Why the data is collected and how it may be used. Checked before personal data is kept."""

    purpose: str = ""
    license: str | None = None
    allow_pii: bool = False
    retention_days: int | None = Field(default=None, ge=1)
    terms_url: str | None = None


class WatermarkConfig(_Strict):
    field: str = "source_modified_at"
    initial_lookback_days: int = Field(default=365, ge=1)
    overlap_hours: int = Field(default=24, ge=0)  # re-read a margin to catch late edits


class QualityConfig(_Strict):
    min_records: int = 1
    max_records: int = 100_000
    max_reject_rate: float = 0.05
    max_null_rate: dict[str, float] = Field(
        default_factory=lambda: {"title": 0.0, "meeting_date": 0.0, "location": 0.5}
    )


class Instruction(_Strict):
    collect: str
    schema_: str = Field(alias="schema")
    pagination: Pagination = "none"
    documents: Literal["ignore", "link-only", "download"] = "link-only"
    max_pages_per_run: int = 50
    fields_excluded: list[str] = Field(default_factory=list)


class SourceConfig(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    name: str
    platform: str  # legistar | custom | fixture
    tenant: str | None = None
    kind: Literal["api", "html", "js", "pdf_index"]
    entry: str
    jurisdiction: Jurisdiction | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    cadence: str = "daily"
    politeness: Politeness = Politeness()
    legal: Legal
    schema_: str = Field(default="Meeting", alias="schema")
    dataset: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,62}$")
    locale: Locale = Locale()
    usage: UsageRights = UsageRights()
    watermark: WatermarkConfig = WatermarkConfig()
    adapter: str
    status: Status = "draft"
    config_version: int = 1
    owner: str = "human"
    pagination: Pagination = "none"
    max_pages_per_run: int = Field(default=50, ge=1, le=10_000)
    quality: QualityConfig = QualityConfig()
    instruction: Instruction | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entry")
    @classmethod
    def _entry_http(cls, value: str) -> str:
        if urlsplit(value).scheme not in ("http", "https"):
            raise ValueError("entry must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _civic_needs_jurisdiction(self) -> SourceConfig:
        from scrapeline.pipeline.models import SCHEMAS

        if self.dataset is None and self.schema_ in SCHEMAS and self.jurisdiction is None:
            raise ValueError(f"schema {self.schema_} needs a jurisdiction")
        return self

    @property
    def dataset_name(self) -> str:
        """Canonical dataset name ('meeting', 'product'); legacy `schema: Meeting` maps to it."""
        from scrapeline.pipeline.models import SCHEMAS

        if self.dataset:
            return self.dataset
        model = SCHEMAS.get(self.schema_)
        return model.record_type if model else self.schema_.lower()

    @property
    def hosts(self) -> list[str]:
        """Allowlist = entry host + explicitly listed hosts."""
        entry_host = (urlsplit(self.entry).hostname or "").lower()
        return sorted({entry_host, *(h.lower() for h in self.allowed_hosts)} - {""})


_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(text: str, env: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in env:
            raise ConfigError(f"undefined variable ${{{name}}} in source config")
        return env[name]

    return _VAR.sub(repl, text)


def load_source_file(path: Path, env: dict[str, str] | None = None) -> SourceConfig:
    from scrapeline.settings import get_settings

    settings = get_settings()
    variables = {"FIXTURE_BASE_URL": settings.fixture_base_url, **os.environ, **(env or {})}
    try:
        data = yaml.safe_load(_expand(path.read_text(), variables))
        return SourceConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc


def load_sources(directory: Path) -> dict[str, SourceConfig]:
    sources: dict[str, SourceConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        cfg = load_source_file(path)
        if cfg.id in sources:
            raise ConfigError(f"duplicate source id {cfg.id} in {path.name}")
        if path.stem != cfg.id:
            raise ConfigError(f"{path.name}: file name must equal source id '{cfg.id}'")
        sources[cfg.id] = cfg
    return sources
