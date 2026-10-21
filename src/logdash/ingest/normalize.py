"""Normalisation of raw (all-varchar) log columns into the typed ``events`` schema."""

from __future__ import annotations

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "UNKNOWN")

LEVEL_MAP: dict[str, str] = {
    "debug": "DEBUG",
    "trace": "DEBUG",
    "info": "INFO",
    "notice": "INFO",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "err": "ERROR",
    "critical": "CRITICAL",
    "crit": "CRITICAL",
    "fatal": "CRITICAL",
}

EVENT_COLUMNS = (
    "ts",
    "level",
    "host",
    "program",
    "logger",
    "python_module",
    "request_id",
    "user_id",
    "tenant_id",
    "user_domain",
    "project_domain",
    "pid",
    "src_id",
    "payload",
    "malformed",
    "hour",
)


def normalize_level(value: str | None) -> str:
    """Map any spelling of a log level to one of ``LEVELS``."""
    return LEVEL_MAP.get((value or "").strip().lower(), "UNKNOWN")


def strip_host_domain(host: str | None) -> str | None:
    """Drop the DNS domain suffix (``a.xerces.lan`` becomes ``a``)."""
    if host is None:
        return None
    return host.split(".", 1)[0]


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def clean_sql(col: str) -> str:
    """Treat empty and ``-`` values as NULL."""
    return f"NULLIF(NULLIF(trim({col}), ''), '-')"


def level_sql(col: str) -> str:
    whens = " ".join(f"WHEN {_q(k)} THEN {_q(v)}" for k, v in LEVEL_MAP.items())
    return f"CASE lower(trim({col})) {whens} ELSE 'UNKNOWN' END"


def ts_sql(ts_col: str, epoch_col: str) -> str:
    """UTC timestamp with millisecond precision, falling back to epoch seconds."""
    parsed = f"CAST(TRY_CAST({ts_col} AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS TIMESTAMP)"
    fallback = f"to_timestamp(TRY_CAST({epoch_col} AS DOUBLE)) AT TIME ZONE 'UTC'"
    return f"date_trunc('millisecond', COALESCE({parsed}, {fallback}))"


def normalized_select(source: str) -> str:
    """SQL selecting the normalised events schema from the raw relation ``source``."""
    host = f"split_part({clean_sql('Hostname')}, '.', 1)"
    return f"""
    WITH t AS (
        SELECT
            {ts_sql('"@timestamp"', '"Timestamp"')} AS ts,
            {level_sql("log_level")} AS level,
            {host} AS host,
            {clean_sql("programname")} AS program,
            {clean_sql("Logger")} AS logger,
            {clean_sql("python_module")} AS python_module,
            {clean_sql("request_id")} AS request_id,
            {clean_sql("user_id")} AS user_id,
            {clean_sql("tenant_id")} AS tenant_id,
            {clean_sql("user_domain")} AS user_domain,
            {clean_sql("project_domain")} AS project_domain,
            TRY_CAST(TRY_CAST({clean_sql("Pid")} AS DOUBLE) AS BIGINT) AS pid,
            TRY_CAST({clean_sql("id")} AS BIGINT) AS src_id,
            NULLIF(Payload, '') AS payload
        FROM {source}
    )
    SELECT
        ts, level, host, program, logger, python_module, request_id, user_id,
        tenant_id, user_domain, project_domain, pid, src_id, payload,
        (host IS NULL AND program IS NULL AND payload IS NULL) AS malformed,
        lpad(CAST(COALESCE(date_part('hour', ts), 0) AS VARCHAR), 2, '0') AS hour
    FROM t
    """
