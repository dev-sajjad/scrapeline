select
    sites.site_id,
    sites.host,
    sites.registered_domain,
    sites.category,
    categories.label as category_label,
    sites.category_confidence,
    sites.category_method,
    sites.tags,
    sites.country,
    sites.language,
    sites.platform,
    sites.classified_at
from {{ ref('stg_lake__sites') }} as sites
left join {{ ref('stg_lake__site_categories') }} as categories
    on categories.code = sites.category
