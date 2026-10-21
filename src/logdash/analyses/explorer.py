"""Module 8: raw log explorer (paged grid over the ``events`` view).

Nothing is precomputed. Every page is one parameterised query with ``LIMIT`` and
``OFFSET``; time ranges are also pushed down to the hour partitions. Other pages
link here with the filters in the URL query (``/explorer?template=12&host=x``).
"""

from __future__ import annotations

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
from logdash.cache.store import CacheStore

KEY = "explorer"
PAGE_SIZE = 100
COLUMNS = ("time", "level", "host", "program", "logger", "python_module", "request_id", "payload")
LINK_KEYS = ("template", "host", "service", "level", "start", "end", "request_id", "q")
SELECT = (
    "SELECT strftime(ts, '%Y-%m-%d %H:%M:%S.%g') AS time, level, host, program, logger, "
    "python_module, request_id, payload FROM events"
)


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


def like_pattern(text: str) -> str:
    """``%text%`` with LIKE wildcards in ``text`` escaped (``ESCAPE '\\'``)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
) -> tuple[str, list[Any]]:
    """``(sql, params)`` for one page; fetches ``size + 1`` rows to detect a next page."""
    where, params = where_clause(filters)
    parts = [where.removeprefix("WHERE ")] if where else []
    hours = hour_values(parse_time(filters.start), parse_time(filters.end))
    if hours is not None:
        parts.append(f"hour IN ({', '.join('?' for _ in hours)})")
        params.extend(hours)
    if template is not None and assignments is not None:
        path = assignments.replace("'", "''")
        parts.append(
            f"masked_hash IN (SELECT masked_hash FROM read_parquet('{path}') WHERE template_id = ?)"
        )
        params.append(template)
    if request_id:
        parts.append("request_id = ?")
        params.append(request_id.strip())
    if text:
        parts.append("payload ILIKE ? ESCAPE '\\'")
        params.append(like_pattern(text))
    clause = f" WHERE {' AND '.join(parts)}" if parts else ""
    sql = f"{SELECT}{clause} ORDER BY ts LIMIT {int(size) + 1} OFFSET {int(page) * int(size)}"
    return sql, params


def fetch_page(
    con: duckdb.DuckDBPyConnection,
    store: CacheStore,
    filters: Filters,
    *,
    template: int | None = None,
    request_id: str | None = None,
    text: str | None = None,
    page: int = 0,
    size: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows of one page and whether another page follows."""
    sql, params = build_query(
        filters,
        assignments=str(store.path("templates", "assignments.parquet")),
        template=template,
        request_id=request_id,
        text=text,
        page=page,
        size=size,
    )
    rows = con.execute(sql, params).fetchall()
    records = [dict(zip(COLUMNS, row, strict=True)) for row in rows[:size]]
    return records, len(rows) > size


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
            _prev: int | None,
            _next: int | None,
            page: int | None,
        ) -> tuple[list[dict[str, Any]], int, str, bool, bool]:
            page_index = next_page(ctx.triggered_id, page or 0)
            effective = merge_link(from_dict(filters), link)
            tid = parse_template(template) if template else None

            def load(index: int) -> tuple[list[dict[str, Any]], bool]:
                return fetch_page(
                    connection(),
                    store,
                    effective,
                    template=tid,
                    request_id=request_id,
                    text=text,
                    page=index,
                )

            rows, more = load(page_index)
            while not rows and page_index > 0:  # paged past the end
                page_index -= 1
                rows, more = load(page_index)
            first = page_index * PAGE_SIZE + 1
            label = f"rows {first}-{first + len(rows) - 1}" if rows else "no matching rows"
            return rows, page_index, label, page_index == 0, not more
