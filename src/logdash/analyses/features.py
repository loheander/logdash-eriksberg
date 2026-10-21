"""Module 4: feature analysis (window x template count matrix).

Built from the pre-aggregated ``templates/template_counts*.parquet`` (template x minute x
service or host), never from ``events/``. Build time writes, for each grouping (service,
host) and window (1, 5, 15 min), a sparse matrix in long format plus its row index:

- ``index_<group>_<s>.parquet``: ``row, bin, entity`` (one row per non-empty window and entity);
- ``matrix_<group>_<s>.parquet``: ``row, template_id, n`` (non-zero counts);
- ``meta.json``: the template ids (columns, sorted) and the grids that were written.

``load_matrix`` turns a pair into a ``scipy.sparse`` CSR matrix that PCA and the detectors can
use directly. The page works on that matrix in memory: the visible range selects rows, scaling
(log1p or TF-IDF) and statistics treat absent rows as zeros, so empty windows count.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dash_mantine_components as dmc
import numpy as np
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component
from scipy import sparse, special

from logdash.analyses.base import ComputeContext, register
from logdash.analyses.templates import short
from logdash.app import figures, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict, page_url, parse_time
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore

KEY = "features"
UPSTREAM = "templates"
GROUPS = ("service", "host")
WINDOWS = (60, 300, 900)  # seconds
DEFAULT_GROUP = "service"
DEFAULT_WINDOW = 60
SCALINGS = ("log1p", "tfidf")
DEFAULT_SCALING = "log1p"
TOP_CHOICES = (15, 25, 40)
DEFAULT_TOP = 25
MAX_ENTITIES = 40  # columns of the association heatmap
RANK_ROWS = 20
TABLE_ROWS = 30
TEXT_CHARS = 60
SOURCES = {  # group: (file in the templates stage, entity column)
    "service": ("template_counts.parquet", "program"),
    "host": ("template_counts_host.parquet", "host"),
}


# --------------------------------------------------------------------------- compute


def window_long(counts: pl.DataFrame, entity: str, window_s: int) -> pl.DataFrame:
    """``(bin, entity, template_id, n)`` summed into windows of ``window_s`` seconds."""
    return (
        counts.rename({entity: "entity"})
        .with_columns(pl.col("bin").dt.truncate(f"{window_s}s"))
        .group_by("bin", "entity", "template_id")
        .agg(pl.col("n").sum())
    )


def index_and_matrix(long: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Row index ``(row, bin, entity)`` and the sparse long matrix ``(row, template_id, n)``."""
    index = long.select("bin", "entity").unique().sort("bin", "entity").with_row_index("row")
    index = index.with_columns(pl.col("row").cast(pl.Int64))
    matrix = (
        long.join(index, on=["bin", "entity"])
        .select("row", "template_id", "n")
        .sort("row", "template_id")
    )
    return index.select("row", "bin", "entity"), matrix


def grid_name(kind: str, group: str, window_s: int) -> str:
    return f"{kind}_{group}_{window_s}.parquet"


def compute_all(
    source_dir: Path, out_dir: Path, log: Callable[[str], None] = print
) -> dict[str, int]:
    """Write every grid and ``meta.json`` from the templates stage; returns row counts."""
    ids = sorted(pl.read_parquet(source_dir / "templates.parquet")["template_id"].to_list())
    span = json.loads((source_dir / "meta.json").read_text())
    rows: dict[str, int] = {}
    for group, (name, entity) in SOURCES.items():
        counts = pl.read_parquet(source_dir / name)
        for window_s in WINDOWS:
            index, matrix = index_and_matrix(window_long(counts, entity, window_s))
            index.write_parquet(out_dir / grid_name("index", group, window_s), compression="zstd")
            matrix.write_parquet(out_dir / grid_name("matrix", group, window_s), compression="zstd")
            rows[f"{group}_{window_s}_windows"] = index.height
            rows[f"{group}_{window_s}_nonzero"] = matrix.height
            log(f"{group} x {window_s // 60} min: {index.height} windows")
    meta = {"template_ids": ids, "ts_min": span["ts_min"], "ts_max": span["ts_max"]}
    (out_dir / "meta.json").write_text(json.dumps(meta))
    return rows


