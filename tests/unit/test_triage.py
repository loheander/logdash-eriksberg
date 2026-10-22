import json
import shutil
import subprocess
from datetime import datetime

import polars as pl
import pytest

from logdash import jev
from logdash.analyses import triage as tr
from logdash.analyses.anomalies import Anomalies
from logdash.analyses.features import Features
from logdash.analyses.hosts import Hosts
from logdash.analyses.pca import Pca
from logdash.analyses.templates import Templates
from logdash.analyses.temporal import Temporal
from logdash.analyses.triage import Triage
from logdash.app.filters import Filters
from tests.conftest import build_with
from tests.synthetic import SERVICES

KEY_ENV = {jev.KEY_ENV: "sk-test"}


def stages(triage=None):
    return (Templates(), Features(), Pca(), Temporal(), Hosts(), Anomalies(), triage or Triage())


@pytest.fixture(scope="module")
def stage(built):
    build_with(built, *stages())
    return built.store


@pytest.fixture(scope="module")
def con(stage):
    c = stage.connect()
    yield c
    c.close()


def answers(severity=2.4, nature="config", p=0.8):
    return {
        "actionable": {"type": "noul", "noul": p},
        "user_impact": {"type": "noul", "noul": 0.52},
        "nature": {
            "type": "choice",
            "choice": nature,
            "probabilities": {o: (0.7 if o == nature else 0.05) for o in tr.NATURE},
            "confidence": 0.7,
        },
        "severity": {
            "type": "score",
            "score": severity,
            "legend": {str(i): t for i, t in enumerate(tr.SEVERITY_LEVELS)},
            "probabilities": {"0": 0.0, "1": 0.1, "2": 0.4, "3": 0.45, "4": 0.05},
            "confidence": 0.3,
        },
    }


class Fake:
    def __init__(self, response=None, error=None):
        self.response = response or {"model": "typesafe/jev-1.14-x", "answers": answers(),
                                      "usage": {"cost": 3e-5}}  # fmt: skip
        self.error = error
        self.bodies = []

    def __call__(self, key, body, timeout_s):
        self.bodies.append(body)
        if self.error:
            raise self.error
        return self.response


# ------------------------------------------------------------------------------- subsystems


def test_every_synthetic_program_has_a_subsystem():
    programs = {s[0] for s in SERVICES} | {"nova-api"}
    assert all(tr.subsystem(p) != tr.UNKNOWN for p in programs)


def test_subsystem_mapping():
    assert (
        tr.subsystem("neutron-l3-agent") == "network" and tr.subsystem("openvswitch") == "network"
    )
    assert tr.subsystem("keystone") == "identity_secrets"
    assert tr.subsystem("keystone-apache-admin-access") == "identity_secrets"
    assert tr.subsystem("barbican_api_uwsgi_access") == "identity_secrets"
    assert (
        tr.subsystem("placement-api-access") == "compute" and tr.subsystem("libvirt") == "compute"
    )
    assert (
        tr.subsystem("mariadb-xinetd") == "control_plane"
        and tr.subsystem("fluent") == "observability"
    )
    assert (
        tr.subsystem("heat-api-cfn") == "orchestration" and tr.subsystem("glance-api") == "storage"
    )
    assert tr.subsystem(None) == tr.UNKNOWN and tr.subsystem("") == tr.UNKNOWN
    assert tr.subsystem("neutron") == tr.UNKNOWN and tr.subsystem("haproxy2") == tr.UNKNOWN


# ------------------------------------------------------------------------------------- rows


def test_row_key_is_stable_and_distinct():
    t = datetime(2023, 5, 21, 1, 2)
    a = tr.row_key("volume", "host", "h", t, 60, None)
    assert a == tr.row_key("volume", "host", "h", t, 60, None) and len(a) == 16
    assert a != tr.row_key("volume", "host", "h", t, 60, 3)
    assert a != tr.row_key("volume", "host", "h", t, 120, None)


