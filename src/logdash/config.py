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
DEFAULT_BURST_K = 6.0
DEFAULT_GAP_MINUTES = 10


@dataclass(frozen=True)
class Settings:
    """Paths and DuckDB resource limits shared by build and serve."""

    data_dir: Path = Path(DEFAULT_DATA)
    cache_dir: Path = Path(DEFAULT_CACHE)
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    threads: int = DEFAULT_THREADS
    burst_k: float = DEFAULT_BURST_K  # bursts: count above rolling median + k robust sigmas
    gap_minutes: int = DEFAULT_GAP_MINUTES  # a silent run at least this long is a gap

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            data_dir=Path(env.get("LOGDASH_DATA", DEFAULT_DATA)),
            cache_dir=Path(env.get("LOGDASH_CACHE", DEFAULT_CACHE)),
            memory_limit=env.get("LOGDASH_MEMORY_LIMIT", DEFAULT_MEMORY_LIMIT),
            threads=int(env.get("LOGDASH_THREADS", DEFAULT_THREADS)),
            burst_k=float(env.get("LOGDASH_BURST_K", DEFAULT_BURST_K)),
            gap_minutes=int(env.get("LOGDASH_GAP_MINUTES", DEFAULT_GAP_MINUTES)),
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

    def analysis_params(self, names: tuple[str, ...]) -> dict[str, Any]:
        """Fingerprint parameters of an analysis: the base ones plus the settings it names."""
        return {**self.fingerprint_params(), **{n: getattr(self, n) for n in names}}
