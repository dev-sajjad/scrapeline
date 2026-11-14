# Fixture sites

Served by nginx at http://localhost:8080 (`docker compose up`). Deterministic, versioned, owned by this project.

| Folder | Simulates |
|---|---|
| static-list/ | static HTML, next-link pagination, messy dates |
| js-table/ | JS-rendered table, "Load more" button |
| pdf-archive/ | document index + PDFs (one scanned for OCR) |
| api-cursor/ | JSON cursor API, 429 rate limiting |
| drift/<case>/ | 20 single-mutation copies for repair evals (see drift/README.md) |

Golden rows: `tests/fixtures/golden/<site>.json`.
