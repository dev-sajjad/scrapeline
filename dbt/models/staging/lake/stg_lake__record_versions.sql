-- Exports can overlap, so the same row may sit in several files; keep one copy.
select * exclude (_file)
from {{ lake_table('record_versions') }} as record_versions
qualify row_number() over (partition by version_id order by _file desc) = 1
