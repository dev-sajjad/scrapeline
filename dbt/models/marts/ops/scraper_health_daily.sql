-- One row per source per UTC day, from the source's first activity to the as-of date, so a
-- day without runs shows up as zero runs rather than as a gap. Runs count on the day they
-- started; record counts come from the run stats the runner stores.
with runs as (
    select
        run_id,
        source_id,
        cast(started_at as date) as date_day,
        status,
        stats
    from {{ ref('stg_lake__runs') }}
),

-- stats.loaded maps each dataset or typed table to its load counts.
run_loads as (
    select
        runs.run_id,
        sum(coalesce(cast(loaded.value ->> 'inserted' as bigint), 0)) as inserted,
        sum(coalesce(cast(loaded.value ->> 'updated' as bigint), 0)) as updated,
        sum(coalesce(cast(loaded.value ->> 'unchanged' as bigint), 0)) as unchanged,
        sum(coalesce(cast(loaded.value ->> 'restored' as bigint), 0)) as restored,
        sum(coalesce(cast(loaded.value ->> 'removed' as bigint), 0)) as removed
    from runs, json_each(runs.stats, '$.loaded') as loaded
    group by runs.run_id
),

run_days as (
    select
        runs.source_id,
        runs.date_day,
        count(*) as runs,
        count(*) filter (where runs.status = 'succeeded') as runs_succeeded,
        count(*) filter (where runs.status = 'failed') as runs_failed,
        sum(coalesce(cast(runs.stats ->> 'pages' as bigint), 0)) as pages,
        sum(coalesce(cast(runs.stats ->> 'rejects' as bigint), 0)) as rejects,
        sum(coalesce(run_loads.inserted, 0)) as records_inserted,
        sum(coalesce(run_loads.updated, 0)) as records_updated,
        sum(coalesce(run_loads.unchanged, 0)) as records_unchanged,
        sum(coalesce(run_loads.restored, 0)) as records_restored,
        sum(coalesce(run_loads.removed, 0)) as records_removed
    from runs
    left join run_loads on run_loads.run_id = runs.run_id
    group by runs.source_id, runs.date_day
),

fetch_days as (
    select
        source_id,
        cast(fetched_at as date) as date_day,
        count(*) as fetches,
        -- The runner's definition of a failed fetch: a transport error or an HTTP error.
        count(*) filter (where error is not null or status >= 400) as fetch_errors,
        count(*) filter (where status in (401, 403, 429)) as blocked_responses,
        round(quantile_cont(elapsed_ms, 0.95), 1) as p95_fetch_ms
    from {{ ref('stg_lake__fetch_log') }}
    group by source_id, date_day
),

check_days as (
    select
        source_id,
        cast(created_at as date) as date_day,
        count(*) filter (where status = 'warn') as checks_warned,
        count(*) filter (where status = 'fail') as checks_failed
    from {{ ref('stg_lake__check_results') }}
    group by source_id, date_day
),

agent_days as (
    select
        source_id,
        cast(started_at as date) as date_day,
        count(*) as agent_runs,
        sum(cost_usd) as agent_cost_usd
    from {{ ref('stg_lake__agent_runs') }}
    group by source_id, date_day
),

first_days as (
    select source_id, min(date_day) as first_day
    from (
        select source_id, date_day from run_days
        union all
        select source_id, date_day from fetch_days
        union all
        select source_id, date_day from check_days
        union all
        select source_id, date_day from agent_days
    )
    group by source_id
),

spine as (
    select first_days.source_id, dates.date_day
    from first_days
    inner join {{ ref('dim_date') }} as dates
        on dates.date_day between first_days.first_day and {{ as_of_date() }}
)

select
    spine.source_id,
    spine.date_day,
    coalesce(run_days.runs, 0) as runs,
    coalesce(run_days.runs_succeeded, 0) as runs_succeeded,
    coalesce(run_days.runs_failed, 0) as runs_failed,
    round(run_days.runs_succeeded / nullif(run_days.runs, 0), 4) as success_rate,
    coalesce(run_days.pages, 0) as pages,
    coalesce(
        run_days.records_inserted
        + run_days.records_updated
        + run_days.records_unchanged
        + run_days.records_restored,
        0
    ) as records_loaded,
    coalesce(run_days.records_inserted, 0) as records_inserted,
    coalesce(run_days.records_updated, 0) as records_updated,
    coalesce(run_days.records_restored, 0) as records_restored,
    coalesce(run_days.records_removed, 0) as records_removed,
    coalesce(run_days.rejects, 0) as rejects,
    coalesce(fetch_days.fetches, 0) as fetches,
    coalesce(fetch_days.fetch_errors, 0) as fetch_errors,
    coalesce(fetch_days.blocked_responses, 0) as blocked_responses,
    fetch_days.p95_fetch_ms,
    coalesce(check_days.checks_warned, 0) as checks_warned,
    coalesce(check_days.checks_failed, 0) as checks_failed,
    coalesce(agent_days.agent_runs, 0) as agent_runs,
    round(coalesce(agent_days.agent_cost_usd, 0), 6) as agent_cost_usd
from spine
left join run_days
    on run_days.source_id = spine.source_id and run_days.date_day = spine.date_day
left join fetch_days
    on fetch_days.source_id = spine.source_id and fetch_days.date_day = spine.date_day
left join check_days
    on check_days.source_id = spine.source_id and check_days.date_day = spine.date_day
left join agent_days
    on agent_days.source_id = spine.source_id and agent_days.date_day = spine.date_day
