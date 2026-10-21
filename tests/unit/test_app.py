import json

import dash_mantine_components as dmc
import pytest
from dash import dcc

from logdash.analyses.base import Registry
from logdash.app import ui
from logdash.app.main import create_app, filter_options, home_layout
from logdash.app.shell import make_layout, navbar_links, next_theme
from tests.conftest import build_with


class Dummy:
    key = "dummy"
    title = "Dummy"
    version = "1"
    depends_on: tuple[str, ...] = ()

    def compute(self, ctx):
        ctx.con.execute("SELECT count(*) AS n FROM events").pl().write_parquet(
            ctx.out_dir / "n.parquet"
        )

    def layout(self, store):
        n = store.read_parquet("dummy", "n.parquet")["n"][0]
        return ui.page("Dummy", dcc.Graph(id="dummy-graph"), dmc.Text(f"rows {n}"))

    def register_callbacks(self, app, store):
        from dash import Input, Output

        @app.callback(Output("dummy-graph", "figure"), Input("theme", "data"))
        def draw(theme):
            return {"data": [], "layout": {"title": {"text": theme}}}


@pytest.fixture(scope="module")
def app(built):
    registry = build_with(built, Dummy())
    return create_app(built.store, registry)


def test_next_theme():
    assert (
        next_theme("light") == "dark"
        and next_theme("dark") == "light"
        and next_theme(None) == "dark"
    )


def test_filter_options(built):
    opts = filter_options(built.store)
    assert opts.services and opts.hosts and "eselda13u21s06" in opts.hosts
    assert opts.min_date == "2023-05-20" or opts.min_date.startswith("2023-05-2")


def test_navbar_and_home(built):
    reg = Registry()
    reg.add(Dummy())
    assert len(navbar_links(reg)) == 2
    assert home_layout(reg) is not None
    assert make_layout(reg, filter_options(built.store)) is not None


def test_ui_helpers():
    import polars as pl

    assert ui.fmt_int(None) == "-" and ui.fmt_int(1234567) == "1 234 567"
    assert ui.table(pl.DataFrame({"a": [1, None]})) is not None
    for comp in (
        ui.stat_card("a", "b"),
        ui.scale_toggle("t"),
        ui.graph("g"),
        ui.card("t", ui.graph("h")),
    ):
        assert comp is not None


def test_pages_and_callbacks_over_http(app):
    client = app.server.test_client()
    for path in ("/", "/dummy"):
        assert client.get(path).status_code == 200
    layout = client.get("/_dash-layout")
    assert layout.status_code == 200 and "mantine-provider" in layout.get_data(as_text=True)
    deps = client.get("/_dash-dependencies").get_json()
    assert any("dummy-graph" in d["output"] for d in deps)
    body = {
        "output": "dummy-graph.figure",
        "outputs": {"id": "dummy-graph", "property": "figure"},
        "inputs": [{"id": "theme", "property": "data", "value": "dark"}],
        "changedPropIds": ["theme.data"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    assert resp.get_json()["response"]["dummy-graph"]["figure"]["layout"]["title"]["text"] == "dark"
    shell_body = {
        "output": "mantine-provider.forceColorScheme",
        "outputs": {"id": "mantine-provider", "property": "forceColorScheme"},
        "inputs": [{"id": "theme", "property": "data", "value": "dark"}],
        "changedPropIds": ["theme.data"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(shell_body), content_type="application/json"
    )
    assert resp.get_json()["response"]["mantine-provider"]["forceColorScheme"] == "dark"


def test_serve_cli_runs_app(built, monkeypatch, synthetic):
    from dash import Dash

    from logdash.cli import main

    calls = {}
    monkeypatch.setattr(Dash, "run", lambda self, **kw: calls.update(kw))
    argv = [
        "serve",
        "--data",
        str(synthetic.data_dir),
        "--cache",
        str(built.settings.cache_dir),
        "--memory-limit",
        "1GB",
        "--threads",
        "2",
        "--port",
        "9999",
        "--debug",
    ]
    assert main(argv) == 0
    assert calls == {"host": "127.0.0.1", "port": 9999, "debug": True}
    assert main([*argv[:-1], "--no-build"]) == 0


def test_serve_without_cache(tmp_path, capsys):
    from logdash.cli import main

    assert main(["serve", "--no-build", "--cache", str(tmp_path / "none")]) == 2
    assert "logdash build" in capsys.readouterr().err


def post(client, output, inputs, state=()):
    prop_id, prop = output.split(".")
    body = {
        "output": output,
        "outputs": {"id": prop_id, "property": prop},
        "inputs": [{"id": i, "property": p, "value": v} for i, p, v in inputs],
        "state": [{"id": i, "property": p, "value": v} for i, p, v in state],
        "changedPropIds": [f"{inputs[0][0]}.{inputs[0][1]}"],
    }
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code == 200
    return resp.get_json()["response"][prop_id][prop]


def test_theme_toggle_and_filter_callbacks(app):
    client = app.server.test_client()
    assert (
        post(client, "theme.data", [("theme-toggle", "n_clicks", 1)], [("theme", "data", "light")])
        == "dark"
    )
    ins = [
        ("filter-dates", "value", ["2023-05-21", "2023-05-21"]),
        ("filter-levels", "value", ["INFO"]),
        ("filter-services", "value", []),
        ("filter-hosts", "value", None),
    ]
    data = post(client, "filters.data", ins, [("filters", "data", None)])
    assert data["levels"] == ["INFO"] and data["end"] == "2023-05-22 00:00:00"
