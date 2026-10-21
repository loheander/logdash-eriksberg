"""Module 0: data quality and overview (headline numbers, missing values, level mapping)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import dash_mantine_components as dmc
import duckdb
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component

from logdash.analyses.base import ComputeContext, register
from logdash.app import figures, ui
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore
from logdash.ingest.normalize import EVENT_COLUMNS

KEY = "overview"
STAT_COLUMNS = tuple(c for c in EVENT_COLUMNS if c not in ("malformed", "hour"))
APPROX_DISTINCT = frozenset({"ts", "request_id", "payload"})  # too many values for exact counts
HEADLINE_COLUMNS = {"hosts": "host", "services": "program", "requests": "request_id"}


def column_stats(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, Any], pl.DataFrame]:
    """Row counts, time span and per-column missing/distinct counts in a single scan."""
    parts = ["count(*) AS rows", "count(*) FILTER (WHERE malformed) AS malformed"]
    parts += ["min(ts) AS ts_min", "max(ts) AS ts_max"]
    for col in STAT_COLUMNS:
        distinct = "approx_count_distinct" if col in APPROX_DISTINCT else "count(DISTINCT {})"
        distinct = distinct.format(col) if "{}" in distinct else f"{distinct}({col})"
        parts += [f"count({col}) AS nn_{col}", f"{distinct} AS d_{col}"]
    row = con.execute(f"SELECT {', '.join(parts)} FROM events").pl().row(0, named=True)
    rows = row["rows"]
    columns = pl.DataFrame(
        {
            "column": list(STAT_COLUMNS),
            "rows": [rows] * len(STAT_COLUMNS),
            "missing": [rows - row[f"nn_{c}"] for c in STAT_COLUMNS],
            "distinct": [row[f"d_{c}"] for c in STAT_COLUMNS],
            "approx": [c in APPROX_DISTINCT for c in STAT_COLUMNS],
        }
    ).with_columns(
        (pl.col("missing") / pl.col("rows").clip(lower_bound=1) * 100).alias("missing_pct")
    )
    return {
        "rows": rows,
        "malformed": row["malformed"],
        "ts_min": row["ts_min"],
        "ts_max": row["ts_max"],
    }, columns


def make_headline(base: dict[str, Any], columns: pl.DataFrame) -> dict[str, Any]:
    """Headline numbers for the cards from the scan result."""
    distinct = dict(zip(columns["column"], columns["distinct"], strict=True))
    head = {**base, **{name: distinct[col] for name, col in HEADLINE_COLUMNS.items()}}
    head["levels"] = distinct["level"]
    lo, hi = head["ts_min"], head["ts_max"]
    head["span_hours"] = round((hi - lo).total_seconds() / 3600, 2) if lo and hi else 0.0
    return head


def compute_level_map(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Raw level spelling to normalised level, with counts."""
    return con.execute(
        "SELECT coalesce(level_raw, '(missing)') AS level_raw, level, count(*) AS n "
        "FROM events GROUP BY ALL ORDER BY n DESC, level_raw"
    ).pl()


