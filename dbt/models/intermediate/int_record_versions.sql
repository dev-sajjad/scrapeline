-- SCD type 2 view of the append-only history (ADR-0006): a version is valid until the next
-- version of the same record starts. A removal is a version of its own, so a removed record
-- has a closed interval before it and, once restored, a new one after it.
select
    record_versions.*,
    lead(valid_from) over record_history as valid_to,
    lead(version) over record_history is null as is_current,
    change_type = 'removed' as is_removed
from {{ ref('stg_lake__record_versions') }} as record_versions
window record_history as (partition by record_id order by version)
