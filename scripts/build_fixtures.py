"""Generate deterministic fixture websites + golden rows. Re-run after editing MEETINGS.

    uv run python scripts/build_fixtures.py

Outputs (all committed, human-reviewed; golden files are human-owned):
  tests/sites/static-list/   3 pages, next-link pagination, 25 meetings, messy date formats
  tests/sites/js-table/      table rendered by JS from inline JSON + "Load more" button
  tests/sites/pdf-archive/   index + 5 PDFs (one image-only "scanned" PDF -> OCR flag)
  tests/sites/blocked/       challenge-style shell (403/429 come from nginx config)
  tests/fixtures/golden/*.json
"""

from __future__ import annotations

import html
import json
from datetime import date, time
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
SITES = ROOT / "tests" / "sites"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"

BODIES = ["City Council", "Planning Commission", "Parks Board", "Finance Committee", "Zoning Board"]
LOCATIONS = ["Council Chamber, City Hall", "Room 204, Civic Center", None, "Public Library Annex"]
STATUSES = {7: "cancelled", 19: "cancelled"}


def meetings() -> list[dict[str, object]]:
    rows = []
    for i in range(25):
        d = date.fromordinal(date(2026, 1, 6).toordinal() + i * 9)
        rows.append(
            {
                "id": f"M-{1001 + i}",
                "body": BODIES[i % len(BODIES)],
                "date": d,
                "time": [time(18, 0), time(9, 30), None, time(14, 0)][i % 4],
                "location": LOCATIONS[i % len(LOCATIONS)],
                "status": STATUSES.get(i, "scheduled"),
                "agenda": f"/pdf-archive/files/agenda-{1001 + i}.pdf" if i % 5 == 0 else None,
            }
        )
    return rows


def fmt_date(d: date, i: int) -> str:
    """Deliberately inconsistent formats, as real municipal sites have."""
    return [
        d.isoformat(),
        d.strftime("%m/%d/%Y"),
        d.strftime("%a, %b ")
        + f"{d.day}{'th' if 10 <= d.day % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d.day % 10, 'th')} {d.year}",
        d.strftime("%d %B %Y"),
        d.strftime("%A, %B %d, %Y"),
    ][i % 5]


def fmt_time(t: time | None) -> str:
    if t is None:
        return "TBD"
    return t.strftime("%I:%M %p").lstrip("0")


def page(title: str, body: str, extra_head: str = "") -> str:
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>{extra_head}</head>\n<body>\n"
        "<header><h1>City of Fixtureville</h1><nav><a href='/static-list/'>Meetings</a></nav></header>\n"
        f"<main>\n{body}\n</main>\n<footer>Fixture site for scrapeline tests.</footer>\n</body></html>\n"
    )


def static_row(m: dict[str, object], i: int) -> str:
    agenda = (
        f"<a class='agenda' href='{m['agenda']}'>Agenda (PDF)</a>"
        if m["agenda"]
        else "<span>-</span>"
    )
    status = " <em class='status'>CANCELLED</em>" if m["status"] == "cancelled" else ""
    loc = html.escape(str(m["location"])) if m["location"] else ""
    return (
        f"<tr data-id='{m['id']}'>"
        f"<td class='title'>{html.escape(str(m['body']))} Meeting{status}</td>"
        f"<td class='body'>{html.escape(str(m['body']))}</td>"
        f"<td class='date'>{fmt_date(m['date'], i)}</td>"  # type: ignore[arg-type]
        f"<td class='time'>{fmt_time(m['time'])}</td>"  # type: ignore[arg-type]
        f"<td class='location'>{loc}</td>"
        f"<td class='docs'>{agenda}</td></tr>"
    )


