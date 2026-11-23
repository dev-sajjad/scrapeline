"""Scrape instruction: what a person asks for, in their words. Pure, no I/O.

An instruction says *what* to collect; the platform works out *how*. It is compiled into a
source config plus a site adapter (declarative spec), either from a known platform, by hand,
or by the generator agent. Minimal form:

    name: Books catalogue
    start_urls: [https://books.toscrape.com/]
    collect: "Every book: title, price, stock status, star rating, product page link"
    usage: {purpose: "Price tracking demo"}

`dataset` picks a dataset type. For an existing type, `fields` selects a subset to collect
(types must match; new fields need a new dataset type or version). For a new type name, or
with no `dataset`, `fields` defines the new type and needs at least one key field. With
neither, a dataset type is proposed from `collect` and needs human approval.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scrapeline.catalog.datasets import (
    NAME_PATTERN,
    RESERVED_FIELDS,
    DatasetType,
    FieldDef,
    default_registry,
)
from scrapeline.errors import ConfigError, DatasetError
from scrapeline.pipeline.locale import Locale
from scrapeline.registry.config import UsageRights

PaginationRequest = Literal["auto", "none", "next-link", "page-number", "cursor", "load-more"]
Schedule = Literal["manual", "hourly", "daily", "weekly"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CrawlRequest(_Strict):
    pagination: PaginationRequest = "auto"
    follow_detail_pages: bool = False
    max_pages: int = Field(default=50, ge=1, le=10_000)
    render: Literal["auto", "never", "always"] = "auto"


class ScrapeInstruction(_Strict):
    instruction_version: Literal[1] = 1
    id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=1, max_length=200)
    start_urls: list[str] = Field(min_length=1, max_length=20)
    collect: str = Field(min_length=10, max_length=4000)
    dataset: str | None = Field(default=None, pattern=NAME_PATTERN)
    fields: dict[str, FieldDef] | None = None
    crawl: CrawlRequest = CrawlRequest()
    schedule: Schedule = "daily"
    documents: Literal["ignore", "link-only", "download"] = "ignore"
    locale: Locale = Locale()
    usage: UsageRights
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("start_urls")
    @classmethod
    def _http_urls(cls, urls: list[str]) -> list[str]:
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"start URL must be http(s) with a host: {url!r}")
        return urls

    @field_validator("fields")
    @classmethod
    def _field_names(cls, fields: dict[str, FieldDef] | None) -> dict[str, FieldDef] | None:
        for name in fields or {}:
            if re.fullmatch(NAME_PATTERN, name) is None or name in RESERVED_FIELDS:
                raise ValueError(f"invalid or reserved field name {name!r}")
        return fields

    @model_validator(mode="after")
    def _fields_fit_the_dataset(self) -> ScrapeInstruction:
        existing = self.existing_dataset
        if existing is not None:
            for name, f in (self.fields or {}).items():
                known = existing.fields.get(name)
                if known is None or known.type != f.type:
                    raise ValueError(
                        f"field {name!r} is not a {f.type} field of dataset {existing.name!r}"
                    )
        elif self.fields is not None and not any(f.key for f in self.fields.values()):
            raise ValueError("a new dataset type needs at least one field with key: true")
        elif self.dataset is not None and self.fields is None:
            raise ValueError(f"unknown dataset {self.dataset!r}; define its fields")
        return self

    @model_validator(mode="after")
    def _personal_data_needs_permission(self) -> ScrapeInstruction:
        existing = self.existing_dataset
        source = existing.fields if existing is not None else (self.fields or {})
        wanted = self.fields.keys() if self.fields else source.keys()
        pii = [name for name in wanted if source[name].pii]
        if pii and not self.usage.allow_pii:
            raise ValueError(f"fields {pii} are personal data; set usage.allow_pii with a purpose")
        if self.usage.allow_pii and not self.usage.purpose:
            raise ValueError("usage.allow_pii needs a stated usage.purpose")
        return self

    @property
    def existing_dataset(self) -> DatasetType | None:
        if self.dataset is None:
            return None
        try:
            return default_registry().get(self.dataset)
        except DatasetError:
            return None

    @property
    def source_id(self) -> str:
        """`id`, else a slug of `name`; names without Latin letters get a stable hash id."""
        if self.id:
            return self.id
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")[:100].strip("-")
        if len(slug) >= 2:
            return slug
        return "src-" + hashlib.sha256(self.name.encode()).hexdigest()[:8]

    @property
    def hosts(self) -> list[str]:
        start = {(urlsplit(u).hostname or "").lower() for u in self.start_urls}
        return sorted((start | {h.lower() for h in self.allowed_hosts}) - {""})

    @property
    def needs_dataset_proposal(self) -> bool:
        return self.dataset is None and not self.fields

    @classmethod
    def from_yaml(cls, text: str) -> ScrapeInstruction:
        try:
            data: Any = yaml.safe_load(text)
            return cls.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"invalid instruction: {exc}") from exc
