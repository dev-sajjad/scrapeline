"""Source registry: YAML configs + status machine (draft -> active -> broken -> retired)."""

from scrapeline.registry.config import (
    TRANSITIONS,
    SourceConfig,
    check_transition,
    load_source_file,
    load_sources,
)

__all__ = ["TRANSITIONS", "SourceConfig", "check_transition", "load_source_file", "load_sources"]
