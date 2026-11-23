"""Schema registry: what one record of each dataset type looks like. Pure, no I/O beyond YAML.

A dataset type is a versioned data contract. Generic types (product, article, ...) are defined
in YAML and stored as validated JSON in `data.records`; typed types (the civic pack) map to
hand-written models with their own tables. Changing a definition requires a version bump;
`catalog.repository.sync_dataset_types` refuses a silent redefinition.

Identity: a parser may supply `external_id`; otherwise it is derived from the key fields
(one key: its canonical value; several keys: a hash of them). Key values are coerced to the
field type first, so "01" and 1 give the same identity. Key fields must not change between
versions of a dataset type, or every record would get a new identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import types
import typing
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, ClassVar, Literal, Union, get_args, get_origin

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from scrapeline.errors import DatasetError
from scrapeline.pipeline.models import SCHEMAS, Record
from scrapeline.pipeline.normalise import clean_text
from scrapeline.pipeline.values import Money

FieldType = Literal[
    "text", "int", "float", "decimal", "money", "bool", "url", "date", "datetime", "time", "enum"
]
Storage = Literal["generic", "typed"]
KEY_TYPES = frozenset({"text", "url", "int", "date", "enum"})

PACKS_DIR = Path(__file__).resolve().parent / "packs"
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"
RESERVED_FIELDS = frozenset(
    {
        "id",
        "external_id",
        "canonical_url",
        "record_type",
        "schema_version",
        "source_id",
        "dataset",
        "payload",
        "content_hash",
        "first_seen_at",
        "last_seen_at",
        "fetched_at",
        "run_id",
        "scraper_version_id",
        "raw_sha256",
    }
)
_PYTHON_TYPES: dict[str, Any] = {
    "text": str,
    "int": int,
    "float": float,
    "decimal": Decimal,
    "money": Money,
    "bool": bool,
    "url": str,
    "date": date,
    "datetime": datetime,
    "time": time,
}


class FieldDef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: FieldType = "text"
    required: bool = False
    key: bool = False
    pii: bool = False
    description: str = ""
    values: tuple[str, ...] | None = None

    @model_validator(mode="before")
    @classmethod
    def _key_implies_required(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"type": data}
        if isinstance(data, dict) and data.get("key"):
            return {**data, "required": True}
        return data

    @model_validator(mode="after")
    def _enum_values(self) -> FieldDef:
        if (self.type == "enum") != bool(self.values):
            raise ValueError("`values` is required for enum fields and only allowed there")
        return self


class DatasetType(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    version: int = Field(default=1, ge=1)
    description: str
    pack: str = "core"
    storage: Storage = "generic"
    fields: dict[str, FieldDef] = Field(min_length=1)
    canonical_url_field: str | None = None

    @model_validator(mode="after")
    def _valid_contract(self) -> DatasetType:
        if self.storage == "typed":
            return self
        for name, f in self.fields.items():
            if not _is_name(name) or name in RESERVED_FIELDS or name in _model_attributes():
                raise ValueError(f"invalid or reserved field name {name!r}")
            if f.key and f.type not in KEY_TYPES:
                raise ValueError(f"key field {name!r} must be one of {sorted(KEY_TYPES)}")
        if not self.key_fields:
            raise ValueError(f"dataset {self.name!r} needs at least one key field")
        if self.canonical_url_field is not None:
            target = self.fields.get(self.canonical_url_field)
            if target is None or target.type != "url":
                raise ValueError("canonical_url_field must name a url field")
        return self

    @property
    def key_fields(self) -> tuple[str, ...]:
        return tuple(name for name, f in self.fields.items() if f.key)

    @property
    def pii_fields(self) -> tuple[str, ...]:
        return tuple(name for name, f in self.fields.items() if f.pii)

    @property
    def definition_hash(self) -> str:
        """Hash of the semantic definition; descriptions and pack do not count."""
        semantic = self.model_dump(
            mode="json",
            exclude={"description": True, "pack": True, "fields": {"__all__": {"description"}}},
        )
        blob = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def external_id(self, data: dict[str, Any]) -> str | None:
        """Identity from key fields; None when a key value is missing.

        Raises ValueError when a key value cannot be read as its field type.
        """
        values: list[str] = []
        for name in self.key_fields:
            raw = data.get(name)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                return None
            values.append(_canonical_key(name, self.fields[name], raw))
        if len(values) == 1:
            return values[0]
        blob = json.dumps(values, separators=(",", ":"))
        return "k:" + hashlib.sha256(blob.encode()).hexdigest()[:32]

    def record_model(self) -> type[Record]:
        if self.storage == "typed":
            return SCHEMAS[_typed_class_name(self.name)]
        key = (self.name, self.version, self.definition_hash)
        if key not in _MODELS:
            _MODELS[key] = _build_model(self)
        return _MODELS[key]


class GenericRecord(Record):
    """Base of every runtime-built record model; `dataset` is set on each subclass.

    The content hash skips empty fields and the schema version, so adding an optional field
    or bumping the version does not turn every record into a change.
    """

    dataset: ClassVar[DatasetType]
    hash_skip_none: ClassVar[bool] = True
    hash_includes_version: ClassVar[bool] = False

    @model_validator(mode="before")
    @classmethod
    def _prepare(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        ds = cls.dataset
        prepared = dict(data)
        for name, f in ds.fields.items():
            if f.type in ("text", "url", "enum") and isinstance(prepared.get(name), str):
                prepared[name] = clean_text(prepared[name])
        if ds.canonical_url_field and not prepared.get("canonical_url"):
            prepared["canonical_url"] = prepared.get(ds.canonical_url_field)
        if not prepared.get("external_id"):
            derived = ds.external_id(prepared)
            if derived is None:
                raise ValueError(f"key field(s) missing: {list(ds.key_fields)}")
            prepared["external_id"] = derived
        return prepared

    @model_validator(mode="after")
    def _check_values(self) -> GenericRecord:
        for name, f in self.dataset.fields.items():
            value = getattr(self, name)
            if value is None:
                continue
            if f.type == "url" and not str(value).startswith(("http://", "https://")):
                raise ValueError(f"{name}: must be an http(s) URL")
            if f.type == "datetime" and value.tzinfo is None:
                raise ValueError(f"{name}: datetime must be timezone-aware")
        return self


def _to_utc(cls: type[GenericRecord], value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC)


def _canonical_key(name: str, f: FieldDef, raw: Any) -> str:
    py: Any = Literal[f.values] if f.type == "enum" else _PYTHON_TYPES[f.type]
    value = raw.strip() if isinstance(raw, str) else raw
    try:
        coerced = TypeAdapter(py).validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"key field {name!r} is not a valid {f.type}: {raw!r}") from exc
    if isinstance(coerced, date):
        return coerced.isoformat()
    return clean_text(str(coerced)) or ""


@cache
def _model_attributes() -> frozenset[str]:
    """Names a field may not use because the record model already has them."""
    return frozenset(name for name in dir(GenericRecord) if not name.startswith("__"))


_MODELS: dict[tuple[str, int, str], type[Record]] = {}


def _build_model(dataset: DatasetType) -> type[Record]:
    fields: dict[str, Any] = {}
    for name, f in dataset.fields.items():
        py: Any = Literal[f.values] if f.type == "enum" else _PYTHON_TYPES[f.type]
        fields[name] = (py, ...) if f.required else (py | None, None)
    model_name = "".join(part.title() for part in dataset.name.split("_")) + f"V{dataset.version}"
    datetimes = [name for name, f in dataset.fields.items() if f.type == "datetime"]
    validators = {"_datetimes_to_utc": field_validator(*datetimes)(_to_utc)} if datetimes else {}
    model: Any = create_model(
        model_name, __base__=GenericRecord, __validators__=validators, **fields
    )
    model.record_type = dataset.name
    model.schema_version = dataset.version
    model.dataset = dataset
    return typing.cast(type[Record], model)


def typed_dataset(cls: type[Record], *, pack: str = "civic") -> DatasetType:
    """Describe a hand-written record model as a typed dataset (for the catalog and analytics)."""
    fields = {
        name: _field_from_annotation(info.annotation, required=info.is_required())
        for name, info in cls.model_fields.items()
        if name not in ("external_id", "canonical_url")
    }
    return DatasetType(
        name=cls.record_type,
        version=cls.schema_version,
        description=(cls.__doc__ or cls.__name__).strip().splitlines()[0],
        pack=pack,
        storage="typed",
        fields=fields,
    )


def _field_from_annotation(annotation: Any, *, required: bool) -> FieldDef:
    args = [a for a in get_args(annotation) if a is not type(None)]
    is_optional = get_origin(annotation) in (Union, types.UnionType)
    inner = args[0] if is_optional and len(args) == 1 else annotation
    if get_origin(inner) is Literal:
        return FieldDef(type="enum", values=tuple(get_args(inner)), required=required)
    for type_name, py in _PYTHON_TYPES.items():
        if inner is py and type_name not in ("url", "money"):
            return FieldDef(type=type_name, required=required)
    raise DatasetError(f"cannot describe annotation {annotation!r} as a dataset field")


def _typed_class_name(name: str) -> str:
    for class_name, cls in SCHEMAS.items():
        if cls.record_type == name:
            return class_name
    raise DatasetError(f"no typed model for dataset {name!r}")


def _is_name(value: str) -> bool:
    return re.fullmatch(NAME_PATTERN, value) is not None


def load_dataset_file(path: Path) -> DatasetType:
    try:
        return DatasetType.model_validate(yaml.safe_load(path.read_text()))
    except Exception as exc:
        raise DatasetError(f"{path.name}: {exc}") from exc


class DatasetRegistry:
    """All known dataset types, by (name, version). Lookups accept typed class names too."""

    def __init__(self, datasets: Iterable[DatasetType]) -> None:
        self._by_key: dict[tuple[str, int], DatasetType] = {}
        for ds in datasets:
            key = (ds.name, ds.version)
            existing = self._by_key.get(key)
            if existing is not None and existing.definition_hash != ds.definition_hash:
                raise DatasetError(f"dataset {ds.name} v{ds.version} is defined twice, differently")
            self._by_key[key] = ds
        self._aliases = {cls_name.lower(): cls.record_type for cls_name, cls in SCHEMAS.items()}

    @classmethod
    def load(cls, extra_dirs: Iterable[Path] = ()) -> DatasetRegistry:
        builtin = [typed_dataset(model) for model in SCHEMAS.values()]
        files = sorted(PACKS_DIR.glob("*.yaml"))
        for directory in extra_dirs:
            if directory.is_dir():
                files.extend(sorted(directory.glob("*.yaml")))
        return cls([*builtin, *(load_dataset_file(p) for p in files)])

    def get(self, name: str, version: int | None = None) -> DatasetType:
        key = self._aliases.get(name.lower(), name.lower())
        if version is not None:
            found = self._by_key.get((key, version))
            if found is None:
                raise DatasetError(f"unknown dataset {name!r} v{version}")
            return found
        versions = [ds for (n, _), ds in self._by_key.items() if n == key]
        if not versions:
            raise DatasetError(f"unknown dataset {name!r}; known: {self.names()}")
        return max(versions, key=lambda ds: ds.version)

    def names(self) -> list[str]:
        return sorted({name for name, _ in self._by_key})

    def __iter__(self) -> Iterator[DatasetType]:
        return iter(sorted(self._by_key.values(), key=lambda ds: (ds.name, ds.version)))

    def canonical_name(self, name: str) -> str:
        """'Meeting', 'meeting' -> 'meeting'; raises DatasetError for unknown names."""
        return self.get(name).name


@cache
def default_registry(extra_dirs: tuple[Path, ...] | None = None) -> DatasetRegistry:
    """Built-in packs, typed models and user types (settings.datasets_dir by default).

    Inside the offline sandbox settings are unavailable; only built-ins load there and the
    tester ships user dataset definitions explicitly.
    """
    if extra_dirs is None:
        try:
            from scrapeline.settings import get_settings
        except ImportError:
            extra_dirs = ()
        else:
            extra_dirs = (get_settings().datasets_dir,)
    return DatasetRegistry.load(extra_dirs)


def resolve_model(name: str, version: int | None = None) -> type[Record]:
    """Record model for a dataset or typed schema name ('product', 'Meeting', 'meeting')."""
    return default_registry().get(name, version).record_model()
