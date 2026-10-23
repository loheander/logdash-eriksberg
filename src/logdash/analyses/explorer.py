"""Module 8: raw log explorer (paged grid over the ``events`` view).

Nothing is precomputed. Every page is one parameterised query with ``LIMIT`` and
``OFFSET``; time ranges are also pushed down to the hour partitions. Other pages
link here with the filters in the URL query (``/explorer?template=12&host=x``).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
import duckdb
from dash import Dash, Input, Output, State, ctx, dcc
from dash.development.base_component import Component

from logdash.analyses.base import ComputeContext, register
from logdash.app import ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict, parse_time, where_clause
from logdash.app.tag_filters import (
    TagFilter,
    apply_tag_filter,
    attach_tags,
    load_tags,
    tag_controls,
    tag_filter,
)
from logdash.cache.store import CacheStore

KEY = "explorer"
PAGE_SIZE = 100
SEARCH_BUDGET_S = 0.15  # a text search stops starting new hour partitions after this long
COLUMNS = ("time", "level", "host", "program", "logger", "python_module", "request_id", "payload")
LINK_KEYS = ("template", "host", "service", "level", "start", "end", "request_id", "q")
SELECT = (
    "SELECT strftime(ts, '%Y-%m-%d %H:%M:%S.%g') AS time, level, host, program, logger, "
    "python_module, request_id, payload FROM events"
)
TS_SELECT = "SELECT ts FROM events"
WINDOW_SELECT = (
    "SELECT ts AS sort_ts, strftime(ts, '%Y-%m-%d %H:%M:%S.%g') AS time, level, host, "
    "program, logger, python_module, request_id, payload FROM events"
)
WINDOW_CAP = 5000  # rows in a two-step window before falling back to one step
TAGS_DESCRIPTION = (
    " Tag filters keep the lines whose template carries the chosen semantic tags; "
    "hide noise keeps lines without tags."
)
EXCLUDED = " (template {tid} is excluded by the tag filters)"


@dataclass(frozen=True)
class TagRestriction:
    """Template ids a tag filter keeps (``keep``) or drops (not ``keep``)."""

    ids: tuple[int, ...]
    keep: bool

    def excludes(self, template_id: int) -> bool:
        """Whether lines of this template are filtered out."""
        return template_id not in self.ids if self.keep else template_id in self.ids


def tag_restriction(store: CacheStore, tag_filter: TagFilter) -> TagRestriction | None:
    """Template ids for the tag filter (ADR002); ``None`` if it is off or there are no tags.

    Only "hide noise" drops the noise templates and keeps lines without a template; with
    "actionable only" or a component, only the passing templates are kept.
    """
    if not tag_filter.active:
        return None
    tags = load_tags(store)
    if tags is None:
        return None
    templates = attach_tags(
        store.read_parquet("templates", "templates.parquet").select("template_id", "template"),
        tags,
    )
    passing = apply_tag_filter(templates, tag_filter)["template_id"].to_list()
    if tag_filter.actionable_only or tag_filter.components:
        return TagRestriction(tuple(sorted(passing)), keep=True)
    dropped = sorted(set(templates["template_id"].to_list()) - set(passing))
    return TagRestriction(tuple(dropped), keep=False) if dropped else None


def tag_clause(path: str, tags: TagRestriction) -> str:
    """``WHERE`` part for a tag restriction; ``path`` is the quoted assignments file."""
    if not tags.ids:
        return "FALSE" if tags.keep else "TRUE"
    marks = ", ".join("?" for _ in tags.ids)
    lines = f"SELECT masked_hash FROM read_parquet('{path}') WHERE template_id IN ({marks})"
    if tags.keep:
        return f"masked_hash IN ({lines})"
    return f"(masked_hash IS NULL OR masked_hash NOT IN ({lines}))"


def hour_values(start: datetime | None, end: datetime | None) -> list[str] | None:
    """Hour partitions (``'00'``..``'23'``) a time range can touch; ``None`` for no pushdown."""
    if start is None or end is None or end <= start or end - start >= timedelta(hours=24):
        return None
    hours: list[str] = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        hours.append(f"{cursor.hour:02d}")
        cursor += timedelta(hours=1)
    return hours


def parse_template(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def merge_link(filters: Filters, link: dict[str, Any] | None) -> Filters:
    """Overlay filters given in the URL query on the global filters."""
    link = link or {}

    def pick(name: str) -> tuple[str, ...] | None:
        value = link.get(name)
        return (str(value),) if value else None

    return Filters(
        start=link.get("start") or filters.start,
        end=link.get("end") or filters.end,
        levels=pick("level") or filters.levels,
        services=pick("service") or filters.services,
        hosts=pick("host") or filters.hosts,
    )


def build_query(
    filters: Filters,
    *,
    assignments: str | None = None,
    template: int | None = None,
    request_id: str | None = None,
    text: str | None = None,
    page: int = 0,
    size: int = PAGE_SIZE,
    hour: str | None = None,
    limit: int | None = None,
    tags: TagRestriction | None = None,
    select: str = SELECT,
    window: tuple[datetime, datetime] | None = None,
) -> tuple[str, list[Any]]:
    """``(sql, params)`` for one page; fetches ``size + 1`` rows to detect a next page.

    ``hour`` restricts the query to one hour partition and ``limit`` replaces the
    page arithmetic by ``LIMIT limit`` (used by the chunked text search). ``tags`` restricts
    to template ids (needs ``assignments``); ``select`` replaces the column list; ``window``
    keeps ``ts`` between two inclusive bounds.
    """
    where, params = where_clause(filters)
    parts = [where.removeprefix("WHERE ")] if where else []
    path = assignments.replace("'", "''") if assignments is not None else None
    hours = (
        [hour]
        if hour is not None
        else hour_values(parse_time(filters.start), parse_time(filters.end))
    )
    if hours is not None:
        parts.append(f"hour IN ({', '.join('?' for _ in hours)})")
        params.extend(hours)
    if template is not None and path is not None:
        parts.append(
            f"masked_hash IN (SELECT masked_hash FROM read_parquet('{path}') WHERE template_id = ?)"
        )
        params.append(template)
    if tags is not None:
        if path is None:
            raise ValueError("a tag restriction needs the assignments path")
        parts.append(tag_clause(path, tags))
        params.extend(tags.ids)
    if request_id:
        parts.append("request_id = ?")
        params.append(request_id.strip())
    if text:
        parts.append("contains(lower(payload), ?)")
        params.append(text.lower())
    if window is not None:
        parts.append("ts >= ? AND ts <= ?")
        params.extend(window)
    clause = f" WHERE {' AND '.join(parts)}" if parts else ""
    if limit is not None:
        return f"{select}{clause} ORDER BY ts LIMIT {int(limit)}", params
    sql = f"{select}{clause} ORDER BY ts LIMIT {int(size) + 1} OFFSET {int(page) * int(size)}"
    return sql, params


def page_slice(found: Sequence[tuple[Any, ...]], stamps: Sequence[Any]) -> list[tuple[Any, ...]]:
    """The page inside window rows (``sort_ts`` first), without ``sort_ts``.

    Lines at the first timestamp that belong to the previous page are skipped.
    """
    lead = sum(1 for s in stamps if s == stamps[0])
    before = sum(1 for row in found if row[0] == stamps[0]) - lead
    return [row[1:] for row in found[before : before + len(stamps)]]


def window_rows(
    con: duckdb.DuckDBPyConnection,
    filters: Filters,
    *,
    assignments: str,
    template: int | None,
    request_id: str | None,
    text: str | None,
    tags: TagRestriction,
    page: int,
    size: int,
) -> list[tuple[Any, ...]] | None:
    """Rows of one page (up to ``size + 1``) in two steps: sort only ``ts``, then read the
    full rows between the first and last stamp. ``None`` means "use the one-step query"
    (all stamps equal, or ``WINDOW_CAP`` rows in the window)."""
    sql, params = build_query(
        filters,
        assignments=assignments,
        template=template,
        request_id=request_id,
        text=text,
        tags=tags,
        select=TS_SELECT,
        page=page,
        size=size,
    )
    stamps = [row[0] for row in con.execute(sql, params).fetchall()]
    if not stamps:
        return []
    if stamps[0] == stamps[-1]:
        return None
    sql, params = build_query(
        filters,
        assignments=assignments,
        template=template,
        request_id=request_id,
        text=text,
        tags=tags,
        select=WINDOW_SELECT,
        window=(stamps[0], stamps[-1]),
        limit=WINDOW_CAP,
    )
    found = con.execute(sql, params).fetchall()
    if len(found) >= WINDOW_CAP:
        return None
    return page_slice(found, stamps)


def fetch_page(
    con: duckdb.DuckDBPyConnection,
    store: CacheStore,
    filters: Filters,
    *,
    template: int | None = None,
    request_id: str | None = None,
    text: str | None = None,
    tags: TagRestriction | None = None,
    page: int = 0,
    size: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows of one page and whether another page follows."""
    assignments = str(store.path("templates", "assignments.parquet"))
    rows = None
    if tags is not None:
        rows = window_rows(
            con,
            filters,
            assignments=assignments,
            template=template,
            request_id=request_id,
            text=text,
            tags=tags,
            page=page,
            size=size,
        )
    if rows is None:
        sql, params = build_query(
            filters,
            assignments=assignments,
            template=template,
            request_id=request_id,
            text=text,
            tags=tags,
            page=page,
            size=size,
        )
        rows = con.execute(sql, params).fetchall()
    records = [dict(zip(COLUMNS, row, strict=True)) for row in rows[:size]]
    return records, len(rows) > size


