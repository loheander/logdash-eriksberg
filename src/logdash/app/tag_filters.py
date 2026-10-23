"""Semantic tag filters shared by the pages (ADR002). Reads only the cache (ADR001)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import dash_mantine_components as dmc
import polars as pl
from dash.development.base_component import Component

from logdash.cache.store import CacheStore
from logdash.tagging.model import COMPONENTS

TAG_COLUMNS = ("tagged", "actionable", "component", "noise", "summary")
TOOLTIP_DELAY_MS = 300


@dataclass(frozen=True)
class TagFilter:
    """Active tag filters; the default restricts nothing."""

    hide_noise: bool = False
    actionable_only: bool = False
    components: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.hide_noise or self.actionable_only or bool(self.components)


def tag_filter(
    hide_noise: bool | None, actionable_only: bool | None, components: Sequence[str] | None
) -> TagFilter:
    """Filter from control values: ``None`` means off; unknown components are dropped."""
    return TagFilter(
        hide_noise=bool(hide_noise),
        actionable_only=bool(actionable_only),
        components=tuple(c for c in (components or ()) if c in COMPONENTS),
    )


def load_tags(store: CacheStore) -> pl.DataFrame | None:
    """Cached tags, or ``None`` if absent or if no template is tagged (pages hide the tag UI)."""
    # imported here: analyses.tags imports analyses.templates, which imports this module
    from logdash.analyses.tags import read_tags

    tags = read_tags(store)
    if tags is None or not tags["tagged"].any():
        return None
    return tags


def attach_tags(templates: pl.DataFrame, tags: pl.DataFrame) -> pl.DataFrame:
    """``templates`` plus ``TAG_COLUMNS``, matched on the template text (ADR002).

    Ids can be renumbered by a templates rebuild that did not re-tag, so the text is the
    key. Texts without tags get ``tagged = False`` and nulls. Row order and count are kept.
    """
    by_text = tags.select("template", *TAG_COLUMNS).unique(
        "template", keep="first", maintain_order=True
    )
    return templates.join(by_text, on="template", how="left", maintain_order="left").with_columns(
        pl.col("tagged").fill_null(False)
    )


def apply_tag_filter(df: pl.DataFrame, tag_filter: TagFilter) -> pl.DataFrame:
    """Rows of a frame with ``TAG_COLUMNS`` that pass the filter.

    Hide noise keeps untagged rows; actionable only and a component choice drop them.
    """
    if tag_filter.hide_noise:
        df = df.filter(~pl.col("noise").fill_null(False))
    if tag_filter.actionable_only:
        df = df.filter(pl.col("actionable").fill_null(False))
    if tag_filter.components:
        df = df.filter(pl.col("component").is_in(list(tag_filter.components)).fill_null(False))
    return df


def tag_controls(prefix: str, visible: bool) -> Component:
    """The three tag filter controls; rendered hidden (not omitted) when there are no tags."""
    hidden: dict[str, Any] = {} if visible else {"style": {"display": "none"}}
    return dmc.Group(
        [
            dmc.Switch(id=f"{prefix}-hide-noise", label="Hide noise", checked=False, size="sm"),
            dmc.Switch(
                id=f"{prefix}-actionable-only", label="Actionable only", checked=False, size="sm"
            ),
            dmc.MultiSelect(
                id=f"{prefix}-components",
                label="Component (empty = all)",
                data=list(COMPONENTS),
                value=[],
                clearable=True,
                size="xs",
                w=320,
            ),
        ],
        id=f"{prefix}-tag-filters",
        align="flex-end",
        gap="lg",
        mb="xs",
        **hidden,
    )


def tag_column_defs() -> list[dict[str, Any]]:
    """AG Grid columns for the tag cells (see ``tag_cells``)."""
    return [
        {"field": "actionable", "headerName": "Actionable", "width": 110},
        {"field": "component", "headerName": "Component", "width": 130},
        {"field": "noise", "headerName": "Noise", "width": 90},
    ]


def flag(value: bool | None) -> str:
    return "" if value is None else "yes" if value else "no"


def tag_cells(row: Mapping[str, Any]) -> dict[str, Any]:
    """Grid values of one row with ``TAG_COLUMNS``; untagged rows get empty cells, no summary."""
    return {
        "actionable": flag(row["actionable"]),
        "component": row["component"] or "",
        "noise": flag(row["noise"]),
        "summary": row["summary"],
    }
