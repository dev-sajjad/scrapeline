select * exclude (_file) rename (id as check_result_id)
from {{ lake_table('check_results') }} as check_results
qualify row_number() over (partition by id order by _file desc) = 1
