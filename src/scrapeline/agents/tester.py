"""Deterministic tester (no LLM). The only judge of agent output (CLAUDE.md §0.11, §5.2).

Stages: (1) static: spec schema validation / AST security check for python;
(2) sandbox: parse sample pages offline (`docker run --network none ...`);
(3) golden: compare records with human-owned expected rows. Output: machine-readable report.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from scrapeline.adapters.declarative import SiteSpec
from scrapeline.agents import sandbox_harness
from scrapeline.errors import ConfigError
from scrapeline.pipeline.models import SCHEMAS
from scrapeline.quality.golden import compare, load_golden
from scrapeline.settings import PROJECT_ROOT, Settings

SandboxMode = Literal["docker", "local"]
PAGE_URL_PREFIX = "/static-list/"  # drift cases keep the original URL layout

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


def _spec_security(spec_yaml: str) -> list[str]:
    findings = []
    if len(spec_yaml) > 20_000:
        findings.append("spec too large (>20k chars)")
    try:
        spec = SiteSpec.from_yaml(spec_yaml)
    except ConfigError as exc:
        return [str(exc)[:500]]
    excluded = {"attendees", "contact_email", "phone", "signatures"}
    leaked = excluded & set(spec.fields)
    if leaked:
        findings.append(f"excluded personal-data fields requested: {sorted(leaked)}")
    return findings


def _prepare_workdir(spec_yaml: str, pages_dir: Path, base_url: str) -> Path:
    scratch = PROJECT_ROOT / "data" / "sandbox"
    scratch.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="case-", dir=scratch))
    files = sorted(pages_dir.glob("*.html"), key=lambda p: (p.name != "index.html", p.name))
    pages = []
    for f in files:
        shutil.copy2(f, work / f.name)
        url = f"{base_url}{PAGE_URL_PREFIX}{'' if f.name == 'index.html' else f.name}"
        pages.append({"file": f.name, "url": url})
    (work / "input.json").write_text(
        json.dumps({"spec_yaml": spec_yaml, "base_url": base_url, "pages": pages, "max_pages": 10})
    )
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
) -> TesterReport:
    """Golden mode when `golden_path` is given; otherwise self-consistency mode for new sites
    (records on every parsed page, all sample pages reached, no rejects/errors, unique ids,
    canonical ids not synthesized for most rows). Self-consistency is weaker evidence than
    golden rows; a human still checks sample rows in the PR."""
    started = time.monotonic()
    report = TesterReport(passed=False, stage="static", sandbox_mode=mode)
    report.security = _spec_security(spec_yaml)
    if report.security:
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report
    work = _prepare_workdir(spec_yaml, pages_dir, base_url)
    try:
        report.stage = "sandbox"
        out = _run_sandbox(work, settings, mode)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report.errors = out.get("errors", [])
    report.pages_parsed = int(out.get("pages_parsed", 0))
    report.rejects = len(out.get("rejects", []))
    spec = SiteSpec.from_yaml(spec_yaml)
    model = SCHEMAS[spec.schema_]
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
