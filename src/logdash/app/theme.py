"""The light/dark switch: the only module that knows about colour schemes (ADR001).

Two stores carry the state. ``THEME_STORE_ID`` holds the user's explicit choice (``None`` until
the toggle is used, so the OS preference applies). ``SCHEME_STORE_ID`` holds the resolved scheme
(``light`` or ``dark``); pages take it through :func:`theme_input` and pass it to :func:`style`.

The explorer grid needs no layout option. dash-ag-grid 35 uses AG Grid's theming API, whose
default Quartz theme follows a ``data-ag-theme-mode`` attribute on an ancestor element, which the
scheme callback sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import dash_mantine_components as dmc
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc
from dash.development.base_component import Component

LIGHT = "light"
DARK = "dark"
AUTO = "auto"
THEME_STORE_ID = "theme"
SCHEME_STORE_ID = "theme-scheme"
PROVIDER_ID = "mantine-provider"
THEME_TOGGLE_ID = "theme-toggle"

PALETTE = (
    "#4c6ef5", "#fa5252", "#40c057", "#fab005", "#15aabf",
    "#be4bdb", "#fd7e14", "#82c91e", "#e64980", "#868e96",
)  # fmt: skip

Scale = str | tuple[tuple[float, str], ...]


@dataclass(frozen=True)
class Colors:
    """Every colour the app uses for one scheme."""

    text: str
    grid: str
    bg: str
    hover_bg: str
    note: str  # dimmed annotations
    edge: str  # thin structural lines (dendrogram)
    excluded: str  # bars that do not take part
    sequential: Scale
    diverging: Scale  # centred on zero
    magnitude: Scale  # low-to-high counts and scores


COLORS = {
    LIGHT: Colors(
        text="#212529",
        grid="#dee2e6",
        bg="rgba(0,0,0,0)",
        hover_bg="#ffffff",
        note="#868e96",
        edge="#868e96",
        excluded="#adb5bd",
        sequential="Blues",
        diverging="RdBu_r",
        magnitude="Viridis",
    ),
    DARK: Colors(
        text="#c9c9c9",
        grid="#424242",
        bg="rgba(0,0,0,0)",
        hover_bg="#25262b",
        note="#909296",
        edge="#909296",
        excluded="#5c5f66",
        sequential=((0.0, "#25262b"), (1.0, "#74c0fc")),
        diverging=(
            (0.0, "#4dabf7"),
            (0.25, "#1c5d99"),
            (0.5, "#25262b"),
            (0.75, "#a61e4d"),
            (1.0, "#ff8787"),
        ),  # fmt: skip
        magnitude="Viridis",
    ),
}


def scheme_name(value: str | None) -> str:
    return DARK if value == DARK else LIGHT


def next_theme(current: str | None) -> str:
    return LIGHT if scheme_name(current) == DARK else DARK


def colors(scheme: str | None) -> Colors:
    return COLORS[scheme_name(scheme)]


def _plotly_scale(scale: Scale) -> str | list[list[Any]]:
    return scale if isinstance(scale, str) else [list(stop) for stop in scale]


def sequential(scheme: str | None) -> str | list[list[Any]]:
    """Plotly colour scale for low-to-high magnitudes that suits the scheme's background."""
    return _plotly_scale(colors(scheme).sequential)


def diverging(scheme: str | None) -> str | list[list[Any]]:
    """Plotly colour scale centred on zero whose midpoint blends into the background."""
    return _plotly_scale(colors(scheme).diverging)


def magnitude(scheme: str | None) -> str | list[list[Any]]:
    """Plotly colour scale for counts and scores whose low end stays dark."""
    return _plotly_scale(colors(scheme).magnitude)


def template(scheme: str | None = LIGHT) -> go.layout.Template:
    """Plotly template matching the Mantine light or dark colour scheme."""
    c = colors(scheme)
    axis = {"gridcolor": c.grid, "zerolinecolor": c.grid, "linecolor": c.grid}
    return go.layout.Template(
        layout={
            "colorway": PALETTE,
            "paper_bgcolor": c.bg,
            "plot_bgcolor": c.bg,
            "font": {"color": c.text},
            "hoverlabel": {"bgcolor": c.hover_bg, "font": {"color": c.text}},
            "xaxis": {**axis, "automargin": True},
            "yaxis": {**axis, "automargin": True},
            "margin": {"l": 56, "r": 16, "t": 32, "b": 48},
            "legend": {"orientation": "v", "x": 1.02, "xanchor": "left", "y": 1, "yanchor": "top"},
        }
    )


def style(fig: go.Figure, scheme: str | None = LIGHT, **layout: object) -> go.Figure:
    """Apply the shared template (and optional layout overrides) to ``fig``."""
    fig.update_layout(template=template(scheme), **layout)
    return fig


def theme_input() -> Input:
    """The input every themed page callback takes: the resolved scheme."""
    return Input(SCHEME_STORE_ID, "data")


def provider_props(scheme: str | None = None) -> dict[str, Any]:
    """``MantineProvider`` props: follow the OS until ``scheme`` is an explicit choice."""
    props: dict[str, Any] = {"defaultColorScheme": AUTO}
    if scheme in (LIGHT, DARK):
        props["forceColorScheme"] = scheme
    return props


def stores() -> list[Component]:
    return [
        dcc.Store(id=THEME_STORE_ID, storage_type="local", data=None),
        dcc.Store(id=SCHEME_STORE_ID, storage_type="memory", data=LIGHT),
    ]


def toggle() -> Component:
    """Header control; its label shows the active scheme."""
    return dmc.Tooltip(
        dmc.Button(
            "Light",
            id=THEME_TOGGLE_ID,
            variant="default",
            size="xs",
            w=64,
            **{"aria-label": "Switch between light and dark mode"},
        ),
        label="Switch between light and dark mode",
    )


_APPLY_SCHEME = """
function(choice) {
    var dark = choice === 'dark' ||
        (choice !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var scheme = dark ? 'dark' : 'light';
    document.documentElement.setAttribute('data-ag-theme-mode', scheme);
    return [scheme, choice ? scheme : null, dark ? 'Dark' : 'Light'];
}
"""


def register_theme_callbacks(app: Dash) -> None:
    @app.callback(
        Output(THEME_STORE_ID, "data"),
        Input(THEME_TOGGLE_ID, "n_clicks"),
        State(SCHEME_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def toggle_theme(_clicks: int | None, current: str | None) -> str:
        return next_theme(current)

    app.clientside_callback(
        _APPLY_SCHEME,
        Output(SCHEME_STORE_ID, "data"),
        Output(PROVIDER_ID, "forceColorScheme"),
        Output(THEME_TOGGLE_ID, "children"),
        Input(THEME_STORE_ID, "data"),
    )
