import json

import polars as pl
import pytest

from logdash.analyses import distributions as d
from logdash.analyses.distributions import Distributions
from logdash.app.filters import Filters
from tests.conftest import build_with


@pytest.fixture(scope="module")
def dist(built):
    build_with(built, Distributions())
    return built.store


@pytest.fixture(scope="module")
def tables(dist):
    return d.load_tables(dist)


def test_category_totals_match_rows(tables, synthetic):
    cat = tables["categories"]
    for dim in d.DIMENSIONS:
        assert cat.filter(pl.col("dim") == dim)["n"].sum() == synthetic.truth.n_rows
    levels = d.category_counts(cat, "level", Filters())
    assert levels["n"].sum() == synthetic.truth.n_rows and "UNKNOWN" in levels["value"].to_list()
    assert levels["value"].to_list() == [v for v in d.LEVELS if v in levels["value"].to_list()]


def test_category_filters(tables):
    cat = tables["categories"]
    hosts = d.category_counts(cat, "host", Filters(hosts=("eselda14u05s02",)))
    assert hosts["value"].to_list() == ["eselda14u05s02"]
    errors = d.category_counts(cat, "program", Filters(levels=("ERROR",)))
    assert errors["n"].sum() > 0
    only = d.category_counts(cat, "program", Filters(services=("haproxy",)), top=1)
    assert only["value"].to_list() == ["haproxy"]
    capped = d.category_counts(cat, "program", Filters(), top=1)
    assert capped["value"].to_list()[-1] == "other"


def test_histogram_tables_sane(tables, synthetic):
    payload, zero = d.collapse_bins(tables["payload"])
    assert zero == 0 and payload["n"].sum() > 0
    lines, _ = d.collapse_bins(tables["lines"])
    n_requests = len(synthetic.truth.request_ids)
    assert lines["n"].sum() >= n_requests
    inter = tables["interarrival"]
    assert set(inter["program"]) >= {"haproxy"}
    lo, hi = d.bin_range(0)
    assert lo == 1 and 1 < d.bin_center(0) < hi


def test_figures_respect_filters_and_scale(tables):
    figs = d.build_figures(tables, Filters(), log=True, scheme="dark")
    assert set(figs) == set(d.GRAPH_IDS)
    assert figs["dist-level"].layout.xaxis.type == "log"
    assert figs["dist-payload"].layout.yaxis.type == "log"
    assert len(figs["dist-interarrival"].data) >= 2
    filtered = d.build_figures(tables, Filters(services=("haproxy",)), log=False, scheme=None)
    assert [t.name for t in filtered["dist-interarrival"].data] == ["haproxy"]
    nothing = d.build_figures(tables, Filters(hosts=("nope",)), log=False, scheme=None)
    assert nothing["dist-host"].layout.annotations
    empty = d.build_figures(tables, Filters(services=("nope",)), log=False, scheme=None)
    assert (
        empty["dist-payload"].layout.annotations and empty["dist-interarrival"].layout.annotations
    )


def test_histogram_zero_annotation():
    df = pl.DataFrame(
        {"bin": [None, 0, 1], "n": [5, 2, 3]}, schema={"bin": pl.Int32, "n": pl.Int64}
    )
    fig = d.histogram_figure(df, "t", "x", False, None)
    assert "5 zero values" in fig.layout.annotations[0].text


def test_bar_figure_vertical():
    counts = pl.DataFrame({"value": ["a"], "n": [1]})
    assert d.bar_figure(counts, False, None, horizontal=False).data[0].x == ("a",)


def test_layout_and_callback(dist, built):
    from logdash.app.main import create_app

    assert "dist-scale" in str(Distributions().layout(dist))
    reg = build_with(built, Distributions())
    app = create_app(dist, reg)
    client = app.server.test_client()
    assert client.get("/distributions").status_code == 200
    body = {
        "output": ".." + "...".join(f"{g}.figure" for g in d.GRAPH_IDS) + "..",
        "outputs": [{"id": g, "property": "figure"} for g in d.GRAPH_IDS],
        "inputs": [
            {"id": "filters", "property": "data", "value": {"levels": ["INFO"]}},
            {"id": "dist-scale", "property": "value", "value": "log"},
            {"id": "theme", "property": "data", "value": "dark"},
        ],
        "changedPropIds": ["dist-scale.value"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert set(resp.get_json()["response"]) == set(d.GRAPH_IDS)
