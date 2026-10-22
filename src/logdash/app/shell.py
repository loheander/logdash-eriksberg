"""Mantine AppShell: header, registry-driven navigation, theme toggle, filter controls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import dash_mantine_components as dmc
from dash import Dash, Input, Output, State, ctx, dcc, page_container
from dash.development.base_component import Component

from logdash.analyses.base import Registry
from logdash.app.figures import DARK, LIGHT, scheme_name
from logdash.app.filters import FILTER_STORE_ID, build_filters, parse_time, with_time_range

THEME_STORE_ID = "theme"
PROVIDER_ID = "mantine-provider"
THEME_TOGGLE_ID = "theme-toggle"
LEVEL_OPTIONS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "UNKNOWN")


@dataclass(frozen=True)
class FilterOptions:
    """Choices offered by the global filter controls."""

    services: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    min_date: str | None = None
    max_date: str | None = None


def next_theme(current: str | None) -> str:
    return LIGHT if scheme_name(current) == DARK else DARK


def navbar_links(registry: Registry) -> list[Component]:
    links = [dmc.NavLink(label="Home", href="/", refresh=False)]
    links += [dmc.NavLink(label=a.title, href=f"/{a.key}", refresh=False) for a in registry.pages()]
    return links


def _multi(control_id: str, label: str, data: Sequence[str]) -> Component:
    return dmc.MultiSelect(
        id=control_id,
        label=label,
        data=list(data),
        value=[],
        searchable=True,
        clearable=True,
        size="xs",
        mb="xs",
    )


def filter_controls(options: FilterOptions) -> Component:
    return dmc.Stack(
        [
            dmc.Text("Filters (UTC)", fw=600, size="sm"),
            dmc.DatePickerInput(
                id="filter-dates",
                type="range",
                label="Date range",
                value=[],
                minDate=options.min_date,
                maxDate=options.max_date,
                clearable=True,
                size="xs",
            ),
            _multi("filter-levels", "Level", LEVEL_OPTIONS),
            _multi("filter-services", "Service", options.services),
            _multi("filter-hosts", "Host", options.hosts),
        ],
        gap=2,
    )


def header() -> Component:
    return dmc.Group(
        [
            dmc.Title("logdash", order=3),
            dmc.ActionIcon("◐", id=THEME_TOGGLE_ID, variant="default", size="lg"),
        ],
        justify="space-between",
        h="100%",
        px="md",
    )


def make_layout(registry: Registry, options: FilterOptions) -> Component:
    """Full page skeleton; pages render into ``page_container``."""
    return dmc.MantineProvider(
        id=PROVIDER_ID,
        defaultColorScheme=LIGHT,
        forceColorScheme=LIGHT,
        children=[
            dcc.Store(id=THEME_STORE_ID, storage_type="local", data=LIGHT),
            dcc.Store(
                id=FILTER_STORE_ID,
                storage_type="session",
                data=build_filters(None, None, None, None),
            ),
            dmc.AppShell(
                [
                    dmc.AppShellHeader(header()),
                    dmc.AppShellNavbar(
                        dmc.ScrollArea(
                            [
                                *navbar_links(registry),
                                dmc.Divider(my="sm"),
                                filter_controls(options),
                            ],
                            p="xs",
                        )
                    ),
                    dmc.AppShellMain(page_container),
                ],
                header={"height": 56},
                navbar={"width": 260, "breakpoint": "sm"},
                padding="md",
            ),
        ],
    )


def merge_filters(
    trigger: str | None,
    current: dict[str, Any] | None,
    dates: list[str] | None,
    levels: list[str] | None,
    services: list[str] | None,
    hosts: list[str] | None,
) -> dict[str, Any]:
    """Filters from the controls; a time range brushed on a chart survives other changes."""
    built = build_filters(dates, levels, services, hosts)
    if trigger == "filter-dates" or not current:
        return built
    return with_time_range(built, parse_time(current.get("start")), parse_time(current.get("end")))


def register_shell_callbacks(app: Dash) -> None:
    @app.callback(
        Output(THEME_STORE_ID, "data"),
        Input(THEME_TOGGLE_ID, "n_clicks"),
        State(THEME_STORE_ID, "data"),
        prevent_initial_call=True,
    )
    def toggle_theme(_clicks: int | None, current: str | None) -> str:
        return next_theme(current)

    @app.callback(Output(PROVIDER_ID, "forceColorScheme"), Input(THEME_STORE_ID, "data"))
    def apply_theme(theme: str | None) -> str:
        return scheme_name(theme)

    @app.callback(
        Output(FILTER_STORE_ID, "data"),
        Input("filter-dates", "value"),
        Input("filter-levels", "value"),
        Input("filter-services", "value"),
        Input("filter-hosts", "value"),
        State(FILTER_STORE_ID, "data"),
    )
    def update_filters(
        dates: list[str] | None,
        levels: list[str] | None,
        services: list[str] | None,
        hosts: list[str] | None,
        current: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return merge_filters(ctx.triggered_id, current, dates, levels, services, hosts)
