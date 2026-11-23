select * exclude (_file) rename (id as agent_run_id)
from {{ lake_table('agent_runs') }} as agent_runs
qualify row_number() over (partition by id order by _file desc) = 1
