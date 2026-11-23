{% macro as_of_date() -%}
    {#- Last day the daily marts cover: the `as_of_date` var, or today in UTC. -#}
    {%- set day = var('as_of_date') -%}
    {%- if day -%}
        date '{{ day }}'
    {%- else -%}
        current_date
    {%- endif -%}
{%- endmacro %}