# ------------------------------------------------------------------------- the matrix


@dataclass(frozen=True)
class Matrix:
    """Window x template counts: CSR matrix, one ``(bin, entity)`` per row, one id per column."""

    counts: sparse.csr_matrix
    bins: np.ndarray  # datetime64[us] per row
    codes: np.ndarray  # entity code per row (index into ``entities``)
    entities: list[str]
    template_ids: np.ndarray
    window_s: int


_CACHE: dict[tuple[str, int, int], Matrix] = {}


def build_matrix(
    index: pl.DataFrame, matrix: pl.DataFrame, template_ids: list[int], window_s: int
) -> Matrix:
    entities = sorted(index["entity"].unique().to_list())
    mapping = {name: i for i, name in enumerate(entities)}
    ids = np.asarray(template_ids, dtype=np.int64)
    cols = np.searchsorted(ids, matrix["template_id"].to_numpy())
    counts = sparse.csr_matrix(
        (matrix["n"].to_numpy().astype(np.float64), (matrix["row"].to_numpy(), cols)),
        shape=(index.height, len(ids)),
    )
    codes = index["entity"].replace_strict(mapping, return_dtype=pl.Int64).to_numpy()
    return Matrix(counts, index["bin"].to_numpy(), codes, entities, ids, window_s)


def load_matrix(
    store: CacheStore, group: str = DEFAULT_GROUP, window_s: int = DEFAULT_WINDOW
) -> Matrix:
    """Matrix of one grid as written by the build; kept in memory per file version."""
    path = store.path(KEY, grid_name("matrix", group, window_s))
    key = (str(path), path.stat().st_mtime_ns, window_s)
    if key not in _CACHE:
        index = store.read_parquet(KEY, grid_name("index", group, window_s))
        matrix = store.read_parquet(KEY, grid_name("matrix", group, window_s))
        ids = store.read_json(KEY, "meta.json")["template_ids"]
        _CACHE[key] = build_matrix(index, matrix, ids, window_s)
    return _CACHE[key]


# ---------------------------------------------------------------------- pure helpers


@dataclass(frozen=True)
class Selection:
    """Rows of a matrix inside the visible range and entity filter, as raw counts."""

    counts: sparse.csr_matrix
    codes: np.ndarray  # local entity code per row
    entities: list[str]
    n_windows: int  # windows in the range per entity, empty ones included
    template_ids: np.ndarray

    @property
    def n_rows(self) -> int:
        """Rows of the full grid (entities x windows) the statistics are taken over."""
        return max(self.n_windows * len(self.entities), 1)


def select_rows(
    matrix: Matrix, lo: datetime, hi: datetime, entities: tuple[str, ...] = ()
) -> Selection:
    """Restrict to ``bin`` in ``[lo, hi)`` and, if given, to the named entities."""
    names = [e for e in matrix.entities if not entities or e in entities]
    lookup = np.full(len(matrix.entities) + 1, -1, dtype=np.int64)
    for local, name in enumerate(names):
        lookup[matrix.entities.index(name)] = local
    local_codes = lookup[matrix.codes]
    keep = (
        (matrix.bins >= np.datetime64(lo, "us"))
        & (matrix.bins < np.datetime64(hi, "us"))
        & (local_codes >= 0)
    )
    rows = np.flatnonzero(keep)
    n_windows = max(math.ceil((hi - lo).total_seconds() / matrix.window_s), 1)
    return Selection(matrix.counts[rows], local_codes[rows], names, n_windows, matrix.template_ids)


