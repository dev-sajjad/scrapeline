from typer.testing import CliRunner

from scrapeline import __version__
from scrapeline.cli import app
from scrapeline.errors import BlockedError, FetchError, URLPolicyError
from scrapeline.settings import Settings


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "version" in result.stdout


def test_settings_prod_disables_private_hosts(monkeypatch) -> None:
    monkeypatch.setenv("SCRAPELINE_ENV", "prod")
    assert Settings(_env_file=None).private_hosts == frozenset()


def test_settings_dev_allows_fixture_hosts(monkeypatch) -> None:
    monkeypatch.delenv("SCRAPELINE_ENV", raising=False)
    assert "fixtures" in Settings(_env_file=None).private_hosts


def test_error_hierarchy() -> None:
    assert issubclass(URLPolicyError, FetchError)
    err = BlockedError("https://x.test", "challenge", 403)
    assert err.kind == "challenge" and err.status == 403
