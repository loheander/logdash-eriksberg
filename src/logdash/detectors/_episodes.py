"""Helpers shared by the structural detectors: per-entity levels, fleet filter, episodes.

A structural score of one window (SPE ratio, isolation score) has no absolute meaning on heavy
tailed counts, so a window is flagged against its own entity's history (``entity_level``), a
minute in which a large share of the active entities is flagged is a fleet-wide event rather
than an entity's anomaly (``drop_fleet_wide``), and consecutive flagged windows are one
incident (``merge_episodes``). Not a detector: ``load_builtin`` skips modules starting with ``_``.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import polars as pl

LEVEL_QUANTILE = 0.995  # an entity flags at most about this share of its windows (0.5 %)
FLEET_SHARE = 0.25  # a minute with more of the active entities flagged is fleet-wide
MIN_FLEET = 4  # active entities needed before a minute can count as fleet-wide
MAX_GAP = 5  # minutes between flagged windows that still belong to one episode
MAX_FLEET_MINUTES = 50  # fleet minutes listed in the detector's meta (the count is complete)


class FleetDrop(NamedTuple):
    """Windows dropped as fleet-wide, the minutes they fall in and the worst of those minutes."""

    windows: int
    minutes: int
    worst: list[dict[str, Any]]  # minute and share flagged, highest share first


def entity_level(column: str, floor: float) -> pl.Expr:
    """Per-entity ``LEVEL_QUANTILE`` of ``column``, at least ``floor`` (a window function)."""
    return pl.col(column).quantile(LEVEL_QUANTILE, "linear").over("entity").clip(lower_bound=floor)


def drop_fleet_wide(flagged: pl.DataFrame, windows: pl.DataFrame) -> tuple[pl.DataFrame, FleetDrop]:
    """``flagged`` without the minutes where more than ``FLEET_SHARE`` of the active entities
    are flagged (``windows``: ``bin, entity`` of every window; active means it has a window)."""
    per_minute = (
        windows.group_by("bin")
        .agg(active=pl.col("entity").n_unique())
        .join(flagged.group_by("bin").agg(hit=pl.col("entity").n_unique()), on="bin")
        .with_columns(share=pl.col("hit") / pl.col("active"))
        .filter((pl.col("active") >= MIN_FLEET) & (pl.col("share") > FLEET_SHARE))
    )
    kept = flagged.join(per_minute.select("bin"), on="bin", how="anti")
    worst = [
        {"minute": str(b), "share": round(s, 3)}
        for b, s in per_minute.sort("share", "bin", descending=[True, False])
        .head(MAX_FLEET_MINUTES)
        .select("bin", "share")
        .iter_rows()
    ]
    return kept, FleetDrop(flagged.height - kept.height, per_minute.height, worst)


def merge_episodes(flagged: pl.DataFrame, step_s: int) -> pl.DataFrame:
    """One row per episode: flagged windows of an entity at most ``MAX_GAP`` windows apart.

    ``flagged`` has ``bin, entity, raw, row`` (``row`` identifies the window for the evidence).
    The result has ``bin`` (first window), ``entity``, ``minutes`` (windows from the first to
    the last), ``raw`` (the peak) and ``row`` (of the peak window), sorted by entity and time.
    """
    ordered = flagged.sort("entity", "bin")
    new = pl.col("bin").diff().over("entity").dt.total_seconds().fill_null(10**9) > (
        MAX_GAP * step_s
    )
    return (
        ordered.with_columns(episode=new.cum_sum())
        .group_by("episode", maintain_order=True)
        .agg(
            pl.col("entity").first(),
            pl.col("bin").min(),
            ((pl.col("bin").max() - pl.col("bin").min()).dt.total_seconds() // step_s + 1)
            .cast(pl.Int64)
            .alias("minutes"),
            pl.col("raw").max(),
            pl.col("row").sort_by("raw").last(),
        )
        .drop("episode")
    )
