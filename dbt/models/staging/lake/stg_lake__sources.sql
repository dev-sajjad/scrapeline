select * exclude (_file) rename (id as source_id, name as source_name)
from {{ lake_table('sources') }} as sources
qualify row_number() over (partition by id order by _file desc) = 1
