"""On-disk cache layout and read access for the dashboard."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from logdash.cache.manifest import Manifest
from logdash.config import Settings

EVENTS_KEY = "events"


class CacheStore:
    """Paths and helpers for ``cache/``: per-module directories, events and manifest."""

    def __init__(self, root: Path, settings: Settings | None = None) -> None:
        self.root = Path(root)
        self.settings = settings or Settings(cache_dir=self.root)

    @property
    def events_dir(self) -> Path:
        return self.root / EVENTS_KEY

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def manifest(self) -> Manifest:
        return Manifest.load(self.manifest_path)

    def module_dir(self, key: str) -> Path:
        return self.root / key

    def path(self, key: str, name: str) -> Path:
        return self.module_dir(key) / name

    def reset_module(self, key: str) -> Path:
        """Empty and recreate a module directory before it is recomputed."""
        out = self.module_dir(key)
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        return out

    def exists(self, key: str, name: str) -> bool:
        return self.path(key, name).exists()

    def write_parquet(self, key: str, name: str, df: pl.DataFrame) -> Path:
        target = self.path(key, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(target, compression="zstd")
        return target

    def read_parquet(self, key: str, name: str) -> pl.DataFrame:
        return pl.read_parquet(self.path(key, name))

    def write_json(self, key: str, name: str, obj: Any) -> Path:
        target = self.path(key, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(obj, indent=2, default=str))
        return target

    def read_json(self, key: str, name: str) -> Any:
        return json.loads(self.path(key, name).read_text())

    def count_parquet_rows(self, key: str) -> dict[str, int]:
        """Row count per parquet file under a module directory (from file metadata)."""
        out: dict[str, int] = {}
        for file in sorted(self.module_dir(key).rglob("*.parquet")):
            rel = str(file.relative_to(self.module_dir(key)))
            out[rel] = pl.scan_parquet(file).select(pl.len()).collect().item()
        return out

    def events_glob(self) -> str:
        return str(self.events_dir / "hour=*" / "*.parquet")

    def connect(self) -> duckdb.DuckDBPyConnection:
        """In-memory DuckDB connection with an ``events`` view over the cache.

        The connection only holds views over Parquet files, so it never writes.
        Use ``.cursor()`` for per-thread access.
        """
        if not self.events_dir.exists():
            raise FileNotFoundError(f"no events cache at {self.events_dir}; run `logdash build`")
        con = duckdb.connect()
        con.execute(f"SET memory_limit = '{self.settings.memory_limit}'")
        con.execute(f"SET threads = {int(self.settings.threads)}")
        con.execute("SET TimeZone = 'UTC'")
        con.execute(
            "CREATE VIEW events AS SELECT * FROM read_parquet("
            f"'{self.events_glob()}', hive_partitioning = true, hive_types_autocast = false)"
        )
        return con
