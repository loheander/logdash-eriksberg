"""Cache directory layout: paths, Parquet/JSON helpers, the manifest and a DuckDB connection."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from logdash.cache.manifest import Manifest
from logdash.config import Settings
from logdash.ingest.reader import connect as open_connection
from logdash.ingest.reader import events_glob

EVENTS_KEY = "events"
MANIFEST_NAME = "manifest.json"


class CacheStore:
    """``cache/``: ``events/`` plus one subdirectory per analysis key."""

    def __init__(self, root: Path, settings: Settings | None = None) -> None:
        self.root = Path(root)
        self.settings = settings if settings is not None else Settings(cache_dir=self.root)

    @property
    def events_dir(self) -> Path:
        return self.root / EVENTS_KEY

    def path(self, module: str, *parts: str) -> Path:
        return self.root.joinpath(module, *parts)

    def exists(self, module: str, name: str) -> bool:
        return self.path(module, name).exists()

    def write_parquet(self, module: str, name: str, df: pl.DataFrame) -> Path:
        target = self.path(module, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(target, compression="zstd")
        return target

    def read_parquet(self, module: str, name: str) -> pl.DataFrame:
        return pl.read_parquet(self.path(module, name))

    def write_json(self, module: str, name: str, data: Any) -> Path:
        target = self.path(module, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, default=str))
        return target

    def read_json(self, module: str, name: str) -> Any:
        return json.loads(self.path(module, name).read_text())

    def count_parquet_rows(self, module: str) -> dict[str, int]:
        """Row count per Parquet file below a module directory, keyed by relative path."""
        base = self.path(module)
        return {
            str(p.relative_to(base)): pl.scan_parquet(p).select(pl.len()).collect().item()
            for p in sorted(base.rglob("*.parquet"))
        }

    def reset_module(self, module: str) -> Path:
        """Empty and recreate a module directory; returns it."""
        target = self.path(module)
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        return target

    def manifest(self) -> Manifest:
        return Manifest.load(self.root / MANIFEST_NAME)

    def connect(self) -> duckdb.DuckDBPyConnection:
        """DuckDB connection with the ``events`` view over the hour partitions."""
        if not self.events_dir.exists():
            raise FileNotFoundError(f"no events in {self.events_dir}; run `logdash build`")
        con = open_connection(self.settings)
        con.execute(
            "CREATE VIEW events AS SELECT * FROM read_parquet("
            f"'{events_glob(self.events_dir)}', hive_partitioning = true, "
            "hive_types_autocast = false)"
        )
        return con
