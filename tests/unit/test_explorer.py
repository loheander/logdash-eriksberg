import json
import shutil
from datetime import datetime
from types import SimpleNamespace

import pytest

from logdash.analyses import explorer as ex
from logdash.analyses.explorer import Explorer, TagRestriction
from logdash.analyses.tags import tags_frame
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from logdash.app.tag_filters import TagFilter
from logdash.tagging.model import Tag
from tests.conftest import build_with

NOISE = Tag(False, "network", True, "Harmless duplicate firewall rule.")
FAILURE = Tag(True, "network", False, "A peer closed a connection unexpectedly.")
ROUTINE = Tag(False, "other", False, "Routine message.")


@pytest.fixture(scope="module")
def exp(built):
    registry = build_with(built, Templates(), Explorer())
    return built.store, registry


@pytest.fixture(scope="module")
def con(exp):
    c = exp[0].connect()
    yield c
    c.close()


def page(con, store, filters=None, **kw):
    return ex.fetch_page(con, store, filters or Filters(), **kw)


def drop_tags(store):
    shutil.rmtree(store.module_dir("tags"), ignore_errors=True)


@pytest.fixture
def no_tags(exp):
    store = exp[0]
    drop_tags(store)
    return store


@pytest.fixture
def tagged(exp):
    store = exp[0]
    drop_tags(store)
    templates = store.read_parquet("templates", "templates.parquet").select(
        "template_id", "template"
    )

    def find(needle):
        hits = [t for t, text in templates.iter_rows() if needle in text]
        assert len(hits) == 1
        return hits[0]

    noise = find("Duplicate iptables rule")
    failure = find("closed unexpectedly")
    untagged = find("bgp session reset")
    mapping = {}
    for tid, text in templates.iter_rows():
        if tid == untagged:
            continue
        mapping[text] = NOISE if tid == noise else FAILURE if tid == failure else ROUTINE
    store.write_parquet("tags", "tags.parquet", tags_frame(templates, mapping))
    store.write_json(
        "tags",
        "meta.json",
        {
            "available": True,
            "model": "stub",
            "prompt_version": "1",
            "templates": templates.height,
            "tagged": templates.height - 1,
            "untagged": 1,
            "calls": 0,
            "aborted": None,
            "failures": [],
        },
    )
    yield SimpleNamespace(store=store, noise=noise, failure=failure, untagged=untagged)
    drop_tags(store)


def count(con, store, template_ids):
    marks = ", ".join(str(int(t)) for t in template_ids)
    path = store.path("templates", "assignments.parquet")
    return con.execute(
        "SELECT count(*) FROM events WHERE masked_hash IN "
        f"(SELECT masked_hash FROM read_parquet(?) WHERE template_id IN ({marks}))",
        [str(path)],
    ).fetchone()[0]


def test_hour_values():
    d = datetime
    assert ex.hour_values(d(2023, 5, 21, 2, 30), d(2023, 5, 21, 4, 10)) == ["02", "03", "04"]
    assert ex.hour_values(d(2023, 5, 21, 23, 30), d(2023, 5, 22, 0, 30)) == ["23", "00"]
    assert ex.hour_values(None, d(2023, 5, 21)) is None
    assert ex.hour_values(d(2023, 5, 21), d(2023, 5, 22)) is None
    assert ex.hour_values(d(2023, 5, 21, 3), d(2023, 5, 21, 2)) is None


def test_build_query_is_parameterised_and_limited():
    sql, params = ex.build_query(
        Filters(hosts=("a'b",), start="2023-05-21 02:00:00", end="2023-05-21 03:30:00"),
        assignments="/x/it's.parquet",
        template=3,
        request_id=" r1 ",
        text="50%_x",
        page=2,
        size=10,
    )
    assert "a'b" not in sql and "LIMIT 11 OFFSET 20" in sql and "hour IN (?, ?)" in sql
    assert "it''s.parquet" in sql and "contains(lower(payload), ?)" in sql
    assert params[-1] == "50%_x" and "r1" in params and 3 in params and "a'b" in params
    plain, p2 = ex.build_query(Filters())
    assert "WHERE" not in plain and p2 == [] and f"LIMIT {ex.PAGE_SIZE + 1}" in plain
    no_template, _ = ex.build_query(Filters(), template=3)
    assert "masked_hash" not in no_template


