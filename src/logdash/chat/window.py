"""Time windows for the chat tools: UTC, relative to the end of the data."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from logdash.analyses import temporal
from logdash.cache.store import CacheStore
from logdash.chat.errors import BAD_ARGUMENT, ToolError

WINDOW_RULE = (
    "All times are UTC. Relative times count from the end of the data, not from the "
    "current clock: now is the end of the data, and today is 00:00 UTC on the day the "
    "data ends. A clock time such as 14:20 is that time on the day the data ends. ISO "
    "timestamps such as 2023-05-21 14:20 are read as UTC, and ones with an offset such as "
    "2023-05-21T16:20+02:00 are converted to UTC. last_minutes=N is the N minutes before "
    "end; end defaults to now and start to the beginning of the data, and start and "
    "last_minutes cannot be combined. Every window is widened to whole minutes, cut to "
    "the data span, and excludes its end."
)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TIME_HINT = (
    "use now, today, a clock time such as 14:20, or an ISO timestamp such as 2023-05-21 14:20 (UTC)"
)
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")
MINUTE = timedelta(minutes=1)


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def data_span(store: CacheStore) -> tuple[datetime, datetime]:
    """First and last event time from ``temporal/meta.json`` as UTC-aware datetimes."""
    meta = store.read_json(temporal.KEY, "meta.json")
    out: list[datetime] = []
    for key in ("ts_min", "ts_max"):
        value = meta.get(key) if isinstance(meta, dict) else None
        try:
            if not isinstance(value, str) or not value:
                raise ValueError(key)
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise FileNotFoundError("temporal/meta.json has no data span") from None
        out.append(parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC))
    return out[0], out[1]


def format_time(dt: datetime) -> str:
    return dt.strftime(TIME_FORMAT)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def ceil_minute(dt: datetime) -> datetime:
    return dt if dt == floor_minute(dt) else floor_minute(dt) + MINUTE


def safe_ceil_minute(dt: datetime) -> datetime:
    """ceil_minute, except that the very last representable minute stays as it is."""
    try:
        return ceil_minute(dt)
    except OverflowError:
        return floor_minute(dt)


@dataclass(frozen=True)
class Window:
    start: datetime  # naive UTC, whole minute, inclusive
    end: datetime  # naive UTC, whole minute, exclusive
    clipped: bool = False

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": format_time(self.start),
            "end": format_time(self.end),
            "minutes": self.minutes,
            "clipped": self.clipped,
        }


def parse_time_value(text: str, *, argument: str, day: date, now: datetime) -> datetime:
    """One time argument as naive UTC."""
    value = text.strip()
    low = value.lower()
    if low == "now":
        return now
    if low == "today":
        return datetime.combine(day, time())
    match = _CLOCK.match(value)
    if match:
        h, m, s = match.groups()
        return datetime.combine(day, time(int(h), int(m), int(s or 0)))
    try:
        return _naive_utc(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        raise ToolError(
            BAD_ARGUMENT,
            f"cannot read {argument} '{text}' as a time",
            argument=argument,
            hint=TIME_HINT,
        ) from None


def resolve_window(
    span: tuple[datetime, datetime],
    *,
    start: str | None = None,
    end: str | None = None,
    last_minutes: int | None = None,
) -> Window:
    """Turn the time arguments into a whole-minute window clipped to the data span."""
    ts_min, ts_max = _naive_utc(span[0]), _naive_utc(span[1])
    first = floor_minute(ts_min)
    stop = floor_minute(ts_max) + MINUTE
    day = ts_max.date()
    now = stop
    if start is not None and last_minutes is not None:
        raise ToolError(
            BAD_ARGUMENT,
            "give start or last_minutes, not both",
            argument="last_minutes",
            hint="drop start, or give start and end",
        )
    hi = parse_time_value(end, argument="end", day=day, now=now) if end is not None else now
    if last_minutes is not None:
        try:
            lo = hi - timedelta(minutes=last_minutes)
        except OverflowError:
            raise ToolError(
                BAD_ARGUMENT,
                f"cannot count {last_minutes} minutes back from {format_time(hi)}",
                argument="end",
                hint=TIME_HINT,
            ) from None
    elif start is not None:
        lo = parse_time_value(start, argument="start", day=day, now=now)
    else:
        lo = first
    if start is None and last_minutes is None and hi <= first:
        raise ToolError(
            BAD_ARGUMENT,
            f"the window end {format_time(hi)} lies before the data, "
            f"which starts at {format_time(first)} UTC",
            argument="end",
            hint="choose a window inside the data span",
        )
    if hi <= lo:
        raise ToolError(
            BAD_ARGUMENT,
            f"the window end {format_time(hi)} is not after its start {format_time(lo)}",
            argument="end",
        )
    lo, hi = floor_minute(lo), safe_ceil_minute(hi)
    if hi <= first or lo >= stop:
        raise ToolError(
            BAD_ARGUMENT,
            f"the window {format_time(lo)} to {format_time(hi)} lies outside the data, "
            f"which runs from {format_time(first)} to {format_time(stop)} UTC",
            argument="end" if hi <= first else "start",
            hint="choose a window inside the data span",
        )
    return Window(max(lo, first), min(hi, stop), clipped=lo < first or hi > stop)
