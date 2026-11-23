-- One row per registered source per UTC day, from the source's first activity to the as-of
-- date, so a day without runs shows up as zero runs rather than as a gap. Only sources in the
-- sources snapshot get rows: agent runs for anything else (generation eval cases log
-- `gen-<case>` ids) are not source health. Runs count on the day they started; record counts
-- come from the run stats the runner stores.
with runs as (
    select
        run_id,
        source_id,
        cast(started_at as date) as date_day,
        status,
        stats,
        -- The runner stores `<exception type>: <message>`; challenge pages and empty shells
        -- arrive as HTTP 200 and only show up here.
        coalesce(error like 'BlockedError:%', false) as blocked
    from {{ ref('stg_lake__runs') }}
),

-- Runs with at least one fetch attempt answered 401, 403 or 429.
blocked_status_runs as (
    select distinct run_id
    from {{ ref('stg_lake__fetch_log') }}
    where status in (401, 403, 429)
),

-- stats.loaded maps each dataset or typed table to its load counts.
run_loads as (
    select
        runs.run_id,
        cast(sum(coalesce(cast(loaded.value ->> 'inserted' as bigint), 0)) as bigint) as inserted,
        cast(sum(coalesce(cast(loaded.value ->> 'updated' as bigint), 0)) as bigint) as updated,
        cast(sum(coalesce(cast(loaded.value ->> 'unchanged' as bigint), 0)) as bigint) as unchanged,
        cast(sum(coalesce(cast(loaded.value ->> 'restored' as bigint), 0)) as bigint) as restored,
        cast(sum(coalesce(cast(loaded.value ->> 'removed' as bigint), 0)) as bigint) as removed
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
        count(*) filter (where runs.blocked) as blocked_runs,
        count(*) filter (
            where runs.blocked and blocked_status_runs.run_id is null
        ) as blocked_runs_without_status,
        cast(sum(coalesce(cast(runs.stats ->> 'pages' as bigint), 0)) as bigint) as pages,
        cast(sum(coalesce(cast(runs.stats ->> 'rejects' as bigint), 0)) as bigint) as rejects,
        cast(sum(coalesce(run_loads.inserted, 0)) as bigint) as records_inserted,
        cast(sum(coalesce(run_loads.updated, 0)) as bigint) as records_updated,
        cast(sum(coalesce(run_loads.unchanged, 0)) as bigint) as records_unchanged,
        cast(sum(coalesce(run_loads.restored, 0)) as bigint) as records_restored,
        cast(sum(coalesce(run_loads.removed, 0)) as bigint) as records_removed
    from runs
    left join run_loads on run_loads.run_id = runs.run_id
    left join blocked_status_runs on blocked_status_runs.run_id = runs.run_id
    group by runs.source_id, runs.date_day
),

fetch_days as (
    select
        source_id,
        cast(fetched_at as date) as date_day,
        count(*) as fetches,
        -- Failed attempts (a transport error or an HTTP error), including ones a retry fixed;
        -- not the runner's per-URL rule, which counts only URLs that never succeeded.
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
    select activity.source_id, min(activity.date_day) as first_day
    from (
        select source_id, date_day from run_days
        union all
        select source_id, date_day from fetch_days
        union all
        select source_id, date_day from check_days
        union all
        select source_id, date_day from agent_days
    ) as activity
    inner join {{ ref('dim_source') }} as sources
        on sources.source_id = activity.source_id
    group by activity.source_id
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
    coalesce(fetch_days.blocked_responses, 0)
    + coalesce(run_days.blocked_runs_without_status, 0) as blocked_responses,
    coalesce(run_days.blocked_runs, 0) as blocked_runs,
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
