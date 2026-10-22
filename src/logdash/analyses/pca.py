"""Module 5: PCA of the window x template matrix and the residual (SPE) anomaly score.

Built from the scaled matrices of the features stage (``features.load_scaled``), never from
``events/``. Build time fits one PCA (scikit-learn, covariance eigendecomposition on the sparse
matrix) per grouping (service, host), window (1, 5, 15 min) and scaling (log1p, TF-IDF), over the
non-empty windows of the whole span, and writes for each fit:

- ``fit_<group>_<s>_<scaling>.json``: explained variance ratios, ``k`` (components that keep
  95 % of the variance), the SPE control limit, the template ids and the first loadings;
- ``scores_<group>_<s>_<scaling>.parquet``: ``row, bin, entity, pc1..pc3, spe, limit, ratio`` per
  window, rows as in the features index (the input of the anomaly module);
- ``residuals_<group>_<s>_<scaling>.parquet``: ``row, rank, template_id, residual, share`` with
  the templates that contribute most to the residual of each window (the evidence for the
  structural detector).

The SPE (squared prediction error, Q-statistic) of a window is the squared norm of what the
first ``k`` components do not explain (Xu et al., 2009); its control limit is the
Jackson-Mudholkar approximation at confidence ``1 - ALPHA`` from the residual eigenvalues. That
limit assumes Gaussian data: the windows are heavy-tailed counts that include the anomalies, so
``ALPHA`` is not the false-alarm rate and the share above the limit is typically far larger.
Treat the limit as a reference line and rank windows by ``ratio = spe / limit`` (``load_spe``).
Without residual variance the limit is infinite and no window is above it. The
page only reads these files: it never fits, and it samples the plotted windows. Time range and
entity filter select windows, and loads are memoised per file version.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any

import dash_mantine_components as dmc
import numpy as np
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component
from scipy import sparse
from sklearn.decomposition import PCA

from logdash.analyses import features
from logdash.analyses.base import ComputeContext, register
from logdash.analyses.features import (
    DEFAULT_GROUP,
    DEFAULT_SCALING,
    DEFAULT_WINDOW,
    GROUPS,
    SCALINGS,
    WINDOWS,
    Lru,
)
from logdash.app import figures, theme, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict, page_url
from logdash.cache.store import CacheStore

KEY = "pca"
UPSTREAM = "features"
VARIANCE_TARGET = 0.95  # share of the variance the retained components keep
MAX_K = 30  # most components kept for the SPE
ALPHA = 0.001  # Gaussian-theory SPE limit quantile 1 - ALPHA, not a false-alarm rate
RESIDUAL_TOLERANCE = 1e-9  # residual variance below this share of the total counts as none
MAX_FLAGGED = 0.25  # share of windows above the limit that is logged as a warning
N_PROJ = 3  # stored scores per window
MAX_LOADINGS = 10  # stored components with their loadings
MAX_SCREE = 30  # components in the scree plot
TOP_RESIDUAL = 3  # templates stored per window
CHUNK = 20_000  # windows per dense block when computing residuals
COLORS = ("time", "entity", "score")
DEFAULT_COLOR = "time"
COMPONENTS = tuple(range(1, 6))  # loadings view
DEFAULT_COMPONENT = 1
LOADINGS_SHOWN = 15
MAX_POINTS_2D = 5000
MAX_POINTS_3D = 3000
MAX_CATEGORIES = 9  # colours for entities; the rest is 'other'
TABLE_ROWS = 30
TEXT_CHARS = 60


# --------------------------------------------------------------------------- compute


def fit_name(kind: str, group: str, window_s: int, scaling: str) -> str:
    extension = "json" if kind == "fit" else "parquet"
    return f"{kind}_{group}_{window_s}_{scaling}.{extension}"


def spe_limit(eigenvalues: np.ndarray, alpha: float = ALPHA) -> float:
    """Jackson-Mudholkar control limit of the SPE from the residual eigenvalues.

    With ``theta_i`` the sums of the ``i``-th powers of the eigenvalues not kept and ``c`` the
    normal quantile at ``1 - alpha``, the limit is
    ``theta1 * (c sqrt(2 theta2 h0^2) / theta1 + 1 + theta2 h0 (h0 - 1) / theta1^2) ^ (1 / h0)``
    with ``h0 = 1 - 2 theta1 theta3 / (3 theta2^2)``. It assumes Gaussian data. Without residual
    variance the limit is infinite, so that rounding noise in the SPE never exceeds it.
    """
    theta1, theta2, theta3 = (float(np.sum(eigenvalues**i)) for i in (1, 2, 3))
    if theta1 <= 0 or theta2 <= 0:
        return math.inf
    c = NormalDist().inv_cdf(1 - alpha)
    h0 = 1 - 2 * theta1 * theta3 / (3 * theta2**2)
    if abs(h0) < 1e-6:  # the exponent blows up; use the normal approximation of the SPE
        return theta1 + c * math.sqrt(2 * theta2)
    base = c * math.sqrt(2 * theta2) * abs(h0) / theta1 + 1 + theta2 * h0 * (h0 - 1) / theta1**2
    return theta1 * base ** (1 / h0)


def components_kept(explained: np.ndarray) -> int:
    """Fewest components reaching ``VARIANCE_TARGET`` of the variance (1 to ``MAX_K``)."""
    if len(explained) == 0:
        return 0
    reached = int(np.searchsorted(np.cumsum(explained), VARIANCE_TARGET - 1e-12)) + 1
    return min(max(reached, 1), MAX_K, len(explained))


@dataclass(frozen=True)
class Model:
    """One fitted PCA with the per-window scores, SPE and top residual templates."""

    explained: np.ndarray  # variance ratio per component
    k: int
    limit: float
    loadings: np.ndarray  # MAX_LOADINGS x templates
    scores: np.ndarray  # windows x N_PROJ
    spe: np.ndarray
    residuals: pl.DataFrame  # row, rank, template_id, residual, share


RESIDUAL_SCHEMA = {
    "row": pl.Int64,
    "rank": pl.Int64,
    "template_id": pl.Int64,
    "residual": pl.Float64,
    "share": pl.Float64,
}


def fit_model(scaled: sparse.csr_matrix, template_ids: np.ndarray) -> Model:
    """PCA over the stored windows; residuals are taken in dense blocks to bound memory."""
    n_rows, n_cols = scaled.shape
    if n_rows < 2 or n_cols == 0:
        return Model(
            np.zeros(0),
            0,
            math.inf,
            np.zeros((0, n_cols)),
            np.zeros((n_rows, N_PROJ)),
            np.zeros(n_rows),
            pl.DataFrame(schema=RESIDUAL_SCHEMA),
        )
    with np.errstate(invalid="ignore", divide="ignore"):  # constant columns: zero total variance
        pca = PCA(svd_solver="covariance_eigh").fit(scaled)
    total = float(pca.explained_variance_.sum())
    explained = np.nan_to_num(pca.explained_variance_ / total) if total > 0 else np.zeros(0)
    k = components_kept(explained) if total > 0 else 1
    kept = pca.components_[:k]
    residual_variance = np.maximum(pca.explained_variance_[k:], 0.0)
    no_residual = residual_variance.sum() <= RESIDUAL_TOLERANCE * max(total, 0.0)
    limit = math.inf if no_residual else spe_limit(residual_variance)
    scores = np.zeros((n_rows, N_PROJ))
    spe = np.zeros(n_rows)
    blocks: list[pl.DataFrame] = []
    for lo in range(0, n_rows, CHUNK):
        dense = scaled[lo : lo + CHUNK].toarray() - pca.mean_
        shown = pca.components_[:N_PROJ]
        scores[lo : lo + len(dense), : len(shown)] = dense @ shown.T
        residual = dense - (dense @ kept.T) @ kept
        square = residual**2
        spe[lo : lo + len(dense)] = square.sum(axis=1)
        blocks.append(top_residuals(residual, square, spe[lo : lo + len(dense)], template_ids, lo))
    return Model(
        explained, k, limit, pca.components_[:MAX_LOADINGS], scores, spe, pl.concat(blocks)
    )


def top_residuals(
    residual: np.ndarray, square: np.ndarray, spe: np.ndarray, template_ids: np.ndarray, offset: int
) -> pl.DataFrame:
    """The ``TOP_RESIDUAL`` templates with the largest squared residual per window."""
    n_top = min(TOP_RESIDUAL, square.shape[1])
    best = np.argpartition(-square, n_top - 1, axis=1)[:, :n_top]
    values = np.take_along_axis(square, best, axis=1)
    order = np.argsort(-values, axis=1, kind="stable")
    best = np.take_along_axis(best, order, axis=1)
    values = np.take_along_axis(values, order, axis=1)
    rows = np.repeat(np.arange(len(best)) + offset, n_top)
    share = values / np.where(spe > 0, spe, 1.0)[:, None]
    frame = pl.DataFrame(
        {
            "row": rows,
            "rank": np.tile(np.arange(1, n_top + 1), len(best)),
            "template_id": template_ids[best].ravel(),
            "residual": np.take_along_axis(residual, best, axis=1).ravel(),
            "share": share.ravel(),
        },
        schema=RESIDUAL_SCHEMA,
    )
    return frame.filter(pl.col("residual") != 0)


def compute_all(
    store: CacheStore, out_dir: Path, log: Callable[[str], None] = print
) -> dict[str, int]:
    """Fit every grid of the features stage and write its files; returns row counts."""
    rows: dict[str, int] = {}
    for group in GROUPS:
        for window_s in WINDOWS:
            matrix = features.load_matrix(store, group, window_s)
            for scaling in SCALINGS:
                scaled = features.load_scaled(store, group, window_s, scaling)
                assert scaled.shape[0] == len(matrix.bins), "scaled matrix and index differ"
                model = fit_model(scaled, matrix.template_ids)
                write_fit(out_dir, group, window_s, scaling, matrix, model)
                flagged = int((model.spe > model.limit).sum())
                rows[f"{group}_{window_s}_{scaling}_flagged"] = flagged
                log(
                    f"{group} x {window_s // 60} min, {scaling}: k={model.k}, "
                    f"{flagged} of {len(model.spe)} windows above the SPE limit"
                )
                if flagged > MAX_FLAGGED * len(model.spe):
                    log("  warning: the Gaussian limit flags many windows; rank by spe / limit")
    features.clear_caches()  # the matrices are large; the page loads what it needs
    return rows


def ratios(spe: np.ndarray, limit: float) -> np.ndarray:
    """SPE relative to the control limit; zero when there is no limit (no residual variance)."""
    return spe / limit if math.isfinite(limit) and limit > 0 else np.zeros(len(spe))


def write_fit(
    out_dir: Path,
    group: str,
    window_s: int,
    scaling: str,
    matrix: features.Matrix,
    model: Model,
) -> None:
    entities = np.asarray(matrix.entities, dtype=object)
    scores = pl.DataFrame(
        {
            "row": np.arange(len(model.spe), dtype=np.int64),
            "bin": matrix.bins,
            "entity": entities[matrix.codes] if len(matrix.codes) else [],
            **{f"pc{i + 1}": model.scores[:, i] for i in range(N_PROJ)},
            "spe": model.spe,
            "limit": np.full(len(model.spe), model.limit),
            "ratio": ratios(model.spe, model.limit),
        },
        schema_overrides={"entity": pl.String, "bin": pl.Datetime("us")},
    )
    scores.write_parquet(out_dir / fit_name("scores", group, window_s, scaling), compression="zstd")
    model.residuals.write_parquet(
        out_dir / fit_name("residuals", group, window_s, scaling), compression="zstd"
    )
    description = {
        "n_windows": len(model.spe),
        "k": model.k,
        "limit": model.limit if math.isfinite(model.limit) else None,
        "alpha": ALPHA,
        "explained": model.explained[:MAX_SCREE].tolist(),
        "template_ids": [int(i) for i in matrix.template_ids],
        "loadings": model.loadings.tolist(),
    }
    (out_dir / fit_name("fit", group, window_s, scaling)).write_text(json.dumps(description))


# ----------------------------------------------------------------------- the loaded fit


@dataclass(frozen=True)
class Fit:
    """A stored fit in memory: one row per window, loadings and the residual templates."""

    bins: np.ndarray  # datetime64[us]
    codes: np.ndarray
    entities: list[str]
    scores: np.ndarray  # windows x N_PROJ
    spe: np.ndarray
    limit: float
    k: int
    explained: np.ndarray
    loadings: np.ndarray
    template_ids: np.ndarray
    residuals: pl.DataFrame
    window_s: int


_FITS: Lru[Fit] = Lru(4)


def clear_caches() -> None:
    """Forget every loaded fit (the next call loads from disk)."""
    _FITS.items.clear()


def fit_stamp(store: CacheStore, group: str, window_s: int, scaling: str) -> tuple[int, ...]:
    names = (fit_name(kind, group, window_s, scaling) for kind in ("fit", "scores", "residuals"))
    return tuple(store.path(KEY, name).stat().st_mtime_ns for name in names)


def load_fit(
    store: CacheStore,
    group: str = DEFAULT_GROUP,
    window_s: int = DEFAULT_WINDOW,
    scaling: str = DEFAULT_SCALING,
) -> Fit:
    """Fit of one grid as written by the build; kept in memory until a file changes."""
    key = (str(store.root), group, window_s, scaling, fit_stamp(store, group, window_s, scaling))

    def make() -> Fit:
        info = store.read_json(KEY, fit_name("fit", group, window_s, scaling))
        scores = store.read_parquet(KEY, fit_name("scores", group, window_s, scaling))
        entities = sorted(scores["entity"].unique().to_list())
        mapping = {name: i for i, name in enumerate(entities)}
        codes = scores["entity"].replace_strict(mapping, return_dtype=pl.Int64).to_numpy()
        return Fit(
            scores["bin"].to_numpy(),
            codes,
            entities,
            scores.select(f"pc{i + 1}" for i in range(N_PROJ)).to_numpy(),
            scores["spe"].to_numpy(),
            math.inf if info["limit"] is None else float(info["limit"]),
            int(info["k"]),
            np.asarray(info["explained"], dtype=np.float64),
            np.asarray(info["loadings"], dtype=np.float64).reshape(
                len(info["loadings"]), len(info["template_ids"])
            ),
            np.asarray(info["template_ids"], dtype=np.int64),
            store.read_parquet(KEY, fit_name("residuals", group, window_s, scaling)),
            window_s,
        )

    return _FITS.get(key, make)


def load_spe(
    store: CacheStore,
    group: str = DEFAULT_GROUP,
    window_s: int = DEFAULT_WINDOW,
    scaling: str = DEFAULT_SCALING,
) -> pl.DataFrame:
    """``row, bin, entity, spe, limit, ratio`` of one grid (what the anomaly module reads).

    ``ratio = spe / limit`` is the comparable score; the limit is Gaussian theory, so prefer the
    ratio or its rank over ``spe > limit``.
    """
    scores = store.read_parquet(KEY, fit_name("scores", group, window_s, scaling))
    return scores.select("row", "bin", "entity", "spe", "limit", "ratio")


def load_residuals(
    store: CacheStore,
    group: str = DEFAULT_GROUP,
    window_s: int = DEFAULT_WINDOW,
    scaling: str = DEFAULT_SCALING,
) -> pl.DataFrame:
    """``row, rank, template_id, residual, share`` of one grid, rows as in ``load_spe``."""
    return store.read_parquet(KEY, fit_name("residuals", group, window_s, scaling))


# ---------------------------------------------------------------------- pure helpers


def select_windows(fit: Fit, lo: Any, hi: Any, entities: tuple[str, ...] = ()) -> np.ndarray:
    """Row numbers with ``bin`` in ``[lo, hi)`` and, if given, an entity in ``entities``."""
    allowed = np.array([not entities or e in entities for e in fit.entities], dtype=bool)
    keep = (
        (fit.bins >= np.datetime64(lo, "us"))
        & (fit.bins < np.datetime64(hi, "us"))
        & allowed[fit.codes]
    )
    return np.flatnonzero(keep)


def sample_rows(rows: np.ndarray, spe: np.ndarray, n: int) -> np.ndarray:
    """At most ``n`` of ``rows``: the quarter with the highest SPE, the rest evenly spread."""
    if len(rows) <= n:
        return rows
    order = rows[np.argsort(-spe[rows], kind="stable")]
    worst, rest = order[: n // 4], np.sort(order[n // 4 :])
    spread = rest[np.linspace(0, len(rest) - 1, n - len(worst)).astype(int)]
    return np.sort(np.concatenate([worst, spread]))


def score_ratio(fit: Fit, rows: np.ndarray) -> np.ndarray:
    """SPE relative to the control limit (above 1 is above the limit)."""
    return ratios(fit.spe[rows], fit.limit)


def label(template_id: int) -> str:
    return f"T{template_id}"


def hours_since(bins: np.ndarray, origin: np.datetime64) -> np.ndarray:
    return (bins - origin) / np.timedelta64(1, "h")


def window_text(fit: Fit, rows: np.ndarray) -> list[str]:
    """Hover text per window: time, entity, SPE and its ratio to the limit."""
    ratio = score_ratio(fit, rows)
    return [
        f"{str(fit.bins[r])[:19]}<br>{fit.entities[fit.codes[r]]}<br>"
        f"SPE {fit.spe[r]:.3g} ({x:.2f} x limit)"
        for r, x in zip(rows, ratio, strict=True)
    ]


# ------------------------------------------------------------------------ figures


def scree_figure(fit: Fit, scheme: str | None) -> go.Figure:
    if len(fit.explained) == 0:
        return figures.empty_figure("no windows", scheme)
    x = list(range(1, len(fit.explained) + 1))
    fig = go.Figure(
        [
            go.Bar(x=x, y=fit.explained.tolist(), name="explained", marker_color=theme.PALETTE[0]),
            go.Scatter(
                x=x, y=np.cumsum(fit.explained).tolist(), name="cumulative", mode="lines+markers"
            ),
        ]
    )
    fig.add_vline(x=fit.k + 0.5, line_dash="dash", line_color=theme.PALETTE[1])
    fig.update_xaxes(title="component", dtick=max(len(x) // 10, 1))
    fig.update_yaxes(title="share of variance", tickformat=".0%")
    cumulative = float(np.sum(fit.explained[: fit.k]))
    figures.add_note(
        fig, f"{fit.k} components keep {cumulative:.0%}; the SPE is what they miss", scheme
    )
    return theme.style(fig, scheme, height=360)


def spe_figure(fit: Fit, rows: np.ndarray, scheme: str | None) -> go.Figure:
    if len(rows) == 0:
        return figures.empty_figure("no windows in the range", scheme)
    shown = sample_rows(rows, fit.spe, MAX_POINTS_2D)
    above = fit.spe[shown] > fit.limit
    fig = go.Figure()
    for flag, name, color in (
        (False, "within limit", theme.PALETTE[0]),
        (True, "above limit", theme.PALETTE[1]),
    ):
        mask = above == flag
        fig.add_trace(
            go.Scattergl(
                x=fit.bins[shown][mask], y=fit.spe[shown][mask], mode="markers", name=name,
                marker={"color": color, "size": 5}, text=window_text(fit, shown[mask]),
                hovertemplate="%{text}<extra></extra>",
            )
        )  # fmt: skip
    if math.isfinite(fit.limit):
        fig.add_hline(y=fit.limit, line_dash="dash", line_color=theme.PALETTE[1])
    fig.update_yaxes(title="SPE", type="log")
    flagged = int((fit.spe[rows] > fit.limit).sum())
    figures.add_note(
        fig, f"{flagged} of {len(rows)} windows above the limit {fit.limit:.3g}", scheme
    )
    return theme.style(fig, scheme, height=360)


def entity_traces(fit: Fit, rows: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Rows grouped by entity: the ``MAX_CATEGORIES`` busiest, the rest as 'other'."""
    counts = np.bincount(fit.codes[rows], minlength=len(fit.entities))
    busiest = np.argsort(-counts, kind="stable")[:MAX_CATEGORIES]
    groups = [(fit.entities[c], rows[fit.codes[rows] == c]) for c in busiest if counts[c] > 0]
    rest = rows[~np.isin(fit.codes[rows], busiest)]
    if len(rest):
        groups.append((f"other ({len(fit.entities) - len(busiest)})", rest))
    return groups


