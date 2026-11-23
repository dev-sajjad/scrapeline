{% macro lake_table(name) -%}
    {#- A lake table as a subquery: every column cast to its layout type, plus `_file`, the
        Parquet file each row came from. The exporter writes no file for a table that has
        no rows yet, so a missing table reads as empty instead of failing the build. -#}
    {%- set relation = source('lake', name) -%}
    {%- if not execute -%}
        {{ return('(select 1)') }}
    {%- endif -%}
    {%- set node = graph.sources['source.' ~ project_name ~ '.lake.' ~ name] -%}
    {%- set present = lake_files_exist(node.config.meta.lake_path) -%}
    (
        select
        {%- for column in node.columns.values() %}
            cast({{ adapter.quote(column.name) if present else 'null' }} as {{ column.data_type }}) as {{ adapter.quote(column.name) }},
        {%- endfor %}
            {{ 'filename' if present else 'cast(null as varchar)' }} as _file
        {{ 'from ' ~ relation if present else 'where false' }}
    )
{%- endmacro %}


{% macro lake_files_exist(path) -%}
    {%- set pattern = path | replace("'", "''") -%}
    {%- set found = run_query("select count(*) from glob('" ~ pattern ~ "')") -%}
    {{ return(found.columns[0].values()[0] > 0) }}
{%- endmacro %}
