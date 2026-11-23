-- valid_from never decreases along a record's versions, so no interval ends before it starts.
select record_id, version, valid_from, valid_to
from {{ ref('int_record_versions') }}
where valid_to < valid_from
