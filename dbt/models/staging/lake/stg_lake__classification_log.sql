select * exclude (_file) rename (id as classification_id)
from {{ lake_table('classification_log') }} as classification_log
qualify row_number() over (partition by id order by _file desc) = 1