def test_rows_have_unique_keys_and_service_subsystems(stage):
    rows = tr.load_rows(stage)
    assert rows.height and rows["row_key"].n_unique() == rows.height
    services = rows.filter(pl.col("kind") == "service")
    assert (services["subsystem"] != tr.UNKNOWN).all()
    assert (rows.filter(pl.col("kind") != "service")["subsystem"] == tr.UNKNOWN).all()
    judged = {rows["row_key"][0]: {"severity": 2.5, "subsystem": "storage"}}
    grid = tr.grid_rows(rows.head(3), judged)
    assert grid[0]["judged"] == 2.5 and grid[1]["judged"] is None
    assert grid[0]["subsystem"] == "storage" and grid[1]["subsystem"] == rows["subsystem"][1]
    assert grid[0]["href"].startswith("/explorer?") and grid[0]["evidence"] == rows["evidence"][0]


def test_template_ids():
    assert tr.template_ids(4, "T86 68% x; T97 17% y; T4 z") == [4, 86, 97]
    assert tr.template_ids(None, "T1 T2 T3 T4 T5 T6 T7") == [1, 2, 3, 4, 5]
    assert tr.template_ids(None, "no T refs, HTTP 500") == []


# ------------------------------------------------------------------------------------ state


def test_facts_query_is_parameterised():
    t = datetime(2023, 5, 21, 2, 30)
    sql, params = tr.facts_query("host", "a'b", t, 3600)
    assert "a'b" not in sql and "host = ?" in sql and "hour IN (?, ?)" in sql
    assert params[-3:] == ["02", "03", "a'b"]
    sql, params = tr.facts_query("fleet", "all hosts", t, 60)
    assert "host" not in sql and "program =" not in sql and params[-1] == "02"
    sql, params = tr.facts_query("request", "req-1", t, 1)
    assert "ts >=" not in sql and params == ["req-1"]
    sql, params = tr.facts_query("service", "nova-api", t, 86400)
    assert "hour IN" not in sql and params[-1] == "nova-api"


def test_window_facts_per_kind(stage, con, synthetic):
    rows = tr.load_rows(stage)
    for kind in set(rows["kind"]):
        row = rows.filter(pl.col("kind") == kind).row(0, named=True)
        facts = tr.window_facts(con, kind, row["entity"], row["bin"], row["window_s"])
        assert sum(facts.levels.values()) > 0, kind
        assert facts.program is not None and tr.row_subsystem(row, facts) != tr.UNKNOWN, kind
    rid = synthetic.truth.request_ids[0]
    req = tr.window_facts(con, "request", rid, datetime(2000, 1, 1), 1)
    assert req.levels == {"INFO": 4} and req.program == "nova-api"
    none = tr.window_facts(con, "host", "nope", datetime(2023, 5, 21, 1), 60)
    assert none == tr.Facts({}, None)


def test_build_state(stage, con):
    rows = tr.load_rows(stage)
    row = rows.filter(pl.col("template_id").is_not_null()).row(0, named=True)
    facts = tr.window_facts(con, row["kind"], row["entity"], row["bin"], row["window_s"])
    state = tr.build_state(row, facts, tr.load_templates(stage))
    assert set(state) == {
        "detector", "detector_description", "type", "kind", "entity", "subsystem",
        "window_start", "window_minutes", "evidence", "templates", "levels",
    }  # fmt: skip
    assert state["evidence"] == row["evidence"] and state["detector_description"]
    assert f"T{row['template_id']}" in state["templates"] and state["levels"] == facts.levels
    json.dumps(state)  # the state must be JSON


def test_every_detector_has_a_description():
    from logdash.detectors.base import DETECTORS

    assert {d.key for d in DETECTORS.all()} <= set(tr.DETECTOR_DESCRIPTIONS)
    assert {d.type for d in DETECTORS.all()} <= set(tr.TYPE_DESCRIPTIONS)


