"""Semantic severity: bursts of lines tagged actionable, merged with level bursts."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from logdash.analyses import tags as semantic_tags
from logdash.analyses import templates as tpl
from logdash.analyses import temporal
from logdash.analyses.anomalies import Anomalies
from logdash.analyses.base import Registry
from logdash.analyses.tags import Tags, tags_frame
from logdash.analyses.templates import Templates
from logdash.analyses.temporal import Temporal
from logdash.build import run_build
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors import severity
from logdash.detectors.base import DETECTORS, DetectContext, DetectorRegistry
from logdash.detectors.severity import Severity, tail_z
from logdash.tagging.model import Tag
from tests.synthetic import SyntheticSpec, write_dataset
from tests.unit.test_anomalies import during
from tests.unit.test_severity import bursts_every, info, poisson

T0 = datetime(2023, 5, 21)
MINUTES = 600
LOST = "Lost connection to broker <IP>"
SLOW = "Retrying request to <*> after timeout"
CHATTER = "Heartbeat sent to <IP>"
ACT = Tag(True, "network", False, "a failure")
NOISE = Tag(True, "network", True, "x")
PLAIN = Tag(False, "other", False, "x")


def burst(start: int, counts: list[int], minutes: int = MINUTES) -> np.ndarray:
    x = np.zeros(minutes)
    x[start : start + len(counts)] = counts
    return x


def counts_frame(x: np.ndarray, **const: Any) -> pl.DataFrame:
    """One row per non-zero minute: bin, n (Int64) and the constant columns."""
    idx = np.flatnonzero(x)
    return pl.DataFrame(
        {
            "bin": [T0 + timedelta(minutes=int(i)) for i in idx],
            "n": x[idx].astype(int),
            **{k: [v] * len(idx) for k, v in const.items()},
        },
        schema={"bin": pl.Datetime("us"), "n": pl.Int64, **dict.fromkeys(const, pl.String)},
    )


def make_store(
    tmp_path: Path,
    levels: dict[tuple[str, str], np.ndarray],
    lines: dict[tuple[int, str, str], np.ndarray],
    tags: dict[str, Tag] | None,
    minutes: int = MINUTES,
) -> CacheStore:
    """Rate table, templates and template counts from hand-made series, and tags (or none)."""
    store = CacheStore(tmp_path, Settings(cache_dir=tmp_path))
    frames = [counts_frame(x, level=lv, program=pr) for (pr, lv), x in levels.items()]
    rate = pl.concat([f.select("bin", "level", "program", "n") for f in frames] + [info(minutes)])
    store.write_parquet(temporal.KEY, "rate_60.parquet", rate)
    templates = pl.DataFrame(
        {"template_id": [i for i, _, _ in lines], "template": [t for _, t, _ in lines]},
        schema={"template_id": pl.Int64, "template": pl.String},
    ).unique("template_id")
    store.write_parquet(tpl.KEY, "templates.parquet", templates)
    counts = [
        counts_frame(x, program=pr).with_columns(template_id=pl.lit(i, pl.Int64))
        for (i, _, pr), x in lines.items()
    ]
    empty = pl.DataFrame(
        schema={
            "bin": pl.Datetime("us"),
            "n": pl.Int64,
            "program": pl.String,
            "template_id": pl.Int64,
        }
    )
    store.write_parquet(
        tpl.KEY,
        "template_counts.parquet",
        pl.concat([empty, *counts]).select("template_id", "bin", "program", "n"),
    )
    if tags is not None:
        store.write_parquet(semantic_tags.KEY, semantic_tags.FILE, tags_frame(templates, tags))
    return store


def run(store: CacheStore) -> tuple[pl.DataFrame, dict[str, Any]]:
    ctx = DetectContext(store, store.settings, lambda _: None)
    rows = Severity().detect(ctx)
    return rows, ctx.meta


def test_info_burst_of_an_actionable_template_is_a_semantic_row(tmp_path):
    store = make_store(
        tmp_path,
        {("api", "INFO"): poisson(4, 20)},
        {(1, LOST, "api"): burst(300, [10, 25, 10])},
        {LOST: ACT},
    )
    rows, meta = run(store)
    assert rows.height == 1
    row = rows.row(0, named=True)
    assert row["bin"] == T0 + timedelta(minutes=300)
    assert row["window_s"] == 180
    assert row["entity"] == "api"
    assert row["evidence"].startswith("ACTIONABLE burst: peak 25/min vs baseline 0/min (Poisson P=")
    assert row["evidence"].endswith(
        "; semantic signal: 1 template tagged actionable (any log level), "
        "most lines from 'Lost connection to broker <IP>'"
    )
    assert row["raw"] == pytest.approx(tail_z(25, severity.MIN_RATE)[0])
    assert meta["semantic"] == {
        "actionable_templates": 1,
        "episodes": 1,
        "merged_into_level_rows": 0,
    }


@pytest.mark.parametrize(
    ("tags", "actionable"),
    [({LOST: NOISE}, 0), ({LOST: PLAIN}, 0), ({CHATTER: ACT}, 1)],
)
def test_noise_untagged_and_routine_templates_give_no_semantic_row(tmp_path, tags, actionable):
    store = make_store(
        tmp_path,
        {("api", "INFO"): poisson(4, 20)},
        {
            (1, LOST, "api"): burst(300, [10, 25, 10]),
            (2, CHATTER, "api"): np.full(MINUTES, 2.0),
        },
        tags,
    )
    rows, meta = run(store)
    assert rows.is_empty()
    assert meta["semantic"]["episodes"] == 0
    assert meta["semantic"]["actionable_templates"] == actionable


def test_tags_match_templates_by_text_not_id(tmp_path):
    store = make_store(
        tmp_path,
        {("api", "INFO"): poisson(4, 20)},
        {(1, LOST, "api"): burst(300, [10, 25, 10])},
        {LOST: ACT},
    )
    # the tags were made when the text had another id
    tags = store.read_parquet(semantic_tags.KEY, semantic_tags.FILE)
    store.write_parquet(
        semantic_tags.KEY, semantic_tags.FILE, tags.with_columns(template_id=pl.lit(7, pl.Int64))
    )
    rows, _ = run(store)
    assert rows.height == 1 and rows["evidence"][0].startswith("ACTIONABLE burst")
    # the same text twice: the first row wins
    first = tags.with_columns(actionable=pl.lit(False))
    store.write_parquet(
        semantic_tags.KEY,
        semantic_tags.FILE,
        pl.concat([first, tags.with_columns(template_id=pl.lit(7, pl.Int64))]),
    )
    rows, meta = run(store)
    assert rows.is_empty() and meta["semantic"]["actionable_templates"] == 0


def test_a_semantic_burst_inside_an_error_burst_annotates_the_error_row(tmp_path):
    levels = {("api", "ERROR"): burst(300, [10, 15, 10])}
    lines = {(1, LOST, "api"): burst(300, [10, 15, 10])}
    plain, _ = run(make_store(tmp_path / "a", levels, lines, None))
    assert plain.height == 1 and plain["evidence"][0].startswith("ERROR burst")
    rows, meta = run(make_store(tmp_path / "b", levels, lines, {LOST: ACT}))
    assert rows.height == 1
    evidence = rows["evidence"][0]
    assert evidence.startswith(
        plain["evidence"][0] + "; semantic signal agrees: ACTIONABLE burst: peak 15/min"
    )
    assert evidence.endswith("most lines from 'Lost connection to broker <IP>'")
    assert rows["raw"][0] == plain["raw"][0]
    assert meta["semantic"]["merged_into_level_rows"] == 1
    assert meta["semantic"]["episodes"] == 0


def test_a_periodic_level_row_does_not_absorb_a_semantic_burst(tmp_path):
    x = bursts_every(40, 9)
    x[100:103] = 300
    store = make_store(
        tmp_path,
        {("agent", "WARNING"): x},
        {(1, LOST, "agent"): burst(100, [10, 25, 10], 2000)},
        {LOST: ACT},
        minutes=2000,
    )
    rows, _ = run(store)
    assert rows.height == 2
    warning = [e for e in rows["evidence"] if e.startswith("WARNING")]
    other = [e for e in rows["evidence"] if e.startswith("ACTIONABLE burst")]
    assert len(warning) == 1 and len(other) == 1
    assert severity.PERIODIC_NOTE in warning[0] and "semantic signal" not in warning[0]
    assert "; semantic signal: 1 template" in other[0]


def test_the_busiest_actionable_template_is_named(tmp_path):
    store = make_store(
        tmp_path,
        {},
        {
            (1, LOST, "api"): burst(300, [5, 8, 5]),
            (2, SLOW, "api"): burst(300, [5, 20, 5]),
        },
        {LOST: ACT, SLOW: ACT},
    )
    rows, _ = run(store)
    assert rows.height == 1
    evidence = rows["evidence"][0]
    assert "peak 28/min" in evidence
    assert (
        "2 templates tagged actionable (any log level), "
        "most lines from 'Retrying request to <*> after timeout'" in evidence
    )


def level_tables(tmp_path, tags):
    warn = poisson(2, 20)
    warn[300:303] = 500
    return make_store(
        tmp_path,
        {
            ("api", "WARNING"): warn,
            ("api", "ERROR"): burst(300, [10, 15, 10]),
            ("db", "INFO"): poisson(3, 5),
        },
        {
            (1, LOST, "api"): burst(300, [10, 15, 10]),
            (2, CHATTER, "db"): burst(200, [40, 40, 40]),
        },
        tags,
    )


def test_without_tags_rows_and_meta_are_unchanged(tmp_path):
    base, base_meta = run(level_tables(tmp_path / "none", None))
    empty, empty_meta = run(level_tables(tmp_path / "empty", {}))
    assert not base.is_empty()
    assert base.equals(empty)
    assert base_meta == empty_meta and "semantic" not in base_meta


def test_tags_without_actionable_templates_change_only_the_meta(tmp_path):
    base, base_meta = run(level_tables(tmp_path / "none", None))
    rows, meta = run(
        level_tables(tmp_path / "tags", {LOST: PLAIN, CHATTER: Tag(False, "other", True, "x")})
    )
    assert rows.equals(base)
    assert meta["semantic"] == {
        "actionable_templates": 0,
        "episodes": 0,
        "merged_into_level_rows": 0,
    }
    assert {k: v for k, v in meta.items() if k != "semantic"} == base_meta


def test_missing_program_is_not_a_semantic_service(tmp_path):
    store = make_store(
        tmp_path,
        {},
        {(1, LOST, temporal.MISSING): burst(300, [10, 25, 10])},
        {LOST: ACT},
    )
    rows, meta = run(store)
    assert rows.is_empty()
    assert meta["semantic"]["actionable_templates"] == 1


def test_version_and_dependencies():
    assert Severity().depends_on == ("temporal", "templates", "tags")
    assert Severity().version.startswith("3:")
    assert str(severity.SEMANTIC) in Severity().version


SPEC = SyntheticSpec(semantic_rows=300)
AMQP = "Lost connection to AMQP server"
PEER = "closed unexpectedly by peer"  # the ERROR burst's template


class RuleModel:
    """Stub tag model: the first rule whose needle is in the template wins."""

    RULES = (
        (AMQP, Tag(True, "network", False, "lost the message broker")),
        (PEER, Tag(True, "network", False, "a peer closed the connection")),
        ("Duplicate iptables rule", Tag(False, "network", True, "duplicate firewall rule")),
    )

    def tag(self, template: str) -> Tag:
        for needle, tag in self.RULES:
            if needle in template:
                return tag
        return Tag(False, "other", False, "routine message")


@pytest.fixture(scope="module")
def semantic(tmp_path_factory):
    """A dataset of its own, tagged by RuleModel, with an anomalies stage of severity only."""
    data_dir = tmp_path_factory.mktemp("semantic_data")
    truth = write_dataset(data_dir / "synthetic_preprocessed.csv.zst", SPEC)
    settings = Settings(
        data_dir, tmp_path_factory.mktemp("semantic_cache"), "1GB", 2, openrouter_api_key="sk-test"
    )
    detectors = DetectorRegistry()
    detectors.add(DETECTORS.get("severity"))
    registry = Registry()
    for analysis in (
        Templates(),
        Temporal(),
        Tags(model_factory=lambda _s: RuleModel(), sleep=lambda _: None),
        Anomalies(detectors),
    ):
        registry.add(analysis)
    run_build(settings, registry=registry, log=lambda _: None)
    return CacheStore(settings.cache_dir, settings), truth


def view(store: CacheStore, tmp_path: Path, tags: dict[str, Tag] | None) -> CacheStore:
    """A cache with the fixture's temporal and templates (symlinked) and its own tags."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    for name in (temporal.KEY, tpl.KEY):
        (cache / name).symlink_to(store.module_dir(name), target_is_directory=True)
    out = CacheStore(cache, store.settings)
    if tags is not None:
        templates = store.read_parquet(tpl.KEY, "templates.parquet")
        out.write_parquet(semantic_tags.KEY, semantic_tags.FILE, tags_frame(templates, tags))
    return out


