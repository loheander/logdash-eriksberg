"""Read the raw zstd-compressed CSV with DuckDB and write the hour-partitioned cache."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from logdash.config import Settings
from logdash.ingest.normalize import normalized_select

RAW_VIEW = "raw"


@dataclass(frozen=True)
class IngestResult:
    rows: int
    malformed: int
    hours: int


def connect(settings: Settings, *, temp_dir: Path | None = None) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection honouring the configured resource limits."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{settings.memory_limit}'")
    con.execute(f"SET threads = {int(settings.threads)}")
    con.execute("SET TimeZone = 'UTC'")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{temp_dir}'")
    return con


def create_raw_view(con: duckdb.DuckDBPyConnection, files: Sequence[Path]) -> None:
    """Expose the CSV files as an all-varchar view named ``raw``."""
    if not files:
        raise FileNotFoundError("no input files")
    paths = ", ".join(f"'{f}'" for f in files)
    con.execute(
        f"""
        CREATE OR REPLACE VIEW {RAW_VIEW} AS
        SELECT * FROM read_csv([{paths}], header = true, all_varchar = true, quote = '"',
                               escape = '"', null_padding = true, union_by_name = true)
        """
    )


def write_events(
    con: duckdb.DuckDBPyConnection, out_dir: Path, *, source: str = RAW_VIEW
) -> IngestResult:
    """Stream normalised rows into ``out_dir/hour=HH/*.parquet`` (zstd, sorted by ts).

    Rows are written by DuckDB itself so nothing is materialised in Python. The
    output replaces ``out_dir`` atomically once complete.
    """
    tmp = out_dir.with_name(out_dir.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    query = f"SELECT * FROM ({normalized_select(source)}) ORDER BY ts"
    con.execute(
        f"COPY ({query}) TO '{tmp}' "
        "(FORMAT parquet, COMPRESSION zstd, PARTITION_BY (hour), OVERWRITE_OR_IGNORE)"
    )
    shutil.rmtree(out_dir, ignore_errors=True)
    tmp.rename(out_dir)
    return summarize(con, out_dir)


def events_glob(events_dir: Path) -> str:
    return str(events_dir / "hour=*" / "*.parquet")


def summarize(con: duckdb.DuckDBPyConnection, events_dir: Path) -> IngestResult:
    row = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE malformed), count(DISTINCT hour) "
        f"FROM read_parquet('{events_glob(events_dir)}', hive_partitioning = true, "
        "hive_types_autocast = false)"
    ).fetchone()
    assert row is not None
    return IngestResult(rows=row[0], malformed=row[1], hours=row[2])


def ingest(settings: Settings, out_dir: Path) -> IngestResult:
    """Full ingestion of ``settings.data_files()`` into ``out_dir``."""
    con = connect(settings, temp_dir=settings.cache_dir / "tmp")
    try:
        create_raw_view(con, settings.data_files())
        return write_events(con, out_dir)
    finally:
        con.close()
        shutil.rmtree(settings.cache_dir / "tmp", ignore_errors=True)