def test_spec_is_valid_and_hashed():
    jev.validate_questions(tr.QUESTIONS)
    assert len(tr.SPEC_HASH) == 10 and tr.spec_hash() == tr.SPEC_HASH
    assert tr.spec_hash(questions={}) != tr.SPEC_HASH
    assert tr.SPEC_HASH in Triage.version


# ---------------------------------------------------------------------------------- display


def normalised(raw):
    return {q: jev.normalize_answer(q, raw[q], tr.QUESTIONS[q]["type"]) for q in tr.QUESTIONS}


def test_answer_view_matches_outputs():
    view, numbers = tr.answer_view(normalised(answers()))
    assert len(view) == len(tr.VIEW_OUTPUTS)
    values = {(o.id, o.prop): v for o, v in zip(tr.VIEW_OUTPUTS, view, strict=True)}
    assert values[("triage-actionable-bar", "value")] == pytest.approx(80)
    assert values[("triage-actionable-bar", "color")] == "blue"
    assert values[("triage-nature-config-row", "style")]["order"] == 0
    assert values[("triage-nature-config-bar", "color")] == "blue"
    assert values[("triage-nature-load-bar", "color")] == "gray.4"
    assert values[("triage-needle", "style")]["transform"] == "rotate(18.0deg)"
    assert values[("triage-severity-label", "children")] == "medium"
    # user impact 0.52 and severity confidence 0.3 need a check; actionable does not
    assert values[("triage-user_impact-check", "style")] == {"visibility": "visible"}
    assert values[("triage-actionable-check", "style")] == {"visibility": "hidden"}
    assert values[("triage-severity-section", "style")]["opacity"] == 0.5
    assert values[("triage-user_impact-bar", "color")] == "gray"
    assert numbers["triage-actionable-num"] == [0.8, 2]
    assert numbers["triage-actionable-conf"] == [pytest.approx(0.6), 2]
    assert numbers["triage-severity-num"] == [2.4, 2]


def test_confidence_and_check():
    noul = {"type": "noul", "value": 0.9}
    assert tr.confidence(noul) == pytest.approx(0.8) and not tr.needs_check(noul)
    assert tr.needs_check({"type": "noul", "value": 0.6})
    close = {"type": "choice", "value": "a", "confidence": 0.9,
             "probabilities": {"a": 0.45, "b": 0.4}}  # fmt: skip
    assert tr.needs_check(close)
    assert tr.confidence({"type": "choice", "value": "a", "probabilities": {"a": 0.7}}) == 0.7
    assert tr.confidence({"type": "score", "value": 1.0}) == 0.0


def test_severity_label_rounds_half_up_as_the_grid_does():
    view, _ = tr.answer_view(normalised(answers(severity=0.5)))
    labels = {o.id: v for o, v in zip(tr.VIEW_OUTPUTS, view, strict=True) if o.prop == "children"}
    assert labels["triage-severity-label"] == "low"


def test_session_values():
    assert tr.session_values(None) == {} and tr.session_values({"a": 1}) == {}
    assert tr.session_values({"a": {"severity": 2, "subsystem": "bogus"}}) == {
        "a": {"severity": 2.0, "subsystem": None}
    }


def test_needle_is_clamped():
    assert tr.needle_style(-1, 5)["transform"] == "rotate(-90.0deg)"
    assert tr.needle_style(9, 5)["transform"] == "rotate(90.0deg)"
    assert tr.needle_style(2, 5)["transform"] == "rotate(0.0deg)"


# ------------------------------------------------------------------------------------ judge


def row_of(stage):
    return tr.load_rows(stage).row(0, named=True)


def test_judge_success(stage, con, built):
    fake = Fake()
    out = tr.judge(con, row_of(stage), {}, built.settings, "sk", fake)
    assert out["error"] is None and out["cost"] == 3e-5 and out["model"] == "typesafe/jev-1.14-x"
    assert out["state_ms"] > 0 and out["jev_ms"] >= 0 and out["severity"] == 2.4
    assert out["key"] == row_of(stage)["row_key"] and len(out["view"]) == len(tr.VIEW_OUTPUTS)
    body = fake.bodies[0]
    assert body["model"] == built.settings.jev_model and body["state"]["context"] == tr.CONTEXT
    assert body["state"]["item"]["entity"] == row_of(stage)["entity"]