def projection_figure(
    fit: Fit, rows: np.ndarray, color: str, entity: str, dims: int, scheme: str | None
) -> go.Figure:
    """Windows on the first two or three components, coloured by time, entity or SPE ratio."""
    if len(rows) == 0 or fit.scores.shape[1] < dims:
        return figures.empty_figure("no windows in the range", scheme)
    limit = MAX_POINTS_2D if dims == 2 else MAX_POINTS_3D
    shown = sample_rows(rows, fit.spe, limit)
    axes = ("x", "y", "z")

    def trace(rows: np.ndarray, **kwargs: Any) -> go.Figure:
        coords = {axes[i]: fit.scores[rows, i] for i in range(dims)}
        marker = {"size": 3 if dims == 3 else 5, **kwargs.pop("marker", {})}
        cls = go.Scatter3d if dims == 3 else go.Scattergl
        return cls(
            **coords, mode="markers", marker=marker, text=window_text(fit, rows),
            hovertemplate="%{text}<extra></extra>", **kwargs,
        )  # fmt: skip

    fig = go.Figure()
    if color == "entity":
        for name, part in entity_traces(fit, shown):
            fig.add_trace(trace(part, name=figures.shorten_middle(name)))
    else:
        if color == "time":
            values, title = hours_since(fit.bins[shown], fit.bins.min()), "hours since start"
        else:
            values = np.log10(np.maximum(score_ratio(fit, shown), 1e-3))
            title = "log10 SPE / limit"
        marker = {
            "color": values,
            "colorscale": theme.magnitude(scheme),
            "colorbar": {"title": title},
        }
        fig.add_trace(trace(shown, marker=marker, showlegend=False))
    titles = [f"PC{i + 1}" for i in range(dims)]
    if dims == 3:
        fig.update_scenes(xaxis_title=titles[0], yaxis_title=titles[1], zaxis_title=titles[2])
    else:
        fig.update_xaxes(title=titles[0])
        fig.update_yaxes(title=titles[1])
    shown_note = (
        f"{len(shown)} of {len(rows)} windows" if len(shown) < len(rows) else f"{len(rows)} windows"
    )
    figures.add_note(
        fig, f"{shown_note}, coloured by {color if color != 'entity' else entity}", scheme
    )
    return theme.style(fig, scheme, height=520)


