#!/usr/bin/env bash
# Price and stock history from one source seen on three days: the shop-sim fixture
# (tests/sites/shop-sim/) is run for day 1, 2 and 3, then the stored versions are queried.
# Needs postgres and the fixture server running (make up). Uses DATABASE_URL and
# FIXTURE_BASE_URL like every scrapeline command.
set -euo pipefail
cd "$(dirname "$0")/.."
step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
sl() { uv run scrapeline --log-level WARNING "$@"; }

# Runs one SQL query through the project's database settings and prints it as a table.
query() {
  uv run python -c '
import sys
from sqlalchemy import text
from scrapeline.store.db import get_engine

with get_engine().connect() as conn:
    result = conn.execute(text(sys.argv[1]))
    head = list(result.keys())
    rows = [["" if v is None else str(v) for v in row] for row in result]
widths = [max(map(len, column)) for column in zip(head, *rows)]
for line in [head, ["-" * w for w in widths], *rows]:
    print("  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip())
' "$1"
}

if ! uv run alembic upgrade head >/dev/null 2>&1; then
  echo "alembic upgrade failed: is postgres up (make up) and DATABASE_URL right?" >&2
  exit 1
fi

step "Versions already stored for shop-sim (a replay adds to them; history is append-only)"
query "SELECT count(*) AS versions FROM data.record_versions v
       JOIN data.records r ON r.id = v.record_id WHERE r.source_id = 'shop-sim'"

for day in 1 2 3; do
  step "Day $day: SHOP_SIM_DAY=$day scrapeline run shop-sim"
  SHOP_SIM_DAY=$day sl run shop-sim | python3 -c '
import json, sys
run = json.load(sys.stdin)
print(run["status"], "pages:", run["pages"], "complete:", run["complete"], run["error"] or "")
print(run["loaded"].get("product", {}))'
done

step "Price and stock history of every product that changed or appeared after the first run"
query "
SELECT r.external_id                                          AS sku,
       r.payload->>'title'                                    AS product,
       v.version,
       v.change_type                                          AS change,
       v.payload->'price'->>'amount'                          AS price_gbp,
       v.payload->>'in_stock'                                 AS in_stock,
       to_char(v.valid_from AT TIME ZONE 'UTC', 'HH24:MI:SS.MS') AS valid_from_utc
FROM data.record_versions v
JOIN data.records r ON r.id = v.record_id
WHERE r.source_id = 'shop-sim'
  AND (r.current_version > 1
       OR r.first_seen_at > (SELECT min(first_seen_at) FROM data.records
                             WHERE source_id = 'shop-sim'))
ORDER BY sku, v.version"