@pytest.mark.parametrize(
    ("poster", "key", "message"),
    [
        (Fake(), None, "OPENROUTER_API_KEY is not set"),
        (Fake(error=jev.JevError("HTTP 401: bad key")), "sk", "HTTP 401"),
        (Fake({"answers": {**answers(), "severity": {"type": "noul", "noul": 1}}}), "sk",
         "severity: expected a score"),
    ],
)  # fmt: skip
def test_judge_errors_are_not_defaults(stage, con, built, poster, key, message):
    out = tr.judge(con, row_of(stage), {}, built.settings, key, poster)
    assert message in out["error"] and "view" not in out and "severity" not in out
    assert out["state_ms"] is not None


def test_judge_state_query_failure(stage, built):
    class Broken:
        def execute(self, *_a):
            import duckdb

            raise duckdb.Error("boom")

    out = tr.judge(Broken(), row_of(stage), {}, built.settings, "sk", Fake())  # type: ignore[arg-type]
    assert "state query failed: boom" in out["error"]


# ------------------------------------------------------------------------------- page, http


def test_draw_and_scatter(stage):
    host = tr.load_rows(stage).filter(pl.col("kind") == "host")["row_key"][0]
    session = {"x": {"severity": 1.0, "subsystem": None},
               host: {"severity": 2.0, "subsystem": "compute"}}  # fmt: skip
    rows, fig, hint = tr.draw_triage(stage, Filters(), [], "overall", "dark", session)
    assert rows and fig.data and "ordered by overall" in hint
    clicked = next(r for r in rows if r["row_key"] == host)
    assert clicked["subsystem"] == "compute" and clicked["judged"] == 2.0
    compute = next(t for t in fig.data if t.name == "compute")
    assert host in compute.customdata  # the subsystem found on click colours the point
    assert {t.name for t in fig.data} <= set(tr.SUBSYSTEM_NAMES)
    keys = [k for t in fig.data for k in t.customdata]
    assert sorted(keys) == sorted(r["row_key"] for r in rows)
    empty, efig, _ = tr.draw_triage(stage, Filters(hosts=("x",), services=("y",)), ["volume"],
                                    "score", None, None)  # fmt: skip
    assert (empty == []) == (not efig.data)


def test_layout_banner_follows_the_key(stage):
    assert "triage-banner" in str(Triage(env={}).layout(stage))
    page = str(Triage(env=KEY_ENV).layout(stage))
    assert "triage-banner" not in page and "triage-drawer" in page and tr.SPEC_HASH in page


def test_unbuilt_cache(tmp_path):
    from logdash.cache.store import CacheStore

    rows, fig, text = tr.draw_triage(CacheStore(tmp_path), Filters(), [], "overall", None, None)
    assert rows == [] and "not built" in text


def post(client, outputs, inputs, state=(), changed=None):
    multi = len(outputs) > 1
    body = {
        "output": (".." + "...".join(outputs) + "..") if multi else outputs[0],
        "outputs": [
            {"id": o.split(".")[0], "property": o.split(".")[1]} for o in outputs
        ] if multi else {"id": outputs[0].split(".")[0], "property": outputs[0].split(".")[1]},
        "inputs": [{"id": i, "property": p, "value": v} for i, p, v in inputs],
        "state": [{"id": i, "property": p, "value": v} for i, p, v in state],
        "changedPropIds": [changed or f"{inputs[0][0]}.{inputs[0][1]}"],
    }  # fmt: skip
    resp = client.post(
        "/_dash-update-component", data=json.dumps(body), content_type="application/json"
    )
    assert resp.status_code in (200, 204), resp.get_data()
    return resp.get_json()["response"] if resp.status_code == 200 else None


