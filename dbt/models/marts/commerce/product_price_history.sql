-- One row per product version, compared with the last observed state before it. A removal
-- observes nothing, so it carries no price or stock, and the version after it (a restore)
-- is compared with the state from before the removal.
with versions as (
    select
        products.*,
        history.valid_to,
        history.is_current,
        history.is_removed
    from {{ ref('stg_product__versions') }} as products
    inner join {{ ref('int_record_versions') }} as history
        on history.version_id = products.version_id
),

with_previous as (
    select
        *,
        last_value(
            case when not is_removed then {
                'title': title,
                'url': url,
                'price_amount': price_amount,
                'price_currency': price_currency,
                'in_stock': in_stock,
                'stock_quantity': stock_quantity
            } end ignore nulls
        ) over (
            partition by record_id order by version
            rows between unbounded preceding and 1 preceding
        ) as previous
    from versions
),

compared as (
    select
        *,
        not is_removed and previous is not null as is_comparable,
        -- Amounts in different currencies are not comparable.
        case
            when price_currency = previous.price_currency
                then price_amount - previous.price_amount
        end as price_change
    from with_previous
)

select
    compared.version_id,
    compared.record_id,
    compared.source_id,
    compared.external_id,
    -- A removed version has no payload; name it by its last observed state.
    coalesce(compared.title, compared.previous.title) as title,
    coalesce(compared.url, compared.previous.url) as url,
    compared.dataset_version,
    compared.version,
    compared.change_type,
    compared.valid_from,
    compared.valid_to,
    compared.is_current,
    compared.is_removed,
    compared.price_amount,
    compared.price_currency,
    compared.list_price_amount,
    compared.previous.price_amount as previous_price_amount,
    compared.previous.price_currency as previous_price_currency,
    compared.price_change,
    round(100 * compared.price_change / nullif(compared.previous.price_amount, 0), 2)
        as price_change_pct,
    case
        when compared.is_comparable
            then compared.price_amount is distinct from compared.previous.price_amount
            or compared.price_currency is distinct from compared.previous.price_currency
    end as price_changed,
    compared.in_stock,
    compared.previous.in_stock as previous_in_stock,
    case
        when compared.is_comparable
            then compared.in_stock is distinct from compared.previous.in_stock
    end as stock_changed,
    compared.stock_quantity,
    compared.previous.stock_quantity as previous_stock_quantity,
    compared.stock_quantity - compared.previous.stock_quantity as stock_quantity_change,
    compared.availability,
    sources.source_name,
    sources.site_id,
    sources.site_host,
    coalesce(sources.site_category, 'unclassified') as site_category,
    compared.run_id,
    compared.raw_sha256
from compared
left join {{ ref('dim_source') }} as sources
    on sources.source_id = compared.source_id
