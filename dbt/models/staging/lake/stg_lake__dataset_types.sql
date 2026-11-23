select * exclude (_file) rename (name as dataset)
from {{ lake_table('dataset_types') }} as dataset_types
qualify row_number() over (partition by name, version order by _file desc) = 1
