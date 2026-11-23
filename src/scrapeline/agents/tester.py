"""Deterministic tester (no LLM). The only judge of agent output.

Stages: (1) static: spec schema validation, field pattern safety, AST security check for
python; (2) sandbox: parse sample pages offline (`docker run --network none ...`);
(3) golden: compare records with human-owned expected rows. Output: machine-readable report.

The sandbox reads the pages under the source's locale and receives the dataset definition,
so user-defined dataset types work there too. Benchmark pages (drift and generation cases)
are copies of the static-list fixture and use that source's locale.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from scrapeline.adapters.declarative import SiteSpec, resolve_dataset
from scrapeline.agents import sandbox_harness
from scrapeline.catalog.datasets import DatasetType
from scrapeline.errors import ConfigError
from scrapeline.pipeline.locale import Locale
from scrapeline.quality.golden import compare, load_golden
from scrapeline.registry.config import SourceConfig, load_source_file
from scrapeline.settings import PROJECT_ROOT, Settings

SandboxMode = Literal["docker", "local"]
PAGE_URL_PREFIX = "/static-list/"  # drift cases keep the original URL layout
CASE_SOURCE_ID = "fixture-static-list"  # benchmark pages are copies of this fixture site
EXCLUDED_FIELDS = frozenset({"attendees", "contact_email", "phone", "signatures"})
# Field patterns run on untrusted page text in the trusted worker, where no time limit applies.
MAX_PATTERN_CHARS = 200
_QUANTIFIER = re.compile(r"(?:[*+?]|\{\d*(?:,\d*)?\})[?+]?")

ALLOWED_IMPORTS = frozenset(
    {
        "re",
        "datetime",
        "json",
        "typing",
        "dataclasses",
        "collections",
        "itertools",
        "decimal",
        "selectolax",
        "selectolax.lexbor",
        "bs4",
        "dateutil",
        "dateutil.parser",
        "scrapeline.pipeline.models",
        "scrapeline.pipeline.normalise",
        "scrapeline.pipeline.identity",
        "scrapeline.adapters.rawdoc",
        "scrapeline.errors",
        "__future__",
    }
)
FORBIDDEN_CALLS = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "input",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "setattr",
        "delattr",
        "memoryview",
    }
)


@dataclass
class TesterReport:
    passed: bool
    stage: str  # static | sandbox | golden | done
    errors: list[dict[str, Any]] = field(default_factory=list)
    security: list[str] = field(default_factory=list)
    golden: dict[str, Any] = field(default_factory=dict)
    records: int = 0
    rejects: int = 0
    pages_parsed: int = 0
    sandbox_mode: str = "docker"
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        """Compact form for the agent (bounded size)."""
        g = self.golden
        return {
            "passed": self.passed,
            "stage": self.stage,
            "errors": self.errors[:3],
            "security": self.security[:5],
            "records": self.records,
            "rejects": self.rejects,
            "pages_parsed": self.pages_parsed,
            "golden": {
                k: g.get(k)
                for k in (
                    "mode",
                    "checks",
                    "expected",
                    "actual",
                    "matched",
                    "field_accuracy",
                    "sample_records",
                )
            }
            | {
                "missing": (g.get("missing") or [])[:5],
                "unexpected": (g.get("unexpected") or [])[:5],
                "field_diffs": (g.get("field_diffs") or [])[:8],
            },
        }


def security_check_python(code: str) -> list[str]:
    """AST check for generated parse.py: allowlisted imports, no I/O, no dynamic execution."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    findings.append(f"import not allowed: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod not in ALLOWED_IMPORTS:
                findings.append(f"import not allowed: from {mod}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                findings.append(f"call not allowed: {node.func.id}()")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr != "__init__"
        ):
            findings.append(f"dunder attribute access not allowed: {node.attr}")
    return findings


def _repeats(quantifier: str) -> bool:
    """True for a quantifier that allows more than one repetition ('+', '*', '{2,}')."""
    if quantifier[0] in "*+":
        return True
    if quantifier[0] == "?":
        return False
    low, comma, high = quantifier[1 : quantifier.index("}")].partition(",")
    if not comma:
        return int(low or 0) > 1
    return not high or int(high) > 1