def compute_malformed_by_hour(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.execute(
        "SELECT hour, count(*) FILTER (WHERE malformed) AS malformed, count(*) AS rows "
        "FROM events GROUP BY hour ORDER BY hour"
    ).pl()


def missing_figure(columns: pl.DataFrame, scheme: str | None = "light") -> go.Figure:
    if columns.is_empty():
        return figures.empty_figure("no data", scheme)
    df = columns.sort("missing_pct")
    fig = go.Figure(
        go.Bar(
            x=df["missing_pct"].to_list(),
            y=df["column"].to_list(),
            orientation="h",
            customdata=df["missing"].to_list(),
            hovertemplate="%{y}: %{x:.1f} % (%{customdata:,} rows)<extra></extra>",
        )
    )
    fig.update_xaxes(title="missing (%)", range=[0, 100])
    return figures.style(fig, scheme, height=420)


def headline_cards(head: dict[str, Any]) -> list[Component]:
    lo, hi = head.get("ts_min"), head.get("ts_max")
    span = f"{lo:%Y-%m-%d %H:%M} to {hi:%Y-%m-%d %H:%M} UTC" if lo and hi else "-"
    items = [
        ("Rows", ui.fmt_int(head["rows"])),
        ("Time span", span),
        ("Span (hours)", str(head["span_hours"])),
        ("Hosts", ui.fmt_int(head["hosts"])),
        ("Services", ui.fmt_int(head["services"])),
        ("Requests", ui.fmt_int(head["requests"])),
        ("Malformed rows", ui.fmt_int(head["malformed"])),
        ("Normalised levels", ui.fmt_int(head["levels"])),
    ]
    return [ui.stat_card(label, value) for label, value in items]


def distinct_table(columns: pl.DataFrame) -> pl.DataFrame:
    return columns.select(
        "column",
        pl.col("distinct").map_elements(ui.fmt_int, return_dtype=pl.String).alias("distinct"),
        pl.when("approx").then(pl.lit("approximate")).otherwise(pl.lit("exact")).alias("count"),
        pl.col("missing").map_elements(ui.fmt_int, return_dtype=pl.String).alias("missing"),
    )


def level_map_table(level_map: pl.DataFrame) -> pl.DataFrame:
    return level_map.with_columns(
        pl.col("n").map_elements(ui.fmt_int, return_dtype=pl.String)
    ).rename({"level_raw": "raw level", "level": "normalised", "n": "rows"})


@register
class Overview:
    key = KEY
    title = "Overview"
    version = "1"
    depends_on: tuple[str, ...] = ()

    def compute(self, ctx: ComputeContext) -> None:
        base, columns = column_stats(ctx.con)
        headline = make_headline(base, columns)
        (ctx.out_dir / "headline.json").write_text(json.dumps(headline, default=str, indent=2))
        columns.write_parquet(ctx.out_dir / "columns.parquet", compression="zstd")
        compute_level_map(ctx.con).write_parquet(ctx.out_dir / "level_map.parquet")
        compute_malformed_by_hour(ctx.con).write_parquet(ctx.out_dir / "malformed.parquet")
        ctx.note_rows("events", base["rows"])

    def layout(self, store: CacheStore) -> Component:
        head = parse_headline(store.read_json(KEY, "headline.json"))
        columns = store.read_parquet(KEY, "columns.parquet")
        level_map = store.read_parquet(KEY, "level_map.parquet")
        malformed = store.read_parquet(KEY, "malformed.parquet").filter(pl.col("malformed") > 0)
        return ui.page(
            "Overview and data quality",
            ui.grid(headline_cards(head), cols={"base": 1, "sm": 2, "lg": 4}),
            ui.grid(
                [
                    ui.card("Missing values per column", ui.graph("overview-missing", 420)),
                    ui.card("Distinct and missing values", ui.table(distinct_table(columns))),
                    ui.card("Level normalisation", ui.table(level_map_table(level_map))),
                    ui.card(
                        "Malformed rows (no host, program or payload) by hour",
                        ui.table(malformed) if malformed.height else dmc.Text("none"),
                    ),
                ],
                cols={"base": 1, "lg": 2},
            ),
            description="Computed once by `logdash build`; unaffected by the global filters.",
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(Output("overview-missing", "figure"), Input(THEME_STORE_ID, "data"))
        def draw_missing(theme: str | None) -> go.Figure:
            return missing_figure(store.read_parquet(KEY, "columns.parquet"), theme)


def parse_headline(data: dict[str, Any]) -> dict[str, Any]:
    """Restore timestamps in a headline read back from JSON."""
    for key in ("ts_min", "ts_max"):
        data[key] = datetime.fromisoformat(data[key]) if data.get(key) else None
    return data
