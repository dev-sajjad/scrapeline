select * exclude (_file) rename (id as incident_id)
from {{ lake_table('incidents') }} as incidents
qualify row_number() over (partition by id order by _file desc) = 1
