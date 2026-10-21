"""Module 6: anomaly detection framework and page.

Build time runs every registered detector (``logdash/detectors/``) and writes one combined
``anomalies.parquet`` (columns in ``detectors.base.ANOMALY_SCHEMA``: ``detector, type, bin,
window_s, kind, entity, template_id, raw, score, evidence``) plus ``meta.json`` with one entry
per detector. Scores are normalised per detector to ``[0.5, 1]`` (``1 - threshold / (2 raw)``),
so 0.5 means "at the detector's own threshold" and the types can be ranked together. At most
``MAX_PER_DETECTOR`` rows are kept per detector, the highest scoring ones.

The page reads that table only. The analysis depends on, and is invalidated by, whatever the
registered detectors depend on and their versions, so a new detector module changes neither this
file nor the page: the type filter, the timeline and the table follow the data.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import dash_mantine_components as dmc
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component

from logdash.analyses.base import ComputeContext, register
from logdash.analyses.features import Lru, segmented
from logdash.analyses.templates import short
from logdash.app import figures, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict, page_url, parse_time
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore
from logdash.detectors import load_builtin
from logdash.detectors.base import (
    ANOMALY_SCHEMA,
    DETECTORS,
    DetectContext,
    DetectorRegistry,
    empty,
    finish,
)

KEY = "anomalies"
FILE = "anomalies.parquet"
MAX_PER_DETECTOR = 20_000
MAX_POINTS = 3000  # timeline points: the highest scores of the selection
TABLE_ROWS = 100
TEXT_CHARS = 110
SORTS = {"score": "score", "time": "time", "type": "type", "entity": "entity"}
DEFAULT_SORT = "score"
LINK_PARAM = {"service": "service", "host": "host", "request": "request_id"}  # kind: explorer key
SORT_COLUMNS = {
    "score": (["score", "bin"], [True, False]),
    "time": (["bin", "score"], [False, True]),
    "type": (["type", "score"], [False, True]),
    "entity": (["entity", "score"], [False, True]),
}  # fmt: skip

load_builtin()  # import the detector modules so that they register


# --------------------------------------------------------------------------- compute


def run_detectors(
    registry: DetectorRegistry, ctx: DetectContext
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Combined rows of every detector and one meta entry (rows, seconds) per detector."""
    parts = [empty()]
    info = []
    for detector in registry.all():
        started = time.perf_counter()
        rows = finish(detector, detector.detect(ctx), MAX_PER_DETECTOR)
        seconds = time.perf_counter() - started
        ctx.log(f"{detector.key}: {rows.height} anomalies in {seconds:.1f}s")
        parts.append(rows)
        info.append(
            {
                "key": detector.key,
                "type": detector.type,
                "title": detector.title,
                "threshold": detector.threshold,
                "rows": rows.height,
                "seconds": round(seconds, 3),
            }
        )
    combined = pl.concat(parts).sort(["score", "bin"], descending=[True, False])
    return combined, info


# ---------------------------------------------------------------------------- loading


_FRAMES: Lru[pl.DataFrame] = Lru(2)


def clear_caches() -> None:
    _FRAMES.items.clear()


def load_anomalies(store: CacheStore) -> pl.DataFrame:
    """The combined table, kept in memory until the file changes."""
    path = store.path(KEY, FILE)
    key = (str(path), path.stat().st_mtime_ns)
    return _FRAMES.get(key, lambda: pl.read_parquet(path))


def load_types(store: CacheStore) -> list[str]:
    """Anomaly types of the built table in detector order (the registry's if not built)."""
    if not store.exists(KEY, "meta.json"):
        return DETECTORS.types()
    return list(dict.fromkeys(d["type"] for d in store.read_json(KEY, "meta.json")["detectors"]))


# ------------------------------------------------------------------------ pure helpers


