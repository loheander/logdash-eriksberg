"""Registry, ``Toolbox`` and the first query tools of the chat assistant (issue #55).

The model reaches the logs only through the tools registered here. Each tool declares a JSON
schema that ``check_arguments`` enforces, reads pre-aggregated tables when it can, and runs
every SQL statement through ``Toolbox.sql`` so that the statements can be recorded and tested.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, cast

import duckdb
import numpy as np
import polars as pl

from logdash.analyses import temporal
from logdash.analyses.explorer import LINK_KEYS, hour_values
from logdash.app.filters import page_url
from logdash.cache.store import CacheStore
from logdash.chat.errors import (
    BAD_ARGUMENT,
    BAD_ARGUMENTS,
    UNAVAILABLE,
    UNKNOWN_TOOL,
    WINDOW_TOO_LONG,
    ToolError,
)
from logdash.chat.window import Window, data_span, format_time, resolve_window
from logdash.ingest.normalize import LEVELS

MAX_EVENTS_MINUTES = 120  # longest window a query on events may cover
MAX_LAST_MINUTES = 1440
MAX_GROUPS = 20  # groups returned by count_events
MAX_SUGGESTIONS = 5
MAX_TEXT_LENGTH = 200  # request_id and text arguments
TEMPLATE_CHARS = 200  # template text in results
EXPLORER = "/explorer"
MISSING = temporal.MISSING
ARGS_HINT = 'send the arguments as one JSON object, for example {"last_minutes": 60}'
EXPECTED = {"string": "a string", "integer": "an integer", "boolean": "true or false"}

ToolFn = Callable[["Toolbox", dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolFn


TOOLS: dict[str, Tool] = {}


def register_tool(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]],
    required: Sequence[str] = (),
) -> Callable[[ToolFn], ToolFn]:
    """Register a tool; its schema is what the model sees and what ``check_arguments`` enforces."""
    if name in TOOLS:
        raise ValueError(f"tool '{name}' is already registered")
    if "\n" in description:
        raise ValueError("a tool description must be one line")
    for prop, spec in properties.items():
        if spec.get("type") not in EXPECTED:
            raise ValueError(f"argument '{prop}' of '{name}' has an unsupported type")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = list(required)

    def decorate(fn: ToolFn) -> ToolFn:
        TOOLS[name] = Tool(name, description, parameters, fn)
        return fn

    return decorate


def tool_specs() -> list[dict[str, Any]]:
    """All tools in the OpenAI/OpenRouter ``tools`` format, as copies."""
    return [
        copy.deepcopy(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
        )
        for t in TOOLS.values()
    ]


WINDOW_PROPERTIES: dict[str, dict[str, Any]] = {
    "start": {
        "type": "string",
        "description": "Window start: now, today, a UTC clock time such as 14:20, or an ISO "
        "timestamp. Default: the start of the data.",
    },
    "end": {
        "type": "string",
        "description": "Window end (exclusive), in the same forms as start. Default: now, "
        "the end of the data.",
    },
    "last_minutes": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_LAST_MINUTES,
        "description": "The N minutes before end. Do not combine with start.",
    },
}
FILTER_PROPERTIES: dict[str, dict[str, Any]] = {
    "host": {"type": "string", "description": "One host name, spelled as in the data."},
    "service": {
        "type": "string",
        "description": "One service (program name) such as nova-compute, spelled as in the data.",
    },
    "level": {
        "type": "string",
        "description": "One log level: DEBUG, INFO, WARNING, ERROR, CRITICAL or UNKNOWN.",
    },
    "template": {"type": "integer", "minimum": 0, "description": "One template id."},
}


def json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list | tuple):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


def parse_arguments(arguments: object) -> dict[str, Any]:
    """The arguments of a tool call as a dict, from a mapping or a JSON string."""
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(
                BAD_ARGUMENTS, f"arguments are not valid JSON ({exc.msg})", hint=ARGS_HINT
            ) from None
        if isinstance(decoded, dict):
            return decoded
    raise ToolError(BAD_ARGUMENTS, "arguments must be a JSON object", hint=ARGS_HINT)


def type_error(name: str, kind: str, value: Any) -> ToolError:
    return ToolError(
        BAD_ARGUMENT, f"{name} must be {EXPECTED[kind]}, not {json_type(value)}", argument=name
    )


def check_arguments(parameters: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Validate ``raw`` against the schema subset the tools use; drop empty values."""
    properties: dict[str, dict[str, Any]] = parameters["properties"]
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in properties:
            raise ToolError(
                BAD_ARGUMENT,
                f"unknown argument '{name}'",
                argument=name,
                suggestions=sorted(properties),
            )
        if value is None:
            continue
        spec = properties[name]
        kind = spec["type"]

        if kind == "string":
            if not isinstance(value, str):
                raise type_error(name, kind, value)
            value = value.strip()
            if not value:
                continue
            limit = spec.get("maxLength")
            if limit is not None and len(value) > limit:
                raise ToolError(
                    BAD_ARGUMENT, f"{name} is longer than {limit} characters", argument=name
                )
            options = spec.get("enum")
            if options is not None and value not in options:
                raise ToolError(
                    BAD_ARGUMENT,
                    f"{name} must be one of {', '.join(options)}, not '{value}'",
                    argument=name,
                    suggestions=list(options),
                )
        elif kind == "integer":
            ok = not isinstance(value, bool) and (
                isinstance(value, int) or (isinstance(value, float) and value.is_integer())
            )
            if not ok:
                raise type_error(name, kind, value)
            value = int(value)
            low, high = spec.get("minimum"), spec.get("maximum")
            if (low is not None and value < low) or (high is not None and value > high):
                if high is None:
                    message = f"{name} must be at least {low}, not {value}"
                else:
                    message = f"{name} must be between {low} and {high}, not {value}"
                raise ToolError(BAD_ARGUMENT, message, argument=name)
        elif not isinstance(value, bool):
            raise type_error(name, kind, value)
        out[name] = value
    for name in parameters.get("required", ()):
        if name not in out:
            raise ToolError(BAD_ARGUMENT, f"missing argument '{name}'", argument=name)
    return out


