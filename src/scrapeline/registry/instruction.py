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

`compile_instruction` turns an instruction into a draft source config, its dataset type (when
new) and a spec.yaml skeleton. The draft never runs on a schedule until a human approves the
dataset type, completes the adapter, reviews the site's terms and activates it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from scrapeline.catalog.datasets import (
    NAME_PATTERN,
    RESERVED_FIELDS,
    DatasetRegistry,
    DatasetType,
    FieldDef,
    default_registry,
)
from scrapeline.errors import ConfigError, DatasetError
from scrapeline.pipeline.locale import Locale
from scrapeline.registry.config import (
    UNREVIEWED_TERMS,
    Pagination,
    SourceConfig,
    UsageRights,
    WatermarkConfig,
)

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
    def _public_http_urls(cls, urls: list[str]) -> list[str]:
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"start URL must be http(s) with a host: {url!r}")
            # The URL is written into a committed source config: credentials never go there.
            if parts.username is not None or parts.password is not None:
                raise ValueError("start URLs may not contain a user name or password")
            problem = host_problem(parts.hostname)
            if problem:
                raise ValueError(f"start URL host {parts.hostname!r}: {problem}")
        return urls

    @field_validator("allowed_hosts")
    @classmethod
    def _public_hosts(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            if urlsplit(f"//{host}").hostname != host.lower():
                raise ValueError(
                    f"allowed_hosts takes bare host names such as docs.example.com, not {host!r}"
                )
            problem = host_problem(host)
            if problem:
                raise ValueError(f"allowed host {host!r}: {problem}")
        return hosts

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


# Top-level names that never resolve on the public internet (RFC 2606, 6761, 6762, 8375,
# ICANN's .internal) plus suffixes that private networks commonly use.
_PRIVATE_TLDS = frozenset(
    "localhost local localdomain internal intranet lan home corp private "
    "test example invalid onion arpa".split()
)


def host_problem(host: str) -> str | None:
    """Why `host` is not a public DNS name, or None. Static checks only, no DNS lookup.

    The URL policy still resolves and checks every address at fetch time; this keeps IP
    literals and local names out of a source config before it is written and committed.
    """
    name = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return "IP addresses are not allowed; use the site's host name"
    labels = name.split(".")
    if not all(labels) or not all(c.isalnum() or c in "-_" for label in labels for c in label):
        return "not a valid host name"
    if len(labels) < 2 or labels[-1] in _PRIVATE_TLDS:
        return "not a public host name"
    if not labels[-1][0].isalpha():
        # Numeric forms such as 127.1 or 0x7f.1, which resolvers read as IP addresses.
        return "IP addresses are not allowed; use the site's host name"
    return None


def site_dir_name(source_id: str) -> str:
    """Directory of a source's site adapter under adapters/sites/, as the runtime finds it."""
    # The declarative adapter owns the id -> directory mapping; derive it, never repeat it.
    from scrapeline.adapters.declarative import spec_path_for

    return spec_path_for(source_id).parent.name


# Dataset types the generator agent can write specs for today: it starts from a Meeting
# skeleton and its tools check specs against the typed civic models. Widen this once it
# works from the dataset registry.
GENERATOR_DATASETS = frozenset({"meeting"})


def generator_supports(dataset: DatasetType | None) -> bool:
    """Whether the generator agent can write a spec for records of `dataset`."""
    return dataset is not None and dataset.storage == "typed" and dataset.name in GENERATOR_DATASETS


DECLARATIVE_ADAPTER = "scrapeline.adapters.declarative:DeclarativeAdapter"
UNKNOWN_JURISDICTION = {"id": "xx-unknown", "name": "TODO(human): jurisdiction", "country": "XX"}

_PAGINATION: dict[str, Pagination] = {
    "auto": "next-link",
    "none": "none",
    "next-link": "next-link",
    "page-number": "page-number",
    "cursor": "cursor",
    "load-more": "load-more",
}
# Spec transform per field type; text and enum fields are read as plain text.
_TRANSFORMS = {
    "int": "int",
    "float": "float",
    "decimal": "decimal",
    "money": "money",
    "bool": "bool",
    "url": "absolute_url",
    "date": "date",
    "datetime": "datetime",
    "time": "time",
}
# load_source_file expands ${NAME} (and later forms such as ${NAME:-default}) from the
# environment, so the rendered config must not contain "${" at all.
_ENV_MARKER = "${"


@dataclass(frozen=True)
class CompiledInstruction:
    """A draft source, its dataset type (None while one must be proposed) and a spec skeleton."""

    source: SourceConfig
    dataset_type: DatasetType | None
    dataset_is_new: bool
    fields: tuple[str, ...]
    spec_skeleton: str
    next_steps: tuple[str, ...]

    @property
    def needs_dataset_proposal(self) -> bool:
        return self.dataset_type is None

    @property
    def site_dir(self) -> str:
        """Directory name of the site adapter under adapters/sites/."""
        return site_dir_name(self.source.id)

    @property
    def generator_supported(self) -> bool:
        """Whether the generator agent can write this source's spec today."""
        return generator_supports(self.dataset_type)

    @property
    def personal_data_fields(self) -> tuple[str, ...]:
        if self.dataset_type is None:
            return ()
        return tuple(n for n in self.fields if self.dataset_type.fields[n].pii)

    def source_yaml(self) -> str:
        return render_source_yaml(self.source)

    def dataset_yaml(self) -> str | None:
        """Definition file for config/datasets/, only when the dataset type is new."""
        if self.dataset_type is None or not self.dataset_is_new:
            return None
        return render_dataset_yaml(self.dataset_type)

    def summary(self) -> dict[str, Any]:
        src = self.source
        return {
            "source_id": src.id,
            "name": src.name,
            "status": src.status,
            "entry": src.entry,
            "allowed_hosts": src.hosts,
            "kind": src.kind,
            "cadence": src.cadence,
            "pagination": src.pagination,
            "max_pages_per_run": src.max_pages_per_run,
            "dataset": src.dataset_name,
            "dataset_is_new": self.dataset_is_new,
            "dataset_proposal_needed": self.needs_dataset_proposal,
            "fields": list(self.fields),
            "fields_excluded": src.instruction.fields_excluded if src.instruction else [],
            "personal_data_fields": list(self.personal_data_fields),
            "next_steps": list(self.next_steps),
        }


def compile_instruction(ins: ScrapeInstruction, registry: DatasetRegistry) -> CompiledInstruction:
    """Compile an instruction into a draft source, dataset type and spec skeleton. Pure.

    Dataset types are looked up in `registry` only. Raises ConfigError when the instruction
    does not fit that registry (unknown dataset, mismatched or missing fields, personal data
    without permission) or when its text would be expanded as an environment variable.
    """
    dataset, is_new = _resolve_dataset(ins, registry)
    if dataset is None:
        dataset_name = ins.dataset or _derived_dataset_name(ins.source_id)
        wanted: dict[str, FieldDef] = {}
        excluded: list[str] = []
    else:
        dataset_name = dataset.name
        wanted = _wanted_fields(ins, dataset)
        excluded = [name for name in dataset.fields if name not in wanted]
    typed = dataset is not None and dataset.storage == "typed"
    # The legacy `schema` key keeps naming the hand-written model of a typed dataset.
    schema = dataset.record_model().__name__ if dataset is not None and typed else dataset_name
    pagination = _PAGINATION[ins.crawl.pagination]
    options: dict[str, Any] = {}
    if len(ins.start_urls) > 1:
        options["extra_start_urls"] = ins.start_urls[1:]
    if ins.crawl.follow_detail_pages:
        options["follow_detail_pages"] = True
    data: dict[str, Any] = {
        "id": ins.source_id,
        "name": ins.name,
        "platform": "custom",
        "kind": "js" if ins.crawl.render == "always" else "html",
        "entry": ins.start_urls[0],
        "allowed_hosts": ins.hosts,
        "cadence": ins.schedule,
        "legal": {"terms_checked": UNREVIEWED_TERMS, "notes": _legal_note(ins)},
        "schema": schema,
        "dataset": dataset_name,
        "locale": ins.locale,
        "usage": ins.usage,
        "adapter": DECLARATIVE_ADAPTER,
        "status": "draft",
        "owner": "instruction",
        "pagination": pagination,
        "max_pages_per_run": ins.crawl.max_pages,
        "quality": {"max_null_rate": {n: 0.0 for n, f in wanted.items() if f.required}},
        "instruction": {
            "collect": ins.collect,
            "schema": dataset_name,
            "pagination": pagination,
            "documents": ins.documents,
            "max_pages_per_run": ins.crawl.max_pages,
            "fields_excluded": excluded,
        },
        "options": options,
    }
    if typed:
        data["jurisdiction"] = UNKNOWN_JURISDICTION
    try:
        source = SourceConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"instruction does not compile to a valid source: {exc}") from exc
    if _ENV_MARKER in render_source_yaml(source):
        raise ConfigError(
            'instruction text may not contain "${": source configs expand ${...} '
            "from the environment"
        )
    return CompiledInstruction(
        source=source,
        dataset_type=dataset,
        dataset_is_new=is_new,
        fields=tuple(wanted),
        spec_skeleton=_spec_skeleton(
            ins, dataset_name, wanted, pagination, generator_supports(dataset)
        ),
        next_steps=tuple(_next_steps(ins, source, dataset, is_new, wanted, typed)),
    )