def loadings_figure(
    fit: Fit, component: int, texts: dict[int, str], scheme: str | None
) -> go.Figure:
    """Templates with the largest absolute loading on one component (signed bars)."""
    if component > len(fit.loadings):
        return figures.empty_figure("no such component", scheme)
    weights = fit.loadings[component - 1]
    order = np.lexsort((np.arange(len(weights)), -np.abs(weights)))[:LOADINGS_SHOWN]
    ids = fit.template_ids[order]
    fig = go.Figure(
        go.Bar(
            x=weights[order].tolist(), y=[label(int(i)) for i in ids], orientation="h",
            customdata=[features.short(texts.get(int(i), ""), TEXT_CHARS) for i in ids],
            hovertemplate="%{y}: %{x:.3f}<br>%{customdata}<extra></extra>",
        )
    )  # fmt: skip
    fig.update_yaxes(autorange="reversed", type="category")
    fig.update_xaxes(title=f"loading on PC{component}")
    share = float(fit.explained[component - 1]) if component <= len(fit.explained) else 0.0
    figures.add_note(fig, f"PC{component} explains {share:.1%} of the variance", scheme)
    return theme.style(fig, scheme, height=420)


def flagged_table(
    fit: Fit, rows: np.ndarray, texts: dict[int, str], group: str
) -> tuple[pl.DataFrame, list[str]]:
    """Windows with the highest SPE in the rows, their residual templates, explorer links."""
    top = rows[np.argsort(-fit.spe[rows], kind="stable")][:TABLE_ROWS]
    top = top[fit.spe[top] > fit.limit]
    residuals = fit.residuals.filter(pl.col("row").is_in(top.tolist())).sort("row", "rank")
    by_row: dict[int, list[tuple[int, float]]] = {}
    for row, template_id, share in residuals.select("row", "template_id", "share").iter_rows():
        by_row.setdefault(row, []).append((template_id, share))
    table = pl.DataFrame(
        {
            "window": [str(fit.bins[r])[:19].replace("T", " ") for r in top],
            group: [fit.entities[fit.codes[r]] for r in top],
            "SPE": [round(float(fit.spe[r]), 3) for r in top],
            "x limit": [round(float(fit.spe[r] / fit.limit), 2) for r in top],
            "residual templates": [
                ", ".join(f"{label(t)} {s:.0%}" for t, s in by_row.get(int(r), [])) for r in top
            ],
            "top template": [
                features.short(texts.get(by_row[int(r)][0][0], ""), TEXT_CHARS)
                if by_row.get(int(r))
                else ""
                for r in top
            ],
        },
        schema={
            "window": pl.String, group: pl.String, "SPE": pl.Float64, "x limit": pl.Float64,
            "residual templates": pl.String, "top template": pl.String,
        },
    )  # fmt: skip
    hrefs = [
        page_url(
            "/explorer",
            start=str(fit.bins[r])[:19].replace("T", " "),
            end=str(fit.bins[r] + np.timedelta64(fit.window_s, "s"))[:19].replace("T", " "),
            **{"service" if group == "service" else "host": fit.entities[fit.codes[r]]},
        )
        for r in top
    ]
    return table, hrefs


