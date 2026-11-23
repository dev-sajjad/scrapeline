select * exclude (_file) rename (id as site_id)
from {{ lake_table('sites') }} as sites
qualify row_number() over (partition by id order by _file desc) = 1