def scale_counts(counts: sparse.csr_matrix, method: str, n_rows: int) -> sparse.csr_matrix:
    """``log1p`` of the counts, or TF-IDF: ``log1p(count) * idf`` with rows L2-normalised.

    ``idf = ln((1 + n_rows) / (1 + df)) + 1`` with ``df`` the windows containing the template
    and ``n_rows`` all windows (empty ones included). Zeros stay zero, so the matrix stays sparse.
    """
    values = np.log1p(counts.data)
    if method == "tfidf":
        df = np.bincount(counts.indices, weights=counts.data > 0, minlength=counts.shape[1])
        values = values * (np.log((1 + n_rows) / (1 + df)) + 1)[counts.indices]
        rows = np.repeat(np.arange(counts.shape[0]), np.diff(counts.indptr))
        norms = np.sqrt(np.bincount(rows, weights=values**2, minlength=counts.shape[0]))
        values = values / np.where(norms > 0, norms, 1.0)[rows]
    return sparse.csr_matrix((values, counts.indices, counts.indptr), shape=counts.shape)


def column_variance(scaled: sparse.csr_matrix, n_rows: int) -> np.ndarray:
    """Variance of each column over ``n_rows`` rows (rows not stored are zero)."""
    mean = np.asarray(scaled.sum(axis=0)).ravel() / n_rows
    square = np.asarray(scaled.multiply(scaled).sum(axis=0)).ravel() / n_rows
    return np.maximum(square - mean**2, 0.0)


def correlation(scaled: sparse.csr_matrix, columns: np.ndarray, n_rows: int) -> np.ndarray:
    """Pearson correlation of the chosen columns over ``n_rows`` rows (constant columns: 0)."""
    part = scaled[:, columns]
    mean = np.asarray(part.sum(axis=0)).ravel() / n_rows
    cov = (part.T @ part).toarray() / n_rows - np.outer(mean, mean)
    sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
    denom = np.outer(sd, sd)
    corr = np.divide(cov, denom, out=np.zeros_like(cov), where=denom > 0)
    np.fill_diagonal(corr, np.where(sd > 0, 1.0, 0.0))
    return np.clip(corr, -1.0, 1.0)


def entity_table(
    counts: sparse.csr_matrix, codes: np.ndarray, n_entities: int, *, presence: bool
) -> np.ndarray:
    """Templates x entities: summed counts, or the number of windows with the template."""
    one_hot = sparse.csr_matrix(
        (np.ones(len(codes)), (np.arange(len(codes)), codes)), shape=(len(codes), n_entities)
    )
    source = (counts > 0).astype(np.float64) if presence else counts
    return (source.T @ one_hot).toarray()


def presence_mutual_information(present: np.ndarray, n_windows: int) -> np.ndarray:
    """Mutual information in bits between 'template occurs in a window' and the entity.

    ``present[t, e]`` is the number of windows of entity ``e`` containing template ``t`` out of
    ``n_windows``; entities are weighted equally. A template that only one service logs, or that
    one service logs far more steadily than the rest, scores high; one spread evenly scores 0.
    """
    if present.shape[1] == 0:
        return np.zeros(present.shape[0])
    p_e = np.clip(present / n_windows, 0.0, 1.0)
    p = p_e.mean(axis=1, keepdims=True)
    terms = special.xlogy(p_e, p_e / np.where(p > 0, p, 1)) + special.xlogy(
        1 - p_e, (1 - p_e) / np.where(p < 1, 1 - p, 1)
    )
    return terms.mean(axis=1) / math.log(2)


@dataclass(frozen=True)
class FeatureStats:
    """Everything the page draws, for the templates that occur in the range."""

    selection: Selection
    scaled: sparse.csr_matrix
    volume: np.ndarray  # events per template
    windows: np.ndarray  # windows containing the template (all entities)
    variance: np.ndarray
    mutual_information: np.ndarray
    by_entity: np.ndarray  # templates x entities, event counts