def _resolve_dataset(
    ins: ScrapeInstruction, registry: DatasetRegistry
) -> tuple[DatasetType | None, bool]:
    if ins.needs_dataset_proposal:
        return None, False
    name = ins.dataset or _derived_dataset_name(ins.source_id)
    try:
        existing: DatasetType | None = registry.get(name)
    except DatasetError:
        existing = None
    if existing is not None and ins.dataset is not None:
        return existing, False
    if not ins.fields:
        raise ConfigError(f"unknown dataset {name!r}; define its fields")
    try:
        candidate = DatasetType(
            name=name,
            description=_description(ins),
            pack="user",
            fields=ins.fields,
            canonical_url_field=_canonical_url_field(ins.fields),
        )
    except ValidationError as exc:
        raise DatasetError(f"invalid dataset type {name!r}: {exc}") from exc
    if existing is None:
        return candidate, True
    # The name was derived from the source id; reuse it only if it is the same contract.
    if existing.definition_hash == candidate.definition_hash:
        return existing, False
    raise ConfigError(
        f"dataset {name!r} already exists with a different definition; "
        "set `dataset` to name the new type"
    )


def _wanted_fields(ins: ScrapeInstruction, dataset: DatasetType) -> dict[str, FieldDef]:
    if ins.fields:
        for name, f in ins.fields.items():
            known = dataset.fields.get(name)
            if known is None or known.type != f.type:
                raise ConfigError(
                    f"field {name!r} is not a {f.type} field of dataset {dataset.name!r}; "
                    "new fields need a new dataset type or version"
                )
        missing = [n for n, f in dataset.fields.items() if f.required and n not in ins.fields]
        if missing:
            raise ConfigError(
                f"fields must include the key and required fields of {dataset.name!r}: {missing}"
            )
    wanted = {n: f for n, f in dataset.fields.items() if not ins.fields or n in ins.fields}
    pii = [n for n, f in wanted.items() if f.pii]
    if pii and not ins.usage.allow_pii:
        raise ConfigError(f"fields {pii} are personal data; set usage.allow_pii with a purpose")
    return wanted


