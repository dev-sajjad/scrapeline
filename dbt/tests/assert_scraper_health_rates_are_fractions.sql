select source_id, date_day, success_rate
from {{ ref('scraper_health_daily') }}
where success_rate < 0 or success_rate > 1