def feature_stats(selection: Selection, scaling: str) -> FeatureStats:
    counts = selection.counts
    scaled = scale_counts(counts, scaling, selection.n_rows)
    presence = entity_table(counts, selection.codes, len(selection.entities), presence=True)
    return FeatureStats(
        selection,
        scaled,
        np.asarray(counts.sum(axis=0)).ravel(),
        presence.sum(axis=1),
        column_variance(scaled, selection.n_rows),
        presence_mutual_information(presence, selection.n_windows),
        entity_table(counts, selection.codes, len(selection.entities), presence=False),
    )


def top_columns(stats: FeatureStats, n: int) -> np.ndarray:
    """Column indices of the ``n`` templates with most events (ties: lower id first)."""
    order = np.lexsort((np.arange(len(stats.volume)), -stats.volume))
    return order[: min(n, int((stats.volume > 0).sum()))]


def stats_table(stats: FeatureStats, texts: dict[int, str]) -> pl.DataFrame:
    """One row per occurring template: volume, windows, variance, MI and dominant entity."""
    sel = stats.selection
    present = np.flatnonzero(stats.volume > 0)
    names = np.asarray(sel.entities, dtype=object)
    peak = stats.by_entity[present].argmax(axis=1) if len(sel.entities) else present * 0
    share = stats.by_entity[present, peak] / stats.volume[present] if len(present) else present
    ids = [int(i) for i in sel.template_ids[present]]
    return pl.DataFrame(
        {
            "template_id": ids,
            "template": [short(texts.get(i, ""), TEXT_CHARS) for i in ids],
            "events": stats.volume[present].astype(np.int64),
            "windows": stats.windows[present].astype(np.int64),
            "variance": stats.variance[present],
            "mutual_information": stats.mutual_information[present],
            "top_entity": names[peak].tolist() if len(present) else [],
            "top_share": share,
        },
        schema={
            "template_id": pl.Int64,
            "template": pl.String,
            "events": pl.Int64,
            "windows": pl.Int64,
            "variance": pl.Float64,
            "mutual_information": pl.Float64,
            "top_entity": pl.String,
            "top_share": pl.Float64,
        },
    )


def label(template_id: int) -> str:
    return f"T{template_id}"


def _texts(ids: np.ndarray, texts: dict[int, str]) -> list[str]:
    return [f"{label(int(i))}: {short(texts.get(int(i), ''), TEXT_CHARS)}" for i in ids]


def correlation_figure(
    corr: np.ndarray, ids: np.ndarray, texts: dict[int, str], scaling: str, scheme: str | None
) -> go.Figure:
    if len(ids) < 2:
        return figures.empty_figure("fewer than two templates in the range", scheme)
    names = _texts(ids, texts)
    labels = [label(int(i)) for i in ids]
    hover = [[f"{a}<br>{b}" for b in names] for a in names]
    fig = go.Figure(
        go.Heatmap(
            z=corr.tolist(), x=labels, y=labels, zmin=-1, zmax=1, colorscale="RdBu_r",
            text=hover, hovertemplate="%{text}<br>r = %{z:.2f}<extra></extra>",
            colorbar={"title": "r"},
        )
    )  # fmt: skip
    fig.update_xaxes(type="category", tickangle=-60)
    fig.update_yaxes(type="category", autorange="reversed")
    figures.add_note(fig, f"{len(ids)} most frequent templates, {scaling} scaling")
    return figures.style(fig, scheme, height=560)