# ------------------------------------------------------------------------- callbacks


def unavailable(store: CacheStore, group: str, window_s: int, scaling: str) -> str | None:
    """Why the page cannot draw (stage missing or empty), or None."""
    names = (fit_name(kind, group, window_s, scaling) for kind in ("fit", "scores", "residuals"))
    if not all(store.exists(KEY, name) for name in names) or not store.exists(
        UPSTREAM, "meta.json"
    ):
        return "The PCA stage is not built; run `logdash build`."
    return None


def draw_pca(
    store: CacheStore,
    filters: Filters,
    window_s: int,
    group: str,
    scaling: str,
    color: str,
    component: int,
    scheme: str | None,
) -> tuple[go.Figure, go.Figure, go.Figure, go.Figure, go.Figure, Component, str]:
    """Scree, SPE, 2D and 3D projection, loadings, flagged windows and a filter hint."""
    problem = unavailable(store, group, window_s, scaling)
    if problem is not None:
        empty = figures.empty_figure(problem, scheme)
        return empty, empty, empty, empty, empty, dmc.Text(problem), ""
    fit = load_fit(store, group, window_s, scaling)
    if len(fit.spe) < 2:
        message = "Too few windows to fit a PCA."
        empty = figures.empty_figure(message, scheme)
        return empty, empty, empty, empty, empty, dmc.Text(message), ""
    lo, hi = features.visible_range(store, filters, window_s)
    rows = select_windows(fit, lo, hi, features.entity_filter(filters, group))
    texts = features.template_texts(store)
    table, hrefs = flagged_table(fit, rows, texts, group)
    return (
        scree_figure(fit, scheme),
        spe_figure(fit, rows, scheme),
        projection_figure(fit, rows, color, group, 2, scheme),
        projection_figure(fit, rows, color, group, 3, scheme),
        loadings_figure(fit, component, texts, scheme),
        ui.link_table(table, hrefs) if table.height else dmc.Text("no windows above the limit"),
        features.ignored_filters(filters, group),
    )


