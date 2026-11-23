"""Runtime settings, loaded from environment and `.env` (never committed)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    scrapeline_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://scrapeline:scrapeline@localhost:5432/scrapeline"

    # Storage and config locations
    raw_store_dir: Path = PROJECT_ROOT / "data" / "raw"
    sources_dir: Path = PROJECT_ROOT / "config" / "sources"
    datasets_dir: Path = PROJECT_ROOT / "config" / "datasets"
    lake_dir: Path = PROJECT_ROOT / "data" / "lake"

    # Crawler identity: public contact only.
    crawler_user_agent: str = "scrapeline/0.1 (+https://github.com/dev-sajjad/scrapeline)"
    fixture_base_url: str = "http://localhost:8080"

    # Hosts that may resolve to private/loopback addresses. Only fixture hosts, never in prod.
    urlpolicy_private_hosts: list[str] = Field(default_factory=lambda: ["localhost", "fixtures"])

    http_timeout_s: float = 30.0
    http_max_retries: int = 4
    http_max_redirects: int = 5
    circuit_failure_threshold: int = 5
    circuit_reset_s: float = 300.0

    # LLM (agents only; scraping never depends on it)
    anthropic_api_key: SecretStr | None = None
    model_generate: str = "claude-opus-5-5"
    model_repair: str = "claude-opus-5-5"
    model_cheap: str = "claude-haiku-5-5"
    agent_max_usd_per_run: float = 2.0

    # Sandbox for agent-generated parsers.
    sandbox_image: str = "scrapeline-sandbox:latest"
    sandbox_timeout_s: int = 60

    @property
    def private_hosts(self) -> frozenset[str]:
        if self.scrapeline_env == "prod":
            return frozenset()
        return frozenset(h.lower() for h in self.urlpolicy_private_hosts)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
