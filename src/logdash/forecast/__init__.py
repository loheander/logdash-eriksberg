"""Learning code of the forecasting lab (ADR003, ADR004). Runs in the background job process.

Modules here must not call polars or DuckDB on the job's path: the job runs in a forked process
and polars' thread pool does not survive ``fork`` (ADR004).
"""
