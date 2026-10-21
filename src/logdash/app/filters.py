"""Global filter state (time range, levels, services, hosts) and its SQL translation.

The state lives in one ``dcc.Store`` (``FILTER_STORE_ID``) holding the dict form of
``Filters``. Pages take it as an ``Input`` and either filter small pre-aggregated
tables (``filter_frame``) or build a parameterised ``WHERE`` clause (``where_clause``).
Values are never interpolated into SQL.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

FILTER_STORE_ID = "filters"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


@dataclass(frozen=True)
class Filters:
    """Active global filters; empty means no restriction. ``end`` is exclusive."""

    start: str | None = None  # ISO timestamp, UTC
    end: str | None = None
    levels: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return any((self.start, self.end, self.levels, self.services, self.hosts))


def to_dict(filters: Filters) -> dict[str, Any]:
    return asdict(filters)


def from_dict(data: dict[str, Any] | None) -> Filters:
    """Parse the store content; missing or malformed fields mean 'no filter'."""
    data = data or {}

    def text(name: str) -> str | None:
        value = data.get(name)
        return value if isinstance(value, str) and value else None

    def items(name: str) -> tuple[str, ...]:
        value = data.get(name) or ()
        return tuple(str(v) for v in value) if isinstance(value, list | tuple) else ()

    return Filters(
        start=text("start"),
        end=text("end"),
        levels=items("levels"),
        services=items("services"),
        hosts=items("hosts"),
    )


def build_filters(
    dates: Sequence[str | None] | None,
    levels: Sequence[str] | None,
    services: Sequence[str] | None,
    hosts: Sequence[str] | None,
) -> dict[str, Any]:
    """Turn control values into the store dict; the end date is inclusive in the UI."""
    first, last = (list(dates or []) + [None, None])[:2]
    start = datetime.fromisoformat(first).date() if first else None
    end = datetime.fromisoformat(last).date() + timedelta(days=1) if last else None
    return to_dict(
        Filters(
            start=_iso(start),
            end=_iso(end),
            levels=tuple(levels or ()),
            services=tuple(services or ()),
            hosts=tuple(hosts or ()),
        )
    )


def _iso(day: date | None) -> str | None:
    return datetime.combine(day, datetime.min.time()).isoformat(sep=" ") if day else None


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"not a SQL identifier: {name!r}")
    return name


def where_clause(
    filters: Filters,
    *,
    ts: str | None = "ts",
    level: str | None = "level",
    service: str | None = "program",
    host: str | None = "host",
) -> tuple[str, list[Any]]:
    """``(sql, params)`` with ``?`` placeholders; ``sql`` is empty or starts with ``WHERE``.

    Pass ``None`` for a column the queried relation does not have to skip that filter.
    """
    parts: list[str] = []
    params: list[Any] = []
    if ts and filters.start:
        parts.append(f"{_ident(ts)} >= CAST(? AS TIMESTAMP)")
        params.append(filters.start)
    if ts and filters.end:
        parts.append(f"{_ident(ts)} < CAST(? AS TIMESTAMP)")
        params.append(filters.end)
    for column, values in (
        (level, filters.levels),
        (service, filters.services),
        (host, filters.hosts),
    ):
        if column and values:
            marks = ", ".join("?" for _ in values)
            parts.append(f"{_ident(column)} IN ({marks})")
            params.extend(values)
    return ("WHERE " + " AND ".join(parts), params) if parts else ("", [])


def filter_frame(
    df: pl.DataFrame,
    filters: Filters,
    *,
    level: str | None = None,
    service: str | None = None,
    host: str | None = None,
) -> pl.DataFrame:
    """Apply level/service/host filters to a small aggregate table (named columns only)."""
    for column, values in (
        (level, filters.levels),
        (service, filters.services),
        (host, filters.hosts),
    ):
        if column and values:
            df = df.filter(pl.col(column).is_in(list(values)))
    return df