def test_callbacks_over_http(stage, built):
    from logdash.app.main import create_app

    fake = Fake()
    app = create_app(stage, build_with(built, *stages(Triage(poster=fake, env=KEY_ENV))))
    client = app.server.test_client()
    assert client.get("/triage").status_code == 200
    assert client.get("/assets/triage.js").status_code == 200
    out = post(
        client,
        ["triage-grid.rowData", "triage-scatter.figure", "triage-hint.children"],
        [("triage-types", "value", None), ("triage-sort", "value", "bogus"),
         ("filters", "data", {}), ("theme", "data", "light")],
        [("triage-session", "data", {"nope": 1})],
    )  # fmt: skip
    rows = out["triage-grid"]["rowData"]
    assert rows and "ordered by overall" in out["triage-hint"]["children"]
    key = rows[0]["row_key"]
    result = post(
        client, ["triage-result.data"], [("triage-selected", "data", {"key": key, "n": 3})]
    )
    data = result["triage-result"]["data"]
    assert data["key"] == key and data["n"] == 3 and data["error"] is None and len(fake.bodies) == 1
    unknown = post(
        client, ["triage-result.data"], [("triage-selected", "data", {"key": "x", "n": 1})]
    )
    assert unknown["triage-result"]["data"]["error"] == "unknown anomaly row"
    assert post(client, ["triage-result.data"], [("triage-selected", "data", None)]) in (
        None, {"triage-result": {"data": None}},
    )  # fmt: skip
    # the client registers every clientside function the page names
    deps = client.get("/_dash-dependencies").get_json()
    names = {
        d["clientside_function"]["function_name"]
        for d in deps
        if (d.get("clientside_function") or {}).get("namespace") == "triage"
    }
    assert names == {"select", "open", "tick", "arrive", "count", "judged"}
    arrive = next(d for d in deps if (d.get("clientside_function") or {}).get("function_name")
                  == "arrive")  # fmt: skip
    assert arrive["output"].count("...") + 1 == tr.FIXED_OUTPUTS + len(tr.VIEW_OUTPUTS)


def test_no_key_shows_the_error_in_the_drawer(stage, built):
    from logdash.app.main import create_app

    app = create_app(stage, build_with(built, *stages(Triage(poster=Fake(), env={}))))
    client = app.server.test_client()
    key = tr.load_rows(stage)["row_key"][0]
    result = post(
        client, ["triage-result.data"], [("triage-selected", "data", {"key": key, "n": 1})]
    )
    assert "OPENROUTER_API_KEY is not set" in result["triage-result"]["data"]["error"]


# ------------------------------------------------------------------- clientside functions

NODE = shutil.which("node")
JS = tr.__file__.replace("analyses/triage.py", "app/assets/triage.js")
HARNESS = """
const NU = {no_update: true};
global.window = {dash_clientside: {no_update: NU, callback_context: {triggered: %s}},
                 requestAnimationFrame: (f) => f(clock += 300)};
let clock = 0;
global.performance = {now: () => 1000};
global.document = {getElementById: (id) => (els[id] = els[id] || {textContent: ''})};
const els = {};
require(%s);
const t = window.dash_clientside.triage;
const out = (%s)(t);
console.log(JSON.stringify(out === NU ? 'NU' : out, (k, v) => v === NU ? 'NU' : v));
"""


