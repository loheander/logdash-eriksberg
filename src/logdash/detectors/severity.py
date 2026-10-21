"""Severity bursts: a surge of WARNING or ERROR lines in one service.

This is the *severity* type of ``PLAN.md`` module 6. Only ``rate_60`` of the temporal stage has a
level (there is no host x level table), so the kind is ``service``. Per service two dense
1-minute series are built: WARNING, and ERROR plus CRITICAL (UNKNOWN is a missing level, not a
severity). ``find_bursts`` of ``analyses/temporal.py`` finds the runs above the rolling median
plus ``burst_k`` robust sigmas, with floors per level because ERROR is rare: WARNING needs 10
lines in the peak minute and 300 above the baseline over the episode (the volume defaults), ERROR
5 and 30. Fewer warnings is not a severity event, so there are no drops or gaps.

The robust z-score is meaningless when the baseline is (nearly) zero: the median and MAD are 0
and the scale floors at 1, so z is just the peak count. When the baseline median is below
``POISSON_BELOW`` events/min the score is therefore the Poisson tail instead: ``p = P(X >= peak)``
for a count with rate ``lambda``, the larger of the median and the series' mean outside the
episodes (at least ``MIN_RATE``), converted to the Gaussian-equivalent z with ``p = Q(z)``. A
busier baseline keeps the robust z. Either way ``raw`` is the z in the evidence and the threshold
is ``burst_k``, so a score of 6 means the same in both regimes.

An entity and level whose episodes recur at a fixed period (the one neutron-l3-agent template that
logs a duplicate iptables rule every 77 minutes is 88 % of all WARNING lines) get one row, found
with the volume detector's ``merge_periodic``. A merged row is known noise: its raw is demoted to
the threshold (the evidence keeps the strongest z), so it stays visible but cannot rank among
real bursts.

Fleet-wide events: this detector does not use the shared fleet filter
(``_episodes.drop_fleet_wide``), which removes the windows of a minute in which more than 25 % of
the active entities are flagged, because a structural score flags windows of every host in a fleet
event. A severity row is an episode that already needs a burst well above its own service's
baseline, so it is rare (see the real-data count in the README), and a surge in several services at
once is the incident to read: each service keeps its row. The meta records the largest number of
services with a burst starting in one minute, so that a fleet-wide surge would show.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, NamedTuple

import numpy as np
import polars as pl
from scipy import special, stats

from logdash.analyses import temporal
from logdash.analyses.temporal import MISSING, find_bursts
from logdash.config import DEFAULT_BURST_K, Settings
from logdash.detectors import volume
from logdash.detectors.base import DetectContext, register

WINDOW_S = volume.WINDOW_S
POISSON_BELOW = 10.0  # events/min: below this baseline median the z-score is a Poisson tail
MIN_RATE = 0.1  # events/min floor of the Poisson rate (a service with no lines still has a rate)


class Level(NamedTuple):
    """One severity series: the log levels summed and the floors a burst needs."""

    label: str
    levels: tuple[str, ...]
    min_peak: int
    min_excess: int


LEVELS = (
    Level("WARNING", ("WARNING",), temporal.MIN_BURST, temporal.MIN_EXCESS),
    Level("ERROR", ("ERROR", "CRITICAL"), 5, 30),
)


def poisson_z(peak: float, rate: float) -> tuple[float, float]:
    """``(z, p)``: the Gaussian-equivalent z of ``P(X >= peak)`` for ``X ~ Poisson(rate)``, and p.

    Computed in log space, so a tail far below ``1e-300`` still has a finite z. Where scipy's
    log survival function underflows (a peak hundreds of times the rate) the tail is bounded by
    its first term, ``pmf(peak) * (peak + 1) / (peak + 1 - rate)``, which is exact to a few
    percent there.
    """
    log_p = float(stats.poisson.logsf(peak - 1, rate))
    if not np.isfinite(log_p):
        log_p = float(stats.poisson.logpmf(peak, rate) + np.log((peak + 1) / (peak + 1 - rate)))
    return -float(special.ndtri_exp(log_p)), float(np.exp(log_p))


def outside_rate(x: np.ndarray, found: list[dict[str, Any]]) -> float:
    """Mean events/min of ``x`` outside the bursts in ``found`` (the smoothed baseline rate)."""
    keep = np.ones(len(x), dtype=bool)
    for f in found:
        keep[f["first"] : f["last"] + 1] = False
    return float(x[keep].mean()) if keep.any() else 0.0


def score(f: dict[str, Any], rate: float) -> tuple[float, str]:
    """``(raw, how)``: the z at the peak and the text that says where it comes from."""
    if f["baseline"] >= POISSON_BELOW:
        return f["score"], f"robust z={f['score']:.1f}"
    lam = max(f["baseline"], rate, MIN_RATE)
    z, p = poisson_z(f["peak"], lam)
    return z, f"Poisson P={p:.1e} at {lam:.2g}/min, z={z:.1f}"


def burst_rows(
    x: np.ndarray, level: Level, entity: str, t0: Any, settings: Settings
) -> list[dict[str, Any]]:
    """Burst rows of one entity's series, periodic repeats merged and demoted."""
    found = find_bursts(x, settings.burst_k, level.min_peak, level.min_excess)
    if not found:
        return []
    rate = outside_rate(x, found)
    rows = []
    for f in found:
        raw, how = score(f, rate)
        minutes = f["last"] - f["first"] + 1
        text = (
            f"{level.label} burst: peak {f['peak']:.0f}/min vs baseline {f['baseline']:.0f}/min "
            f"({how}), {f['excess']:.0f} extra over {minutes} min"
        )
        rows.append(volume.row(t0, f, "service", entity, raw, text))
    merged = volume.merge_periodic(rows)
    kept = {id(r) for r in rows}  # a merged row is a new dict
    note = "; ranked at the threshold as periodic noise, z is the strongest episode's"
    return [
        r if id(r) in kept else {**r, "raw": settings.burst_k, "evidence": r["evidence"] + note}
        for r in merged
    ]


