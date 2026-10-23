"""Module 6: anomaly detection framework and page.

Build time runs every registered detector (``logdash/detectors/``) and writes one combined
``anomalies.parquet`` (columns in ``detectors.base.ANOMALY_SCHEMA``: ``detector, type, bin,
window_s, kind, entity, template_id, raw, score, evidence``) plus ``meta.json`` with one entry
per detector (rows above the threshold, rows kept, rows dropped, or the error of a detector that
failed and was skipped). The score in ``[0, 1]`` blends a row's rank among the detector's rows of
its kind with its log-scaled excess over the threshold (``detectors.base.score_columns``). At most
``MAX_PER_ENTITY`` rows are kept per service or host, then ``MAX_PER_DETECTOR`` per detector.
``overall = 1 / (60 + rank)`` (reciprocal rank fusion of the rank by score within detector and
kind) is what the page sorts by, so that no detector with many rows fills the top of the table.

The page reads that table only. The analysis depends on, and is invalidated by, whatever the
registered detectors depend on and their versions, so a new detector module changes neither this
file nor the page: the type filter, the timeline and the table follow the data. Semantic tag
filters (ADR002) restrict the rows that have a template; the others stay.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import dash_mantine_components as dmc
import numpy as np
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component

from logdash.analyses.base import ComputeContext, register
from logdash.analyses.features import Lru, segmented
from logdash.analyses.templates import KEY as TEMPLATES_KEY
from logdash.analyses.templates import short
from logdash.app import figures, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict, page_url, parse_time
from logdash.app.shell import THEME_STORE_ID
from logdash.app.tag_filters import (
    TagFilter,
    apply_tag_filter,
    attach_tags,
    load_tags,
    tag_controls,
    tag_filter,
)
from logdash.cache.store import CacheStore
from logdash.detectors import load_builtin
from logdash.detectors.base import (
    ANOMALY_SCHEMA,
    DETECTORS,
    FRAMEWORK_VERSION,
    KINDS,
    DetectContext,
    DetectorRegistry,
    EntityKind,
    empty,
    finish,
    threshold_of,
)

KEY = "anomalies"
FILE = "anomalies.parquet"
MAX_PER_ENTITY = 100  # highest windows kept per service or host
MAX_PER_DETECTOR = 20_000
MAX_POINTS = 3000  # timeline points, shared between the types: the highest scores of each
TABLE_ROWS = 100
TEXT_CHARS = 110
SORTS = {"overall": "overall", "score": "score", "time": "time", "type": "type", "entity": "entity"}
DEFAULT_SORT = "overall"
SORT_COLUMNS = {
    "overall": (["overall", "score", "bin"], [True, True, False]),
    "score": (["score", "bin"], [True, False]),
    "time": (["bin", "score"], [False, True]),
    "type": (["type", "score"], [False, True]),
    "entity": (["entity", "score"], [False, True]),
}  # fmt: skip
DESCRIPTION = (
    "Every registered detector writes its candidates at build time. The score in "
    "[0, 1] blends a row's rank among the detector's rows of the same kind with its "
    "log-scaled excess over the detector's threshold; the default order, overall, "
    "is the reciprocal rank (1 / (60 + rank by score within detector and kind)), "
    "which compares detectors by position, not by how many rows each has. The time "
    "range, service and host filters apply (each to its own kind of row); each row "
    "links to the raw lines of its window in the explorer."
)
TAGS_DESCRIPTION = (
    " Hide noise, actionable only and component use the semantic tags of a row's "
    "template (from a language model at build time); rows without a template are not "
    "filtered by them."
)

load_builtin()  # import the detector modules so that they register


# --------------------------------------------------------------------------- compute


def json_safe(value: Any) -> Any:
    """``value`` with numpy scalars and arrays as Python values and NaN or infinity as None.

    Detector meta goes into ``meta.json`` outside the per-detector ``try``, so whatever a
    detector puts there must not be able to fail the write.
    """
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset | np.ndarray):
        return [
            json_safe(v) for v in (sorted(value) if isinstance(value, set | frozenset) else value)
        ]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value if value is None or isinstance(value, str | int | bool) else str(value)


def run_detectors(
    registry: DetectorRegistry, ctx: DetectContext
) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Combined rows of every detector and one meta entry per detector.

    A detector that raises is logged, skipped and recorded in its meta entry (``error``); the
    others still run, so one broken detector does not discard the stage.
    """
    parts = [empty()]
    info = []
    for detector in registry.all():
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "key": detector.key,
            "type": detector.type,
            "title": detector.title,
        }
        own = replace(ctx, meta={})
        try:
            threshold = threshold_of(detector, ctx.settings)
            rows, above = finish(
                detector, detector.detect(own), MAX_PER_DETECTOR, MAX_PER_ENTITY, threshold
            )
        except Exception as error:  # noqa: BLE001 - any failure of a plug-in is isolated
            message = f"{type(error).__name__}: {error}"
            ctx.log(f"detector {detector.key!r} failed and was skipped: {message}")
            threshold, rows, above = detector.threshold, empty(), 0
            entry["error"] = message
        seconds = time.perf_counter() - started
        ctx.log(f"{detector.key}: kept {rows.height} of {above} anomalies in {seconds:.1f}s")
        parts.append(rows)
        entry |= {
            "threshold": threshold,
            "above_threshold": above,
            "rows": rows.height,
            "dropped": above - rows.height,
            "seconds": round(seconds, 3),
        }
        if own.meta:
            entry["info"] = json_safe(own.meta)
        info.append(entry)
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