def test_paging_and_filters(exp, con, synthetic):
    store = exp[0]
    rows, more = page(con, store)
    assert len(rows) == ex.PAGE_SIZE and more and rows[0]["time"] <= rows[-1]["time"]
    second, _ = page(con, store, page=1)
    assert second[0]["time"] >= rows[-1]["time"] and second[0] != rows[0]
    small, more = page(con, store, size=7000000)
    assert not more and len(small) == synthetic.truth.n_rows
    host = page(con, store, Filters(hosts=("eselda14u05s02",)), size=50)[0]
    assert host and {r["host"] for r in host} == {"eselda14u05s02"}
    err = page(con, store, Filters(levels=("ERROR",)), size=50)[0]
    assert err and {r["level"] for r in err} == {"ERROR"}
    start = datetime.fromisoformat("2023-05-21 01:00:00")
    ranged = Filters(start=start.isoformat(" "), end="2023-05-21 01:30:00")
    inside = page(con, store, ranged, size=2000)[0]
    assert inside and all("2023-05-21 01:00" <= r["time"] < "2023-05-21 01:30" for r in inside)


def test_text_request_and_template(exp, con, synthetic):
    store = exp[0]
    bgp, _ = page(con, store, text="BGP SESSION", size=100)
    assert len(bgp) == 20 and all("bgp" in r["payload"] for r in bgp)
    assert page(con, store, text="%")[0] == [] and page(con, store, text="no such text")[0] == []
    rid = synthetic.truth.request_ids[0]
    req, _ = page(con, store, request_id=rid)
    assert len(req) == 4 and {r["request_id"] for r in req} == {rid}
    tid = next(
        r["template_id"]
        for r in store.read_parquet("templates", "templates.parquet").iter_rows(named=True)
        if "bgp session" in r["template"]
    )
    by_template, _ = page(con, store, template=tid, size=100)
    assert {r["payload"] for r in by_template} == {r["payload"] for r in bgp}
    assert page(con, store, template=99999)[0] == []
    both, _ = page(con, store, Filters(hosts=("nope",)), template=tid)
    assert both == []


def test_helpers():
    assert ex.next_page("explorer-next", 2) == 3 and ex.next_page("explorer-next", 2, False) == 2
    assert ex.next_page("explorer-prev", 0) == 0 and ex.next_page("explorer-prev", 3) == 2
    assert ex.next_page("filters", 5) == 0 and ex.next_page(None, 5) == 0
    assert ex.parse_template(" 12 ") == 12 and ex.parse_template("x") is None
    link = ex.link_from_query(
        {"host": "h", "template": "3", "bogus": "x", "q": "", "level": ["a", "b"]}
    )
    assert link == {"host": "h", "template": "3"}
    assert ex.describe_link(link) == "host=h"
    merged = ex.merge_link(Filters(hosts=("a",), levels=("INFO",)), {"host": "b", "start": "s"})
    assert merged.hosts == ("b",) and merged.levels == ("INFO",) and merged.start == "s"
    assert ex.merge_link(Filters(hosts=("a",)), None).hosts == ("a",)
    assert ex.merge_link(Filters(), {"service": "x", "level": "ERROR", "end": "e"}).services == (
        "x",
    )


