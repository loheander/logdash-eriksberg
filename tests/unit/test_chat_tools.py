import copy
import json
import re
import shutil
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import duckdb
import numpy as np
import pytest

from logdash.analyses.explorer import LINK_KEYS, link_from_query, merge_link
from logdash.analyses.templates import Templates
from logdash.analyses.temporal import Temporal
from logdash.app.filters import Filters, parse_time
from logdash.cache.store import CacheStore
from logdash.chat import tools
from logdash.chat.tools import (
    TEMPLATE_CHARS,
    TOOLS,
    Toolbox,
    check_arguments,
    register_tool,
    to_json,
    tool_specs,
)
from logdash.chat.window import Window
from logdash.ingest.normalize import LEVELS, strip_host_domain
from tests.conftest import build_with
from tests.synthetic import ERROR_HOST, ERROR_SERVICE, HOSTS, SPIKE_HOST

HOST = strip_host_domain(SPIKE_HOST)
ERR_HOST = strip_host_domain(ERROR_HOST)
SERVICE = ERROR_SERVICE[0]


@pytest.fixture(scope="module")
def store(built):
    build_with(built, Temporal(), Templates())
    return built.store


@pytest.fixture
def box(store):
    b = Toolbox(store)
    yield b
    b.close()


@pytest.fixture(scope="module")
def template(store):
    return int(store.read_parquet("templates", "templates.parquet")["template_id"].min())


def scalar(store, sql, params=()):
    con = store.connect()
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


def assert_json(result):
    json.dumps(result, allow_nan=False)
    return result


def call(box, name, args):
    return assert_json(box.call(name, args))


def template_sql(store, template, where="TRUE"):
    path = str(store.path("templates", "assignments.parquet"))
    return (
        "SELECT count(*) FROM events WHERE masked_hash IN (SELECT masked_hash FROM "
        f"read_parquet('{path}') WHERE template_id = {template}) AND {where}"
    )


def test_specs_format():
    specs = tool_specs()
    assert {s["function"]["name"] for s in specs} == {"count_events", "explorer_link"}
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["description"] and "\n" not in fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"]["additionalProperties"] is False
    before = copy.deepcopy(TOOLS["count_events"].parameters)
    specs[0]["function"]["parameters"]["properties"].clear()
    assert TOOLS["count_events"].parameters == before


def test_register_tool_rejects():
    def fn(box, args):
        return {}

    with pytest.raises(ValueError):
        register_tool("count_events", "x", {})(fn)
    with pytest.raises(ValueError):
        register_tool("new_tool", "two\nlines", {})(fn)
    with pytest.raises(ValueError):
        register_tool("new_tool", "x", {"a": {"type": "array"}})(fn)
    assert "new_tool" not in TOOLS


def test_call_accepts_dict_string_and_empty(box):
    results = [call(box, "count_events", a) for a in ({}, "{}", "")]
    assert results[0] == results[1] == results[2]


def test_total_matches_events(box, store):
    r = call(box, "count_events", {})
    assert r["total"] == scalar(store, "SELECT count(*) FROM events")[0][0]
    assert r["source"] == "temporal/rate_60.parquet"
    assert r["window"]["clipped"] is False
    lo, hi = scalar(store, "SELECT min(ts), max(ts) FROM events")[0]
    assert r["window"]["start"] == lo.strftime("%Y-%m-%d %H:%M:00")
    assert datetime.fromisoformat(r["window"]["end"]) > hi


def test_group_by_level(box, store):
    r = call(box, "count_events", {"group_by": "level"})
    counts = [g["count"] for g in r["groups"]]
    assert sum(counts) == r["total"]
    assert counts == sorted(counts, reverse=True)
    e = call(box, "count_events", {"level": "error"})
    assert e["filters"]["level"] == "ERROR"
    assert e["total"] == scalar(store, "SELECT count(*) FROM events WHERE level = 'ERROR'")[0][0]


