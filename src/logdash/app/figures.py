"""Shared Plotly styling and small figure helpers (downsampling, top-N, binning)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import plotly.graph_objects as go
import polars as pl

MAX_POINTS = 5000
LIGHT = "light"
DARK = "dark"

PALETTE = (
    "#4c6ef5", "#fa5252", "#40c057", "#fab005", "#15aabf",
    "#be4bdb", "#fd7e14", "#82c91e", "#e64980", "#868e96",
)  # fmt: skip

_SCHEMES = {
    LIGHT: {"text": "#212529", "grid": "#dee2e6", "bg": "rgba(0,0,0,0)"},
    DARK: {"text": "#c9c9c9", "grid": "#424242", "bg": "rgba(0,0,0,0)"},
}

# Bin sizes in seconds offered by the time-series pages.
BIN_LADDER = (1, 10, 60, 300, 900, 3600)


def scheme_name(value: str | None) -> str:
    return DARK if value == DARK else LIGHT


def template(scheme: str | None = LIGHT) -> go.layout.Template:
    """Plotly template matching the Mantine light or dark colour scheme."""
    colors = _SCHEMES[scheme_name(scheme)]
    axis = {
        "gridcolor": colors["grid"],
        "zerolinecolor": colors["grid"],
        "linecolor": colors["grid"],
    }
    return go.layout.Template(
        layout={
            "colorway": PALETTE,
            "paper_bgcolor": colors["bg"],
            "plot_bgcolor": colors["bg"],
            "font": {"color": colors["text"]},
            "xaxis": axis,
            "yaxis": axis,
            "margin": {"l": 56, "r": 16, "t": 32, "b": 48},
            "legend": {"orientation": "h", "y": -0.2},
        }
    )


def style(fig: go.Figure, scheme: str | None = LIGHT, **layout: object) -> go.Figure:
    """Apply the shared template (and optional layout overrides) to ``fig``."""
    fig.update_layout(template=template(scheme), **layout)
    return fig


def empty_figure(message: str, scheme: str | None = LIGHT) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, showarrow=False, font={"size": 14})
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return style(fig, scheme)


def axis_type(log: bool) -> str:
    return "log" if log else "linear"


def lttb(
    x: Sequence[float], y: Sequence[float], threshold: int = MAX_POINTS
) -> tuple[np.ndarray, np.ndarray]:
    """Largest-Triangle-Three-Buckets downsampling to at most ``threshold`` points."""
    xs, ys = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    n = len(xs)
    if threshold >= n or threshold < 3:
        return xs, ys
    edges = np.linspace(1, n - 1, threshold - 1).astype(int)
    keep = [0]
    for i in range(threshold - 2):
        lo, hi = edges[i], edges[i + 1]
        nxt_lo, nxt_hi = hi, edges[i + 2] if i + 2 < len(edges) else n
        avg_x, avg_y = xs[nxt_lo:nxt_hi].mean(), ys[nxt_lo:nxt_hi].mean()
        ax, ay = xs[keep[-1]], ys[keep[-1]]
        area = np.abs((ax - avg_x) * (ys[lo:hi] - ay) - (ax - xs[lo:hi]) * (avg_y - ay))
        keep.append(lo + int(area.argmax()))
    keep.append(n - 1)
    return xs[keep], ys[keep]


def top_n_other(
    df: pl.DataFrame,
    label: str,
    value: str,
    n: int = 15,
    other: str = "other",
) -> pl.DataFrame:
    """Keep the ``n`` largest labels (by summed ``value``) and fold the rest into ``other``."""
    totals = df.group_by(label).agg(pl.col(value).sum()).sort(value, descending=True)
    head, tail = totals.head(n), totals.tail(max(totals.height - n, 0))
    if tail.height:
        extra = pl.DataFrame({label: [other], value: [tail[value].sum()]}, schema=head.schema)
        head = pl.concat([head, extra])
    return head


def choose_bin_seconds(start_s: float, end_s: float, max_points: int = MAX_POINTS) -> int:
    """Smallest bin from ``BIN_LADDER`` keeping the visible range under ``max_points``."""
    span = max(end_s - start_s, 0.0)
    for size in BIN_LADDER:
        if math.ceil(span / size) <= max_points:
            return size
    return BIN_LADDER[-1]