def color_value(value: str | None) -> str:
    return value if value in COLORS else DEFAULT_COLOR


def component_value(value: str | None) -> int:
    return int(value) if value in tuple(map(str, COMPONENTS)) else DEFAULT_COMPONENT


@register
class Pca:
    key = KEY
    title = "PCA"
    version = "1"
    depends_on: tuple[str, ...] = (UPSTREAM,)

    def compute(self, ctx: ComputeContext) -> None:
        store = CacheStore(ctx.out_dir.parent, ctx.settings)
        for name, count in compute_all(store, ctx.out_dir, ctx.progress).items():
            ctx.note_rows(name, count)

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        segmented = features.segmented
        controls = dmc.Group(
            [
                segmented(
                    "pca-window",
                    [(str(s), f"{s // 60} min") for s in WINDOWS],
                    str(DEFAULT_WINDOW),
                ),
                segmented("pca-group", [(g, f"per {g}") for g in GROUPS], DEFAULT_GROUP),
                segmented("pca-scale", [("log1p", "log1p"), ("tfidf", "TF-IDF")], DEFAULT_SCALING),
                segmented(
                    "pca-color",
                    [
                        ("time", "colour: time"),
                        ("entity", "colour: service or host"),
                        ("score", "colour: SPE"),
                    ],
                    DEFAULT_COLOR,
                ),
                segmented(
                    "pca-component",
                    [(str(c), f"PC{c}") for c in COMPONENTS],
                    str(DEFAULT_COMPONENT),
                ),
            ],
            gap="md",
        )
        return ui.page(
            "PCA",
            controls,
            ui.grid(
                [
                    ui.card("Scree plot", ui.graph("pca-scree", 360)),
                    ui.card("Residual SPE per window", ui.graph("pca-spe", 360)),
                ],
                cols={"base": 1, "lg": 2},
            ),
            ui.card("Projection on PC1 and PC2", ui.graph("pca-2d", 520)),
            ui.card("Projection on PC1 to PC3", ui.graph("pca-3d", 520)),
            ui.card("Loadings (the component picker applies)", ui.graph("pca-loadings", 420)),
            ui.card("Windows above the SPE control limit", dmc.Box(id="pca-table")),
            dmc.Text(id="pca-hint", size="sm", c="dimmed"),
            description=(
                "PCA of the window x template matrix, fitted at build time over all windows. The "
                "time range and the service or host filter select the plotted windows; the "
                "SPE control limit is fixed per fit and comes from Gaussian theory, so read it "
                "as a reference line, not a false-alarm rate. The SPE depends on the "
                "components kept (95 % of the variance): a pattern that a kept component "
                "captures, such as a "
                "volume spike in the per-host fit, is not above the limit. Large selections are "
                "sampled."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            Output("pca-scree", "figure"),
            Output("pca-spe", "figure"),
            Output("pca-2d", "figure"),
            Output("pca-3d", "figure"),
            Output("pca-loadings", "figure"),
            Output("pca-table", "children"),
            Output("pca-hint", "children"),
            Input("pca-window", "value"),
            Input("pca-group", "value"),
            Input("pca-scale", "value"),
            Input("pca-color", "value"),
            Input("pca-component", "value"),
            Input(FILTER_STORE_ID, "data"),
            theme.theme_input(),
        )
        def draw(
            window: str | None,
            group: str | None,
            scaling: str | None,
            color: str | None,
            component: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
        ) -> tuple[go.Figure, go.Figure, go.Figure, go.Figure, go.Figure, Component, str]:
            return draw_pca(
                store,
                from_dict(filters),
                features.window_value(window),
                group if group in GROUPS else DEFAULT_GROUP,
                scaling if scaling in SCALINGS else DEFAULT_SCALING,
                color_value(color),
                component_value(component),
                theme,
            )

        try:  # load the default fit now so that the first request does not read files
            if unavailable(store, DEFAULT_GROUP, DEFAULT_WINDOW, DEFAULT_SCALING) is None:
                load_fit(store)
        except (OSError, ValueError):
            pass
