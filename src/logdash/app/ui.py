"""Small Mantine building blocks shared by the pages."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import dash_mantine_components as dmc
import polars as pl
from dash import dcc
from dash.development.base_component import Component


def page(title: str, *children: Component, description: str | None = None) -> Component:
    """Standard page body: title, optional description, then the given blocks."""
    head: list[Component] = [dmc.Title(title, order=2)]
    if description:
        head.append(dmc.Text(description, c="dimmed", size="sm"))
    return dmc.Stack([*head, *children], gap="md")


def card(title: str, *children: Component) -> Component:
    return dmc.Card(
        [dmc.Text(title, fw=600, mb="xs"), *children],
        withBorder=True,
        padding="md",
        radius="md",
    )


def stat_card(label: str, value: str) -> Component:
    return dmc.Card(
        [dmc.Text(label, size="xs", c="dimmed", tt="uppercase"), dmc.Text(value, fw=700, fz=24)],
        withBorder=True,
        padding="md",
        radius="md",
    )


def grid(children: Sequence[Component], cols: int | dict[str, int] = 2) -> Component:
    return dmc.SimpleGrid(list(children), cols=cols, spacing="md")


def graph(graph_id: str, height: int = 360) -> Component:
    return dcc.Graph(
        id=graph_id, style={"height": height}, config={"displaylogo": False}, figure={}
    )


ROW_PX = 15  # height of one labelled row in the tall plots
TALL_PX = 720  # height of the scrolling area holding a tall plot


def pixel_height(rows: int) -> int:
    """Height for a plot with one labelled row per item (at least 320 px)."""
    return max(320, ROW_PX * rows + 110)


def tall_graph(graph_id: str) -> Component:
    """A tall plot inside a scrolling area (its height is set by the callback)."""
    return dmc.ScrollArea(graph(graph_id, 320), h=TALL_PX, type="auto")


def scale_toggle(toggle_id: str) -> Component:
    """Linear/log switch; the value is ``"linear"`` or ``"log"``."""
    return dmc.SegmentedControl(
        id=toggle_id, data=["linear", "log"], value="linear", size="xs", w=160
    )


def fmt_int(value: Any) -> str:
    return "-" if value is None else f"{int(value):,}".replace(",", " ")


def table(df: pl.DataFrame, max_rows: int = 200) -> Component:
    """Static Mantine table of a small frame."""
    head = list(df.columns)
    body = [["" if v is None else str(v) for v in row] for row in df.head(max_rows).iter_rows()]
    return dmc.Table(
        data={"head": head, "body": body}, striped=True, highlightOnHover=True, withTableBorder=True
    )


def link_table(
    df: pl.DataFrame, hrefs: Sequence[str], label: str = "explorer", max_rows: int = 200
) -> Component:
    """Static table with a trailing column of links, one per row (``hrefs`` aligns with rows)."""
    rows = list(df.head(max_rows).iter_rows())
    head = dmc.TableThead(dmc.TableTr([dmc.TableTh(c) for c in [*df.columns, label]]))
    body = dmc.TableTbody(
        [
            dmc.TableTr(
                [
                    dmc.TableTd(
                        "" if v is None else str(v),
                        style={"whiteSpace": "nowrap"}
                        if len(str(v)) <= 24
                        else {"wordBreak": "break-all"},
                    )
                    for v in row
                ]
                + [dmc.TableTd(dmc.Anchor("view", href=href, refresh=False))]
            )
            for row, href in zip(rows, hrefs, strict=False)
        ]
    )
    table = dmc.Table([head, body], striped=True, highlightOnHover=True, withTableBorder=True)
    return dmc.ScrollArea(table, h=420) if len(rows) > 10 else table
