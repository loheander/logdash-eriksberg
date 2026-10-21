import pytest

from logdash.analyses import BUILTIN, load_builtin
from logdash.analyses.base import Registry
from logdash.build import compute_fingerprints, run_build, stage_statuses
from logdash.cache.store import CacheStore
from logdash.cli import main
from logdash.config import Settings


class CountLevels:
    title = "Levels"
    version = "1"
    depends_on: tuple[str, ...] = ()

    def __init__(self, key="levels", deps=(), fail=False):
        self.key, self.depends_on, self.fail, self.calls = key, tuple(deps), fail, 0

    def compute(self, ctx):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        df = ctx.con.execute("SELECT level, count(*) AS n FROM events GROUP BY 1").pl()
        df.write_parquet(ctx.out_dir / "levels.parquet")
        ctx.progress("done")
        ctx.note_rows("extra", 7)

    def layout(self, store):
        return None

    def register_callbacks(self, app, store):
        pass


@pytest.fixture
def settings(synthetic, cache_dir):
    return Settings(synthetic.data_dir, cache_dir, "1GB", 2)


def registry_with(*analyses):
    reg = Registry()
    for a in analyses:
        reg.add(a)
    return reg


def statuses(results):
    return {r.key: r.status for r in results}


def quiet(_):
    pass


def test_build_runs_analyses_in_order_and_records_manifest(settings, cache_dir):
    first, second = CountLevels("a"), CountLevels("b", deps=["a"])
    reg = registry_with(second, first)
    logs = []
    results = run_build(settings, registry=reg, log=logs.append)
    assert [r.key for r in results] == ["events", "a", "b"]
    assert all(r.status == "built" for r in results)
    entry = CacheStore(cache_dir).manifest().get("a")
    assert entry is not None
    assert entry.rows["levels.parquet"] > 0 and entry.rows["extra"] == 7
    assert CacheStore(cache_dir).read_parquet("b", "levels.parquet").height > 0
    assert any("a: done" in line for line in logs)

    again = run_build(settings, registry=reg, log=quiet)
    assert set(statuses(again).values()) == {"fresh"}
    assert first.calls == 1


def test_force_and_only(settings):
    a, b, c = CountLevels("a"), CountLevels("b", deps=["a"]), CountLevels("c")
    reg = registry_with(a, b, c)
    run_build(settings, registry=reg, log=quiet)
    forced = run_build(settings, registry=reg, only="b", force=True, log=quiet)
    assert statuses(forced) == {"events": "fresh", "a": "fresh", "b": "built"}
    assert c.calls == 1
    everything = run_build(settings, registry=reg, force=True, log=quiet)
    assert set(statuses(everything).values()) == {"built"}


def test_only_builds_missing_dependencies_and_events(settings):
    a, b = CountLevels("a"), CountLevels("b", deps=["a"])
    results = run_build(settings, registry=registry_with(a, b), only="b", log=quiet)
    assert statuses(results) == {"events": "built", "a": "built", "b": "built"}
    only_events = run_build(
        settings, registry=registry_with(a, b), only="events", force=True, log=quiet
    )
    assert statuses(only_events) == {"events": "built"}


def test_upstream_rebuild_cascades(settings):
    a, b = CountLevels("a"), CountLevels("b", deps=["a"])
    reg = registry_with(a, b)
    run_build(settings, registry=reg, log=quiet)
    run_build(settings, registry=reg, only="events", force=True, log=quiet)
    cascaded = run_build(settings, registry=reg, log=quiet)
    assert statuses(cascaded) == {"events": "fresh", "a": "fresh", "b": "fresh"}
    # forcing events and then building everything rebuilds dependents in the same run
    mixed = run_build(settings, registry=reg, force=True, log=quiet)
    assert set(statuses(mixed).values()) == {"built"}


def test_version_bump_invalidates_only_that_module(settings):
    a, b = CountLevels("a"), CountLevels("b")
    reg = registry_with(a, b)
    run_build(settings, registry=reg, log=quiet)
    a.version = "2"
    assert statuses(run_build(settings, registry=reg, log=quiet)) == {
        "events": "fresh",
        "a": "built",
        "b": "fresh",
    }


def test_failure_leaves_module_stale(settings):
    bad = CountLevels("bad", fail=True)
    reg = registry_with(bad)
    with pytest.raises(RuntimeError):
        run_build(settings, registry=reg, log=quiet)
    states = {s.key: s.state for s in stage_statuses(settings, reg)}
    assert states == {"events": "fresh", "bad": "missing"}


def test_fingerprints_chain_through_dependencies(settings):
    reg = registry_with(CountLevels("a"), CountLevels("b", deps=["a"]))
    before = compute_fingerprints(settings, reg)
    reg.get("a").version = "9"
    after = compute_fingerprints(settings, reg)
    assert before["events"] == after["events"]
    assert before["a"] != after["a"] and before["b"] != after["b"]


def test_status_states(settings, cache_dir):
    reg = registry_with(CountLevels("a"))
    assert [s.state for s in stage_statuses(settings, reg)] == ["missing", "missing"]
    run_build(settings, registry=reg, log=quiet)
    assert [s.state for s in stage_statuses(settings, reg)] == ["fresh", "fresh"]
    reg.get("a").version = "2"
    assert [s.state for s in stage_statuses(settings, reg)] == ["fresh", "stale"]
    no_data = Settings(settings.data_dir / "none", cache_dir, "1GB", 2)
    assert [s.state for s in stage_statuses(no_data, reg)] == ["unverified", "unverified"]


def test_cli_build_unknown_only(synthetic, cache_dir, capsys):
    common = ["--data", str(synthetic.data_dir), "--cache", str(cache_dir)]
    assert main(["build", *common, "--only", "nope"]) == 2
    assert "unknown analysis" in capsys.readouterr().err


def test_load_builtin_is_idempotent():
    load_builtin()
    load_builtin()
    assert isinstance(BUILTIN, tuple)
