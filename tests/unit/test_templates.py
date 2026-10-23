import json
import shutil
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from logdash.analyses import templates as tp
from logdash.analyses.tags import tags_frame
from logdash.analyses.templates import Templates
from logdash.app.filters import Filters
from logdash.app.tag_filters import TagFilter
from logdash.ingest.masking import mask
from logdash.tagging.model import Tag
from tests.conftest import build_with
from tests.synthetic import utc


@pytest.fixture(scope="module")
def tpl(built):
    build_with(built, Templates())
    return built.store


NOISE = Tag(False, "network", True, "Harmless duplicate firewall rule.")
FAILURE = Tag(True, "network", False, "A peer closed a connection unexpectedly.")
ROUTINE = Tag(False, "other", False, "Routine message.")


def one_text(templates, needle):
    hit = templates.filter(pl.col("template").str.contains(needle, literal=True))
    assert hit.height == 1
    return hit["template"][0]


@pytest.fixture
def no_tags(tpl):
    shutil.rmtree(tpl.module_dir("tags"), ignore_errors=True)
    return tpl


@pytest.fixture
def tagged(tpl):
    shutil.rmtree(tpl.module_dir("tags"), ignore_errors=True)
    templates = tpl.read_parquet("templates", "templates.parquet")
    noise = one_text(templates, "Duplicate iptables rule")
    failure = one_text(templates, "closed unexpectedly")
    untagged = one_text(templates, "bgp session reset")
    mapping = {
        text: NOISE if text == noise else FAILURE if text == failure else ROUTINE
        for text in templates["template"]
        if text != untagged
    }
    tpl.write_parquet("tags", "tags.parquet", tags_frame(templates, mapping))
    tpl.write_json(
        "tags",
        "meta.json",
        {
            "available": True,
            "model": "stub",
            "prompt_version": "1",
            "templates": templates.height,
            "tagged": len(mapping),
            "untagged": 1,
            "calls": 0,
            "aborted": None,
            "failures": [],
        },
    )

    def tid(text):
        return templates.filter(pl.col("template") == text)["template_id"][0]

    yield SimpleNamespace(store=tpl, noise=tid(noise), failure=tid(failure), untagged=tid(untagged))
    shutil.rmtree(tpl.module_dir("tags"), ignore_errors=True)


def walk(node):
    yield node
    children = getattr(node, "children", None)
    for child in children if isinstance(children, list | tuple) else [children]:
        if child is not None and not isinstance(child, str):
            yield from walk(child)


def find(layout, id_):
    return next(c for c in walk(layout) if getattr(c, "id", None) == id_)


def test_layout_without_tags_is_unchanged(no_tags):
    layout = Templates().layout(no_tags)
    grid = find(layout, "templates-grid")
    assert [c["field"] for c in grid.columnDefs] == [
        "template_id",
        "template",
        "count",
        "variants",
        "first_seen",
        "last_seen",
    ]
    assert all("tooltipField" not in c for c in grid.columnDefs)
    assert all(
        set(r) == {"template_id", "template", "count", "variants", "first_seen", "last_seen"}
        for r in grid.rowData
    )
    assert find(layout, "templates-tag-filters").style == {"display": "none"}
    assert "Semantic tags" not in str(layout)
    templates = no_tags.read_parquet("templates", "templates.parquet")
    rows, selection = tp.filtered_grid(no_tags, TagFilter(True, True, ("storage",)), None)
    assert rows == tp.grid_rows(templates) and selection == rows[:1]


def test_layout_with_tags_shows_columns_and_tooltip(tagged):
    layout = Templates().layout(tagged.store)
    grid = find(layout, "templates-grid")
    assert [c["field"] for c in grid.columnDefs] == [
        "template_id",
        "template",
        "actionable",
        "component",
        "noise",
        "count",
        "variants",
        "first_seen",
        "last_seen",
    ]
    assert grid.columnDefs[1]["tooltipField"] == "summary"
    assert grid.getRowId
    by_id = {r["template_id"]: r for r in grid.rowData}
    noisy = by_id[tagged.noise]
    assert (noisy["noise"], noisy["component"], noisy["summary"]) == (
        "yes",
        "network",
        NOISE.summary,
    )
    if tagged.untagged in by_id:
        assert by_id[tagged.untagged]["actionable"] == ""
        assert by_id[tagged.untagged]["summary"] is None
    assert getattr(find(layout, "templates-tag-filters"), "style", None) is None
    assert "Semantic tags" in str(layout)


