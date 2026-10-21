"""Severity bursts: a surge of WARNING or ERROR lines in one service.

This is the *severity* type of ``PLAN.md`` module 6. Only ``rate_60`` of the temporal stage has a
level (there is no host x level table), so the kind is ``service``. Per service two dense
1-minute series are built: WARNING, and ERROR plus CRITICAL (UNKNOWN is a missing level, not a
severity). ``find_bursts`` of ``analyses/temporal.py`` finds the runs above the rolling median
plus ``burst_k`` robust sigmas. Fewer warnings is not a severity event, so there are no drops
or gaps.

Gates. A run needs ``x > median + burst_k * scale`` with the scale at least 1, so at a zero
baseline the peak minute needs 7 lines, at a median of 2 about 14, at 5 about 25: for the peak
minute that statistical gate binds, so neither level has a floor on it (ERROR had one of 5, which
the gate always exceeded). The floor on the excess over the whole incident is a chosen incident
size, not derived from a rate, and it binds: WARNING keeps the volume detector's 300 (the bulk
level; 118 episodes on the real data without it, 23 with it), ERROR 30 (about 2 % of the 1,244
ERROR lines of the day; it removes an 8-line minute and a precursor of an incident on the real
data and 14 ERROR lines inside the synthetic volume spike, which the tail gate alone would keep).
Finally the score must reach ``burst_k``.

One scale for the score. The robust z is meaningless when the baseline is (nearly) zero and it
grows linearly with the peak, so every incident is scored by the same tail instead:
``p = P(X >= peak)`` of a count with the mean ``lambda`` (the larger of the baseline median, the
series' mean outside the incidents, and ``MIN_RATE``), converted to the Gaussian-equivalent z with
``p = Q(z)``. Counts of a busy service are over-dispersed (the rolling MAD is far above
``sqrt(lambda)``), so then the tail is the negative binomial with the same mean and the variance of
the robust scale; at zero or low baselines the variance is Poisson's. The z is monotone in the
peak, so rankings across baselines are comparable. ``raw`` is that z, the threshold ``burst_k``.

Incidents. A service that fails for an hour produces bursts separated by quiet minutes (7 ERROR
episodes of barbican-api in 2 hours on the real data). Episodes of one service and level less
than ``MERGE_GAP`` minutes apart are one incident, one row: the span, the number of bursts and
the strongest minute are in the evidence. The baseline of an incident is the rolling median of the
series with the incident's minutes interpolated away, so the incident cannot raise its own
baseline.

An entity and level whose episodes recur at a fixed period (the one neutron-l3-agent template that
logs a duplicate iptables rule every 77 minutes is 88 % of all WARNING lines) get one row, found
with the volume detector's ``merge_periodic`` before incidents are merged (a period under
``MERGE_GAP`` is not one incident). A merged row is known noise: its raw is demoted to
the threshold (the evidence keeps the strongest z), so it stays visible but cannot rank among
real bursts.

Fleet-wide events: this detector does not use the shared fleet filter
(``_episodes.drop_fleet_wide``), which removes the windows of a minute in which more than 25 % of
the active entities are flagged, because a structural score flags windows of every host in a fleet
event. A severity row is an incident that already needs a burst well above its own service's
baseline, so it is rare (see the real-data count in the README), and a surge in several services at
once is the incident to read: each service keeps its row. The meta records the largest number of
services with a burst starting in one minute, so that a fleet-wide surge would show.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any, NamedTuple

import numpy as np
import polars as pl
from scipy import special, stats

from logdash.analyses import temporal
from logdash.analyses.temporal import MISSING, find_bursts, rolling_baseline
from logdash.config import DEFAULT_BURST_K, Settings
from logdash.detectors import volume
from logdash.detectors.base import DetectContext, register

WINDOW_S = volume.WINDOW_S
MIN_RATE = 0.1  # events/min floor of the tail's rate (a service with no lines still has a rate)
MERGE_GAP = temporal.ROLL_WINDOW - 1  # minutes: episodes closer than this are one incident


class Level(NamedTuple):
    """One severity series: the log levels summed and the floors a burst needs."""

    label: str
    levels: tuple[str, ...]
    min_peak: int
    min_excess: int


LEVELS = (
    Level("WARNING", ("WARNING",), temporal.MIN_BURST, temporal.MIN_EXCESS),
    Level("ERROR", ("ERROR", "CRITICAL"), 1, 30),  # no peak floor: the tail gate binds
)


def tail_z(peak: float, rate: float, variance: float | None = None) -> tuple[float, float]:
    """``(z, log_p)``: the Gaussian-equivalent z of ``P(X >= peak)`` and the natural log of p.

    ``X`` is Poisson with mean ``rate``, or negative binomial with that mean and ``variance``
    when the variance is larger. Computed in log space, so a tail far below ``1e-300`` still has
    a finite z. Where scipy's log survival function underflows (a peak hundreds of times the
    rate) the tail is bounded by its first term over a geometric series,
    ``pmf(peak) / (1 - ratio)`` with ``ratio`` the largest ratio of consecutive terms, exact to a
    few percent there.
    """
    if variance is not None and variance > rate:
        size = rate * rate / (variance - rate)
        prob = size / (size + rate)
        dist: Any = stats.nbinom(size, prob)
        ratio = max((peak + size) / (peak + 1) * (1 - prob), 1 - prob)
    else:
        dist = stats.poisson(rate)
        ratio = rate / (peak + 1)
    log_p = float(dist.logsf(peak - 1))
    if not np.isfinite(log_p):
        log_p = float(dist.logpmf(peak) - np.log1p(-min(ratio, 1 - 1e-9)))
    return -float(special.ndtri_exp(log_p)), log_p


def p_text(log_p: float) -> str:
    """The p of ``tail_z`` for the evidence; a p below float range as a power of ten."""
    log10 = log_p / np.log(10)
    return f"={np.exp(log_p):.1e}" if log10 > -300 else f"≈1e{log10:.0f}"


def outside_rate(x: np.ndarray, found: list[dict[str, Any]]) -> float:
    """Mean events/min of ``x`` outside the bursts in ``found`` (the smoothed baseline rate)."""
    keep = np.ones(len(x), dtype=bool)
    for f in found:
        keep[f["first"] : f["last"] + 1] = False
    return float(x[keep].mean()) if keep.any() else 0.0


def score(peak: float, baseline: float, scale: float, rate: float) -> tuple[float, str]:
    """``(raw, how)``: the tail z of the peak and the text that says where it comes from."""
    lam = max(baseline, rate, MIN_RATE)
    # the scale is floored by sqrt(median) and 1: only a larger one is real over-dispersion
    spread = scale * scale if scale * scale > max(baseline, 1.0) * (1 + 1e-9) else None
    z, log_p = tail_z(peak, lam, spread)
    kind = "negative binomial" if spread is not None and spread > lam else "Poisson"
    return z, f"{kind} P{p_text(log_p)} at {lam:.2g}/min, z={z:.1f}"


def incidents(found: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Episodes grouped into incidents: gaps of fewer than ``MERGE_GAP`` minutes."""
    groups: list[list[dict[str, Any]]] = []
    for f in sorted(found, key=lambda f: f["first"]):
        if groups and f["first"] - groups[-1][-1]["last"] - 1 < MERGE_GAP:
            groups[-1].append(f)
        else:
            groups.append([f])
    return groups


