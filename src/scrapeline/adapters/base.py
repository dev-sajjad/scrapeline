"""Adapter contract. Adapters interpret pages; the runtime does all I/O."""

from __future__ import annotations

import hashlib
import importlib
import inspect
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from scrapeline.adapters.rawdoc import ParseResult, RawDoc, Target
from scrapeline.errors import ConfigError
from scrapeline.fetch.client import FetchClient, FetchResponse
from scrapeline.registry.config import SourceConfig


@dataclass(frozen=True)
class Window:
    """What a run should cover. Incremental runs set `since` from the watermark."""

    since: datetime | None = None
    until: datetime | None = None
    backfill: bool = False


@runtime_checkable
class Adapter(Protocol):
    source: SourceConfig

    def discover(self, window: Window) -> Iterator[Target]: ...

    def fetch(self, target: Target, client: FetchClient) -> FetchResponse: ...

    def parse(self, raw: RawDoc) -> ParseResult: ...


class BaseAdapter:
    """Default `fetch` through FetchClient. Subclasses implement `discover` and `parse`."""

    def __init__(self, source: SourceConfig) -> None:
        self.source = source

    def discover(self, window: Window) -> Iterator[Target]:
        yield Target(url=self.source.entry, kind="page")

    def fetch(self, target: Target, client: FetchClient) -> FetchResponse:
        if target.render:
            raise ConfigError("render targets are fetched by the runtime BrowserPool")
        return client.get(target.url)

    def parse(self, raw: RawDoc) -> ParseResult:  # pragma: no cover - abstract
        raise NotImplementedError


def load_adapter(source: SourceConfig) -> Adapter:
    """Import `package.module:ClassName` and construct it with the source config."""
    module_name, _, class_name = source.adapter.partition(":")
    if not module_name.startswith("scrapeline.adapters.") or not class_name:
        raise ConfigError(f"adapter must be 'scrapeline.adapters.<...>:<Class>': {source.adapter}")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ConfigError(f"adapter class {class_name} not found in {module_name}")
    adapter = cls(source)
    if not isinstance(adapter, Adapter):
        raise ConfigError(f"{source.adapter} does not implement the Adapter protocol")
    return adapter


def adapter_code_hash(adapter: Adapter) -> str:
    """Hash of the adapter's source files (module + spec.yaml if any) -> scraper_versions."""
    module_file = Path(inspect.getfile(type(adapter)))
    digest = hashlib.sha256(module_file.read_bytes())
    for extra in sorted(module_file.parent.glob("*.yaml")):
        digest.update(extra.read_bytes())
    spec_path = getattr(adapter, "spec_path", None)
    if isinstance(spec_path, Path) and spec_path.exists():
        digest.update(spec_path.read_bytes())
    return digest.hexdigest()
