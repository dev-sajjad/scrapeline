select
    sources.source_id,
    sources.source_name,
    sources.platform,
    sources.dataset,
    sources.status,
    sources.cadence,
    sources.created_at,
    sources.site_id,
    sites.host as site_host,
    -- A source without a site has not been classified either.
    coalesce(sites.category, 'unclassified') as site_category
from {{ ref('stg_lake__sources') }} as sources
left join {{ ref('dim_site') }} as sites
    on sites.site_id = sources.site_id
