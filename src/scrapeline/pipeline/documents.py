"""Document download + text extraction.

Download goes through FetchClient (URL policy, robots, rate limit); bytes land in the raw store.
Text: PyMuPDF text layer first; pages with almost no text flag `needs_ocr`; OCR runs only when
Tesseract is installed (it is in the docker image), otherwise the flag stays for a later pass.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

import pymupdf
from sqlalchemy import Engine, select, update

from scrapeline.errors import FetchError, ParseError, ScrapelineError
from scrapeline.fetch.client import FetchClient
from scrapeline.ops.logging import get_logger
from scrapeline.store import tables as t
from scrapeline.store.raw import RawStore

log = get_logger(__name__)

MIN_CHARS_PER_PAGE = 40
MAX_TEXT_CHARS = 500_000


@dataclass(frozen=True)
class PdfText:
    page_count: int
    text: str
    needs_ocr: bool
    ocr_used: bool


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def extract_pdf(content: bytes, *, allow_ocr: bool | None = None) -> PdfText:
    """Pure: PDF bytes -> text. Raises ParseError for non-PDF / encrypted input."""
    if not content.startswith(b"%PDF"):
        raise ParseError("not a PDF (missing %PDF header)")
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ParseError(f"unreadable PDF: {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise ParseError("encrypted PDF")
        pages = [doc[i].get_text("text") for i in range(doc.page_count)]
        sparse = [i for i, txt in enumerate(pages) if len(txt.strip()) < MIN_CHARS_PER_PAGE]
        ocr_used = False
        use_ocr = tesseract_available() if allow_ocr is None else allow_ocr
        if sparse and use_ocr:
            for i in sparse:
                try:
                    tp = doc[i].get_textpage_ocr(language="eng", dpi=200, full=True)
                    pages[i] = doc[i].get_text("text", textpage=tp)
                    ocr_used = True
                except Exception as exc:  # OCR is best effort; keep the flag
                    log.warning("documents.ocr_failed", page=i, error=repr(exc))
            sparse = [i for i, txt in enumerate(pages) if len(txt.strip()) < MIN_CHARS_PER_PAGE]
        text = "\n\f".join(p.strip() for p in pages)[:MAX_TEXT_CHARS]
        return PdfText(len(pages), text, needs_ocr=bool(sparse), ocr_used=ocr_used)


def download_documents(
    engine: Engine,
    client: FetchClient,
    raw_store: RawStore,
    *,
    source_id: str,
    limit: int = 20,
) -> dict[str, int]:
    """Fetch + extract documents not yet downloaded for one source. Idempotent."""
    stats = {"downloaded": 0, "failed": 0, "needs_ocr": 0}
    with engine.connect() as conn:
        rows = conn.execute(
            select(t.documents.c.id, t.documents.c.url)
            .where(t.documents.c.source_id == source_id, t.documents.c.doc_sha256.is_(None))
            .order_by(t.documents.c.id)
            .limit(limit)
        ).fetchall()
    for row in rows:
        try:
            resp = client.get(row.url, expect_content=False)
            ref = raw_store.put(source_id, resp.content, "pdf", resp.fetched_at)
            extracted = extract_pdf(resp.content)
        except (FetchError, ParseError) as exc:
            stats["failed"] += 1
            log.warning("documents.failed", url=row.url, error=str(exc))
            continue
        except ScrapelineError:
            raise
        with engine.begin() as conn:
            conn.execute(
                update(t.documents)
                .where(t.documents.c.id == row.id)
                .values(
                    doc_sha256=ref.sha256,
                    page_count=extracted.page_count,
                    text_chars=len(extracted.text),
                    needs_ocr=int(extracted.needs_ocr),
                    text=extracted.text,
                )
            )
        stats["downloaded"] += 1
        stats["needs_ocr"] += int(extracted.needs_ocr)
    return stats