def test_sources(box, store, template):
    host = call(box, "count_events", {"host": HOST})
    assert host["source"] == "temporal/host_60.parquet"
    assert (
        host["total"] == scalar(store, "SELECT count(*) FROM events WHERE host = ?", [HOST])[0][0]
    )
    t = call(box, "count_events", {"template": template})
    assert t["source"] == "templates/template_counts.parquet"
    assert t["total"] == scalar(store, template_sql(store, template))[0][0]
    assert 0 < len(t["template_text"]) <= TEMPLATE_CHARS
    th = call(box, "count_events", {"template": template, "host": HOST})
    assert th["source"] == "templates/template_counts_host.parquet"
    assert th["total"] == scalar(store, template_sql(store, template, f"host = '{HOST}'"))[0][0]


CASES = [
    {"level": "INFO"},
    {"service": SERVICE},
    {"host": HOST, "group_by": "host"},
    {"template": "T", "group_by": "service"},
    {"template": "T", "host": HOST},
]


@pytest.mark.parametrize("extra", CASES)
def test_aggregates_match_events(store, template, monkeypatch, extra):
    args = {k: (template if v == "T" else v) for k, v in extra.items()}
    args |= {"start": "02:00", "end": "03:00"}
    fast = call(Toolbox(store), "count_events", args)
    monkeypatch.setattr(tools, "AGGREGATES", ())
    slow = call(Toolbox(store), "count_events", args)
    assert slow["source"] == "events"
    assert fast["source"] != "events"
    assert fast["total"] == slow["total"] > 0
    assert fast.get("groups") == slow.get("groups")


def test_events_path(box, store, synthetic):
    start = synthetic.truth.error_start.astimezone(UTC)
    args = {
        "level": "ERROR",
        "host": ERR_HOST,
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": (start + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M"),
    }
    r = call(box, "count_events", args)
    assert "error" not in r
    assert r["source"] == "events"
    expected = scalar(
        store,
        "SELECT count(*) FROM events WHERE level = 'ERROR' AND host = ? AND ts >= ? AND ts < ?",
        [
            ERR_HOST,
            datetime.fromisoformat(r["window"]["start"]),
            datetime.fromisoformat(r["window"]["end"]),
        ],
    )[0][0]
    assert r["total"] == expected >= 600
    long = call(box, "count_events", {"level": "ERROR", "host": ERR_HOST})
    err = long["error"]
    assert err["code"] == "window_too_long"
    assert err["limit"] == 120
    assert err["argument"] == "start"
    assert "120" in err["message"]


def test_group_cap(box, store, monkeypatch):
    monkeypatch.setattr(tools, "MAX_GROUPS", 2)
    r = call(box, "count_events", {"group_by": "host"})
    assert len(r["groups"]) == 2
    assert r["groups_truncated"] is True
    expected = scalar(store, "SELECT count(DISTINCT coalesce(host, '(missing)')) FROM events")
    assert r["groups_total"] == expected[0][0]
    monkeypatch.setattr(tools, "MAX_GROUPS", 100)
    full = call(box, "count_events", {"group_by": "host"})
    missing = [g for g in full["groups"] if g["host"] == "(missing)"]
    assert missing
    assert "link" not in missing[0]
    assert all("link" in g for g in full["groups"] if g["host"] != "(missing)")


def check_link(link, window, **expected):
    parts = urlsplit(link)
    assert parts.path == "/explorer"
    query = parse_qs(parts.query)
    assert set(query) <= set(LINK_KEYS)
    merged = merge_link(Filters(), link_from_query({k: v[0] for k, v in query.items()}))
    assert parse_time(merged.start) == parse_time(window["start"])
    assert parse_time(merged.end) == parse_time(window["end"])
    for key, value in expected.items():
        assert query[key] == [str(value)]


def test_links(box, template):
    r = call(
        box,
        "count_events",
        {"template": template, "level": "info", "group_by": "host", "last_minutes": 60},
    )
    check_link(r["link"], r["window"], template=template, level="INFO")
    for g in r["groups"]:
        check_link(g["link"], r["window"], host=g["host"], template=template, level="INFO")
    w = call(box, "count_events", {"last_minutes": 30})
    check_link(w["link"], w["window"])


