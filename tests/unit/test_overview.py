import json
from datetime import datetime

import polars as pl
import pytest

from logdash.analyses import overview
from logdash.analyses.overview import Overview
from tests.conftest import build_with


@pytest.fixture(scope="module")
def ov(built):
    build_with(built, Overview())
    return built.store


def test_headline_matches_truth(ov, synthetic):
    head = overview.parse_headline(ov.read_json("overview", "headline.json"))
    assert head["rows"] == synthetic.truth.n_rows
    assert head["malformed"] == synthetic.truth.n_malformed
    assert head["hosts"] == 6 and head["services"] >= 3
    assert isinstance(head["ts_min"], datetime) and head["span_hours"] > 0
    assert overview.parse_headline({"ts_min": None, "ts_max": None})["ts_min"] is None


def test_columns_missing_and_distinct(ov, synthetic):
    cols = ov.read_parquet("overview", "columns.parquet").to_dicts()
    by = {c["column"]: c for c in cols}
    assert by["host"]["missing"] >= synthetic.truth.n_malformed
    assert 0 <= by["host"]["missing_pct"] <= 100 and by["payload"]["approx"]
    assert by["level"]["missing"] == 0 and by["level_raw"]["missing"] > 0


def test_level_map_covers_spellings(ov, synthetic):
    lm = ov.read_parquet("overview", "level_map.parquet")
    assert lm["n"].sum() == synthetic.truth.n_rows
    pairs = set(zip(lm["level_raw"], lm["level"], strict=True))
    assert {
        ("warn", "WARNING"),
        ("info", "INFO"),
        ("INFO", "INFO"),
        ("(missing)", "UNKNOWN"),
    } <= pairs
    mal = ov.read_parquet("overview", "malformed.parquet")
    assert mal["malformed"].sum() == synthetic.truth.n_malformed


def test_figure_and_tables(ov):
    cols = ov.read_parquet("overview", "columns.parquet")
    fig = overview.missing_figure(cols, "dark")
    assert set(fig.data[0].y) == set(cols["column"]) and fig.data[0].orientation == "h"
    assert overview.missing_figure(pl.DataFrame(), None).layout.annotations
    assert overview.distinct_table(cols).columns == ["column", "distinct", "count", "missing"]
    assert (
        overview.level_map_table(ov.read_parquet("overview", "level_map.parquet")).columns[0]
        == "raw level"
    )
    assert (
        len(
            overview.headline_cards(
                {
                    "rows": 1,
                    "ts_min": None,
                    "ts_max": None,
                    "span_hours": 0,
                    "hosts": 1,
                    "services": 1,
                    "requests": 1,
                    "malformed": 0,
                    "levels": 1,
                }
            )
        )
        == 8
    )


def test_layout_and_callback_over_http(ov, built):
    from logdash.app.main import create_app

    reg = build_with(built, Overview())
    app = create_app(ov, reg)
    client = app.server.test_client()
    assert client.get("/overview").status_code == 200
    assert client.get("/").status_code == 200
    body = {
        "output": "overview-missing.figure",
        "outputs": {"id": "overview-missing", "property": "figure"},
        "inputs": [{"id": "theme", "property": "data", "value": "dark"}],
        "changedPropIds": ["theme.data"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert resp.get_json()["response"]["overview-missing"]["figure"]["data"]


def test_layout_renders(ov):
    layout = Overview().layout(ov)
    assert "overview-missing" in str(layout)
