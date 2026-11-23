select * exclude (_file)
from {{ lake_table('site_categories') }} as site_categories
qualify row_number() over (partition by code order by _file desc) = 1