def test_explorer_link(box):
    r = call(
        box,
        "explorer_link",
        {
            "host": HOST.upper(),
            "level": "warning",
            "start": "01:00",
            "end": "02:00",
            "request_id": "req-1",
            "text": "Error",
        },
    )
    check_link(r["link"], r["window"], host=HOST, level="WARNING", request_id="req-1", q="Error")
    assert r["window"]["start"] == "2023-05-21 01:00:00"
    assert r["filters"]["request_id"] == "req-1"
    whole = call(box, "explorer_link", {})
    check_link(whole["link"], whole["window"])
    bad = call(box, "explorer_link", {"host": "nope-host"})
    assert bad["error"]["argument"] == "host"


def code(result, expected, argument=None):
    error = result["error"]
    assert error["code"] == expected
    assert error["message"]
    if argument is not None:
        assert error["argument"] == argument
    return error


def test_errors(box):
    e = code(call(box, "nope", {}), "unknown_tool")
    assert "count_events" in e["suggestions"]
    code(call(box, "count_events", "{not json"), "bad_arguments")
    code(call(box, "count_events", "[1, 2]"), "bad_arguments")
    code(call(box, "count_events", 5), "bad_arguments")  # type: ignore[arg-type]
    e = code(call(box, "count_events", {"hosts": "x"}), "bad_argument", "hosts")
    assert "host" in e["suggestions"]
    code(call(box, "count_events", {"last_minutes": "60"}), "bad_argument", "last_minutes")
    code(call(box, "count_events", {"last_minutes": True}), "bad_argument", "last_minutes")
    code(call(box, "count_events", {"host": 5}), "bad_argument", "host")
    code(call(box, "count_events", {"template": 1.5}), "bad_argument", "template")
    code(call(box, "count_events", {"last_minutes": 0}), "bad_argument", "last_minutes")
    code(call(box, "count_events", {"last_minutes": 5000}), "bad_argument", "last_minutes")
    code(call(box, "count_events", {"template": -1}), "bad_argument", "template")
    code(call(box, "count_events", {"group_by": "program"}), "bad_argument", "group_by")
    e = code(call(box, "count_events", {"host": "eselda"}), "bad_argument", "host")
    assert 0 < len(e["suggestions"]) <= 5
    assert all(s in {strip_host_domain(h) for h in HOSTS} for s in e["suggestions"])
    e = code(call(box, "count_events", {"service": "neutron"}), "bad_argument", "service")
    assert {"neutron-l3-agent", "neutron-openvswitch-agent"} <= set(e["suggestions"])
    e = code(call(box, "count_events", {"host": "zzzz"}), "bad_argument", "host")
    assert e["suggestions"] == []
    assert "none looks like" in e["hint"]
    e = code(call(box, "count_events", {"level": "fatal"}), "bad_argument", "level")
    assert e["suggestions"] == list(LEVELS)
    e = code(call(box, "count_events", {"template": 99999}), "bad_argument", "template")
    assert "ids run from" in e["hint"]
    code(call(box, "count_events", {"start": "soon"}), "bad_argument", "start")
    code(call(box, "count_events", {"start": "12:00", "end": "11:00"}), "bad_argument", "end")
    code(
        call(box, "count_events", {"start": "2024-01-01 00:00", "end": "2024-01-01 01:00"}),
        "bad_argument",
        "start",
    )
    e = code(call(box, "explorer_link", {"text": "x" * 201}), "bad_argument", "text")
    assert "200" in e["message"]
    # null and blank values count as not given
    assert "error" not in call(box, "count_events", {"host": None, "level": "  "})


def test_check_arguments_boolean_and_required():
    schema = {
        "type": "object",
        "properties": {"flag": {"type": "boolean"}, "n": {"type": "integer"}},
        "required": ["flag"],
    }
    assert check_arguments(schema, {"flag": True, "n": 60.0}) == {"flag": True, "n": 60}
    with pytest.raises(tools.ToolError):
        check_arguments(schema, {"flag": "yes"})
    with pytest.raises(tools.ToolError) as info:
        check_arguments(schema, {"n": 1})
    assert info.value.argument == "flag"
    with pytest.raises(tools.ToolError) as info:
        check_arguments({"properties": {"n": {"type": "integer", "minimum": 3}}}, {"n": 1})
    assert "at least 3" in info.value.message
    for value, name in [([1], "an array"), ({}, "an object"), (object(), "object")]:
        with pytest.raises(tools.ToolError) as info:
            check_arguments({"properties": {"s": {"type": "string"}}}, {"s": value})
        assert info.value.message.endswith(f"not {name}")


