{% test unique_combination_of_columns(model, combination_of_columns) %}
{%- set columns = combination_of_columns | join(', ') %}
select {{ columns }}, count(*) as row_count
from {{ model }}
group by {{ columns }}
having count(*) > 1
{% endtest %}