def association_figure(
    stats: FeatureStats,
    columns: np.ndarray,
    texts: dict[int, str],
    group: str,
    scheme: str | None,
) -> go.Figure:
    """Share of each template's events per entity (rows sum to 1); busiest entities only."""
    sel = stats.selection
    if len(columns) == 0 or not sel.entities:
        return figures.empty_figure("no templates in the range", scheme)
    by = stats.by_entity[columns]
    busiest = np.argsort(-stats.by_entity.sum(axis=0), kind="stable")[:MAX_ENTITIES]
    share = by / by.sum(axis=1, keepdims=True)
    names = [figures.shorten_middle(sel.entities[i]) for i in busiest]
    ids = sel.template_ids[columns]
    fig = go.Figure(
        go.Heatmap(
            z=share[:, busiest].tolist(), x=names, y=[label(int(i)) for i in ids],
            text=[[t] * len(names) for t in _texts(ids, texts)], colorscale="Blues", zmin=0, zmax=1,
            hovertemplate="%{text}<br>%{x}: %{z:.0%} of its events<extra></extra>",
            colorbar={"title": "share", "tickformat": ".0%"},
        )
    )  # fmt: skip
    fig.update_xaxes(type="category", tickangle=-60)
    fig.update_yaxes(type="category", autorange="reversed")
    figures.add_note(fig, f"share of each template per {group}, busiest {len(names)} {group}s")
    return figures.style(fig, scheme, height=560)


def ranking_figure(table: pl.DataFrame, column: str, title: str, scheme: str | None) -> go.Figure:
    """Horizontal bars of the top templates by one statistic of ``stats_table``."""
    top = table.sort(column, "template_id", descending=[True, False]).head(RANK_ROWS)
    if top.is_empty():
        return figures.empty_figure("no templates in the range", scheme)
    fig = go.Figure(
        go.Bar(
            x=top[column].to_list(),
            y=[label(i) for i in top["template_id"]],
            orientation="h",
            customdata=top["template"].to_list(),
            hovertemplate="%{y}: %{x:.4g}<br>%{customdata}<extra></extra>",
        )
    )
    fig.update_yaxes(autorange="reversed", type="category")
    fig.update_xaxes(title=title)
    return figures.style(fig, scheme, height=420)


def stats_view(table: pl.DataFrame) -> Component:
    """Statistics of the busiest templates with an explorer link per row."""
    if table.is_empty():
        return dmc.Text("none")
    top = table.sort("events", "template_id", descending=[True, False]).head(TABLE_ROWS)
    shown = top.select(
        "template_id",
        "template",
        "events",
        "windows",
        pl.col("variance").round(4),
        pl.col("mutual_information").round(4).alias("MI (bits)"),
        "top_entity",
        pl.col("top_share").round(3),
    )
    return ui.link_table(shown, [page_url("/explorer", template=t) for t in top["template_id"]])


# ------------------------------------------------------------------------- callbacks


def floor_window(value: datetime, window_s: int) -> datetime:
    """Start of the window containing ``value`` (windows are aligned to the Unix epoch)."""
    seconds = int(value.replace(tzinfo=UTC).timestamp())
    return datetime.fromtimestamp(seconds - seconds % window_s, UTC).replace(tzinfo=None)


def visible_range(store: CacheStore, filters: Filters, window_s: int) -> tuple[datetime, datetime]:
    """Whole windows covering the filter's time range, clamped to the data span."""
    meta = store.read_json(KEY, "meta.json")
    first = floor_window(datetime.fromisoformat(meta["ts_min"]), window_s)
    last = floor_window(datetime.fromisoformat(meta["ts_max"]), window_s) + timedelta(
        seconds=window_s
    )
    lo = max(floor_window(parse_time(filters.start) or first, window_s), first)
    hi = min(floor_window(parse_time(filters.end) or last, window_s), last)
    return lo, max(hi, lo + timedelta(seconds=window_s))


def entity_filter(filters: Filters, group: str) -> tuple[str, ...]:
    return filters.services if group == "service" else filters.hosts