def test_unavailable(store, tmp_path):
    empty = Toolbox(CacheStore(tmp_path / "empty"))
    for name in ("count_events", "explorer_link"):
        e = code(empty.call(name, {}), "unavailable")
        assert str(tmp_path) not in e["message"]
        assert "meta.json" in e["message"]
    part = CacheStore(tmp_path / "part")
    shutil.copytree(store.module_dir("temporal"), part.module_dir("temporal"))
    e = code(Toolbox(part).call("count_events", {"template": 1}), "unavailable")
    assert "templates/templates.parquet" in e["message"]
    shutil.copytree(store.module_dir("templates"), part.module_dir("templates"))
    box = Toolbox(part)
    e = code(
        box.call("count_events", {"level": "ERROR", "host": HOST, "last_minutes": 30}),
        "unavailable",
    )
    assert "events" in e["message"]
    box.close()
    assert tools.missing_message(part, FileNotFoundError("boom")).startswith("a cache stage")
    outside = FileNotFoundError(2, "no", "/elsewhere/x.json")
    assert "elsewhere" not in tools.missing_message(part, outside)


def test_results_are_json():
    value = {"a": datetime(2023, 5, 21, 1, 2, 3), "b": float("nan"), "c": np.int64(3), "d": (1, 2)}
    assert to_json(value) == {"a": "2023-05-21 01:02:03", "b": None, "c": 3, "d": [1, 2]}
    assert to_json(date(2023, 5, 21)) == "2023-05-21"
    assert to_json(1.5) == 1.5
    with pytest.raises(TypeError):
        to_json(object())


def test_events_clause_guard(box):
    window = Window(datetime(2023, 5, 21, 1, 0), datetime(2023, 5, 21, 4, 0))
    with pytest.raises(ValueError):
        box.events_clause(window)
    clause, params = box.events_clause(
        Window(datetime(2023, 5, 21, 1, 0), datetime(2023, 5, 21, 2, 0))
    )
    assert "hour IN (?)" in clause
    assert params[0] == "01"


def test_close(store):
    own = Toolbox(store)
    own.call("count_events", {})
    assert own._con is not None
    con = own._con
    own.close()
    assert own._con is None
    with pytest.raises(duckdb.Error):
        con.execute("SELECT 1")
    shared = store.connect()
    other = Toolbox(store, con=shared)
    other.call("count_events", {})
    other.close()
    assert shared.execute("SELECT 1").fetchall() == [(1,)]
    shared.close()


class RecordingConnection:
    def __init__(self, con):
        self.con = con
        self.seen: list[str] = []

    def execute(self, sql, params=None):
        self.seen.append(sql)
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def __getattr__(self, name):
        return getattr(self.con, name)


# Add the cases of every new tool here; test_every_tool_has_cases fails otherwise.
PUSHDOWN_CASES: dict[str, list[dict[str, Any]]] = {
    "count_events": [
        {},
        {"group_by": "service"},
        {"host": HOST, "group_by": "host"},
        {"template": 1},
        {"template": 1, "host": HOST},
        {"level": "ERROR", "host": HOST, "start": "02:00", "end": "03:00"},
        {"template": 1, "group_by": "level", "last_minutes": 120},
        {"service": SERVICE, "host": HOST, "last_minutes": 30},
    ],
    "explorer_link": [{}, {"host": HOST, "text": "error", "last_minutes": 60}],
}


def test_every_tool_has_cases():
    assert set(PUSHDOWN_CASES) == set(tools.TOOLS)


def test_events_queries_have_pushdown(store):
    proxy = RecordingConnection(store.connect())
    box = Toolbox(store, con=cast(Any, proxy))
    for name, cases in PUSHDOWN_CASES.items():
        for args in cases:
            assert "error" not in box.call(name, args), (name, args)
    assert proxy.seen == box.statements
    from_events = [s for s in box.statements if re.search(r"\bfrom\s+events\b", s, re.I)]
    assert from_events
    assert all("hour IN (" in s for s in from_events)
    proxy.con.close()
