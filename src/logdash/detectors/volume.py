"""Volume spikes and drops: rolling robust z-score of the event rate per service and per host.

This is the *volume* type of ``PLAN.md`` module 6. It reads the 1-minute rate tables of the
temporal stage (``rate_60`` per service, ``host_60`` per host) and reuses ``find_bursts`` (a run of
minutes above the rolling median plus ``burst_k`` robust sigmas) and ``find_gaps`` (a silent run of
at least ``gap_minutes`` minutes) of ``analyses/temporal.py``, so the Temporal page and this
detector agree on what a burst and a gap are. One row is one episode: ``bin`` is its first minute
and ``window_s`` its length.

The raw score of a spike is the robust z-score at its peak, so the threshold is ``burst_k``. A gap
has no z-score (the baseline may be tiny), so its raw score is ``burst_k * minutes / gap_minutes``:
a gap of the minimum length sits at the threshold and a gap twice as long at twice that. Gaps are
reported for hosts only, as in Temporal: a service that logs rarely is silent most of the time.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import polars as pl

from logdash.analyses import temporal
from logdash.analyses.temporal import MISSING, find_bursts, find_gaps
from logdash.config import DEFAULT_BURST_K, Settings
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60


def episodes(
    frame: pl.DataFrame,
    entity_col: str,
    kind: str,
    t0: Any,
    length: int,
    settings: Settings,
    gaps: bool,
) -> list[dict[str, Any]]:
    """Spike and (if ``gaps``) gap rows of every entity in ``frame`` (bin, entity, n)."""
    rows: list[dict[str, Any]] = []
    for (entity,), group in frame.group_by([entity_col], maintain_order=True):
        if entity == MISSING:  # malformed rows, not a machine
            continue
        x = temporal.dense_series(group["bin"], group["n"], t0, WINDOW_S, length)
        for f in find_bursts(x, settings.burst_k):
            rows.append(
                row(t0, f, kind, str(entity), f["score"], spike_text(f)),
            )
        if gaps:
            for f in find_gaps(x, settings.gap_minutes):
                minutes = f["last"] - f["first"] + 1
                raw = settings.burst_k * minutes / settings.gap_minutes
                text = f"silent for {minutes} min (usually {f['baseline']:.0f} events/min)"
                rows.append(row(t0, f, kind, str(entity), raw, text))
    return rows


def row(
    t0: Any, f: dict[str, Any], kind: str, entity: str, raw: float, text: str
) -> dict[str, Any]:
    return {
        "bin": t0 + timedelta(minutes=f["first"]),
        "window_s": (f["last"] - f["first"] + 1) * WINDOW_S,
        "kind": kind,
        "entity": entity,
        "raw": raw,
        "evidence": text,
    }


def spike_text(f: dict[str, Any]) -> str:
    minutes = f["last"] - f["first"] + 1
    return (
        f"spike: peak {f['peak']:.0f} events/min vs baseline {f['baseline']:.0f}, "
        f"{f['excess']:.0f} extra events over {minutes} min"
    )


@register
class Volume:
    key = "volume"
    type = "volume"
    title = "Volume spike or drop"
    version = (
        "1"  # the algorithm lives in analyses.temporal (find_bursts, find_gaps): bump on change
    )
    setting_names: tuple[str, ...] = ("burst_k", "gap_minutes")
    depends_on: tuple[str, ...] = ("temporal",)
    threshold = DEFAULT_BURST_K

    def threshold_for(self, settings: Settings) -> float:
        return settings.burst_k

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        schema: dict[str, Any] = {
            "bin": pl.Datetime("us"),
            "window_s": pl.Int64,
            "kind": pl.String,
            "entity": pl.String,
            "raw": pl.Float64,
            "evidence": pl.String,
        }
        rate = pl.read_parquet(ctx.store.path(temporal.KEY, "rate_60.parquet"))
        hosts = pl.read_parquet(ctx.store.path(temporal.KEY, "host_60.parquet"))
        if rate.is_empty():
            return pl.DataFrame(schema=schema)
        t0, t1 = temporal.span(pl.concat([rate["bin"], hosts["bin"]]))
        length = int((t1 - t0).total_seconds() // WINDOW_S) + 1
        by_service = rate.group_by("bin", "program").agg(pl.col("n").sum())
        rows = episodes(by_service, "program", "service", t0, length, ctx.settings, gaps=False)
        rows += episodes(hosts, "host", "host", t0, length, ctx.settings, gaps=True)
        ctx.log(f"volume: {len(rows)} episodes")
        return pl.DataFrame(rows, schema=schema)
