# Runbook: `quality` incident

**Trigger:** a blocking check in `ops.check_result` failed (record count out of range, required-field nulls, duplicates inside one raw document, reject rate).

1. `scrapeline status <source>`; read the failing `check_name` and `details`.
2. Rejects: `select reason, count(*) from pipeline.rejects where run_id = ... group by 1`.
3. Zero rows on a first run or backfill = failure; zero rows on an incremental window with a watermark = `warn` only.
4. Parser bug → fix the adapter + test; then `scrapeline reprocess <source> --from-raw` rebuilds rows from stored raw.
5. The incident closes on the next passing run.
