"""``create_app``: Dash Pages generated from the analysis registry."""

from __future__ import annotations

from functools import partial

import dash
import dash_mantine_components as dmc
from dash import Dash
from dash.development.base_component import Component

from logdash.analyses.base import REGISTRY, Registry
from logdash.app import ui
from logdash.app.shell import FilterOptions, make_layout, register_shell_callbacks
from logdash.cache.store import CacheStore


def home_layout(registry: Registry) -> Component:
    cards = [
        dmc.Anchor(
            ui.card(a.title, dmc.Text(f"/{a.key}", size="xs", c="dimmed")),
            href=f"/{a.key}",
            refresh=False,
            underline="never",
        )
        for a in registry.ordered()
    ]
    return ui.page(
        "logdash",
        ui.grid(cards, cols=3),
        description="Analyses of the OpenStack log data set. Filters apply to every page.",
    )


def filter_options(store: CacheStore) -> FilterOptions:
    """Distinct services and hosts and the date range, read once from ``events``."""
    con = store.connect()
    try:
        services = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT program FROM events WHERE program IS NOT NULL ORDER BY 1"
            ).fetchall()
        ]
        hosts = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT host FROM events WHERE host IS NOT NULL ORDER BY 1"
            ).fetchall()
        ]
        lo, hi = con.execute("SELECT min(ts), max(ts) FROM events").fetchone() or (None, None)
    finally:
        con.close()
    day = lambda v: v.date().isoformat() if v is not None else None  # noqa: E731
    return FilterOptions(tuple(services), tuple(hosts), day(lo), day(hi))


def create_app(store: CacheStore, registry: Registry | None = None) -> Dash:
    """Build the Dash app: one page per registered analysis at ``/<key>``, overview at ``/``."""
    registry = REGISTRY if registry is None else registry
    app = Dash(
        __name__,
        use_pages=True,
        pages_folder="",
        suppress_callback_exceptions=True,
        title="logdash",
    )
    dash.page_registry.clear()
    dash.register_page("home", path="/", title="logdash", layout=partial(home_layout, registry))
    for analysis in registry.ordered():
        dash.register_page(
            analysis.key,
            path=f"/{analysis.key}",
            title=f"logdash: {analysis.title}",
            layout=partial(analysis.layout, store),
        )
        analysis.register_callbacks(app, store)
    app.layout = make_layout(registry, filter_options(store))
    register_shell_callbacks(app)
    return app