def load_meta(store: CacheStore) -> list[dict[str, Any]]:
    """Per-detector meta entries of the built stage (empty if not built)."""
    if not store.exists(KEY, "meta.json"):
        return []
    return list(store.read_json(KEY, "meta.json")["detectors"])


def load_types(store: CacheStore) -> list[str]:
    """Anomaly types of the built table in detector order (the registry's if not built)."""
    meta = load_meta(store)
    return list(dict.fromkeys(d["type"] for d in meta)) if meta else DETECTORS.types()


# ------------------------------------------------------------------------ pure helpers


def filter_values(filters: Filters, kind: EntityKind) -> tuple[str, ...]:
    """The values of the global filter that restricts rows of this kind (empty: none)."""
    return getattr(filters, kind.filter_field) if kind.filter_field else ()


def select(frame: pl.DataFrame, filters: Filters, types: list[str]) -> pl.DataFrame:
    """Rows whose window overlaps the filter's time range, of the chosen types (all if none).

    The service and host filters restrict the kinds that declare them (``KINDS``: service and
    host rows); rows of other kinds (such as requests) are not restricted by them.
    """
    lo, hi = parse_time(filters.start), parse_time(filters.end)
    if lo is not None:
        frame = frame.filter(pl.col("bin") + pl.duration(seconds=pl.col("window_s")) > lo)
    if hi is not None:
        frame = frame.filter(pl.col("bin") < hi)
    if types:
        frame = frame.filter(pl.col("type").is_in(types))
    for kind in KINDS.values():
        values = filter_values(filters, kind)
        if values:
            frame = frame.filter(
                (pl.col("kind") != kind.name) | pl.col("entity").is_in(list(values))
            )
    return frame


def template_tags(store: CacheStore) -> pl.DataFrame | None:
    """Tags of the current templates by ``template_id`` (matched on the text, ADR002).

    ``None`` when no tags are cached; the tag filters are then a no-op.
    """
    tags = load_tags(store)
    if tags is None:
        return None
    templates = store.read_parquet(TEMPLATES_KEY, "templates.parquet")
    return attach_tags(templates.select("template_id", "template"), tags)


def restrict_by_tags(rows: pl.DataFrame, by_id: pl.DataFrame, wanted: TagFilter) -> pl.DataFrame:
    """Rows that pass the tag filter by their template's tags, in their order.

    Rows without a ``template_id`` are kept: the tags describe templates. An id missing from
    ``by_id`` counts as untagged (ADR002: hide noise keeps it, the other filters drop it).
    """
    if not wanted.active:
        return rows
    ids = rows.select(pl.col("template_id").drop_nulls().unique())
    passing = apply_tag_filter(ids.join(by_id, on="template_id", how="left"), wanted)
    return rows.filter(
        pl.col("template_id").is_null()
        | pl.col("template_id").is_in(passing["template_id"].to_list())
    )