def select(frame: pl.DataFrame, filters: Filters, types: list[str]) -> pl.DataFrame:
    """Rows in the filter's time range, of the chosen types (all if none) and entities.

    The service filter applies to service rows and the host filter to host rows; other kinds
    (such as requests) are not restricted by them.
    """
    lo, hi = parse_time(filters.start), parse_time(filters.end)
    if lo is not None:
        frame = frame.filter(pl.col("bin") >= lo)
    if hi is not None:
        frame = frame.filter(pl.col("bin") < hi)
    if types:
        frame = frame.filter(pl.col("type").is_in(types))
    for kind, values in (("service", filters.services), ("host", filters.hosts)):
        if values:
            frame = frame.filter((pl.col("kind") != kind) | pl.col("entity").is_in(list(values)))
    return frame


def sort_rows(frame: pl.DataFrame, sort: str) -> pl.DataFrame:
    by, descending = SORT_COLUMNS[sort]
    return frame.sort(by, descending=descending)


def stamp(value: datetime) -> str:
    return value.isoformat(sep=" ")[:19]


def explorer_url(
    bin_: datetime, window_s: int, kind: str, entity: str, template_id: int | None
) -> str:
    """Explorer link for one anomaly: its window, its entity and its template if it has one."""
    params: dict[str, object] = {
        "start": stamp(bin_),
        "end": stamp(bin_ + timedelta(seconds=window_s)),
    }
    if kind in LINK_PARAM:
        params[LINK_PARAM[kind]] = entity
    if template_id is not None:
        params["template"] = template_id
    return page_url("/explorer", **params)