@register
class Severity:
    key = "severity"
    type = "severity"
    title = "Severity burst"
    # the algorithm lives in analyses.temporal (find_bursts) and volume (merge_periodic): their
    # constants and the ones above are part of the version, so editing any of them invalidates
    version = (
        f"1:{WINDOW_S}:{POISSON_BELOW}:{MIN_RATE}:{LEVELS}:{temporal.ROLL_WINDOW}:"
        f"{temporal.MERGE_BINS}:{volume.MIN_REPEATS}:{volume.PERIOD_TOLERANCE}:{volume.MIN_PERIOD}"
    )
    setting_names: tuple[str, ...] = ("burst_k",)
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
        if rate.is_empty():
            return pl.DataFrame(schema=schema)
        t0, t1 = temporal.span(rate["bin"])
        length = int((t1 - t0).total_seconds() // WINDOW_S) + 1
        rows: list[dict[str, Any]] = []
        for level in LEVELS:
            part = rate.filter(pl.col("level").is_in(level.levels))
            by_service = part.group_by("bin", "program").agg(pl.col("n").sum())
            for (program,), group in by_service.group_by(["program"], maintain_order=True):
                if program == MISSING:  # malformed rows, not a service
                    continue
                x = temporal.dense_series(group["bin"], group["n"], t0, WINDOW_S, length)
                rows += burst_rows(x, level, str(program), t0, ctx.settings)
        concurrent = max(Counter(r["bin"] for r in rows).values(), default=0)
        ctx.meta["episodes"] = len(rows)
        ctx.meta["max_services_starting_in_one_minute"] = concurrent
        ctx.log(f"severity: {len(rows)} episodes")
        return pl.DataFrame(rows, schema=schema)