def test_layout_prefilled_and_callback(exp):
    store, registry = exp
    from logdash.app.main import create_app

    layout = str(Explorer().layout(store, template="7", host="h1", bogus="x"))
    assert "value='7'" in layout and "host=h1" in layout
    app = create_app(store, registry)
    client = app.server.test_client()
    assert client.get("/explorer").status_code == 200
    assert client.get("/explorer?template=1&host=eselda14u05s02").status_code == 200

    def call(inputs, state, changed):
        outs = ["explorer-grid.rowData", "explorer-page.data", "explorer-label.children",
                "explorer-prev.disabled", "explorer-next.disabled"]  # fmt: skip
        body = {
            "output": ".." + "...".join(outs) + "..",
            "outputs": [{"id": o.split(".")[0], "property": o.split(".")[1]} for o in outs],
            "inputs": [{"id": i, "property": p, "value": v} for i, p, v in inputs],
            "state": [{"id": "explorer-page", "property": "data", "value": state}],
            "changedPropIds": [changed],
        }
        resp = client.post(
            "/_dash-update-component", data=json.dumps(body), content_type="application/json"
        )
        assert resp.status_code == 200, resp.get_data()
        return resp.get_json()["response"]

    def inputs(**over):
        base = {
            "filters": ("data", {}),
            "explorer-link": ("data", {}),
            "explorer-template": ("value", ""),
            "explorer-request": ("value", ""),
            "explorer-text": ("value", ""),
            "explorer-prev": ("n_clicks", None),
            "explorer-next": ("n_clicks", None),
        }
        base.update(over)
        return [(k, p, v) for k, (p, v) in base.items()]

    out = call(inputs(), 0, "filters.data")
    assert len(out["explorer-grid"]["rowData"]) == ex.PAGE_SIZE and out["explorer-prev"]["disabled"]
    nxt = call(inputs(**{"explorer-next": ("n_clicks", 1)}), 0, "explorer-next.n_clicks")
    assert nxt["explorer-page"]["data"] == 1 and "101-200" in nxt["explorer-label"]["children"]
    # paging past the end steps back one page
    end = call(
        inputs(**{"explorer-text": ("value", "bgp session"), "explorer-next": ("n_clicks", 1)}),
        1,
        "explorer-next.n_clicks",
    )
    assert end["explorer-page"]["data"] == 0 and end["explorer-next"]["disabled"]
    none = call(inputs(**{"explorer-text": ("value", "zzzz")}), 0, "explorer-text.value")
    assert none["explorer-label"]["children"] == "no matching rows"
    linked = call(
        inputs(
            **{
                "explorer-link": ("data", {"host": "eselda14u05s02"}),
                "explorer-template": ("value", "1"),
            }
        ),
        0,
        "explorer-link.data",
    )
    assert all(r["host"] == "eselda14u05s02" for r in linked["explorer-grid"]["rowData"])


def test_search_page_matches_fetch_page_and_stops_on_budget(exp, con):
    store = exp[0]
    rows, more, cut = ex.search_page(con, store, Filters(), text="bgp session")
    assert cut is None and not more
    assert [r["time"] for r in rows] == [r["time"] for r in page(con, store, text="BGP SESSION")[0]]
    first, more1, _ = ex.search_page(con, store, Filters(), text="e", size=5)
    second, _, _ = ex.search_page(con, store, Filters(), text="e", size=5, page=1)
    assert more1 and len(first) == 5 and second and second[0] not in first
    none, more0, cut0 = ex.search_page(con, store, Filters(), text="zzzz", budget_s=-1.0)
    assert none == [] and not more0 and cut0 == "00"


def test_build_query_tag_clauses():
    keep, kp = ex.build_query(
        Filters(), assignments="/a.parquet", template=3, tags=TagRestriction((4, 9), True)
    )
    assert "masked_hash IN (SELECT masked_hash FROM read_parquet('/a.parquet') WHERE" in keep
    assert "template_id IN (?, ?)" in keep and kp == [3, 4, 9]
    drop, _ = ex.build_query(Filters(), assignments="/a", tags=TagRestriction((4,), False))
    assert "masked_hash IS NULL OR masked_hash NOT IN (" in drop
    none, np_ = ex.build_query(Filters(), assignments="/a", tags=TagRestriction((), True))
    assert "FALSE" in none and np_ == []
    nothing, _ = ex.build_query(Filters(), assignments="/a", tags=TagRestriction((), False))
    assert "TRUE" in nothing
    with pytest.raises(ValueError):
        ex.build_query(Filters(), tags=TagRestriction((1,), True))
    a, b = datetime(2023, 1, 1), datetime(2023, 1, 2)
    sql, params = ex.build_query(Filters(), select=ex.TS_SELECT, window=(a, b), limit=7)
    assert sql.startswith("SELECT ts FROM events") and "ts >= ? AND ts <= ?" in sql
    assert params[-2:] == [a, b] and sql.endswith("LIMIT 7")


def test_page_slice_and_restriction_helpers():
    found = [(1, "a"), (1, "b"), (1, "c"), (2, "d"), (3, "e"), (3, "f")]
    assert ex.page_slice(found, [1, 2, 3]) == [("c",), ("d",), ("e",)]
    assert ex.page_slice(found, [1, 1, 2]) == [("b",), ("c",), ("d",)]
    keep, drop = TagRestriction((2,), True), TagRestriction((2,), False)
    assert keep.excludes(3) and not keep.excludes(2)
    assert drop.excludes(2) and not drop.excludes(3)


