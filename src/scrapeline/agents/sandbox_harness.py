"""Entry point inside the offline sandbox container (ADR-0004). No network, read-only FS.

Contract: reads /work/input.json (+ page files next to it), parses offline, writes ONE JSON
report to stdout. Imports only pure modules: no fetch/, store/, settings, DB or network code.

input.json: {"spec_yaml": str, "base_url": str, "pages": [{"file": str, "url": str}], "max_pages": int}
Pagination is followed offline: a next-page URL is mapped to the page file with the same name.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def run(work: Path) -> dict[str, Any]:
    from scrapeline.adapters.declarative import SiteSpec, parse_html
    from scrapeline.adapters.rawdoc import RawDoc
    from scrapeline.errors import ScrapelineError

    started = time.monotonic()
    spec_input = json.loads((work / "input.json").read_text())
    report: dict[str, Any] = {"records": [], "rejects": [], "errors": [], "pages_parsed": 0}
    try:
        spec = SiteSpec.from_yaml(spec_input["spec_yaml"])
    except ScrapelineError as exc:
        report["errors"].append({"stage": "spec", "error": str(exc)[:2000]})
        return report
    by_name = {Path(p["url"]).name or "index.html": p for p in spec_input["pages"]}
    page = spec_input["pages"][0]
    seen: set[str] = set()
    while page is not None and report["pages_parsed"] < int(spec_input.get("max_pages", 10)):
        if page["file"] in seen:
            break
        seen.add(page["file"])
        content = (work / page["file"]).read_bytes()
        raw = RawDoc(
            source_id="sandbox",
            url=page["url"],
            final_url=page["url"],
            status=200,
            content_type="text/html",
            content=content,
            sha256=RawDoc.hash_bytes(content),
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            meta={"page": report["pages_parsed"] + 1},
        )
        try:
            result = parse_html(spec, raw)
        except ScrapelineError as exc:
            report["errors"].append(
                {"stage": "parse", "page": page["file"], "error": str(exc)[:2000]}
            )
            break
        except Exception as exc:  # untrusted spec may trigger anything; report, never crash
            report["errors"].append(
                {
                    "stage": "parse",
                    "page": page["file"],
                    "error": f"{type(exc).__name__}: {exc}"[:2000],
                    "trace": traceback.format_exc()[-1500:],
                }
            )
            break
        report["pages_parsed"] += 1
        for rec in result.records:
            report["records"].append(
                {
                    "record_type": rec.record_type,
                    "external_id": rec.external_id,
                    **rec.model_dump(mode="json"),
                }
            )
        report["rejects"].extend(r.model_dump() for r in result.rejects)
        page = None
        for target in result.next_targets:
            name = Path(target.url.split("?")[0]).name or "index.html"
            page = by_name.get(name)
            if page is None:
                report["errors"].append(
                    {"stage": "pagination", "error": f"next page {target.url} not in sample"}
                )
    report["duration_ms"] = int((time.monotonic() - started) * 1000)
    return report


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/work")
    sys.stdout.write(json.dumps(run(work)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