def test_filtered_grid_moves_selection(tagged):
    store = tagged.store
    noise = [{"template_id": tagged.noise}]
    failure = [{"template_id": tagged.failure}]
    rows, sel = tp.filtered_grid(store, TagFilter(hide_noise=True), noise)
    assert tagged.noise not in [r["template_id"] for r in rows] and sel == rows[:1]
    rows, sel = tp.filtered_grid(store, TagFilter(actionable_only=True), None)
    assert [r["template_id"] for r in rows] == [tagged.failure] and sel == rows
    rows, sel = tp.filtered_grid(store, TagFilter(hide_noise=True), failure)
    assert sel[0]["template_id"] == tagged.failure
    assert tp.filtered_grid(store, TagFilter(components=("storage",)), None) == ([], [])


def test_no_selection_views(tpl):
    fig = tp.param_figure(Templates.params(tpl), None, None, None)
    assert fig.layout.annotations[0].text == tp.NO_SELECTION


def test_events_have_masked_hash(built, tpl, synthetic):
    con = tpl.connect()
    try:
        n_hash, n_pay = con.execute(
            "SELECT count(masked_hash), count(payload) FROM events"
        ).fetchone()
        assert n_hash == n_pay > 0
        distinct_raw = con.execute("SELECT count(DISTINCT payload) FROM events").fetchone()[0]
        distinct_masked = con.execute("SELECT count(DISTINCT masked_hash) FROM events").fetchone()[
            0
        ]
    finally:
        con.close()
    assert distinct_masked < distinct_raw / 3


def test_templates_are_few_and_complete(tpl, synthetic):
    templates = tpl.read_parquet("templates", "templates.parquet")
    assign = tpl.read_parquet("templates", "assignments.parquet")
    assert templates.height < 25 and templates["template_id"].to_list() == list(
        range(1, templates.height + 1)
    )
    assert (
        templates["count"].is_sorted(descending=True)
        or templates["count"][0] == templates["count"].max()
    )
    n_payload = synthetic.truth.n_rows - synthetic.truth.n_malformed
    assert templates["count"].sum() == n_payload
    assert assign["template_id"].n_unique() == templates.height
    assert templates["n_variants"].sum() == assign.height
    counts = tpl.read_parquet("templates", "template_counts.parquet")
    assert counts["n"].sum() == n_payload and counts["template_id"].n_unique() == templates.height


def test_novel_template_detected(tpl, synthetic):
    truth = synthetic.truth
    templates = tpl.read_parquet("templates", "templates.parquet")
    meta = tpl.read_json("templates", "meta.json")
    lo, hi = datetime.fromisoformat(meta["ts_min"]), datetime.fromisoformat(meta["ts_max"])
    novel = tp.novel_templates(templates, lo, hi, 0.1)
    hit = novel.filter(pl.col("template").str.contains("bgp session reset"))
    assert hit.height == 1
    row = hit.row(0, named=True)
    assert row["count"] == 20
    assert abs(row["first_seen"] - utc(truth.novel_first_seen).replace(tzinfo=None)) < timedelta(
        seconds=1
    )
    assert novel.height <= 3
    rare = tp.rare_templates(templates, 25)
    assert hit["template_id"][0] in rare["template_id"].to_list()
    assert tp.rare_templates(templates, 0).is_empty()


def test_masking_collapses_variants(tpl):
    templates = tpl.read_parquet("templates", "templates.parquet")
    port = templates.filter(pl.col("template").str.contains("Port <UUID> updated"))
    assert port.height == 1 and port["n_variants"][0] >= 1
    assert templates.filter(pl.col("template").str.contains("iptables")).height == 1


def test_mine_templates_and_renumber():
    lines = ["a b <NUM>", "a b <NUM>", "x y z q", "a b <NUM>"]
    ids, texts = tp.mine_templates(lines, 0.4, 4)
    assert ids[0] == ids[1] == ids[3] != ids[2] and set(texts) == set(ids)
    assignments, mapping = tp.renumber_by_count([1, 2, 3, 4], [5, 1, 9, 2], [7, 8, 7, 9])
    assert mapping[7] == 1 and assignments["template_id"].to_list() == [1, 3, 1, 2]


def test_extract_params():
    line = "Port 8d1a3c2e-1111-2222-3333-444455556666 on 10.0.0.1/24 took 12.5 ms at /a/b/c 0xdead"
    kinds = [k for k, _ in tp.extract_params(line)]
    assert kinds == ["UUID", "IP", "NUM", "PATH", "HEX"]
    assert tp.extract_params("no variables here") == []
    assert mask(line).count("<") == 5