def sort_rows(frame: pl.DataFrame, sort: str) -> pl.DataFrame:
    by, descending = SORT_COLUMNS[sort]
    return frame.sort(by, descending=descending)


def stamp(value: datetime) -> str:
    return value.isoformat(sep=" ")[:19]


def explorer_url(
    bin_: datetime, window_s: int, kind: str, entity: str, template_id: int | None
) -> str:
    """Explorer link for one anomaly: its entity, its template if it has one and, for a kind
    whose lines stay inside the window, the window (a request's lines may fall outside it)."""
    declared = KINDS.get(kind) or EntityKind(kind)  # an undeclared kind: window only
    params: dict[str, object] = {}
    if declared.link_window:
        params = {"start": stamp(bin_), "end": stamp(bin_ + timedelta(seconds=window_s))}
    if declared.link_param:
        params[declared.link_param] = entity
    if template_id is not None:
        params["template"] = template_id
    return page_url("/explorer", **params)


def table_rows(rows: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """Display table and aligned explorer links for the given anomaly rows."""
    table = pl.DataFrame(
        {
            "window": [stamp(b) for b in rows["bin"]],
            "type": rows["type"],
            "overall": rows["overall"].round(4),
            "score": rows["score"].round(3),
            "on": rows["kind"],
            "entity": rows["entity"],
            "detector": rows["detector"],
            "evidence": [short(e, TEXT_CHARS) for e in rows["evidence"]],
        },
        schema={
            "window": pl.String, "type": pl.String, "overall": pl.Float64, "score": pl.Float64,
            "on": pl.String,
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
    """Score against time, one trace per type; the highest scores of each type when many."""
    if rows.is_empty():
        return figures.empty_figure("no anomalies in the selection", scheme)
    present = [t for t in types if (rows["type"] == t).any()]
    per_type = MAX_POINTS // max(1, len(present))  # no type crowds out the others
    shown = 0
    fig = go.Figure()
    for i, kind in enumerate(types):
        # rows arrive sorted by descending score
        part = rows.filter(pl.col("type") == kind).head(per_type)
        if part.is_empty():
            continue
        shown += part.height
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
    fig.update_yaxes(title="score", range=[-0.02, 1.02])
    if shown < rows.height:
        figures.add_note(
            fig, f"{shown:,} of {rows.height:,} anomalies: the highest {per_type:,} of each type"
        )
    return figures.style(fig, scheme, height=380)


def unavailable(store: CacheStore) -> str | None:
    if not store.exists(KEY, FILE):
        return "The anomaly stage is not built; run `logdash build`."
    return None


def draw_anomalies(
    store: CacheStore,
    filters: Filters,
    types: list[str],
    sort: str,
    scheme: str | None,
    by_tags: TagFilter | None = None,
) -> tuple[go.Figure, Component, str]:
    """Timeline, ranked table with explorer links, and a hint on what was selected.

    ``by_tags`` restricts the rows about one template by its semantic tags (no-op without
    cached tags); rows without a template are kept.
    """
    problem = unavailable(store)
    if problem is not None:
        return figures.empty_figure(problem, scheme), dmc.Text(problem), ""
    rows = select(load_anomalies(store), filters, types)  # sorted by descending score
    untemplated: int | None = None
    if by_tags is not None and by_tags.active:
        by_id = template_tags(store)
        if by_id is not None:
            rows = restrict_by_tags(rows, by_id, by_tags)
            untemplated = rows["template_id"].null_count()
    known = load_types(store)
    fig = timeline_figure(rows, known, scheme)
    if rows.is_empty():
        return (
            fig,
            dmc.Text("no anomalies in the selection"),
            hint(filters, 0, sort, load_meta(store), untemplated=untemplated),
        )
    ordered = rows if sort == "score" else sort_rows(rows, sort)  # sort the whole selection
    table, hrefs = table_rows(ordered.head(TABLE_ROWS))
    shown = ui.link_table(table, hrefs, max_rows=TABLE_ROWS)
    return (
        fig,
        shown,
        hint(filters, rows.height, sort, load_meta(store), untemplated=untemplated),
    )


def kept_text(meta: list[dict[str, Any]]) -> str:
    """'N of M kept' per type, and the detectors that failed."""
    kept: dict[str, list[int]] = {}
    for entry in meta:
        counts = kept.setdefault(entry["type"], [0, 0])
        counts[0] += entry.get("rows", 0)
        counts[1] += entry.get("above_threshold", 0)
    text = (
        "Kept "
        + "; ".join(f"{k:,} of {m:,} {t}" for t, (k, m) in kept.items())
        + (f" windows above threshold (at most {MAX_PER_ENTITY} per service or host).")
    )
    for entry in meta:
        if "note" in entry.get("info", {}):
            text += f" {entry['title']}: {entry['info']['note']}."
    failed = [f"{e['key']} ({e['error']})" for e in meta if e.get("error")]
    return text + (" Failed and skipped: " + "; ".join(failed) + "." if failed else "")


def hint(
    filters: Filters,
    count: int,
    sort: str = DEFAULT_SORT,
    meta: list[dict[str, Any]] | None = None,
    untemplated: int | None = None,
) -> str:
    """What the selection is. ``untemplated``: rows without a template in the view while a
    tag filter is active (None: no tag filter)."""
    text = (
        f"{count:,} anomalies in the selection; the table shows the first {TABLE_ROWS} by {sort}."
    )
    applied = [
        f"{k.filter_field} filter applies to {k.name} rows only"
        for k in KINDS.values()
        if filter_values(filters, k)
    ]
    if applied:
        text += " The " + "; the ".join(applied) + "; other kinds stay in the view."
    if untemplated is not None:
        text += (
            " The tag filters apply to rows about one template only; "
            f"{untemplated:,} rows without a template stay in the view."
        )
    if filters.levels:
        text += " The level filter does not apply here."
    return text + (" " + kept_text(meta) if meta else "")


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
        caps = f"{MAX_PER_ENTITY}:{MAX_PER_DETECTOR}"
        detectors = ",".join(f"{d.key}={d.version}" for d in self.detectors.all())
        return f"{FRAMEWORK_VERSION}:{caps}:{detectors}"

    @property
    def setting_names(self) -> tuple[str, ...]:
        names = {n for d in self.detectors.all() for n in getattr(d, "setting_names", ())}
        return tuple(sorted(names))

    def compute(self, ctx: ComputeContext) -> None:
        store = CacheStore(ctx.out_dir.parent, ctx.settings)
        rows, info = run_detectors(self.detectors, DetectContext(store, ctx.settings, ctx.progress))
        store.write_parquet(KEY, FILE, rows.cast(pl.Schema(ANOMALY_SCHEMA)))
        store.write_json(
            KEY,
            "meta.json",
            {
                "detectors": info,
                "max_per_entity": MAX_PER_ENTITY,
                "max_per_detector": MAX_PER_DETECTOR,
            },
        )
        for entry in info:
            ctx.note_rows(f"detector_{entry['key']}", entry["rows"])

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        types = load_types(store)
        with_tags = load_tags(store) is not None
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
            tag_controls("anomalies", with_tags),
            ui.card("Anomalies over time", ui.graph("anomalies-timeline", 380)),
            ui.card("Highest scoring anomalies", dmc.Box(id="anomalies-table")),
            dmc.Text(id="anomalies-hint", size="sm", c="dimmed"),
            description=DESCRIPTION + (TAGS_DESCRIPTION if with_tags else ""),
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
            Input("anomalies-hide-noise", "checked"),
            Input("anomalies-actionable-only", "checked"),
            Input("anomalies-components", "value"),
        )
        def draw(
            types: list[str] | None,
            sort: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
            hide_noise: bool | None = None,
            actionable_only: bool | None = None,
            components: list[str] | None = None,
        ) -> tuple[go.Figure, Component, str]:
            return draw_anomalies(
                store,
                from_dict(filters),
                type_values(types),
                sort_value(sort),
                theme,
                tag_filter(hide_noise, actionable_only, components),
            )

        try:  # load the table now so that the first request does not read the file
            if unavailable(store) is None:
                load_anomalies(store)
        except (OSError, ValueError):
            pass
