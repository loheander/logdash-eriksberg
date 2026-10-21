import json
from datetime import datetime

import pytest

from logdash.analyses import explorer as ex
from logdash.analyses.explorer import Explorer
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from tests.conftest import build_with


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
    assert "it''s.parquet" in sql and "ESCAPE" in sql
    assert params[-1] == "%50\\%\\_x%" and "r1" in params and 3 in params and "a'b" in params
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
