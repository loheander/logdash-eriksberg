"""Runtime settings: environment variables with explicit overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

DEFAULT_DATA = "data"
DEFAULT_CACHE = "cache"
DEFAULT_MEMORY_LIMIT = "8GB"
DEFAULT_THREADS = 8


@dataclass(frozen=True)
class Settings:
    """Paths and DuckDB resource limits shared by build and serve."""

    data_dir: Path = Path(DEFAULT_DATA)
    cache_dir: Path = Path(DEFAULT_CACHE)
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    threads: int = DEFAULT_THREADS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("LOGDASH_DATA", DEFAULT_DATA)),
            cache_dir=Path(env.get("LOGDASH_CACHE", DEFAULT_CACHE)),
            memory_limit=env.get("LOGDASH_MEMORY_LIMIT", DEFAULT_MEMORY_LIMIT),
            threads=int(env.get("LOGDASH_THREADS", DEFAULT_THREADS)),
        )

    def with_overrides(self, **overrides: Any) -> Settings:
        """Return a copy with every non-None override applied."""
        given = {k: v for k, v in overrides.items() if v is not None}
        if "data_dir" in given:
            given["data_dir"] = Path(given["data_dir"])
        if "cache_dir" in given:
            given["cache_dir"] = Path(given["cache_dir"])
        return replace(self, **given)

    def data_files(self) -> list[Path]:
        """Input files, sorted by name."""
        return sorted(self.data_dir.glob("*.csv.zst"))

    def fingerprint_params(self) -> dict[str, Any]:
        """Settings that change computed results (resource limits do not)."""
        return {"timezone": "UTC"}
