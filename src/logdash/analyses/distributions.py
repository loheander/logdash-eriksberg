"""Module 1: distributions (categorical counts and log-binned histograms).

Everything is aggregated in DuckDB at build time into small tables. The page only
filters those tables (level/service/host) and draws them.
"""

from __future__ import annotations

from typing import Any

import dash_mantine_components as dmc
import duckdb
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component

from logdash.analyses.base import ComputeContext, register
from logdash.app import figures, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, filter_frame, from_dict
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore
from logdash.ingest.normalize import LEVELS

KEY = "distributions"
BINS_PER_DECADE = 10
TOP_N = 15
MISSING = "(missing)"
SERVICE_LINES = 8

# dimension id -> (events column, card title)
DIMENSIONS = {
    "level": ("level", "Log level"),
    "program": ("program", "Program"),
    "host": ("host", "Host"),
    "logger": ("logger", "Logger"),
    "module": ("python_module", "Python module"),
    "tenant": ("tenant_id", "Tenant"),
}
HISTOGRAMS = {
    "payload": "Payload length (characters)",
    "lines": "Log lines per request",
    "duration": "Request duration (seconds, first to last line)",
    "interarrival": "Inter-arrival time per service (seconds)",
}
GRAPH_IDS = [f"dist-{name}" for name in (*DIMENSIONS, *HISTOGRAMS)]
FILES = {
    "categories": "categories.parquet",
    "payload": "payload_length.parquet",
    "lines": "lines_per_request.parquet",
    "duration": "request_duration.parquet",
    "interarrival": "interarrival.parquet",
}


def log_bin_sql(expr: str) -> str:
    """Log-spaced bin index (``BINS_PER_DECADE`` per decade); NULL for values <= 0."""
    return (
        f"CASE WHEN {expr} > 0 THEN CAST(floor(log10({expr}) * {BINS_PER_DECADE}) AS INTEGER) END"
    )


def _label(col: str) -> str:
    return f"coalesce({col}, '{MISSING}')"


def categories_sql() -> str:
    """Counts per (dimension, value, level, program); small, so host/level/service filter."""
    selects = [
        f"SELECT '{dim}' AS dim, {_label(col)} AS value, level, {_label('program')} AS program, "
        "count(*) AS n FROM events GROUP BY ALL"
        for dim, (col, _) in DIMENSIONS.items()
    ]
    return " UNION ALL ".join(selects)


QUERIES = {
    "categories": categories_sql(),
    "payload": f"""
        SELECT {log_bin_sql("length(payload)")} AS bin, level, {_label("program")} AS program,
               count(*) AS n
        FROM events WHERE payload IS NOT NULL GROUP BY ALL""",
    "lines": f"""
        SELECT {log_bin_sql("c")} AS bin, count(*) AS n
        FROM (SELECT count(*) AS c FROM events WHERE request_id IS NOT NULL GROUP BY request_id)
        GROUP BY ALL""",
    "duration": f"""
        SELECT {log_bin_sql("d")} AS bin, count(*) AS n
        FROM (SELECT epoch_ms(max(ts)) / 1000.0 - epoch_ms(min(ts)) / 1000.0 AS d
              FROM events WHERE request_id IS NOT NULL GROUP BY request_id)
        GROUP BY ALL""",
    "interarrival": f"""
        SELECT program, {log_bin_sql("gap")} AS bin, count(*) AS n
        FROM (SELECT {_label("program")} AS program,
                     (epoch_ms(ts) - lag(epoch_ms(ts)) OVER (PARTITION BY program ORDER BY ts))
                         / 1000.0 AS gap
              FROM events)
        WHERE gap IS NOT NULL GROUP BY ALL""",
}


def compute_tables(con: duckdb.DuckDBPyConnection) -> dict[str, pl.DataFrame]:
    """Run every aggregate query; one DuckDB pass each, results are small."""
    return {name: con.execute(sql).pl() for name, sql in QUERIES.items()}


def bin_center(bin_index: int) -> float:
    return 10 ** ((bin_index + 0.5) / BINS_PER_DECADE)


def bin_range(bin_index: int) -> tuple[float, float]:
    return 10 ** (bin_index / BINS_PER_DECADE), 10 ** ((bin_index + 1) / BINS_PER_DECADE)


def category_counts(
    table: pl.DataFrame, dim: str, filters: Filters, top: int = TOP_N
) -> pl.DataFrame:
    """``(value, n)`` for one dimension after filtering; top-N plus "other" except for levels."""
    df = table.filter(pl.col("dim") == dim)
    df = filter_frame(df, filters, level="level", service="program")
    if dim == "host":
        df = filter_frame(df, filters, host="value")
    totals = df.group_by("value").agg(pl.col("n").sum())
    if dim == "level":
        order = {name: i for i, name in enumerate(LEVELS)}
        return totals.sort(pl.col("value").replace_strict(order, default=len(order)))
    return figures.top_n_other(totals, "value", "n", top)