def build_static(rows: list[dict[str, object]]) -> None:
    out = SITES / "static-list"
    out.mkdir(parents=True, exist_ok=True)
    chunks = [rows[0:10], rows[10:20], rows[20:25]]
    names = ["index.html", "page2.html", "page3.html"]
    for n, chunk in enumerate(chunks):
        trs = "\n".join(static_row(m, 10 * n + j) for j, m in enumerate(chunk))
        nxt = f"<a rel='next' class='next' href='{names[n + 1]}'>Next page</a>" if n < 2 else ""
        prev = f"<a rel='prev' href='{names[n - 1]}'>Previous</a>" if n > 0 else ""
        body = (
            f"<h2>Public Meetings</h2>\n<table class='meetings'><thead><tr><th>Meeting</th><th>Body</th>"
            f"<th>Date</th><th>Time</th><th>Location</th><th>Documents</th></tr></thead>\n"
            f"<tbody>\n{trs}\n</tbody></table>\n<nav class='pager'>{prev} {nxt}</nav>"
        )
        (out / names[n]).write_text(page(f"Meetings - page {n + 1}", body))


def build_js(rows: list[dict[str, object]]) -> None:
    out = SITES / "js-table"
    out.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "id": m["id"],
            "body": m["body"],
            "date": fmt_date(m["date"], i),  # type: ignore[arg-type]
            "time": fmt_time(m["time"]),  # type: ignore[arg-type]
            "location": m["location"] or "",
            "cancelled": m["status"] == "cancelled",
            "agenda": m["agenda"],
        }
        for i, m in enumerate(rows)
    ]
    script = """
<script>
(function () {
  var data = JSON.parse(document.getElementById('meeting-data').textContent);
  var shown = 0, PAGE = 10;
  var tbody = document.querySelector('#meetings tbody');
  var btn = document.getElementById('load-more');
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
  function render() {
    data.slice(shown, shown + PAGE).forEach(function (m) {
      var tr = document.createElement('tr');
      tr.className = 'meeting';
      tr.setAttribute('data-meeting-id', m.id);
      tr.innerHTML = '<td class="m-title">' + esc(m.body) + ' Meeting' +
        (m.cancelled ? ' <em class="status">CANCELLED</em>' : '') + '</td>' +
        '<td class="m-body">' + esc(m.body) + '</td>' +
        '<td class="m-date">' + esc(m.date) + '</td><td class="m-time">' + esc(m.time) + '</td>' +
        '<td class="m-loc">' + esc(m.location) + '</td>' +
        '<td>' + (m.agenda ? '<a class="m-agenda" href="' + m.agenda + '">Agenda</a>' : '') + '</td>';
      tbody.appendChild(tr);
    });
    shown += PAGE;
    if (shown >= data.length) { btn.remove(); }
  }
  btn.addEventListener('click', function () { setTimeout(render, 150); });
  setTimeout(render, 100);
})();
</script>"""
    body = (
        "<h2>Meetings (interactive)</h2>\n<table id='meetings'><thead><tr><th>Meeting</th><th>Body</th>"
        "<th>Date</th><th>Time</th><th>Location</th><th>Agenda</th></tr></thead><tbody></tbody></table>\n"
        "<button id='load-more' type='button'>Load more</button>\n"
        f"<script id='meeting-data' type='application/json'>{json.dumps(data)}</script>{script}"
    )
    (out / "index.html").write_text(page("Meetings (JS)", body))


AGENDA_ITEMS = [
    "Call to order and roll call",
    "Approval of minutes from the previous meeting",
    "Public comment",
    "Resolution 2026-{n}: Budget amendment for parks maintenance",
    "Ordinance {n}-A: Zoning text amendment (second reading)",
    "Adjournment",
]