def search_page(
    con: duckdb.DuckDBPyConnection,
    store: CacheStore,
    filters: Filters,
    *,
    text: str,
    template: int | None = None,
    request_id: str | None = None,
    tags: TagRestriction | None = None,
    page: int = 0,
    size: int = PAGE_SIZE,
    budget_s: float = SEARCH_BUDGET_S,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Text search over hour partitions in time order, stopping early on a time budget.

    A substring scan of ``payload`` over all hours takes seconds, so the hours are scanned
    one at a time until the page is full or ``budget_s`` has passed. Returns the rows, whether
    another page follows, and the last hour scanned when the search was cut short (else None).
    """
    hours = hour_values(parse_time(filters.start), parse_time(filters.end)) or [
        f"{h:02d}" for h in range(24)
    ]
    skip, wanted = page * size, page * size + size + 1
    assignments = str(store.path("templates", "assignments.parquet"))
    deadline = time.monotonic() + budget_s
    found: list[tuple[Any, ...]] = []
    cut: str | None = None
    for index, hour in enumerate(hours):
        if index and time.monotonic() > deadline:
            cut = hours[index - 1]
            break
        sql, params = build_query(
            filters,
            assignments=assignments,
            template=template,
            request_id=request_id,
            text=text,
            hour=hour,
            limit=wanted - len(found),
            tags=tags,
        )
        found.extend(con.execute(sql, params).fetchall())
        if len(found) >= wanted:
            break
    records = [dict(zip(COLUMNS, row, strict=True)) for row in found[skip : skip + size]]
    return records, len(found) > skip + size, cut


def next_page(trigger: str | None, page: int, has_more: bool = True) -> int:
    """Page index after a click; any other trigger (new filters) goes back to page 0."""
    if trigger == "explorer-prev":
        return max(0, page - 1)
    if trigger == "explorer-next":
        return page + 1 if has_more else page
    return 0


def link_from_query(query: dict[str, Any]) -> dict[str, str]:
    """Known, non-empty string values of the URL query."""
    return {k: str(query[k]) for k in LINK_KEYS if isinstance(query.get(k), str) and query[k]}


def describe_link(link: dict[str, str]) -> str:
    extra = {k: v for k, v in link.items() if k not in ("template", "request_id", "q")}
    return ", ".join(f"{k}={v}" for k, v in extra.items())


@register
class Explorer:
    key = KEY
    title = "Explorer"
    version = "1"
    depends_on: tuple[str, ...] = ("templates",)

    def compute(self, ctx: ComputeContext) -> None:
        # Nothing to precompute; the stage only anchors the page in the cache and manifest.
        ctx.note_rows("page_size", PAGE_SIZE)

    def layout(self, store: CacheStore, **query: Any) -> Component:
        link = link_from_query(query)
        with_tags = load_tags(store) is not None
        grid = dag.AgGrid(
            id="explorer-grid",
            rowData=[],
            columnDefs=[
                {"field": "time", "width": 210},
                {"field": "level", "width": 100},
                {"field": "host", "width": 160},
                {"field": "program", "width": 200},
                {"field": "logger", "width": 140},
                {"field": "python_module", "width": 200},
                {"field": "request_id", "width": 300},
                {"field": "payload", "flex": 1, "minWidth": 500, "tooltipField": "payload"},
            ],
            defaultColDef={"resizable": True, "sortable": False},
            style={"height": 620},
        )
        return ui.page(
            "Log explorer",
            dmc.Group(
                [
                    dmc.TextInput(
                        id="explorer-template",
                        label="Template ID",
                        size="xs",
                        w=110,
                        value=link.get("template", ""),
                    ),  # fmt: skip
                    dmc.TextInput(
                        id="explorer-request",
                        label="Request ID",
                        size="xs",
                        w=320,
                        value=link.get("request_id", ""),
                    ),  # fmt: skip
                    dmc.TextInput(
                        id="explorer-text",
                        label="Search payload (case-insensitive)",
                        size="xs",
                        w=320,
                        value=link.get("q", ""),
                    ),  # fmt: skip
                ],
                align="flex-end",
            ),
            tag_controls("explorer", with_tags),
            dmc.Text(describe_link(link), id="explorer-link-text", size="xs", c="dimmed"),
            dmc.Group(
                [
                    dmc.Button("Previous", id="explorer-prev", size="xs", variant="default"),
                    dmc.Button("Next", id="explorer-next", size="xs", variant="default"),
                    dmc.Text("", id="explorer-label", size="sm"),
                ]
            ),
            grid,
            dcc.Store(id="explorer-link", data=link),
            dcc.Store(id="explorer-page", data=0),
            description=(
                "Global filters apply, overridden by filters given in the page URL. "
                f"{PAGE_SIZE} rows per page, ordered by time."
                f"{TAGS_DESCRIPTION if with_tags else ''}"
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        state: dict[str, duckdb.DuckDBPyConnection] = {}

        def connection() -> duckdb.DuckDBPyConnection:
            if "con" not in state:
                state["con"] = store.connect()
            return state["con"].cursor()

        @app.callback(
            Output("explorer-grid", "rowData"),
            Output("explorer-page", "data"),
            Output("explorer-label", "children"),
            Output("explorer-prev", "disabled"),
            Output("explorer-next", "disabled"),
            Input(FILTER_STORE_ID, "data"),
            Input("explorer-link", "data"),
            Input("explorer-template", "value"),
            Input("explorer-request", "value"),
            Input("explorer-text", "value"),
            Input("explorer-hide-noise", "checked"),
            Input("explorer-actionable-only", "checked"),
            Input("explorer-components", "value"),
            Input("explorer-prev", "n_clicks"),
            Input("explorer-next", "n_clicks"),
            State("explorer-page", "data"),
        )
        def update(
            filters: dict[str, Any] | None,
            link: dict[str, Any] | None,
            template: str | None,
            request_id: str | None,
            text: str | None,
            hide_noise: bool | None,
            actionable_only: bool | None,
            components: list[str] | None,
            _prev: int | None,
            _next: int | None,
            page: int | None,
        ) -> tuple[list[dict[str, Any]], int, str, bool, bool]:
            page_index = next_page(ctx.triggered_id, page or 0)
            effective = merge_link(from_dict(filters), link)
            tid = parse_template(template) if template else None
            restriction = tag_restriction(
                store, tag_filter(hide_noise, actionable_only, components)
            )

            cut: list[str | None] = [None]

            def load(index: int) -> tuple[list[dict[str, Any]], bool]:
                if text:
                    rows, more, cut[0] = search_page(
                        connection(),
                        store,
                        effective,
                        text=text,
                        template=tid,
                        request_id=request_id,
                        tags=restriction,
                        page=index,
                    )
                    return rows, more
                return fetch_page(
                    connection(),
                    store,
                    effective,
                    template=tid,
                    request_id=request_id,
                    tags=restriction,
                    page=index,
                )

            rows, more = load(page_index)
            while not rows and page_index > 0 and cut[0] is None:  # paged past the end
                page_index -= 1
                rows, more = load(page_index)
            first = page_index * PAGE_SIZE + 1
            label = f"rows {first}-{first + len(rows) - 1}" if rows else "no matching rows"
            if (
                not rows
                and tid is not None
                and restriction is not None
                and restriction.excludes(tid)
            ):
                label += EXCLUDED.format(tid=tid)
            if cut[0] is not None:
                label += f" (searched through hour {cut[0]} UTC; narrow the time range for more)"
            return rows, page_index, label, page_index == 0, not more