def without(x: np.ndarray, spans: list[dict[str, Any]]) -> np.ndarray:
    """``x`` with the minutes of ``spans`` interpolated from the minutes around them."""
    keep = np.ones(len(x), dtype=bool)
    for s in spans:
        keep[s["first"] : s["last"] + 1] = False
    if keep.all() or not keep.any():
        return x
    index = np.arange(len(x))
    return np.interp(index, index[keep], x[keep])


def incident_row(
    x: np.ndarray, group: list[dict[str, Any]], base: tuple[np.ndarray, np.ndarray], rate: float
) -> dict[str, Any]:
    """The incident of ``group`` scored against ``base``, the baseline without the incidents."""
    median, scale = base
    first, last = group[0]["first"], group[-1]["last"]
    peak = first + int(np.argmax(x[first : last + 1]))
    excess = sum(
        float(x[f["first"] : f["last"] + 1].sum() - median[f["first"] : f["last"] + 1].sum())
        for f in group
    )
    raw, how = score(float(x[peak]), float(median[peak]), float(scale[peak]), rate)
    return {
        "first": first,
        "last": last,
        "peak": float(x[peak]),
        "baseline": float(median[peak]),
        "raw": raw,
        "how": how,
        "excess": excess,
        "bursts": len(group),
    }


