from datetime import UTC, datetime

import pytest

from logdash.cache.store import CacheStore
from logdash.chat.errors import ToolError
from logdash.chat.window import WINDOW_RULE, data_span, resolve_window

SPAN = (
    datetime(2023, 5, 21, 0, 1, 10, 665000, tzinfo=UTC),
    datetime(2023, 5, 21, 23, 59, 7, 815000, tzinfo=UTC),
)


def bounds(**kw):
    w = resolve_window(SPAN, **kw)
    return w.start.strftime("%Y-%m-%d %H:%M"), w.end.strftime("%Y-%m-%d %H:%M")


def test_default_window_is_whole_span():
    w = resolve_window(SPAN)
    assert bounds() == ("2023-05-21 00:01", "2023-05-22 00:00")
    assert not w.clipped
    assert w.minutes == 1439


def test_now_and_last_minutes():
    assert bounds(last_minutes=60) == ("2023-05-21 23:00", "2023-05-22 00:00")
    assert bounds(end="12:00", last_minutes=30) == ("2023-05-21 11:30", "2023-05-21 12:00")
    assert bounds(end="now") == bounds()


def test_today_is_clipped():
    w = resolve_window(SPAN, start="today")
    assert w.start == datetime(2023, 5, 21, 0, 1)
    assert w.clipped


@pytest.mark.parametrize("value", ["14:20", "4:05", "14:20:30", " NOW "])
def test_clock_times(value):
    w = resolve_window(SPAN, start=value, end="23:00") if value.strip() != "NOW" else None
    if w is None:
        assert resolve_window(SPAN, start="23:00", end=value).end == resolve_window(SPAN).end
    else:
        hour, minute = {"14:20": (14, 20), "4:05": (4, 5), "14:20:30": (14, 20)}[value]
        assert w.start == datetime(2023, 5, 21, hour, minute)


@pytest.mark.parametrize(
    "value", ["2023-05-21 14:20", "2023-05-21T14:20:00Z", "2023-05-21T16:20+02:00"]
)
def test_iso_timestamps(value):
    assert resolve_window(SPAN, start=value, end="15:00").start == datetime(2023, 5, 21, 14, 20)


def test_rounds_outwards():
    assert bounds(start="2023-05-21 10:00:30", end="2023-05-21 10:05:01") == (
        "2023-05-21 10:00",
        "2023-05-21 10:06",
    )


def test_clips_end():
    w = resolve_window(SPAN, start="23:30", end="2023-05-22 02:00")
    assert w.end == datetime(2023, 5, 22, 0, 0)
    assert w.clipped


@pytest.mark.parametrize(
    ("kw", "argument"),
    [
        ({"start": "10:00", "last_minutes": 5}, "last_minutes"),
        ({"start": "yesterday"}, "start"),
        ({"end": "25:00"}, "end"),
        ({"start": "12:00", "end": "11:00"}, "end"),
        ({"start": "12:00", "end": "12:00"}, "end"),
        ({"start": "2023-05-23 00:00", "end": "2023-05-23 01:00"}, "start"),
        ({"end": "2023-05-20 10:00", "last_minutes": 10}, "end"),
    ],
)
def test_window_errors(kw, argument):
    with pytest.raises(ToolError) as info:
        resolve_window(SPAN, **kw)
    error = info.value.result()["error"]
    assert error["code"] == "bad_argument"
    assert error["argument"] == argument
    if kw.get("start") == "yesterday" or kw.get("end") == "25:00":
        assert error["hint"]


def test_window_rule_is_one_paragraph():
    assert "\n" not in WINDOW_RULE
    for word in ("UTC", "now", "today", "14:20", "last_minutes", "whole minutes"):
        assert word in WINDOW_RULE


def test_data_span(tmp_path):
    store = CacheStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        data_span(store)
    store.write_json(
        "temporal",
        "meta.json",
        {"ts_min": "2023-05-21 00:01:10.665000", "ts_max": "2023-05-21 23:59:07"},
    )
    lo, hi = data_span(store)
    assert lo == datetime(2023, 5, 21, 0, 1, 10, 665000, tzinfo=UTC)
    assert hi.tzinfo is not None
    store.write_json("temporal", "meta.json", {"ts_min": None, "ts_max": "None"})
    with pytest.raises(FileNotFoundError):
        data_span(store)
    store.write_json(
        "temporal", "meta.json", {"ts_min": "2023-05-21T00:00:00+02:00", "ts_max": "x"}
    )
    with pytest.raises(FileNotFoundError):
        data_span(store)
    store.write_json(
        "temporal",
        "meta.json",
        {"ts_min": "2023-05-21T02:00:00+02:00", "ts_max": "2023-05-21 05:00"},
    )
    assert data_span(store)[0] == datetime(2023, 5, 21, 0, 0, tzinfo=UTC)