def _derived_dataset_name(source_id: str) -> str:
    name = source_id.replace("-", "_")
    return (name if name[0].isalpha() else f"d_{name}")[:63]


def _description(ins: ScrapeInstruction) -> str:
    first_line = (ins.collect.strip().splitlines() or [ins.name])[0]
    return first_line.strip()[:200]


def _canonical_url_field(fields: dict[str, FieldDef]) -> str | None:
    urls = [name for name, f in fields.items() if f.type == "url"]
    for name in urls:
        if fields[name].key:
            return name
    return "url" if "url" in urls else None


def _legal_note(ins: ScrapeInstruction) -> str:
    terms = f" ({ins.usage.terms_url})" if ins.usage.terms_url else ""
    return (
        f"TODO(human): not reviewed. Read the terms of use{terms} and robots.txt of "
        f"{', '.join(ins.hosts)}, then set terms_checked to the review date and replace "
        "this note with why collecting is allowed and what is excluded."
    )


def _field_placeholder(name: str, f: FieldDef) -> dict[str, Any]:
    entry: dict[str, Any] = {"css": f"TODO: CSS selector for {name}"}
    # Typed (civic) models describe URLs as text, so the name decides there.
    if f.type == "url" or (f.type == "text" and (name == "url" or name.endswith("_url"))):
        entry["attr"] = "src" if "image" in name else "href"
        entry["transform"] = "absolute_url"
    elif f.type in _TRANSFORMS:
        entry["transform"] = _TRANSFORMS[f.type]
    if not f.required:
        entry["optional"] = True
    return entry


def _spec_skeleton(
    ins: ScrapeInstruction,
    schema: str,
    fields: dict[str, FieldDef],
    pagination: Pagination,
    by_generator: bool,
) -> str:
    spec: dict[str, Any] = {
        "spec_version": 1,
        "schema": schema,
        "record": "TODO: CSS selector matching one record element",
        "fields": {name: _field_placeholder(name, f) for name, f in fields.items()},
    }
    if pagination == "next-link":
        spec["next_page"] = {"css": "TODO: CSS selector for the next-page link", "attr": "href"}
    if ins.crawl.render == "always":
        render = {"wait_for": "TODO: CSS selector present once the records have rendered"}
        if pagination == "load-more":
            render["click_until_gone"] = "TODO: CSS selector for the load-more button"
        spec["render"] = render
    header = f"# Site adapter skeleton for {ins.source_id}. Replace every TODO selector by hand"
    header += (
        ",\n# or run the generator agent on captured sample pages.\n" if by_generator else ".\n"
    )
    # Flow style and a wide line keep one field per line, like hand-written specs.
    body = yaml.safe_dump(
        spec, sort_keys=False, allow_unicode=True, default_flow_style=None, width=200
    )
    return header + body