def test_window_rows_falls_back(monkeypatch):
    class Fake:
        def __init__(self, *results):
            self.results = list(results)
            self.calls = 0

        def execute(self, sql, params):
            self.calls += 1
            return self

        def fetchall(self):
            return self.results.pop(0)

    t1, t2 = datetime(2023, 1, 1), datetime(2023, 1, 2)
    kw = {
        "assignments": "/a",
        "template": None,
        "request_id": None,
        "text": None,
        "tags": TagRestriction((1,), True),
        "page": 0,
        "size": 2,
    }
    assert ex.window_rows(Fake([]), Filters(), **kw) == []
    same = Fake([(t1,), (t1,)])
    assert ex.window_rows(same, Filters(), **kw) is None and same.calls == 1
    monkeypatch.setattr(ex, "WINDOW_CAP", 3)
    full = Fake([(t1,), (t2,)], [(t1, "a"), (t1, "b"), (t2, "c")])
    assert ex.window_rows(full, Filters(), **kw) is None


def test_tag_restriction(tagged):
    store, noise, failure = tagged.store, tagged.noise, tagged.failure
    assert ex.tag_restriction(store, TagFilter()) is None
    assert ex.tag_restriction(store, TagFilter(hide_noise=True)) == TagRestriction((noise,), False)
    assert ex.tag_restriction(store, TagFilter(actionable_only=True)) == TagRestriction(
        (failure,), True
    )
    both = tuple(sorted({noise, failure}))
    assert ex.tag_restriction(store, TagFilter(components=("network",))) == TagRestriction(
        both, True
    )
    assert ex.tag_restriction(store, TagFilter(True, False, ("network",))) == TagRestriction(
        (failure,), True
    )
    assert ex.tag_restriction(store, TagFilter(components=("auth",))) == TagRestriction((), True)


def test_tag_restriction_without_tags(no_tags):
    assert ex.tag_restriction(no_tags, TagFilter(True, True, ("network",))) is None


def test_fetch_page_with_tags(tagged, con):
    store, noise, failure = tagged.store, tagged.noise, tagged.failure
    big = 100_000
    total = con.execute("SELECT count(*) FROM events").fetchone()[0]
    hide = ex.tag_restriction(store, TagFilter(hide_noise=True))
    rows, _ = page(con, store, tags=hide, size=big)
    assert all("Duplicate iptables rule" not in (r["payload"] or "") for r in rows)
    assert len(rows) == total - count(con, store, [noise])
    act = ex.tag_restriction(store, TagFilter(actionable_only=True))
    rows, _ = page(con, store, tags=act, size=big)
    assert len(rows) == count(con, store, [failure])
    assert all("closed unexpectedly" in r["payload"] for r in rows)
    assert page(con, store, tags=ex.tag_restriction(store, TagFilter(components=("auth",)))) == (
        [],
        False,
    )
    host = rows[0]["host"]
    own, _ = page(con, store, Filters(hosts=(host,)), tags=act)
    assert own and all(r["host"] == host for r in own)
    alone = page(con, store, template=failure, size=big)
    assert page(con, store, template=failure, tags=hide, size=big) == alone
    assert page(con, store, template=noise, tags=hide) == ([], False)


@pytest.mark.parametrize("which", ["hide", "act"])
def test_two_step_pages_match_one_step(tagged, con, monkeypatch, which):
    store = tagged.store
    flt = TagFilter(hide_noise=True) if which == "hide" else TagFilter(actionable_only=True)
    tags = ex.tag_restriction(store, flt)
    two = [page(con, store, tags=tags, page=i, size=50) for i in range(4)]
    monkeypatch.setattr(ex, "WINDOW_CAP", 0)
    one = [page(con, store, tags=tags, page=i, size=50) for i in range(4)]
    assert [[r["time"] for r in rows] for rows, _ in two] == [
        [r["time"] for r in rows] for rows, _ in one
    ]
    assert [more for _, more in two] == [more for _, more in one]


def test_search_page_with_tags(tagged, con):
    store = tagged.store
    hide = ex.tag_restriction(store, TagFilter(hide_noise=True))
    act = ex.tag_restriction(store, TagFilter(actionable_only=True))
    assert ex.search_page(con, store, Filters(), text="iptables", tags=hide)[0] == []
    found = ex.search_page(con, store, Filters(), text="closed", tags=act)[0]
    expected = page(con, store, text="closed", tags=act)[0]
    assert found and [r["time"] for r in found] == [r["time"] for r in expected]
