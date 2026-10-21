"""Structural anomalies, part 1: the PCA residual (SPE) of a window above its control limit.

This is the PCA half of the *structural* type of ``PLAN.md`` module 6; the Isolation Forest half
belongs in a sibling module with ``type = "structural"`` (issue 6), so both share one type in
the page. It reads ``pca.load_spe`` and ``pca.load_residuals`` of the per-service and per-host
1-minute log1p fits and flags windows with ``ratio = spe / limit`` of at least ``THRESHOLD``.
The limit is Gaussian theory on heavy-tailed counts, so many windows exceed it; the score keeps
the ranking (``ratio`` is the raw score) and only the highest ``MAX_PER_GRID`` windows per
grouping are kept. The evidence names the templates that contribute most to the window's
residual.
"""

from __future__ import annotations

import polars as pl

from logdash.analyses import features, pca
from logdash.analyses.templates import short
from logdash.cache.store import CacheStore
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
SCALING = "log1p"
THRESHOLD = 1.0  # SPE at the control limit
MAX_PER_GRID = 5000  # highest ratios kept per grouping, so that neither crowds out the other
TEXT_CHARS = 50
GRIDS = (("service", "service"), ("host", "host"))  # (PCA grouping, entity kind)


def evidence(store: CacheStore, flagged: pl.DataFrame, group: str) -> pl.DataFrame:
    """``row, evidence``: ratio to the limit and the top residual templates of each window."""
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
            "SPE {}x limit; residual: {}",
            pl.col("ratio").round(1),
            pl.col("part").fill_null("none"),
        ).alias("evidence"),
    )


@register
class StructuralPca:
    key = "structural_pca"
    type = "structural"
    title = "PCA residual (SPE)"
    version = "1"
    depends_on: tuple[str, ...] = ("pca", "templates")
    threshold = THRESHOLD

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        parts = []
        for group, kind in GRIDS:
            spe = pca.load_spe(ctx.store, group, WINDOW_S, SCALING)
            flagged = spe.filter(pl.col("ratio") >= THRESHOLD)
            total = flagged.height
            flagged = flagged.sort("ratio", descending=True).head(MAX_PER_GRID)
            ctx.log(
                f"structural_pca: {group}: {total} of {spe.height} windows above the limit, "
                f"keeping {flagged.height}"
            )
            parts.append(
                flagged.join(evidence(ctx.store, flagged, group), on="row")
                .with_columns(
                    pl.lit(WINDOW_S, dtype=pl.Int64).alias("window_s"),
                    pl.lit(kind).alias("kind"),
                    pl.col("ratio").alias("raw"),
                )
                .select("bin", "window_s", "kind", "entity", "raw", "evidence")
            )
        return pl.concat(parts)
