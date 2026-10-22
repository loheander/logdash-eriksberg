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
DEFAULT_DRAIN_SIM_TH = 0.4
DEFAULT_DRAIN_DEPTH = 4
DEFAULT_TEMPLATE_TOP_K = 50
DEFAULT_PARAM_SAMPLE = 2000
DEFAULT_RARE_THRESHOLD = 5
DEFAULT_NOVEL_FRACTION = 0.1
DEFAULT_JEV_MODEL = "typesafe/jev-1.14"
DEFAULT_JEV_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Settings:
    """Paths and DuckDB resource limits shared by build and serve."""

    data_dir: Path = Path(DEFAULT_DATA)
    cache_dir: Path = Path(DEFAULT_CACHE)
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    threads: int = DEFAULT_THREADS
    burst_k: float = DEFAULT_BURST_K  # bursts: count above rolling median + k robust sigmas
    gap_minutes: int = DEFAULT_GAP_MINUTES  # a silent run at least this long is a gap
    drain_sim_th: float = DEFAULT_DRAIN_SIM_TH  # Drain3 similarity threshold
    drain_depth: int = DEFAULT_DRAIN_DEPTH  # Drain3 parse tree depth
    template_top_k: int = DEFAULT_TEMPLATE_TOP_K  # templates with parameter extraction
    param_sample: int = DEFAULT_PARAM_SAMPLE  # raw payloads sampled per top template
    rare_threshold: int = DEFAULT_RARE_THRESHOLD  # templates with at most this many events are rare
    novel_fraction: float = DEFAULT_NOVEL_FRACTION  # first seen in the last fraction of the span
    jev_model: str = DEFAULT_JEV_MODEL  # decision model of the triage page (called live)
    jev_timeout_s: float = DEFAULT_JEV_TIMEOUT_S  # per attempt; the client retries once

    def __post_init__(self) -> None:
        if not 0 < self.novel_fraction < 1:
            raise ValueError(
                f"novel_fraction must be between 0 and 1 (exclusive), got {self.novel_fraction}"
            )

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
            drain_sim_th=float(env.get("LOGDASH_DRAIN_SIM_TH", DEFAULT_DRAIN_SIM_TH)),
            drain_depth=int(env.get("LOGDASH_DRAIN_DEPTH", DEFAULT_DRAIN_DEPTH)),
            template_top_k=int(env.get("LOGDASH_TEMPLATE_TOP_K", DEFAULT_TEMPLATE_TOP_K)),
            param_sample=int(env.get("LOGDASH_PARAM_SAMPLE", DEFAULT_PARAM_SAMPLE)),
            rare_threshold=int(env.get("LOGDASH_RARE_THRESHOLD", DEFAULT_RARE_THRESHOLD)),
            novel_fraction=float(env.get("LOGDASH_NOVEL_FRACTION", DEFAULT_NOVEL_FRACTION)),
            jev_model=env.get("LOGDASH_JEV_MODEL", DEFAULT_JEV_MODEL),
            jev_timeout_s=float(env.get("LOGDASH_JEV_TIMEOUT_S", DEFAULT_JEV_TIMEOUT_S)),
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
