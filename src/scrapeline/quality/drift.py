"""Drift diagnosis: which selectors stopped matching, and how the page structure changed.

Deterministic. Feeds incidents and the repair agent's first message (so the agent starts from
evidence, not guesses). Signals: record selector matches, per-field matches inside records,
next-page link presence, and a structure signature (tag.class vocabulary) diff.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from scrapeline.adapters.declarative import SiteSpec


def class_vocabulary(html: str) -> Counter[str]:
    tree = LexborHTMLParser(html)
    vocab: Counter[str] = Counter()
    for node in tree.css("*"):
        classes = (node.attributes.get("class") or "").split()
        if classes:
            for cls in classes:
                vocab[f"{node.tag or '?'}.{cls}"] += 1
        else:
            vocab[node.tag or "?"] += 1
    return vocab


def structure_signature(html: str) -> str:
    return hashlib.sha256("|".join(sorted(class_vocabulary(html))).encode()).hexdigest()[:16]


def selector_report(spec: SiteSpec, html: str) -> dict[str, Any]:
    tree = LexborHTMLParser(html)
    records = tree.css(spec.record)
    sample = records[:5]
    fields: dict[str, dict[str, Any]] = {}
    for name, f in spec.fields.items():
        if f.css is None:
            hits = sum(1 for r in sample if f.attr is None or r.attributes.get(f.attr) is not None)
        else:
            hits = sum(1 for r in sample if r.css_first(f.css) is not None)
        fields[name] = {
            "selector": f.css or "<record>",
            "attr": f.attr,
            "optional": f.optional,
            "matched_in_sample": f"{hits}/{len(sample)}",
        }
    report: dict[str, Any] = {
        "record_selector": spec.record,
        "record_matches": len(records),
        "fields": fields,
    }
    if spec.next_page is not None:
        report["next_page"] = {
            "selector": spec.next_page.css,
            "matches": len(tree.css(spec.next_page.css)),
        }
    broken = [
        n
        for n, v in fields.items()
        if not v["optional"] and not v["matched_in_sample"].startswith(str(len(sample)))
    ]
    report["suspect_fields"] = broken if records else ["<record selector>"]
    return report


def diagnose(spec: SiteSpec, new_html: str, old_html: str | None = None) -> dict[str, Any]:
    report = selector_report(spec, new_html)
    report["signature_new"] = structure_signature(new_html)
    if old_html is not None:
        old_v, new_v = class_vocabulary(old_html), class_vocabulary(new_html)
        report["signature_old"] = structure_signature(old_html)
        report["structure_changed"] = report["signature_old"] != report["signature_new"]
        report["classes_removed"] = sorted(set(old_v) - set(new_v))[:20]
        report["classes_added"] = sorted(set(new_v) - set(old_v))[:20]
    return report