def _class_end(pattern: str, start: int) -> int:
    """Index just after the character class that opens at `start`."""
    i = start + 1
    if pattern.startswith("^", i):
        i += 1
    if pattern.startswith("]", i):
        i += 1
    while i < len(pattern) and pattern[i] != "]":
        i += 2 if pattern[i] == "\\" else 1
    return i + 1


def has_nested_quantifier(pattern: str) -> bool:
    """A repeated group that contains a repeat: '(a+)+', '(.*)*', '(\\w+\\s?)+$'.

    These backtrack exponentially on text that almost matches. The scan is deliberately
    simple and also refuses some safe patterns such as '(?:\\d+,)*'.
    """
    open_groups: list[bool] = []  # per open group: whether it contains a repeat
    i = 0
    while i < len(pattern):
        char = pattern[i]
        group_repeats = False
        if char == "\\":
            i += 2
        elif char == "[":
            i = _class_end(pattern, i)
        elif char == "(":
            open_groups.append(False)
            i += 1
            continue
        elif char == ")":
            group_repeats = open_groups.pop() if open_groups else False
            if open_groups:
                open_groups[-1] = open_groups[-1] or group_repeats
            i += 1
        else:
            i += 1
        quantifier = _QUANTIFIER.match(pattern, i)
        if quantifier is None:
            continue
        if _repeats(quantifier.group()):
            if group_repeats:
                return True
            if open_groups:
                open_groups[-1] = True
        i = quantifier.end()
    return False


def pattern_problems(pattern: str) -> list[str]:
    """Why a field `pattern` is unsafe to run on untrusted text without a time limit."""
    problems = []
    if len(pattern) > MAX_PATTERN_CHARS:
        problems.append(f"pattern longer than {MAX_PATTERN_CHARS} characters")
    if has_nested_quantifier(pattern):
        problems.append(f"pattern {pattern[:80]!r} repeats a group that repeats (backtracking)")
    return problems


def case_source(settings: Settings, source_id: str | None) -> SourceConfig | None:
    """Config of the source under test; unknown or new sources fall back to the case source."""
    for candidate in (source_id, CASE_SOURCE_ID):
        path = settings.sources_dir / f"{candidate}.yaml"
        if candidate and path.exists():
            return load_source_file(path)
    return None


def _spec_security(
    spec_yaml: str, dataset: DatasetType | None, source: SourceConfig | None
) -> list[str]:
    findings = []
    if len(spec_yaml) > 20_000:
        findings.append("spec too large (>20k chars)")
    try:
        spec = SiteSpec.from_yaml(spec_yaml, dataset=dataset)
        dataset = resolve_dataset(spec.schema_, dataset)
    except ConfigError as exc:
        return [str(exc)[:500]]
    for name, field_spec in spec.fields.items():
        if field_spec.pattern is not None:
            findings += [f"{name}: {p}" for p in pattern_problems(field_spec.pattern)]
    leaked = EXCLUDED_FIELDS & set(spec.fields)
    if leaked:
        findings.append(f"excluded personal-data fields requested: {sorted(leaked)}")
    personal = set(dataset.pii_fields) & set(spec.fields)
    if personal and not (source is not None and source.usage.allow_pii):
        findings.append(
            f"personal-data fields {sorted(personal)} need usage.allow_pii on the source"
        )
    return findings


def _prepare_workdir(
    spec_yaml: str, pages_dir: Path, base_url: str, locale: Locale, dataset: DatasetType
) -> Path:
    scratch = PROJECT_ROOT / "data" / "sandbox"
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="case-", dir=scratch))
    files = sorted(pages_dir.glob("*.html"), key=lambda p: (p.name != "index.html", p.name))
    pages = []
    for f in files:
        shutil.copy2(f, work / f.name)
        url = f"{base_url}{PAGE_URL_PREFIX}{'' if f.name == 'index.html' else f.name}"
        pages.append({"file": f.name, "url": url})
    spec_input = {
        "spec_yaml": spec_yaml,
        "base_url": base_url,
        "pages": pages,
        "max_pages": 10,
        "locale": locale.model_dump(mode="json"),
        "dataset": dataset.model_dump(mode="json"),
    }
    (work / "input.json").write_text(json.dumps(spec_input))
    work.chmod(0o755)
    for p in work.iterdir():
        p.chmod(0o644)
    return work


