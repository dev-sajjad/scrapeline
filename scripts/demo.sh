#!/usr/bin/env bash
# scrapeline end-to-end demo (~5 minutes). No API key needed; the agent step uses a key if present.
set -euo pipefail
cd "$(dirname "$0")/.."
step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
sl() { uv run scrapeline --log-level WARNING "$@"; }

step "1. Infrastructure (postgres + fixture sites) and migrations"
make -s up
uv run alembic upgrade head

step "2. Real data: Legistar platform adapter, one YAML per city"
sl source list
sl run legistar-seattle | python3 -c "import sys,json;d=json.load(sys.stdin);print({k:d[k] for k in ('status','pages','records','loaded','checks')})"

step "3. Idempotent: run again -> nothing inserted, rows only touched"
sl run legistar-seattle | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['loaded'])"

step "4. Recoverable: rebuild every row from immutable raw"
sl reprocess legistar-seattle | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'], d['loaded']['meeting'])"

step "5. Hard pages: static pagination + JS table with 'Load more' (Playwright), verified vs golden"
sl run fixture-static-list >/dev/null && sl verify fixture-static-list | python3 -c "import sys,json;d=json.load(sys.stdin);print('static-list golden:', d['passed'], d['field_accuracy'])"
sl run fixture-js-table >/dev/null && sl verify fixture-js-table | python3 -c "import sys,json;d=json.load(sys.stdin);print('js-table golden:', d['passed'], d['field_accuracy'])"

step "6. Documents: PDF text + OCR flag for the scanned one"
sl documents fixture-static-list

step "7. Drift: the site renamed a class -> which selector broke?"
sl drift diagnose fixture-static-list --case tests/sites/drift/01-title-class-renamed | python3 -c "import sys,json;d=json.load(sys.stdin);print('suspect fields:', d['suspect_fields'])"

step "8. Sandbox isolation on this container runtime"
make -s sandbox-check

step "9. Agent evals (baseline without LLM; 'make eval' runs Claude)"
sl eval run --suite repair --driver noop --no-db | head -16
if grep -q '^ANTHROPIC_API_KEY=.' .env 2>/dev/null; then
  step "10. Repair agent on a drift case (Claude, max 3 attempts, offline sandbox judge)"
  sl agent repair fixture-static-list --case tests/sites/drift/01-title-class-renamed || true
fi

step "11. Read API"
echo "make api  ->  http://localhost:8000/meetings?jurisdiction=us-wa-seattle&limit=5"
