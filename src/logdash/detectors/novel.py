"""Novel and rare templates: log messages that the data has hardly or never shown before.

This is the *novel* type of ``PLAN.md`` module 6. It reads the template table and the per-minute
template counts of the templates stage and has two detectors.

``novel_template``: one row per template first seen in the last ``novel_fraction`` of the span
(the rule of the novel list on the templates page), at its first minute. A steady template with
``b`` active minutes has its first event after the fraction ``u`` of the span with probability
``(1 - u) ** b``, so the raw score is ``-b * log2(1 - u)`` bits. ``b`` counts distinct minutes,
not events, so a one-minute burst of many lines is not inflated; a template that was active for
many minutes and still appeared only late is stronger evidence than one that fired once. The
threshold ``log2(1 / novel_fraction)`` is exactly the gate for ``b = 1``.

``rare_template``: one row per (template, minute, service) of a template with at most
``rare_threshold`` events in all (the rare list of the page). The raw score is
``(rare_threshold + 0.5) / count``, which is at least 1 exactly for the rare templates (no tuned
constant) and larger for the rarer ones. The first window of a template that ``novel_template``
reports is left out, so one event never gives two rows.

The entity of both is the service, so the global service filter and the explorer link apply.
Timestamps are UTC, naive, as in the template tables.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from logdash.analyses import templates as tpl
from logdash.analyses.templates import novel_templates, rare_templates, short
from logdash.config import DEFAULT_NOVEL_FRACTION, Settings
from logdash.detectors.base import DetectContext, register

WINDOW_S = 60
CLUSTER_MINUTES = 10  # templates first seen this close are one story in the evidence
MAX_U = 1 - 1e-9  # keeps log2(1 - u) finite for a template first seen at the very end

SCHEMA: dict[str, Any] = {
    "bin": pl.Datetime("us"),
    "window_s": pl.Int64,
    "kind": pl.String,
    "entity": pl.String,
    "raw": pl.Float64,
    "evidence": pl.String,
    "template_id": pl.Int64,
}


def read_span(ctx: DetectContext) -> tuple[datetime, datetime]:
    meta = ctx.store.read_json(tpl.KEY, "meta.json")
    return datetime.fromisoformat(meta["ts_min"]), datetime.fromisoformat(meta["ts_max"])


def novel_ids(templates: pl.DataFrame, lo: datetime, hi: datetime, fraction: float) -> pl.DataFrame:
    """Novel templates (page rule) with ``u``, the fraction of the span before their first line."""
    span = (hi - lo).total_seconds()
    if templates.is_empty() or span <= 0:
        return templates.head(0).with_columns(u=pl.lit(0.0))
    return novel_templates(templates, lo, hi, fraction).with_columns(
        u=((pl.col("first_seen") - pl.lit(lo)).dt.total_microseconds() / (span * 1e6)).clip(
            upper_bound=MAX_U
        )
    )


def first_windows(counts: pl.DataFrame) -> pl.DataFrame:
    """Per template: first minute, active minutes and the service busiest in the first minute."""
    first = counts.group_by("template_id").agg(
        bin=pl.col("bin").min(), minutes=pl.col("bin").n_unique()
    )
    busiest = (
        counts.join(first.select("template_id", "bin"), on=["template_id", "bin"])
        .group_by("template_id", "program")
        .agg(pl.col("n").sum())
        .sort(["template_id", "n", "program"], descending=[False, True, False])
        .group_by("template_id", maintain_order=True)
        .agg(entity=pl.col("program").first(), first_n=pl.col("n").first())
    )
    return first.join(busiest, on="template_id")


def novel_rows(
    templates: pl.DataFrame, counts: pl.DataFrame, lo: datetime, hi: datetime, fraction: float
) -> pl.DataFrame:
    novel = novel_ids(templates, lo, hi, fraction)
    joined = novel.join(first_windows(counts), on="template_id")  # no counts: skipped
    if joined.is_empty():
        return pl.DataFrame(schema=SCHEMA)
    near = timedelta(minutes=CLUSTER_MINUTES)
    firsts = joined["first_seen"].to_list()
    rows = []
    for r in joined.iter_rows(named=True):
        others = sum(1 for f in firsts if abs(f - r["first_seen"]) <= near) - 1
        p = (1 - r["u"]) ** r["minutes"]
        raw = -r["minutes"] * math.log2(1 - r["u"])
        rows.append(
            {
                "bin": r["bin"],
                "window_s": WINDOW_S,
                "kind": "service",
                "entity": r["entity"],
                "raw": raw,
                "evidence": (
                    f"novel template, first seen {r['first_seen']:%H:%M:%S} "
                    f"({r['u']:.1%} into the span): {short(r['template'], 120)}; "
                    f"{r['count']} events in {r['minutes']} min, p = {p:.2g} for a steady "
                    f"template; {others} other new templates within {CLUSTER_MINUTES} min"
                ),
                "template_id": r["template_id"],
            }
        )
    return pl.DataFrame(rows, schema=SCHEMA)


def rare_rows(
    templates: pl.DataFrame, counts: pl.DataFrame, threshold: int, novel: pl.DataFrame
) -> pl.DataFrame:
    rare = rare_templates(templates, threshold)
    if rare.is_empty():
        return pl.DataFrame(schema=SCHEMA)
    first = counts.group_by("template_id").agg(first_bin=pl.col("bin").min())
    skip = novel.select("template_id").join(first, on="template_id").with_columns(skip=True)
    found = (
        rare.select("template_id", "template", "count")
        .join(counts, on="template_id")
        .join(skip, on="template_id", how="left")
        .filter(~(pl.col("skip").fill_null(False) & (pl.col("bin") == pl.col("first_bin"))))
    )
    return found.select(
        pl.col("bin"),
        pl.lit(WINDOW_S).alias("window_s"),
        pl.lit("service").alias("kind"),
        pl.col("program").alias("entity"),
        ((threshold + 0.5) / pl.col("count")).alias("raw"),
        pl.format(
            "rare template, {} events in all ({} here): {}",
            pl.col("count"),
            pl.col("n"),
            pl.col("template").map_elements(lambda t: short(t, 120), return_dtype=pl.String),
        ).alias("evidence"),
        pl.col("template_id"),
    ).cast(pl.Schema(SCHEMA))


@register
class NovelTemplate:
    key = "novel_template"
    type = "novel"
    title = "Novel template"
    version = "1"
    setting_names: tuple[str, ...] = ("novel_fraction",)
    depends_on: tuple[str, ...] = (tpl.KEY,)
    threshold = math.log2(1 / DEFAULT_NOVEL_FRACTION)

    def threshold_for(self, settings: Settings) -> float:
        return math.log2(1 / settings.novel_fraction)

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        templates = ctx.store.read_parquet(tpl.KEY, "templates.parquet")
        counts = ctx.store.read_parquet(tpl.KEY, "template_counts.parquet")
        lo, hi = read_span(ctx)
        rows = novel_rows(templates, counts, lo, hi, ctx.settings.novel_fraction)
        ctx.log(f"novel_template: {rows.height} novel templates")
        return rows


@register
class RareTemplate:
    key = "rare_template"
    type = "novel"
    title = "Rare template"
    version = "1"
    setting_names: tuple[str, ...] = ("rare_threshold", "novel_fraction")
    depends_on: tuple[str, ...] = (tpl.KEY,)
    threshold = 1.0  # raw = (rare_threshold + 0.5) / count: at least 1 for a rare template

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        templates = ctx.store.read_parquet(tpl.KEY, "templates.parquet")
        counts = ctx.store.read_parquet(tpl.KEY, "template_counts.parquet")
        lo, hi = read_span(ctx)
        novel = novel_ids(templates, lo, hi, ctx.settings.novel_fraction)
        rows = rare_rows(templates, counts, ctx.settings.rare_threshold, novel)
        ctx.log(f"rare_template: {rows.height} windows")
        return rows
