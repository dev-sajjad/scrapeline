select * exclude (_file) rename (id as fetch_id)
from {{ lake_table('fetch_log') }} as fetch_log
qualify row_number() over (partition by id order by _file desc) = 1
