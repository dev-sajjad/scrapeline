-- One row per product per UTC day on which it existed: some non-removed version was valid
-- during the day. The price is the last one known that day, from the latest such version.
-- Versions that are still open run to the as-of date.
with observed as (
    select
        *,
        cast(valid_from as date) as first_day,
        least(
            -- valid_to is exclusive: a version ending at midnight did not exist that day.
            coalesce(cast(valid_to - interval 1 microsecond as date), {{ as_of_date() }}),
            {{ as_of_date() }}
        ) as last_day
    from {{ ref('product_price_history') }}
    where not is_removed
),

days as (
    select
        *,
        cast(unnest(generate_series(first_day, last_day, interval 1 day)) as date) as date_day
    from observed
)

select
    date_day,
    record_id,
    source_id,
    external_id,
    title,
    url,
    price_amount,
    price_currency,
    in_stock,
    stock_quantity,
    version,
    version_id,
    site_category
from days
qualify row_number() over (partition by record_id, date_day order by version desc) = 1