def to_json(value: Any) -> Any:
    """``value`` with only JSON types; a bug (``TypeError``) if it holds anything else."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return format_time(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_json(v) for v in value]
    if isinstance(value, np.generic):
        return to_json(value.item())
    raise TypeError(f"not JSON-ready: {type(value).__name__}")


def require(store: CacheStore, key: str, name: str) -> Path:
    """Path of a cache file, or an ``unavailable`` error when the stage has not been built."""
    if not store.exists(key, name):
        raise ToolError(UNAVAILABLE, f"the cache has no {key}/{name}; run `logdash build`")
    return store.path(key, name)


def missing_message(store: CacheStore, exc: FileNotFoundError) -> str:
    if exc.filename is None:
        return f"a cache stage is missing ({exc}); run `logdash build`"
    path = Path(str(exc.filename))
    try:
        rel = str(path.relative_to(store.root))
    except ValueError:
        rel = path.name
    return f"the cache has no {rel}; run `logdash build`"


def suggest(value: str, known: Sequence[str]) -> list[str]:
    """Known values that contain ``value``, topped up with close spellings."""
    low = value.lower()
    out = sorted(k for k in known if low in k.lower())
    if len(out) < MAX_SUGGESTIONS:
        close = difflib.get_close_matches(value, known, n=MAX_SUGGESTIONS, cutoff=0.6)
        out += [c for c in close if c not in out]
    return out[:MAX_SUGGESTIONS]


@dataclass(frozen=True)
class Selection:
    host: str | None = None
    service: str | None = None
    level: str | None = None
    template: int | None = None

    def dimensions(self) -> set[str]:
        return set(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None}


class Catalog:
    """Known hosts, services, levels and templates, read lazily from the cache."""

    def __init__(self, store: CacheStore) -> None:
        self.store = store

    @cached_property
    def meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = self.store.read_json(temporal.KEY, "meta.json")
        return meta

    @cached_property
    def hosts(self) -> tuple[str, ...]:
        return tuple(sorted(h for h in self.meta["hosts"] if h != MISSING))

    @cached_property
    def services(self) -> tuple[str, ...]:
        return tuple(sorted(s for s in self.meta["services"] if s != MISSING))

    @cached_property
    def templates(self) -> dict[int, str]:
        path = require(self.store, "templates", "templates.parquet")
        df = pl.read_parquet(path, columns=["template_id", "template"])
        return dict(zip(df["template_id"].to_list(), df["template"].to_list(), strict=True))

    @staticmethod
    def _known(argument: str, value: str, known: Sequence[str]) -> str:
        by_lower = {k.lower(): k for k in known}
        found = by_lower.get(value.lower())
        if found is not None:
            return found
        suggestions = suggest(value, known)
        if suggestions:
            hint = (
                f"the data has {len(known)} {argument}s; pick one of the suggestions "
                "or check the spelling"
            )
        else:
            hint = f"the data has {len(known)} {argument}s and none looks like '{value}'"
        raise ToolError(
            BAD_ARGUMENT,
            f"unknown {argument} '{value}'",
            argument=argument,
            hint=hint,
            suggestions=suggestions,
        )

    def host(self, value: str) -> str:
        return self._known("host", value, self.hosts)

    def service(self, value: str) -> str:
        return self._known("service", value, self.services)

    def level(self, value: str) -> str:
        if value.upper() in LEVELS:
            return value.upper()
        raise ToolError(
            BAD_ARGUMENT, f"unknown level '{value}'", argument="level", suggestions=list(LEVELS)
        )

    def template(self, value: int) -> int:
        ids = self.templates
        if value in ids:
            return value
        raise ToolError(
            BAD_ARGUMENT,
            f"unknown template {value}",
            argument="template",
            hint=f"template ids run from {min(ids)} to {max(ids)}",
        )


class Toolbox:
    """Runs tools by name over one cache. Not thread-safe; use one Toolbox per question."""

    def __init__(self, store: CacheStore, con: duckdb.DuckDBPyConnection | None = None) -> None:
        self.store = store
        self.statements: list[str] = []  # every SQL statement run, in order
        self._con = con
        self._owns_con = False

    def call(self, name: str, arguments: str | Mapping[str, Any]) -> dict[str, Any]:
        """Run a tool; bad input and missing cache stages come back as an error result."""
        tool = TOOLS.get(name)
        if tool is None:
            return ToolError(
                UNKNOWN_TOOL, f"unknown tool '{name}'", suggestions=list(TOOLS)
            ).result()
        try:
            args = check_arguments(tool.parameters, parse_arguments(arguments))
            result: dict[str, Any] = to_json(tool.run(self, args))
            return result
        except ToolError as exc:
            result = to_json(exc.result())
            return result
        except FileNotFoundError as exc:
            return ToolError(UNAVAILABLE, missing_message(self.store, exc)).result()

    def sql(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run one statement and record it; the only place that runs SQL."""
        self.statements.append(sql)
        if self._con is None:
            if not self.store.events_dir.exists():
                raise ToolError(UNAVAILABLE, "the cache has no events; run `logdash build`")
            self._con = self.store.connect()
            self._owns_con = True
        return self._con.execute(sql, list(params)).fetchall()

    def parquet(self, key: str, name: str) -> str:
        """Path of a cache file, quoted for use inside ``read_parquet('...')``."""
        return str(require(self.store, key, name)).replace("'", "''")

    @cached_property
    def span(self) -> tuple[datetime, datetime]:
        return data_span(self.store)

    @cached_property
    def catalog(self) -> Catalog:
        return Catalog(self.store)

    def window(self, args: Mapping[str, Any]) -> Window:
        return resolve_window(
            self.span,
            start=args.get("start"),
            end=args.get("end"),
            last_minutes=args.get("last_minutes"),
        )

    def selection(self, args: Mapping[str, Any]) -> Selection:
        cat = self.catalog
        host, service, level, template = (args.get(k) for k in FILTER_PROPERTIES)
        return Selection(
            host=cat.host(host) if host is not None else None,
            service=cat.service(service) if service is not None else None,
            level=cat.level(level) if level is not None else None,
            template=cat.template(template) if template is not None else None,
        )

    def events_clause(self, window: Window) -> tuple[str, list[Any]]:
        """Hour pushdown and time bounds for a query on ``events``."""
        hours = hour_values(window.start, window.end)
        if hours is None or window.minutes > MAX_EVENTS_MINUTES:
            raise ValueError("events window too long")
        marks = ", ".join("?" for _ in hours)
        return f"hour IN ({marks}) AND ts >= ? AND ts < ?", [*hours, window.start, window.end]

    def close(self) -> None:
        if self._con is not None and self._owns_con:
            self._con.close()
        self._con = None
        self._owns_con = False