def build_pdfs(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    out = SITES / "pdf-archive"
    files = out / "files"
    files.mkdir(parents=True, exist_ok=True)
    docs = []
    with_agenda = [m for m in rows if m["agenda"]]
    for k, m in enumerate(with_agenda):
        name = Path(str(m["agenda"])).name
        lines = [
            f"CITY OF FIXTUREVILLE - {str(m['body']).upper()}",
            f"AGENDA - {m['date']:%B %d, %Y}",  # type: ignore[str-format]
            "",
            *[f"{i + 1}. {item.format(n=1001 + k)}" for i, item in enumerate(AGENDA_ITEMS)],
        ]
        text_doc = pymupdf.open()
        pg = text_doc.new_page()
        pg.insert_text((54, 72), "\n".join(lines), fontsize=11)
        scanned = k == len(with_agenda) - 1
        if scanned:
            # Image-only PDF: render the page to pixels, embed the picture. No text layer.
            pix = pg.get_pixmap(dpi=150)
            doc = pymupdf.open()
            img_page = doc.new_page(width=pg.rect.width, height=pg.rect.height)
            img_page.insert_image(img_page.rect, pixmap=pix)
        else:
            doc = text_doc
        doc.set_metadata(
            {
                "title": f"Agenda {m['id']}",
                "creationDate": "D:20260101000000Z",
                "modDate": "D:20260101000000Z",
                "producer": "scrapeline fixtures",
            }
        )
        doc.save(files / name, garbage=4, deflate=True, no_new_id=True)
        docs.append(
            {
                "meeting_id": m["id"],
                "file": name,
                "title": f"{m['body']} agenda",
                "date": m["date"],
                "scanned": scanned,
                "items": len(AGENDA_ITEMS),
            }
        )
    lis = "\n".join(
        f"<li class='doc'><span class='doc-date'>{d['date']:%m/%d/%Y}</span> "  # type: ignore[str-format]
        f"<a class='doc-link' href='files/{d['file']}'>{html.escape(str(d['title']))}</a></li>"
        for d in docs
    )
    (out / "index.html").write_text(
        page("Document archive", f"<h2>Agenda archive</h2>\n<ul class='documents'>\n{lis}\n</ul>")
    )
    return docs


def build_blocked() -> None:
    out = SITES / "blocked"
    out.mkdir(parents=True, exist_ok=True)
    (out / "challenge.html").write_text(
        "<!doctype html><html><head><title>Just a moment...</title></head><body>"
        "<div data-scrapeline-challenge='1'>Checking your browser. Verify you are human.</div>"
        "<script>/* fixture challenge shell: detection test only */</script></body></html>\n"
    )
    (out / "empty-shell.html").write_text(
        "<!doctype html><html><head><title>App</title></head><body><div id='root'></div>"
        "<script src='/blocked/app.js'></script></body></html>\n"
    )


def golden_meetings(rows: list[dict[str, object]], base: str) -> list[dict[str, object]]:
    return [
        {
            "external_id": m["id"],
            "title": f"{m['body']} Meeting",
            "body_name": m["body"],
            "meeting_date": m["date"].isoformat(),  # type: ignore[union-attr]
            "start_time": m["time"].isoformat() if m["time"] else None,  # type: ignore[union-attr]
            "location": m["location"],
            "status": m["status"],
            "agenda_url": f"{base}{m['agenda']}" if m["agenda"] else None,
        }
        for m in rows
    ]


def main() -> None:
    rows = meetings()
    build_static(rows)
    build_js(rows)
    docs = build_pdfs(rows)
    build_blocked()
    GOLDEN.mkdir(parents=True, exist_ok=True)
    base = "{FIXTURE_BASE_URL}"
    for site in ("static-list", "js-table"):
        golden = {"site": site, "schema": "Meeting", "records": golden_meetings(rows, base)}
        (GOLDEN / f"{site}.json").write_text(json.dumps(golden, indent=1) + "\n")
    (GOLDEN / "pdf-archive.json").write_text(
        json.dumps(
            {
                "site": "pdf-archive",
                "documents": [
                    {
                        "file": d["file"],
                        "meeting_id": d["meeting_id"],
                        "page_count": 1,
                        "needs_ocr": d["scanned"],
                        "contains": [] if d["scanned"] else ["Public comment", "Adjournment"],
                    }
                    for d in docs
                ],
            },
            indent=1,
        )
        + "\n"
    )
    print(f"fixtures: {len(rows)} meetings, {len(docs)} pdfs")


if __name__ == "__main__":
    main()
