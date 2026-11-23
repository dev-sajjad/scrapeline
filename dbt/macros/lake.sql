{% macro lake_table(name) -%}
    {#- A lake table as a subquery: every column cast to its layout type, plus `_file`, the
        Parquet file each row came from. The exporter writes no file for an append table that
        has no rows yet, so such a table reads as empty. It rewrites every snapshot on each
        export, so a missing snapshot means LAKE_DIR is wrong or nothing was exported: that
        fails the build instead of replacing the marts with empty tables. -#}
    {%- set relation = source('lake', name) -%}
    {%- if not execute -%}
        {{ return('(select 1)') }}
    {%- endif -%}
    {%- set node = graph.sources['source.' ~ project_name ~ '.lake.' ~ name] -%}
    {%- set present = lake_files_exist(node.config.meta.lake_path) -%}
    {%- if not present -%}
        {%- set missing = lake_missing_snapshots() -%}
        {%- if missing -%}
            {{ exceptions.raise_compiler_error(
                "lake table `" ~ name ~ "`: no lake at LAKE_DIR="
                ~ env_var('LAKE_DIR', 'data/lake')
                ~ " (missing " ~ missing | join(', ')
                ~ "). Every export writes the snapshots; set LAKE_DIR to an exported lake. "
                ~ "A relative path resolves from the directory dbt runs in."
            ) }}
        {%- endif -%}
    {%- endif -%}
    (
        select
        {%- for column in node.columns.values() %}
            cast({{ adapter.quote(column.name) if present else 'null' }} as {{ column.data_type }}) as {{ adapter.quote(column.name) }},
        {%- endfor %}
            {{ 'filename' if present else 'cast(null as varchar)' }} as _file
        {{ 'from ' ~ relation if present else 'where false' }}
    )
{%- endmacro %}


{% macro lake_missing_snapshots() -%}
    {#- Snapshot files of the lake that are not on disk; all of them when the root is wrong. -#}
    {%- set missing = [] -%}
    {%- for node in graph.sources.values()
        if node.source_name == 'lake' and node.config.meta.mode == 'snapshot' -%}
        {%- if not lake_files_exist(node.config.meta.lake_path) -%}
            {%- do missing.append(node.config.meta.lake_path) -%}
        {%- endif -%}
    {%- endfor -%}
    {{ return(missing | sort) }}
{%- endmacro %}


{% macro lake_files_exist(path) -%}
    {%- set pattern = path | replace("'", "''") -%}
    {%- set found = run_query("select count(*) from glob('" ~ pattern ~ "')") -%}
    {{ return(found.columns[0].values()[0] > 0) }}
{%- endmacro %}