def test_synthetic_info_burst_tagged_actionable_is_a_severity_episode(semantic):
    store, truth = semantic
    rows = store.read_parquet("anomalies", "anomalies.parquet")
    mine = rows.filter(
        (pl.col("detector") == "severity") & (pl.col("entity") == truth.semantic_service)
    )
    hit = mine.filter(during(truth.semantic_start, truth.semantic_end))
    assert hit.height == 1
    evidence = hit["evidence"][0]
    assert evidence.startswith("ACTIONABLE burst")
    assert (
        "; semantic signal: 1 template tagged actionable (any log level), "
        "most lines from 'Lost connection to AMQP server" in evidence
    )
    assert hit["raw"][0] >= Settings().burst_k
    assert not any(e.startswith(("WARNING", "ERROR")) for e in mine["evidence"])
    templates = store.read_parquet(tpl.KEY, "templates.parquet")["template"]
    meta = store.read_json("anomalies", "meta.json")
    sev = next(d for d in meta["detectors"] if d["key"] == "severity")["info"]["semantic"]
    assert sev["actionable_templates"] == sum(AMQP in t or PEER in t for t in templates)
    assert sev["merged_into_level_rows"] == 1


def test_synthetic_error_burst_of_an_actionable_template_is_one_annotated_row(semantic):
    store, truth = semantic
    rows = store.read_parquet("anomalies", "anomalies.parquet").filter(
        (pl.col("detector") == "severity")
        & (pl.col("entity") == truth.error_service)
        & during(truth.error_start, truth.error_end)
    )
    assert rows.height == 1
    evidence = rows["evidence"][0]
    assert evidence.startswith("ERROR burst")
    assert "; semantic signal agrees: ACTIONABLE burst" in evidence


def test_synthetic_noise_tag_gives_no_semantic_episode(semantic, tmp_path):
    store, _ = semantic
    templates = store.read_parquet(tpl.KEY, "templates.parquet")["template"]
    noisy = {t: NOISE for t in templates if AMQP in t}
    rows, _ = run(view(store, tmp_path, noisy))
    assert not any("semantic signal" in e for e in rows["evidence"])


def test_synthetic_without_tags_is_unchanged(semantic, tmp_path):
    store, _ = semantic
    none, none_meta = run(view(store, tmp_path / "none", None))
    empty, empty_meta = run(view(store, tmp_path / "empty", {}))
    assert none.equals(empty)
    assert not any("semantic signal" in e or e.startswith("ACTIONABLE") for e in none["evidence"])
    assert "semantic" not in none_meta and "semantic" not in empty_meta
