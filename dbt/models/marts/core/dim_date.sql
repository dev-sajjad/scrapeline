-- Every UTC day from the first activity in the lake to the as-of date (or the last
-- activity, if that is later).
with activity as (
    select cast(valid_from as date) as day from {{ ref('stg_lake__record_versions') }}
    union all
    select cast(started_at as date) from {{ ref('stg_lake__runs') }}
    union all
    select cast(fetched_at as date) from {{ ref('stg_lake__fetch_log') }}
    union all
    select cast(created_at as date) from {{ ref('stg_lake__check_results') }}
    union all
    select cast(started_at as date) from {{ ref('stg_lake__agent_runs') }}
),

bounds as (
    select
        coalesce(min(day), {{ as_of_date() }}) as first_day,
        greatest(coalesce(max(day), {{ as_of_date() }}), {{ as_of_date() }}) as last_day
    from activity
),

days as (
    select cast(unnest(generate_series(first_day, last_day, interval 1 day)) as date) as date_day
    from bounds
)

select
    date_day,
    year(date_day) as year,
    quarter(date_day) as quarter,
    month(date_day) as month,
    monthname(date_day) as month_name,
    cast(date_trunc('month', date_day) as date) as month_start,
    week(date_day) as iso_week,
    day(date_day) as day_of_month,
    isodow(date_day) as iso_day_of_week,
    dayname(date_day) as day_name,
    isodow(date_day) >= 6 as is_weekend
from days