def explorer_url(
    window: Window,
    selection: Selection,
    *,
    request_id: str | None = None,
    text: str | None = None,
) -> str:
    """Explorer deep link for a window and filters; ``end`` is exclusive like the page's."""
    values: dict[str, Any] = {
        "template": str(selection.template) if selection.template is not None else None,
        "host": selection.host,
        "service": selection.service,
        "level": selection.level,
        "start": format_time(window.start),
        "end": format_time(window.end),
        "request_id": request_id,
        "q": text,
    }
    return page_url(EXPLORER, **{k: values.get(k) for k in LINK_KEYS})


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass(frozen=True)
class Source:
    key: str
    file: str
    columns: dict[str, str]  # dimension -> column

    @property
    def label(self) -> str:
        return f"{self.key}/{self.file}"


AGGREGATES: tuple[Source, ...] = (
    Source("temporal", "rate_60.parquet", {"level": "level", "service": "program"}),
    Source("temporal", "host_60.parquet", {"host": "host"}),
    Source(
        "templates", "template_counts.parquet", {"template": "template_id", "service": "program"}
    ),
    Source(
        "templates", "template_counts_host.parquet", {"template": "template_id", "host": "host"}
    ),
)
EVENTS_FILTERS = {"host": "host = ?", "service": "program = ?", "level": "level = ?"}
EVENTS_GROUPS = {
    "host": f"coalesce(host, '{MISSING}')",
    "service": f"coalesce(program, '{MISSING}')",
    "level": "level",
}
GROUP_BY = ("host", "service", "level")
DIMENSIONS = ("host", "service", "level", "template")


