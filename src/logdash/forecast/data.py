"""Spike grid and (host, t) samples for the job. Never uses polars (ADR004)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow.parquet as pq

from logdash.analyses.forecast import KEY, PERIODS, period_index, sample_mask, targets
from logdash.cache.store import CacheStore


@dataclass(frozen=True)
class SpikeGrid:
    spike: np.ndarray  # (hosts, minutes) bool
    hosts: list[str]  # index -> host name, as in frame.parquet (sorted)
    meta: dict[str, Any]  # cache/forecast/meta.json


def load_spikes(store: CacheStore) -> SpikeGrid:
    """The spike column of ``frame.parquet`` as a host-by-minute array.

    Reads with pyarrow and ``json``: polars hangs in a forked job process (ADR004).
    """
    meta = store.read_json(KEY, "meta.json")
    h, m = int(meta["hosts"]), int(meta["minutes"])
    table = pq.read_table(store.path(KEY, "frame.parquet"), columns=["host", "spike"])
    if table.num_rows != h * m:
        raise ValueError(
            f"forecast: frame.parquet has {table.num_rows} rows, meta.json says {h} hosts x "
            f"{m} minutes; rebuild with `logdash build --only forecast`"
        )
    spike = table.column("spike").to_numpy().astype(bool).reshape(h, m)  # host-major (ADR003)
    hosts = table.column("host").take(np.arange(0, h * m, m)).to_pylist() if h else []
    return SpikeGrid(spike, hosts, meta)


@dataclass(frozen=True)
class Samples:
    """(host, t) samples of one period, host-major, with the target y(t)."""

    host: np.ndarray  # int32 host index
    minute: np.ndarray  # int32 minute index t
    labels: np.ndarray  # bool: a spike minute in (t, t+H]

    def __len__(self) -> int:
        return len(self.labels)


def period_samples(spike: np.ndarray, horizon: int, period: str) -> Samples:
    """Every host at every minute of ``period`` that ``sample_mask`` keeps (the full grid)."""
    hosts, minutes = spike.shape
    keep = sample_mask(minutes, horizon) & (period_index(minutes) == PERIODS.index(period))
    ts = np.flatnonzero(keep)
    host = np.repeat(np.arange(hosts), len(ts))
    minute = np.tile(ts, hosts)
    labels = targets(spike, horizon)[host, minute]
    return Samples(host.astype(np.int32), minute.astype(np.int32), labels)
