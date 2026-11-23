-- Runs are exported with an overlap window, so a run can appear in two exports.
select * exclude (_file)
from {{ lake_table('runs') }} as runs
qualify row_number() over (partition by run_id order by _file desc) = 1