def _run_sandbox(work: Path, settings: Settings, mode: SandboxMode) -> dict[str, Any]:
    if mode == "local":
        return sandbox_harness.run(work)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp",  # noqa: S108 - tmpfs mount point inside the sandbox container
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "1g",
        "--pids-limit",
        "128",
        "-v",
        f"{work}:/work:ro",
        settings.sandbox_image,
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=settings.sandbox_timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return {"records": [], "rejects": [], "errors": [{"stage": "sandbox", "error": "timeout"}]}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {
            "records": [],
            "rejects": [],
            "errors": [
                {"stage": "sandbox", "error": f"exit {proc.returncode}: {proc.stderr[-800:]}"}
            ],
        }
    result: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


def evaluate_spec(
    spec_yaml: str,
    pages_dir: Path,
    golden_path: Path | None,
    settings: Settings,
    *,
    mode: SandboxMode = "docker",
    base_url: str = "http://localhost:8080",
    min_records: int = 1,
    source_id: str | None = None,
    locale: Locale | None = None,
    dataset: DatasetType | None = None,
) -> TesterReport:
    """Golden mode when `golden_path` is given; otherwise self-consistency mode for new sites
    (records on every parsed page, all sample pages reached, no rejects/errors, unique ids,
    canonical ids not synthesized for most rows). Self-consistency is weaker evidence than
    golden rows; a human still checks sample rows in the PR.

    The locale is `locale`, else the config of `source_id`, else the case source's. The
    dataset is `dataset`, else the registry entry the spec names.
    """
    started = time.monotonic()
    report = TesterReport(passed=False, stage="static", sandbox_mode=mode)
    source = case_source(settings, source_id)
    report.security = _spec_security(spec_yaml, dataset, source)
    if report.security:
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report
    spec = SiteSpec.from_yaml(spec_yaml, dataset=dataset)
    dataset = resolve_dataset(spec.schema_, dataset)
    if locale is None:
        locale = source.locale if source is not None else Locale()
    work = _prepare_workdir(spec_yaml, pages_dir, base_url, locale, dataset)
    try:
        report.stage = "sandbox"
        out = _run_sandbox(work, settings, mode)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report.errors = out.get("errors", [])
    report.pages_parsed = int(out.get("pages_parsed", 0))
    report.rejects = len(out.get("rejects", []))
    model = dataset.record_model()
    records = []
    for row in out.get("records", []):
        if row.get("record_type") != model.record_type:
            continue
        data = {k: v for k, v in row.items() if k != "record_type"}
        try:
            records.append(model.model_validate(data))
        except Exception as exc:
            report.errors.append({"stage": "validate", "error": str(exc)[:300]})
    report.records = len(records)
    report.stage = "golden"
    if golden_path is not None:
        golden = compare(records, load_golden(golden_path, base_url))
        report.golden = golden.as_dict()
        report.passed = golden.passed and not report.errors and report.rejects == 0
    else:
        ids = [r.external_id for r in records]
        n_pages = len(list(pages_dir.glob("*.html")))
        synthesized = sum(1 for i in ids if i.startswith("nk:"))
        checks = {
            "records_at_least_min": len(records) >= min_records,
            "all_sample_pages_reached": report.pages_parsed == n_pages,
            "unique_external_ids": len(ids) == len(set(ids)),
            "few_synthesized_ids": synthesized <= len(ids) // 2,
            "no_rejects": report.rejects == 0,
            "no_errors": not report.errors,
        }
        sample = [r.model_dump(mode="json") | {"external_id": r.external_id} for r in records[:3]]
        report.golden = {
            "mode": "self_consistency",
            "checks": checks,
            "expected": None,
            "actual": len(records),
            "matched": None,
            "field_accuracy": None,
            "sample_records": sample,
        }
        report.passed = all(checks.values())
    report.stage = "done"
    report.duration_ms = int((time.monotonic() - started) * 1000)
    return report
