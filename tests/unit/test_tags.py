"""The tags build stage: cache contents, failures, memo, serve and the dashboard."""

from __future__ import annotations

import dataclasses
import shutil

import dash
import polars as pl
import pytest

from logdash.analyses.base import Registry
from logdash.analyses.tags import (
    TAGS_SCHEMA,
    Tags,
    read_meta,
    read_tags,
)
from logdash.analyses.templates import Templates
from logdash.app.main import create_app, home_layout
from logdash.app.shell import navbar_links
from logdash.build import run_build, stage_statuses
from logdash.tagging.model import COMPONENTS, InvalidAnswer
from logdash.tagging.runner import MAX_ATTEMPTS
from tests.unit.test_tagging import StubModel


@pytest.fixture
def tagged_cache(built):
    """The session cache with the tags stage and its memo removed before and after."""

    def clean():
        for key in ("tags", "tags_memo"):
            shutil.rmtree(built.store.module_dir(key), ignore_errors=True)
        manifest = built.store.manifest()
        manifest.remove("tags")
        manifest.save()

    clean()
    yield built
    clean()


def build_tags(built, model, *, stub=None, key="sk-test", **run_kw):
    stub = stub or StubModel()
    settings = dataclasses.replace(built.settings, openrouter_api_key=key, tag_model=model)
    registry = Registry()
    registry.add(Templates())
    registry.add(Tags(model_factory=lambda s: stub, sleep=lambda _: None))
    results = run_build(settings, registry=registry, log=lambda _: None, **run_kw)
    return stub, settings, registry, results


def statuses(results):
    return {r.key: r.status for r in results}


def test_build_writes_one_row_per_template(tagged_cache):
    stub, _, _, _ = build_tags(tagged_cache, "m-rows")
    store = tagged_cache.store
    templates = store.read_parquet("templates", "templates.parquet")
    frame = read_tags(store)
    assert frame is not None
    assert frame["template_id"].to_list() == sorted(templates["template_id"].to_list())
    assert dict(frame.schema) == TAGS_SCHEMA
    assert frame["tagged"].all() and set(frame["component"]) <= set(COMPONENTS)
    assert not frame["summary"].str.contains("\n").any()
    assert store.manifest().get("tags").rows["tagged"] == templates.height
    assert read_meta(store)["available"] is True
    assert len(stub.calls) == templates["template"].n_unique()


def test_persistent_invalid_answer_is_untagged_and_build_succeeds(tagged_cache):
    templates = tagged_cache.store.read_parquet("templates", "templates.parquet")
    bad = templates.sort("template_id")["template"][0]
    stub = StubModel({bad: [InvalidAnswer("always")] * 10})
    build_tags(tagged_cache, "m-bad", stub=stub)
    frame = read_tags(tagged_cache.store)
    row = frame.filter(pl.col("template") == bad).row(0, named=True)
    assert row["tagged"] is False and row["component"] is None and row["summary"] is None
    meta = read_meta(tagged_cache.store)
    assert meta["untagged"] == 1
    assert "InvalidAnswer" in meta["failures"][0]["reason"]
    assert stub.calls.count(bad) == MAX_ATTEMPTS


def test_model_change_retags(tagged_cache):
    stub, _, _, _ = build_tags(tagged_cache, "m-one")
    first = len(stub.calls)
    stub2, _, _, results = build_tags(tagged_cache, "m-two")
    assert statuses(results)["tags"] == "built"
    assert len(stub2.calls) == first


def test_no_key_skips_and_records_absence(tagged_cache):
    def refuse(_settings):
        raise AssertionError("factory must not be called without a key")

    settings = dataclasses.replace(tagged_cache.settings, tag_model="m-nokey")
    registry = Registry()
    registry.add(Templates())
    registry.add(Tags(model_factory=refuse))
    results = run_build(settings, registry=registry, log=lambda _: None)
    assert statuses(results)["tags"] == "built"
    store = tagged_cache.store
    assert not store.exists("tags", "tags.parquet") and read_tags(store) is None
    assert read_meta(store)["available"] is False
    states = {s.key: s.state for s in stage_statuses(settings, registry)}
    assert states["tags"] == "fresh"


def test_adding_key_triggers_tagging(tagged_cache):
    settings = dataclasses.replace(tagged_cache.settings, tag_model="m-addkey")
    registry = Registry()
    registry.add(Templates())
    registry.add(Tags(model_factory=lambda s: StubModel(), sleep=lambda _: None))
    run_build(settings, registry=registry, log=lambda _: None)
    assert read_tags(tagged_cache.store) is None
    _, settings2, _, results = build_tags(tagged_cache, "m-addkey")
    assert statuses(results)["tags"] == "built"
    assert read_tags(tagged_cache.store) is not None
    assert "sk-test" not in tagged_cache.store.manifest_path.read_text()
    assert "sk-test" not in tagged_cache.store.path("tags", "meta.json").read_text()


def test_rebuild_with_same_texts_makes_no_calls(tagged_cache):
    stub, settings, registry, _ = build_tags(tagged_cache, "m-same")
    before = read_tags(tagged_cache.store)
    calls = len(stub.calls)
    run_build(settings, registry=registry, only="tags", force=True, log=lambda _: None)
    assert len(stub.calls) == calls
    assert read_meta(tagged_cache.store)["calls"] == 0
    assert read_tags(tagged_cache.store).equals(before)


def test_serve_build_skips_tags(tagged_cache):
    stub = StubModel()
    _, _, _, results = build_tags(tagged_cache, "m-serve", stub=stub, allow_model=False)
    assert statuses(results)["tags"] == "skipped"
    assert stub.calls == [] and read_tags(tagged_cache.store) is None


def test_dashboard_has_no_tags_page(tagged_cache):
    def refuse(_settings):
        raise AssertionError("the dashboard must not call the model")

    tags = Tags(model_factory=refuse)
    registry = Registry()
    registry.add(Templates())
    registry.add(tags)
    create_app(tagged_cache.store, registry)
    assert "tags" not in dash.page_registry
    assert "/tags" not in str(navbar_links(registry))
    assert "/tags" not in str(home_layout(registry))
    with pytest.raises(RuntimeError):
        tags.layout(tagged_cache.store)
    assert tags.register_callbacks(None, tagged_cache.store) is None  # type: ignore[arg-type]


def test_read_helpers_without_cache(tmp_path):
    from logdash.cache.store import CacheStore

    store = CacheStore(tmp_path)
    assert read_tags(store) is None
    assert read_meta(store) == {"available": False, "reason": "not built"}