def run_js(body, triggered=()):
    script = HARNESS % (
        json.dumps([{"prop_id": p} for p in triggered]),
        json.dumps(JS),
        body,
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
CONFIG = json.dumps({"model": "jev-1.14", "view_n": len(tr.VIEW_OUTPUTS)})


@needs_node
def test_js_select():
    cell = "t => t.select({rowId: 'k1'}, null, null, {key: 'k0', n: 4})"
    assert run_js(cell, ["triage-grid.cellClicked"]) == {"key": "k1", "n": 5}
    point = "t => t.select(null, {points: [{customdata: 'k2'}]}, null, null)"
    assert run_js(point, ["triage-scatter.clickData"]) == {"key": "k2", "n": 1}
    again = "t => t.select(null, null, 2, {key: 'k0', n: 4})"
    assert run_js(again, ["triage-again.n_clicks"]) == {"key": "k0", "n": 5}
    assert run_js("t => t.select(null, null, 1, null)", ["triage-again.n_clicks"]) == "NU"


@needs_node
def test_js_open_and_tick():
    rows = json.dumps([{"row_key": "k", "entity": "nova-api", "subsystem": "compute"}])
    out = run_js(f"t => t.open({{key: 'k', n: 1}}, {rows}, {CONFIG})")
    assert len(out) == 14 and out[0] is True and out[2] == "nova-api" and out[3] == "compute"
    assert out[8] is False and out[9].startswith("asking jev-1.14")
    assert run_js(f"t => t.tick(1, {CONFIG})") == "NU"  # no call in flight
    ticking = f"t => {{ t.open({{key: 'k', n: 1}}, [], {CONFIG}); return t.tick(1, {CONFIG}); }}"
    assert run_js(ticking) == "asking jev-1.14 … 0.00 s"


@needs_node
def test_js_arrive_ignores_stale_results():
    view, numbers = tr.answer_view(normalised(answers()))
    result = {"key": "k", "n": 2, "error": None, "state_ms": 40, "jev_ms": 900, "cost": 2e-5,
              "severity": 2.4, "view": view, "numbers": numbers,
              "subsystem": "network"}  # fmt: skip
    total = tr.FIXED_OUTPUTS + len(tr.VIEW_OUTPUTS)
    for selected in ("{key: 'k', n: 3}", "{key: 'other', n: 2}"):
        stale = run_js(f"t => t.arrive({json.dumps(result)}, {selected}, {{}}, {CONFIG})")
        assert stale == ["NU"] * total
    fresh = run_js(
        f"t => {{ t.open({{key: 'k', n: 2}}, [], {CONFIG});"
        f" return t.arrive({json.dumps(result)}, {{key: 'k', n: 2}},"
        f" {{a: {{severity: 1}}}}, {CONFIG}); }}"
    )
    assert len(fresh) == total and fresh[0] is True and fresh[1] == "answered in 0.00 s"
    assert fresh[2] == "state 40 ms · jev 900 ms · other 0 ms · $0.000020"
    assert fresh[3] == {"display": "none"} and fresh[6] == "network"
    assert (
        fresh[7] == {"a": {"severity": 1}, "k": {"severity": 2.4, "subsystem": "network"}}
        and fresh[8] == numbers
        and fresh[9:] == view
    )
    failed = {**result, "error": "HTTP 401", "view": None}
    err = run_js(f"t => t.arrive({json.dumps(failed)}, {{key: 'k', n: 2}}, {{}}, {CONFIG})")
    assert (
        err[4] == "HTTP 401" and err[5] == {"display": "none"} and err[7:] == ["NU"] * (total - 7)
    )


@needs_node
def test_js_count_and_judged():
    counted = run_js(
        "t => { t.count({a: [0.5, 2]}); t.count({a: [0.25, 2]}); return els.a.textContent; }"
    )
    assert counted == "0.25"
    rows = json.dumps([{"row_key": "k", "judged": None, "subsystem": "unknown"},
                       {"row_key": "j", "judged": 1, "subsystem": "network"}])  # fmt: skip
    session = "{k: {severity: 2.5, subsystem: 'compute'}, j: {severity: 1, subsystem: null}}"
    assert run_js(f"t => t.judged({session}, {rows})") == {
        "update": [{"row_key": "k", "judged": 2.5, "subsystem": "compute"}]
    }
    assert run_js(f"t => t.judged({{j: {{severity: 1}}}}, {rows})") == "NU"
    assert run_js(f"t => t.judged({{k: 2, j: null}}, {rows})") == "NU"  # an older session format