def draw_features(
    store: CacheStore,
    filters: Filters,
    window_s: int,
    group: str,
    scaling: str,
    top: int,
    scheme: str | None,
) -> tuple[go.Figure, go.Figure, go.Figure, go.Figure, Component]:
    """Correlation heatmap, association heatmap, variance bars, MI bars and the table."""
    lo, hi = visible_range(store, filters, window_s)
    selection = select_rows(
        load_matrix(store, group, window_s), lo, hi, entity_filter(filters, group)
    )
    stats = feature_stats(selection, scaling)
    texts = template_texts(store)
    columns = top_columns(stats, top)
    table = stats_table(stats, texts)
    return (
        correlation_figure(
            correlation(stats.scaled, columns, selection.n_rows),
            selection.template_ids[columns],
            texts,
            scaling,
            scheme,
        ),
        association_figure(stats, columns, texts, group, scheme),
        ranking_figure(table, "variance", f"variance of the {scaling} features", scheme),
        ranking_figure(table, "mutual_information", "mutual information (bits)", scheme),
        stats_view(table),
    )


_TEXTS: dict[tuple[str, int], dict[int, str]] = {}


def template_texts(store: CacheStore) -> dict[int, str]:
    """Template id to template text, read once per cache version."""
    path = store.path("templates", "templates.parquet")
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _TEXTS:
        frame = pl.read_parquet(path, columns=["template_id", "template"])
        _TEXTS[key] = dict(zip(frame["template_id"], frame["template"], strict=True))
    return _TEXTS[key]


def segmented(component_id: str, options: list[tuple[str, str]], value: str) -> Component:
    return dmc.SegmentedControl(
        id=component_id,
        data=[{"value": v, "label": text} for v, text in options],
        value=value,
        size="xs",
    )


@register
class Features:
    key = KEY
    title = "Features"
    version = "1"
    depends_on: tuple[str, ...] = (UPSTREAM,)

    def compute(self, ctx: ComputeContext) -> None:
        for name, count in compute_all(
            ctx.out_dir.parent / UPSTREAM, ctx.out_dir, ctx.progress
        ).items():
            ctx.note_rows(name, count)

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        controls = dmc.Group(
            [
                segmented(
                    "features-window",
                    [(str(s), f"{s // 60} min") for s in WINDOWS],
                    str(DEFAULT_WINDOW),
                ),
                segmented("features-group", [(g, f"per {g}") for g in GROUPS], DEFAULT_GROUP),
                segmented(
                    "features-scale", [("log1p", "log1p"), ("tfidf", "TF-IDF")], DEFAULT_SCALING
                ),
                segmented(
                    "features-top", [(str(n), f"top {n}") for n in TOP_CHOICES], str(DEFAULT_TOP)
                ),
            ],
            gap="md",
        )
        return ui.page(
            "Feature analysis",
            controls,
            ui.card("Template correlation", ui.graph("features-corr", 560)),
            ui.card("Template association", ui.graph("features-assoc", 560)),
            ui.grid(
                [
                    ui.card("Variance ranking", ui.graph("features-variance", 420)),
                    ui.card("Mutual information with the entity", ui.graph("features-mi", 420)),
                ],
                cols={"base": 1, "lg": 2},
            ),
            ui.card("Template statistics (busiest templates)", dmc.Box(id="features-table")),
            description=(
                "Window x template count matrix from the template counts. The time range applies; "
                "the service filter applies per service and the host filter per host. Empty "
                "windows count as zeros."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            Output("features-corr", "figure"),
            Output("features-assoc", "figure"),
            Output("features-variance", "figure"),
            Output("features-mi", "figure"),
            Output("features-table", "children"),
            Input("features-window", "value"),
            Input("features-group", "value"),
            Input("features-scale", "value"),
            Input("features-top", "value"),
            Input(FILTER_STORE_ID, "data"),
            Input(THEME_STORE_ID, "data"),
        )
        def draw(
            window: str | None,
            group: str | None,
            scaling: str | None,
            top: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
        ) -> tuple[go.Figure, go.Figure, go.Figure, go.Figure, Component]:
            return draw_features(
                store,
                from_dict(filters),
                int(window or DEFAULT_WINDOW),
                group if group in GROUPS else DEFAULT_GROUP,
                scaling if scaling in SCALINGS else DEFAULT_SCALING,
                int(top or DEFAULT_TOP),
                theme,
            )