def _next_steps(
    ins: ScrapeInstruction,
    source: SourceConfig,
    dataset: DatasetType | None,
    is_new: bool,
    wanted: dict[str, FieldDef],
    typed: bool,
) -> list[str]:
    sid = source.id
    name = source.dataset_name
    spec = f"adapters/sites/{site_dir_name(sid)}/spec.yaml"
    # A browser-rendered source captured without --render saves the empty JS shell.
    capture = f"scrapeline capture {sid}" + (" --render" if source.kind == "js" else "")
    steps: list[str] = []
    if dataset is None:
        steps += [
            f"Capture sample pages: {capture}.",
            f"A dataset type must be proposed: define '{name}' in config/datasets/{name}.yaml "
            "by hand and have a human approve it.",
            f"Then add `dataset: {name}` and its `fields` to the instruction and apply it "
            "again with --force.",
        ]
    elif is_new:
        keys = ", ".join(dataset.key_fields)
        steps.append(
            f"Approve the new dataset type '{name}' (config/datasets/{name}.yaml): check field "
            f"types, key fields ({keys}) and personal-data flags."
        )
    if typed:
        steps.append(
            "Fill in the jurisdiction block of the source config (placeholder xx-unknown)."
        )
    if generator_supports(dataset):
        steps.append(
            f"Fill the TODO selectors in {spec} by hand, or capture sample pages "
            f"({capture}) and run the generator agent: scrapeline agent generate "
            f"--instruction <instruction file> --pages data/samples/{sid}."
        )
    else:
        steps.append(
            f"Fill the TODO selectors in {spec} by hand from sample pages ({capture}). "
            f"The generator agent writes only {', '.join(sorted(GENERATOR_DATASETS))} specs "
            "until it works from the dataset registry."
        )
    if ins.crawl.pagination == "auto":
        steps.append(
            "Pagination 'auto' was compiled as next-link: point next_page at the next-page "
            "link, or remove it and set pagination: none."
        )
    if ins.crawl.pagination == "load-more" and ins.crawl.render != "always":
        steps.append("Load-more pagination needs a browser: set crawl.render to always.")
    if ins.crawl.follow_detail_pages:
        steps.append(
            "The skeleton reads list pages only; following detail pages needs the generator "
            "agent or a hand-written parse.py."
        )
    if len(ins.start_urls) > 1:
        steps.append(
            "Only the first start URL is crawled; the others are kept in options.extra_start_urls."
        )
    if ins.documents != "ignore":
        steps.append(
            f"Documents are set to '{ins.documents}': list the document link fields under "
            "`documents` in the spec."
        )
    pii = [n for n, f in wanted.items() if f.pii]
    if pii:
        retention = (
            f"retention is {ins.usage.retention_days} days"
            if ins.usage.retention_days
            else "set usage.retention_days"
        )
        steps.append(
            f"Personal-data fields {pii} will be stored: confirm the stated purpose and "
            f"licence; {retention}."
        )
    steps += [
        f"Review the terms of use and robots.txt of {', '.join(source.hosts)}; record the "
        "review date in legal.terms_checked and why collecting is allowed in legal.notes.",
        f"Draft live run: scrapeline run {sid}; check the sample rows and the quality checks.",
        f"Activate: scrapeline source set-status {sid} active.",
    ]
    if ins.schedule == "manual":
        steps.append(
            f"Schedule is manual: the scheduler never enqueues {sid}; run it with "
            f"scrapeline run {sid}."
        )
    return steps


def render_source_yaml(source: SourceConfig) -> str:
    """Source config as YAML. safe_dump quotes untrusted text, so it cannot add keys."""
    data = source.model_dump(mode="json", by_alias=True, exclude_none=True)
    if source.watermark == WatermarkConfig():
        data.pop("watermark")
    if not source.options:
        data.pop("options")
    header = (
        "# Draft source compiled from a scrape instruction. It stays draft until a human\n"
        "# completes the next steps printed by `scrapeline instruction apply`.\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def render_dataset_yaml(dataset: DatasetType) -> str:
    fields = {
        name: {"type": f.type, **f.model_dump(mode="json", exclude_defaults=True, exclude={"type"})}
        for name, f in dataset.fields.items()
    }
    data: dict[str, Any] = {
        "name": dataset.name,
        "version": dataset.version,
        "pack": dataset.pack,
        "description": dataset.description,
    }
    if dataset.canonical_url_field:
        data["canonical_url_field"] = dataset.canonical_url_field
    data["fields"] = fields
    header = "# User dataset type proposed by a scrape instruction. A human approves it.\n"
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
