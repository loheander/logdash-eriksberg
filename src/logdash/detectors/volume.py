"""Volume spikes and drops: rolling robust z-score of the event rate per service and per host.

This is the *volume* type of ``PLAN.md`` module 6. It reads the 1-minute rate tables of the
temporal stage (``rate_60`` per service, ``host_60`` per host) and uses ``find_bursts`` (a run of
minutes above the rolling median plus ``burst_k`` robust sigmas), ``find_drops`` (the low side: a
run below the trailing median minus ``burst_k`` robust sigmas, for entities busy enough that a
fall is meaningful) and ``find_gaps`` (a silent run of at least ``gap_minutes`` minutes) of
``analyses/temporal.py``. One row is one episode: ``bin`` is its first minute and ``window_s`` its
length. Nothing is reported before an entity's first or after its last line.

The raw score of a spike or drop is the robust z-score at its peak or trough, so the threshold is
``burst_k``. A gap has no z-score (the baseline may be tiny), so its raw score is
``burst_k * minutes / gap_minutes``: a gap of the minimum length sits at the threshold and a gap
twice as long at twice that. Gaps are reported for hosts only; a busy service that falls silent is
a drop (a service that logs rarely is silent most of the time, and has no drop baseline).

An entity whose episodes of one kind recur at a fixed period (a cron job, a polling agent) gets
one row: its strongest episode, with the count and the period in the evidence.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import polars as pl

from logdash.analyses import temporal
from logdash.analyses.temporal import MISSING, find_bursts, find_drops, find_gaps
from logdash.config import DEFAULT_BURST_K, Settings
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
MIN_REPEATS = 6  # episodes at one period needed to call them periodic
PERIOD_TOLERANCE = 0.04  # relative; at least one minute
MIN_PERIOD = 10  # minutes


def episodes(
    frame: pl.DataFrame,
    entity_col: str,
    kind: str,
    t0: Any,
    length: int,
    settings: Settings,
    gaps: bool,
) -> list[dict[str, Any]]:
    """Spike, drop and (if ``gaps``) gap rows of every entity in ``frame`` (bin, entity, n)."""
    rows: list[dict[str, Any]] = []
    for (entity,), group in frame.group_by([entity_col], maintain_order=True):
        if entity == MISSING:  # malformed rows, not a machine
            continue
        name = str(entity)
        x = temporal.dense_series(group["bin"], group["n"], t0, WINDOW_S, length)
        spikes = [
            row(t0, f, kind, name, f["score"], spike_text(f))
            for f in find_bursts(x, settings.burst_k)
        ]
        skip = settings.gap_minutes if gaps else None
        drops = [
            row(t0, f, kind, name, f["score"], drop_text(f))
            for f in find_drops(x, settings.burst_k, skip)
        ]
        rows += merge_periodic(spikes) + merge_periodic(drops)
        if gaps:
            for f in find_gaps(x, settings.gap_minutes):
                minutes = f["last"] - f["first"] + 1
                raw = settings.burst_k * minutes / settings.gap_minutes
                text = f"silent for {minutes} min (usually {f['baseline']:.0f} events/min)"
                rows.append(row(t0, f, kind, name, raw, text))
    return rows


def periodic_members(starts: np.ndarray) -> tuple[np.ndarray, int]:
    """Mask of the episodes with a partner one period away, and that period in minutes.

    ``starts`` are sorted start minutes. The period is the smallest one (at least ``MIN_PERIOD``)
    that covers the most episodes; an episode is covered when another starts one period before
    or after it, within ``PERIOD_TOLERANCE``. The mask is empty (all False) if no period covers
    at least ``MIN_REPEATS`` episodes and half of them.
    """
    best, best_period = np.zeros(len(starts), dtype=bool), 0
    for period in np.unique(starts[None, :] - starts[:, None]):
        if period < MIN_PERIOD:
            continue
        tol = max(1.0, PERIOD_TOLERANCE * period)
        ahead = np.searchsorted(starts, starts + period - tol) < np.searchsorted(
            starts, starts + period + tol, side="right"
        )
        covered = ahead.copy()
        for i in np.flatnonzero(ahead):  # the partner is covered too
            covered[np.searchsorted(starts, starts[i] + period - tol)] = True
        if covered.sum() > best.sum():
            best, best_period = covered, int(period)
    if best.sum() < max(MIN_REPEATS, len(starts) / 2):
        return np.zeros(len(starts), dtype=bool), 0
    return best, best_period


def merge_periodic(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace the periodic episodes of one entity by their strongest one, noting the period."""
    if len(found) < MIN_REPEATS:
        return found
    found = sorted(found, key=lambda r: r["bin"])
    first = found[0]["bin"]
    starts = np.array([(r["bin"] - first).total_seconds() / WINDOW_S for r in found])
    members, period = periodic_members(starts)
    if not members.any():
        return found
    strongest = max((r for r, m in zip(found, members, strict=True) if m), key=lambda r: r["raw"])
    note = f"; {int(members.sum())} episodes in all, repeating about every {period} min"
    merged = {**strongest, "evidence": strongest["evidence"] + note}
    rest = [r for r, m in zip(found, members, strict=True) if not m]
    return sorted([*rest, merged], key=lambda r: r["bin"])


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


def drop_text(f: dict[str, Any]) -> str:
    minutes = f["last"] - f["first"] + 1
    return (
        f"drop: trough {f['peak']:.0f} events/min vs baseline {f['baseline']:.0f}, "
        f"{f['excess']:.0f} fewer events over {minutes} min"
    )


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
    # the algorithm lives in analyses.temporal (find_bursts, find_drops, find_gaps): bump on change
    version = "2"
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