def table_rows(rows: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """Display table and aligned explorer links for the given anomaly rows."""
    table = pl.DataFrame(
        {
            "window": [stamp(b) for b in rows["bin"]],
            "type": rows["type"],
            "score": rows["score"].round(3),
            "on": rows["kind"],
            "entity": rows["entity"],
            "detector": rows["detector"],
            "evidence": [short(e, TEXT_CHARS) for e in rows["evidence"]],
        },
        schema={
            "window": pl.String, "type": pl.String, "score": pl.Float64, "on": pl.String,
            "entity": pl.String, "detector": pl.String, "evidence": pl.String,
        },
    )  # fmt: skip
    hrefs = [
        explorer_url(b, w, k, e, t)
        for b, w, k, e, t in rows.select(
            "bin", "window_s", "kind", "entity", "template_id"
        ).iter_rows()
    ]
    return table, hrefs


def hover(rows: pl.DataFrame) -> list[str]:
    return [
        f"{stamp(b)}<br>{k}: {figures.shorten_middle(e, 40)}<br>{d}, score {s:.2f}<br>"
        f"{short(ev, TEXT_CHARS)}"
        for b, k, e, d, s, ev in rows.select(
            "bin", "kind", "entity", "detector", "score", "evidence"
        ).iter_rows()
    ]


def timeline_figure(rows: pl.DataFrame, types: list[str], scheme: str | None) -> go.Figure:
    """Score against time, one trace per type; the highest scores when there are many."""
    if rows.is_empty():
        return figures.empty_figure("no anomalies in the selection", scheme)
    shown = rows.head(MAX_POINTS)  # rows arrive sorted by descending score
    fig = go.Figure()
    for i, kind in enumerate(types):
        part = shown.filter(pl.col("type") == kind)
        if part.is_empty():
            continue
        fig.add_trace(
            go.Scattergl(
                x=part["bin"].to_list(),
                y=part["score"].to_list(),
                mode="markers",
                name=kind,
                marker={"size": 7, "color": figures.PALETTE[i % len(figures.PALETTE)]},
                text=hover(part),
                hovertemplate="%{text}<extra></extra>",
            )
        )
    fig.update_yaxes(title="score (0.5 = detector threshold)", range=[0.48, 1.02])
    if shown.height < rows.height:
        figures.add_note(fig, f"{shown.height:,} highest of {rows.height:,} anomalies")
    return figures.style(fig, scheme, height=380)


def unavailable(store: CacheStore) -> str | None:
    if not store.exists(KEY, FILE):
        return "The anomaly stage is not built; run `logdash build`."
    return None


def draw_anomalies(
    store: CacheStore, filters: Filters, types: list[str], sort: str, scheme: str | None
) -> tuple[go.Figure, Component, str]:
    """Timeline, ranked table with explorer links, and a hint on what was selected."""
    problem = unavailable(store)
    if problem is not None:
        return figures.empty_figure(problem, scheme), dmc.Text(problem), ""
    rows = select(load_anomalies(store), filters, types)  # sorted by descending score
    known = load_types(store)
    fig = timeline_figure(rows, known, scheme)
    if rows.is_empty():
        return fig, dmc.Text("no anomalies in the selection"), hint(filters, 0)
    table, hrefs = table_rows(sort_rows(rows.head(TABLE_ROWS), sort))
    return fig, ui.link_table(table, hrefs, max_rows=TABLE_ROWS), hint(filters, rows.height)


def hint(filters: Filters, count: int) -> str:
    text = f"{count:,} anomalies in the selection; the table shows the {TABLE_ROWS} highest."
    if filters.levels:
        text += " The level filter does not apply here."
    return text


def sort_value(value: str | None) -> str:
    return value if value in SORTS else DEFAULT_SORT


def type_values(value: list[str] | None) -> list[str]:
    return [v for v in value or [] if isinstance(v, str)]


@register
class Anomalies:
    key = KEY
    title = "Anomalies"

    def __init__(self, detectors: DetectorRegistry | None = None) -> None:
        self.detectors = DETECTORS if detectors is None else detectors

    @property
    def depends_on(self) -> tuple[str, ...]:
        """Union of what the registered detectors read."""
        return tuple(sorted({d for det in self.detectors.all() for d in det.depends_on}))

    @property
    def version(self) -> str:
        return "1:" + ",".join(f"{d.key}={d.version}" for d in self.detectors.all())

    @property
    def setting_names(self) -> tuple[str, ...]:
        names = {n for d in self.detectors.all() for n in getattr(d, "setting_names", ())}
        return tuple(sorted(names))

    def compute(self, ctx: ComputeContext) -> None:
        store = CacheStore(ctx.out_dir.parent, ctx.settings)
        rows, info = run_detectors(self.detectors, DetectContext(store, ctx.settings, ctx.progress))
        store.write_parquet(KEY, FILE, rows.cast(pl.Schema(ANOMALY_SCHEMA)))
        store.write_json(
            KEY, "meta.json", {"detectors": info, "max_per_detector": MAX_PER_DETECTOR}
        )
        for entry in info:
            ctx.note_rows(f"detector_{entry['key']}", entry["rows"])

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        types = load_types(store)
        controls = dmc.Group(
            [
                dmc.MultiSelect(
                    id="anomalies-types",
                    label="Type (empty = all)",
                    data=types,
                    value=[],
                    size="xs",
                    w=360,
                    clearable=True,
                ),
                dmc.Stack(
                    [
                        dmc.Text("Sort table by", size="xs", fw=500),
                        segmented(
                            "anomalies-sort",
                            [(s, s) for s in SORTS],
                            DEFAULT_SORT,
                        ),
                    ],
                    gap=4,
                ),
            ],
            align="flex-end",
            gap="lg",
        )
        return ui.page(
            "Anomalies",
            controls,
            ui.card("Anomalies over time", ui.graph("anomalies-timeline", 380)),
            ui.card("Highest scoring anomalies", dmc.Box(id="anomalies-table")),
            dmc.Text(id="anomalies-hint", size="sm", c="dimmed"),
            description=(
                "Every registered detector writes its candidates at build time; scores are "
                "normalised per detector so that 0.5 is the detector's own threshold and 1 "
                "is far above it. The time range, service and host filters apply (service "
                "filters service rows, host filters host rows); each row links to the raw "
                "lines of its window in the explorer."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            Output("anomalies-timeline", "figure"),
            Output("anomalies-table", "children"),
            Output("anomalies-hint", "children"),
            Input("anomalies-types", "value"),
            Input("anomalies-sort", "value"),
            Input(FILTER_STORE_ID, "data"),
            Input(THEME_STORE_ID, "data"),
        )
        def draw(
            types: list[str] | None,
            sort: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
        ) -> tuple[go.Figure, Component, str]:
            return draw_anomalies(
                store, from_dict(filters), type_values(types), sort_value(sort), theme
            )

        try:  # load the table now so that the first request does not read the file
            if unavailable(store) is None:
                load_anomalies(store)
        except (OSError, ValueError):
            pass
