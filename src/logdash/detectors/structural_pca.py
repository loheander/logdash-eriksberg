"""Structural anomalies, part 1: the PCA residual (SPE) of a window against its own entity.

This is the PCA half of the *structural* type of ``PLAN.md`` module 6; the Isolation Forest half
is ``structural_iforest.py``. It reads ``pca.load_spe`` and ``pca.load_residuals`` of the
per-service and per-host 1-minute log1p fits. ``ratio = spe / limit`` at 1 is not a usable
threshold: the limit is Gaussian theory on heavy-tailed counts, so about a third of the host
windows exceed it, and some hosts sit far above it all the time. Per service or host:

1. a window is flagged when its ratio exceeds the entity's own 99.5 % quantile (and the limit,
   1): the false-positive rate is a stated 0.5 % of an entity's windows and a chronically high
   entity cannot flag itself;
2. a minute in which more than 25 % of the active entities (those with a window) are flagged is
   fleet-wide, not specific to an entity: its windows are dropped and the minute is one row of
   kind ``fleet`` (counted, with the worst minutes, in the meta);
3. flagged windows at most five minutes apart are one episode.

``raw = peak ratio / the entity's level``, so the threshold is 1. The evidence names the
templates that contribute most to the residual of the episode's peak window.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from logdash.analyses import features, pca
from logdash.analyses.templates import short
from logdash.cache.store import CacheStore
from logdash.detectors import _episodes
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
SCALING = "log1p"
THRESHOLD = 1.0  # raw is the ratio to the entity's own level, so 1 is "at the level"
MIN_LEVEL = 1.0  # an entity's level is at least the control limit
TEXT_CHARS = 50
GRIDS = (("service", "service"), ("host", "host"))  # (PCA grouping, entity kind)


def evidence(store: CacheStore, flagged: pl.DataFrame, group: str) -> pl.DataFrame:
    """``row, evidence``: ratio to the limit, episode length and top residual templates.

    ``flagged`` has ``row`` (the peak window), ``ratio``, ``level`` and ``minutes``.
    """
    texts = pl.DataFrame(
        {
            "template_id": list(features.template_texts(store)),
            "text": [short(t, TEXT_CHARS) for t in features.template_texts(store).values()],
        },
        schema={"template_id": pl.Int64, "text": pl.String},
    )
    parts = (
        pca.load_residuals(store, group, WINDOW_S, SCALING)
        .join(flagged.select("row"), on="row")
        .join(texts, on="template_id", how="left")
        .sort("row", "rank")
        .with_columns(
            pl.format(
                "T{} {}% {}",
                "template_id",
                (pl.col("share") * 100).round(0).cast(pl.Int64),
                pl.col("text").fill_null(""),
            ).alias("part")
        )
        .group_by("row", maintain_order=True)
        .agg(pl.col("part").str.join("; "))
    )
    return flagged.join(parts, on="row", how="left").select(
        "row",
        pl.format(
            "SPE {}x limit (entity level {}x), {} min; residual: {}",
            pl.col("ratio").round(1),
            pl.col("level").round(1),
            pl.col("minutes"),
            pl.col("part").fill_null("none"),
        ).alias("evidence"),
    )


@register
class StructuralPca:
    key = "structural_pca"
    type = "structural"
    title = "PCA residual (SPE)"
    version = f"3:{WINDOW_S}:{SCALING}:{THRESHOLD}:{MIN_LEVEL}"  # constants change the output
    setting_names: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ("pca", "templates")
    threshold = THRESHOLD

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        parts = []
        info: dict[str, Any] = {}
        for group, kind in GRIDS:
            spe = pca.load_spe(ctx.store, group, WINDOW_S, SCALING).with_columns(
                level=_episodes.entity_level("ratio", MIN_LEVEL)
            )
            flagged = spe.filter(pl.col("ratio") > pl.col("level")).with_columns(
                raw=pl.col("ratio") / pl.col("level")
            )
            kept, fleet = _episodes.drop_fleet_wide(flagged, spe, WINDOW_S, f"{kind}s")
            episodes = _episodes.merge_episodes(
                kept.select("bin", "entity", "raw", "row"), WINDOW_S
            )
            ctx.log(
                f"structural_pca: {group}: {flagged.height} of {spe.height} windows flagged, "
                f"{fleet.windows} fleet-wide in {fleet.minutes} minutes, "
                f"{episodes.height} episodes"
            )
            info[group] = {
                "windows": spe.height,
                "flagged": flagged.height,
                "fleet_windows": fleet.windows,
                "fleet_minutes": fleet.minutes,
                "worst_fleet_minutes": fleet.worst,
                "episodes": episodes.height,
            }
            peaks = episodes.join(spe.select("row", "ratio", "level"), on="row")
            parts.append(
                peaks.join(evidence(ctx.store, peaks, group), on="row")
                .with_columns(
                    (pl.col("minutes") * WINDOW_S).alias("window_s"), pl.lit(kind).alias("kind")
                )
                .select("bin", "window_s", "kind", "entity", "raw", "evidence")
            )
            parts.append(fleet.rows)
        ctx.meta.update(info)
        return pl.concat(parts)