def collapse_bins(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Sum counts per bin; returns ``(bins sorted ascending, count of zero values)``."""
    zero = int(df.filter(pl.col("bin").is_null())["n"].sum())
    bins = df.filter(pl.col("bin").is_not_null()).group_by("bin").agg(pl.col("n").sum()).sort("bin")
    return bins, zero


def bar_figure(
    counts: pl.DataFrame, title: str, log: bool, scheme: str | None, *, horizontal: bool = True
) -> go.Figure:
    if counts.is_empty():
        return figures.empty_figure("no data for the current filters", scheme)
    labels, values = counts["value"].to_list(), counts["n"].to_list()
    fig = go.Figure(
        go.Bar(x=values, y=labels, orientation="h") if horizontal else go.Bar(x=labels, y=values)
    )
    count_axis = fig.update_xaxes if horizontal else fig.update_yaxes
    count_axis(type=figures.axis_type(log), title="events")
    if horizontal:
        fig.update_yaxes(autorange="reversed")
    return figures.style(fig, scheme, title=title, height=max(280, 24 * len(labels) + 120))


def histogram_figure(
    df: pl.DataFrame, title: str, x_title: str, log: bool, scheme: str | None
) -> go.Figure:
    bins, zero = collapse_bins(df)
    if bins.is_empty():
        return figures.empty_figure("no data for the current filters", scheme)
    indices = bins["bin"].to_list()
    fig = go.Figure(
        go.Bar(
            x=[bin_center(b) for b in indices],
            y=bins["n"].to_list(),
            width=[bin_range(b)[1] - bin_range(b)[0] for b in indices],
            customdata=[bin_range(b) for b in indices],
            hovertemplate="%{customdata[0]:.3g} to %{customdata[1]:.3g}: %{y:,}<extra></extra>",
        )
    )
    fig.update_xaxes(type="log", title=x_title)
    fig.update_yaxes(type=figures.axis_type(log), title="count")
    if zero:
        fig.add_annotation(
            text=f"{zero:,} zero values not shown", xref="paper", yref="paper", x=1, y=1.12,
            showarrow=False, xanchor="right",
        )  # fmt: skip
    return figures.style(fig, scheme, title=title, showlegend=False)


def interarrival_figure(
    df: pl.DataFrame, title: str, log: bool, scheme: str | None, lines: int = SERVICE_LINES
) -> go.Figure:
    """One line per service (largest ``lines`` by event count) over log-spaced gap bins."""
    df = df.filter(pl.col("bin").is_not_null())
    if df.is_empty():
        return figures.empty_figure("no data for the current filters", scheme)
    top = df.group_by("program").agg(pl.col("n").sum()).sort("n", descending=True).head(lines)
    fig = go.Figure()
    for program in top["program"]:
        part = df.filter(pl.col("program") == program).sort("bin")
        fig.add_scatter(
            x=[bin_center(b) for b in part["bin"]],
            y=part["n"].to_list(),
            name=program,
            mode="lines",
        )
    fig.update_xaxes(type="log", title="seconds between consecutive events")
    fig.update_yaxes(type=figures.axis_type(log), title="count")
    return figures.style(fig, scheme, title=title)


def build_figures(
    tables: dict[str, pl.DataFrame], filters: Filters, log: bool, scheme: str | None
) -> dict[str, go.Figure]:
    """All page figures keyed by graph id; pure, so it is tested without a browser."""
    out: dict[str, go.Figure] = {}
    for dim, (_, title) in DIMENSIONS.items():
        counts = category_counts(tables["categories"], dim, filters)
        out[f"dist-{dim}"] = bar_figure(counts, title, log, scheme)
    payload = filter_frame(tables["payload"], filters, level="level", service="program")
    out["dist-payload"] = histogram_figure(
        payload, HISTOGRAMS["payload"], "characters", log, scheme
    )
    for name, x_title in (("lines", "lines"), ("duration", "seconds")):
        title = f"{HISTOGRAMS[name]} (not filtered)"
        out[f"dist-{name}"] = histogram_figure(tables[name], title, x_title, log, scheme)
    inter = filter_frame(tables["interarrival"], filters, service="program")
    out["dist-interarrival"] = interarrival_figure(inter, HISTOGRAMS["interarrival"], log, scheme)
    return out


def load_tables(store: CacheStore) -> dict[str, pl.DataFrame]:
    return {name: store.read_parquet(KEY, file) for name, file in FILES.items()}


@register
class Distributions:
    key = KEY
    title = "Distributions"
    version = "1"
    depends_on: tuple[str, ...] = ()

    def compute(self, ctx: ComputeContext) -> None:
        for name, df in compute_tables(ctx.con).items():
            ctx.progress(f"{name}: {df.height} rows")
            df.write_parquet(ctx.out_dir / FILES[name], compression="zstd")

    def layout(self, store: CacheStore) -> Component:
        cards = [
            ui.card(title, ui.graph(f"dist-{dim}", 320)) for dim, (_, title) in DIMENSIONS.items()
        ]
        hists = [
            ui.card(title, ui.graph(f"dist-{name}", 360)) for name, title in HISTOGRAMS.items()
        ]
        return ui.page(
            "Distributions",
            dmc.Group([dmc.Text("Count axis", size="sm"), ui.scale_toggle("dist-scale")]),
            ui.grid(cards, cols={"base": 1, "lg": 2}),
            ui.grid(hists, cols={"base": 1, "lg": 2}),
            description=(
                "Level, service and host filters apply to the category and payload charts; "
                "host also applies to the host chart. Request charts are not filtered."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            [Output(gid, "figure") for gid in GRAPH_IDS],
            Input(FILTER_STORE_ID, "data"),
            Input("dist-scale", "value"),
            Input(THEME_STORE_ID, "data"),
        )
        def update(
            filters: dict[str, Any] | None, scale: str | None, theme: str | None
        ) -> list[go.Figure]:
            figs = build_figures(load_tables(store), from_dict(filters), scale == "log", theme)
            return [figs[gid] for gid in GRAPH_IDS]