def episode_text(level: Level, f: dict[str, Any], how: str, when: str) -> str:
    return (
        f"{level.label} burst: peak {f['peak']:.0f}/min vs baseline {f['baseline']:.0f}/min "
        f"({how}), {f['excess']:.0f} extra {when}"
    )


def burst_rows(
    x: np.ndarray, level: Level, entity: str, t0: Any, settings: Settings
) -> list[dict[str, Any]]:
    """Incident rows of one entity's series: periodic repeats merged and demoted."""
    found = find_bursts(x, settings.burst_k, level.min_peak, level.min_excess)
    if not found:
        return []
    # Periodic episodes first (scored one by one), so that a period shorter than MERGE_GAP is
    # not mistaken for one incident.
    rate = outside_rate(x, found)
    rows, episode = [], {}
    for f in found:
        scale = (f["peak"] - f["baseline"]) / f["score"]
        raw, how = score(f["peak"], f["baseline"], scale, rate)
        minutes = f["last"] - f["first"] + 1
        row = volume.row(
            t0, f, "service", entity, raw, episode_text(level, f, how, f"over {minutes} min")
        )
        rows.append(row)
        episode[id(row)] = f
    merged = volume.merge_periodic(rows)
    note = "; ranked at the threshold as periodic noise, z is the strongest episode's"
    out = [
        {**r, "raw": settings.burst_k, "evidence": r["evidence"] + note}
        for r in merged
        if id(r) not in episode  # a merged row is a new dict
    ]
    groups = incidents([episode[id(r)] for r in merged if id(r) in episode])
    spans = [{"first": g[0]["first"], "last": g[-1]["last"]} for g in groups]
    rate = outside_rate(x, found + spans)
    base = rolling_baseline(without(x, spans))
    for g in groups:
        inc = incident_row(x, g, base, rate)
        if inc["excess"] < level.min_excess:
            continue
        minutes = inc["last"] - inc["first"] + 1
        when = f"over {minutes} min"
        if inc["bursts"] > 1:
            start, end = (t0 + timedelta(minutes=inc[k]) for k in ("first", "last"))
            when = f"in {inc['bursts']} bursts over {minutes} min ({start:%H:%M} to {end:%H:%M})"
        out.append(
            volume.row(
                t0, inc, "service", entity, inc["raw"], episode_text(level, inc, inc["how"], when)
            )
        )
    return sorted(out, key=lambda r: r["bin"])


@register
class Severity:
    key = "severity"
    type = "severity"
    title = "Severity burst"
    # the algorithm lives in analyses.temporal (find_bursts) and volume (merge_periodic): their
    # constants and the ones above are part of the version, so editing any of them invalidates
    version = (
        f"2:{WINDOW_S}:{MIN_RATE}:{MERGE_GAP}:{LEVELS}:{temporal.ROLL_WINDOW}:"
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
        starting = defaultdict(set)
        for r in rows:
            starting[r["bin"]].add(r["entity"])
        concurrent = max(map(len, starting.values()), default=0)
        ctx.meta["episodes"] = len(rows)
        ctx.meta["max_services_starting_in_one_minute"] = concurrent
        ctx.log(f"severity: {len(rows)} episodes")
        return pl.DataFrame(rows, schema=schema)