def test_params_extracted(tpl):
    params = pl.read_parquet(tpl.path("templates", "params") / "values.parquet")
    assert params.height > 0
    assert {"IP", "NUM", "UUID"} <= set(params["kind"])
    top = tpl.read_parquet("templates", "templates.parquet").sort("count").tail(1)["template_id"][0]
    assert top in params["template_id"].to_list()
    assert tp.params_frame(
        pl.DataFrame(
            {"template_id": [], "payload": []},
            schema={"template_id": pl.Int64, "payload": pl.String},
        )
    ).is_empty()
    opts = tp.param_options(params, int(top))
    assert opts and opts[0]["value"].count("|") == 1
    assert tp.param_options(params, None) == []


def test_figures(tpl):
    params = pl.read_parquet(tpl.path("templates", "params") / "values.parquet")
    kinds = params.select("template_id", "position", "kind").unique()
    for r in kinds.head(30).iter_rows(named=True):
        fig = tp.param_figure(params, r["template_id"], f"{r['position']}|{r['kind']}", "dark")
        assert fig.data
    assert tp.param_figure(params, 1, None, None).layout.annotations
    assert tp.param_figure(params, 999, "0|NUM", None).layout.annotations
    meta_filters = Filters()
    fig = tp.volume_for(tpl, meta_filters, 1, "light")
    assert fig.data and sum(fig.data[0].y) > 0
    narrowed = tp.volume_for(tpl, Filters(services=("haproxy",)), 1, None)
    assert narrowed.data or narrowed.layout.annotations
    assert tp.volume_for(tpl, Filters(), 9999, None).layout.annotations
    f = Filters(start="2023-05-21 00:30:00", end="2023-05-21 00:40:00")
    assert tp.volume_for(tpl, f, 1, None) is not None


def test_tables_and_helpers(tpl):
    templates = tpl.read_parquet("templates", "templates.parquet")
    rows = tp.grid_rows(templates, 3)
    assert len(rows) == 3 and rows[0]["count"] >= rows[1]["count"]
    assert "/explorer?template=" in str(tp.list_table(templates))
    assert "none" in str(tp.list_table(templates.clear()))
    assert tp.selected_id([{"template_id": 4}]) == 4 and tp.selected_id([]) is None
    assert tp.short("x" * 500).endswith("…") and tp.short("abc") == "abc"


def test_layout_and_callbacks_over_http(tagged, built):
    tpl = tagged.store
    from logdash.app.main import create_app

    assert "templates-grid" in str(Templates().layout(tpl))
    app = create_app(tpl, build_with(built, Templates()))
    client = app.server.test_client()
    assert client.get("/templates").status_code == 200

    def call(outputs, inputs, state=()):
        multi = len(outputs) > 1
        body = {
            "output": ".." + "...".join(f"{o}.{p}" for o, p in outputs) + ".."
            if multi
            else f"{outputs[0][0]}.{outputs[0][1]}",
            "outputs": [{"id": o, "property": p} for o, p in outputs]
            if multi
            else {"id": outputs[0][0], "property": outputs[0][1]},
            "inputs": [{"id": i, "property": p, "value": v} for i, p, v in inputs],
            "state": [{"id": i, "property": p, "value": v} for i, p, v in state],
            "changedPropIds": [f"{inputs[0][0]}.{inputs[0][1]}"],
        }
        resp = client.post(
            "/_dash-update-component", data=json.dumps(body), content_type="application/json"
        )
        assert resp.status_code == 200, resp.get_data()
        return resp.get_json()["response"]

    sel = ("templates-grid", "selectedRows", [{"template_id": 1}])
    out = call(
        [("templates-volume", "figure"), ("templates-link", "href")],
        [sel, ("filters", "data", {}), ("theme", "data", "dark")],
    )
    assert out["templates-link"]["href"] == "/explorer?template=1"
    opts = call([("templates-param", "data"), ("templates-param", "value")], [sel])
    assert opts["templates-param"]["value"]
    call(
        [("templates-params", "figure")],
        [
            ("templates-param", "value", opts["templates-param"]["value"]),
            sel,
            ("theme", "data", None),
        ],
    )
    out = call(
        [("templates-grid", "rowData"), ("templates-grid", "selectedRows")],
        [
            ("templates-hide-noise", "checked", True),
            ("templates-actionable-only", "checked", False),
            ("templates-components", "value", []),
        ],
        state=[("templates-grid", "selectedRows", [{"template_id": int(tagged.noise)}])],
    )
    row_data = out["templates-grid"]["rowData"]
    assert tagged.noise not in [r["template_id"] for r in row_data]
    assert out["templates-grid"]["selectedRows"][0]["template_id"] == row_data[0]["template_id"]
    empty = call(
        [("templates-volume", "figure"), ("templates-link", "href")],
        [("templates-grid", "selectedRows", []), ("filters", "data", {}), ("theme", "data", None)],
    )
    assert empty["templates-link"]["href"] == "/explorer"