def pick_source(needed: set[str]) -> Source | None:
    """First aggregate table whose columns cover the needed dimensions."""
    return next((s for s in AGGREGATES if needed <= s.columns.keys()), None)


@register_tool(
    "count_events",
    "Count log events in a time window, optionally filtered by host, service, level and "
    "template and split by host, service or level.",
    {
        **WINDOW_PROPERTIES,
        **FILTER_PROPERTIES,
        "group_by": {
            "type": "string",
            "enum": list(GROUP_BY),
            "description": f"Split the count by host, service or level; at most {MAX_GROUPS} "
            "groups, largest first.",
        },
    },
)
def count_events(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    window = box.window(args)
    sel = box.selection(args)
    group: str | None = args.get("group_by")
    needed = sel.dimensions() | ({group} if group else set())
    source = pick_source(needed)
    values = sel.as_dict()
    params: list[Any]
    if source is not None:
        path = box.parquet(source.key, source.file)
        where = ["bin >= ?", "bin < ?"]
        params = [window.start, window.end]
        for dim in DIMENSIONS:
            if dim in values:
                where.append(f"{source.columns[dim]} = ?")
                params.append(values[dim])
        head = f"{source.columns[group]} AS grp, " if group else ""
        sql = (
            f"SELECT {head}sum(n)::BIGINT AS n FROM read_parquet('{path}') "
            f"WHERE {' AND '.join(where)}" + (" GROUP BY 1" if group else "")
        )
        label = source.label
    else:
        if window.minutes > MAX_EVENTS_MINUTES:
            raise ToolError(
                WINDOW_TOO_LONG,
                f"counting by {', '.join(sorted(needed))} together needs the events table, "
                f"which allows a window of at most {MAX_EVENTS_MINUTES} minutes; this window "
                f"has {window.minutes}",
                argument="start",
                hint=f"narrow the window, for example last_minutes={MAX_EVENTS_MINUTES}, "
                "or drop a filter or the group_by",
                limit=MAX_EVENTS_MINUTES,
                minutes=window.minutes,
            )
        clause, params = box.events_clause(window)
        where = [clause]
        for dim, condition in EVENTS_FILTERS.items():
            if dim in values:
                where.append(condition)
                params.append(values[dim])
        if sel.template is not None:
            assignments = box.parquet("templates", "assignments.parquet")
            where.append(
                "masked_hash IN (SELECT masked_hash FROM "
                f"read_parquet('{assignments}') WHERE template_id = ?)"
            )
            params.append(sel.template)
        head = f"{EVENTS_GROUPS[group]} AS grp, " if group else ""
        sql = f"SELECT {head}count(*) AS n FROM events WHERE {' AND '.join(where)}" + (
            " GROUP BY 1" if group else ""
        )
        label = "events"
    rows = box.sql(sql, params)
    pairs: list[tuple[str, int]] = []
    if group:
        pairs = sorted(((str(g), int(n)) for g, n in rows if n), key=lambda p: (-p[1], p[0]))
        total = sum(n for _, n in pairs)
    else:
        total = int(rows[0][0] or 0)
    result: dict[str, Any] = {
        "total": total,
        "window": window.as_dict(),
        "filters": values,
        "source": label,
        "link": explorer_url(window, sel),
    }
    if sel.template is not None:
        result["template_text"] = truncate(box.catalog.templates[sel.template], TEMPLATE_CHARS)
    if group:
        groups: list[dict[str, Any]] = []
        for value, n in pairs[:MAX_GROUPS]:
            entry: dict[str, Any] = {group: value, "count": n}
            if value != MISSING:
                entry["link"] = explorer_url(
                    window, replace(sel, **cast(dict[str, Any], {group: value}))
                )
            groups.append(entry)
        result["group_by"] = group
        result["groups"] = groups
        result["groups_total"] = len(pairs)
        result["groups_truncated"] = len(pairs) > MAX_GROUPS
    return result


@register_tool(
    "explorer_link",
    "Build a link to the log explorer page that shows the raw lines for a window and filters.",
    {
        **WINDOW_PROPERTIES,
        **FILTER_PROPERTIES,
        "request_id": {
            "type": "string",
            "maxLength": MAX_TEXT_LENGTH,
            "description": "One request id, such as req-…",
        },
        "text": {
            "type": "string",
            "maxLength": MAX_TEXT_LENGTH,
            "description": "Case-insensitive text the payload must contain.",
        },
    },
)
def explorer_link(box: Toolbox, args: dict[str, Any]) -> dict[str, Any]:
    window = box.window(args)
    sel = box.selection(args)
    return {
        "link": explorer_url(window, sel, request_id=args.get("request_id"), text=args.get("text")),
        "window": window.as_dict(),
        "filters": {**sel.as_dict(), **{k: args[k] for k in ("request_id", "text") if k in args}},
    }
