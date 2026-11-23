-- One row per dataset, described by its newest version.
select
    dataset,
    version as latest_version,
    count(*) over (partition by dataset) as version_count,
    pack,
    storage,
    description,
    len(json_keys(fields)) as field_count,
    key_fields
from {{ ref('stg_lake__dataset_types') }}
qualify row_number() over (partition by dataset order by version desc) = 1
