select record_id, count(*) filter (where is_current) as current_versions
from {{ ref('int_record_versions') }}
group by record_id
having count(*) filter (where is_current) <> 1
