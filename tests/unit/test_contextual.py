"""Contextual detector: expected distance, same-minute peers, evidence, unscored hosts, meta."""

import json
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest
from scipy import sparse

from logdash.analyses import anomalies as an
from logdash.analyses import hosts
from logdash.analyses.anomalies import Anomalies
from logdash.analyses.base import Registry
from logdash.analyses.features import Features
from logdash.analyses.hosts import Centroids, Hosts
from logdash.analyses.pca import Pca
from logdash.analyses.templates import Templates
from logdash.analyses.temporal import Temporal
from logdash.build import run_build
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors import contextual
from logdash.detectors.base import DetectContext
from tests.conftest import Built, build_with
from tests.synthetic import SyntheticSpec, write_dataset
from tests.unit.test_anomalies import context_host_window, during

SPEC = SyntheticSpec(context_minutes=6, context_busy_rows=12_000)
T0 = datetime(2023, 5, 21)
K = 8  # templates of the hand-made fleets
PROFILE = np.full(K, 1 / K)  # every peer centroid: the same uniform mix


def fleet(
    lines: dict[str, list[int]],
    extra: dict[tuple[str, int], np.ndarray] | None = None,
    seed: int = 1,
):
    """Hosts with one multinomial window per minute from the uniform mix and who peers with whom.

    ``lines[host]`` is the line count of each minute; ``extra[(host, minute)]`` replaces that
    window's counts. Every host has all the others as peers.
    """
    rng = np.random.default_rng(seed)
    names = list(lines)
    rows, counts = [], []
    for host in names:
        for minute, n in enumerate(lines[host]):
            rows.append((host, T0 + timedelta(minutes=minute)))
            counts.append(
                (extra or {}).get((host, minute))
                if (host, minute) in (extra or {})
                else rng.multinomial(n, PROFILE)
            )
    windows = pl.DataFrame(rows, schema=["host", "bin"], orient="row")
    centroids = Centroids(tuple(names), np.tile(np.sqrt(PROFILE), (len(names), 1)))
    peers = pl.DataFrame(
        [(a, b) for a in names for b in names if a != b], schema=["host", "peer"], orient="row"
    )
    return windows, sparse.csr_matrix(np.array(counts, dtype=float)), centroids, peers


def stray(n: int, k: int) -> np.ndarray:
    """``n`` lines of which ``k`` are template 0 and the rest spread evenly over the others."""
    counts = np.full(K, (n - k) // (K - 1), dtype=float)
    counts[1 : 1 + (n - k) % (K - 1)] += 1
    counts[0] = k
    assert counts.sum() == n
    return counts


def flagged(scored: pl.DataFrame) -> set[tuple[str, int]]:
    return {
        (h, int((b - T0).total_seconds() // 60))
        for h, b in scored.filter(pl.col("z") >= contextual.THRESHOLD)[["host", "bin"]].iter_rows()
    }


def test_expected_distance_falls_with_the_line_count_and_the_flag_rate_is_level():
    # one host, windows of 60..400 lines drawn from the peers' own mix: pure multinomial noise
    sizes = [60, 80, 120, 200, 400]
    n_windows = 600
    lines = {"a": [sizes[i % len(sizes)] for i in range(n_windows)], "b": [100] * n_windows}
    scored = contextual.scored_windows(*fleet(lines))
    a = scored.filter(pl.col("host") == "a")
    mean = {n: a.filter(pl.col("lines") == n)["distance"].mean() for n in sizes}
    assert mean[60] > 1.5 * mean[400]  # the noise floor is not constant in n
    expected = {n: a.filter(pl.col("lines") == n)["expected"].mean() for n in sizes}
    assert all(abs(expected[n] - mean[n]) < 0.15 * mean[n] for n in sizes)  # and the fit follows it
    rate = {n: float((a.filter(pl.col("lines") == n)["z"] >= 3).mean()) for n in sizes}
    assert max(rate.values()) < 0.1 and max(rate.values()) - min(rate.values()) < 0.1, rate


def test_sparse_windows_get_a_wider_scale():
    lines = np.array([60.0, 200.0] * 50)
    distance = 0.05 + 0.5 / np.sqrt(lines) + np.tile([0.0, 0.01], 50)
    expected, scale = contextual.fit_host(lines, distance)
    assert expected[0] == pytest.approx(distance[0], abs=0.01)  # the 1 / sqrt(n) fit follows
    # sqrt(median lines / n), median 130: 1.47 for 60 lines, 0.8 for 200
    assert scale[0] / scale[1] == pytest.approx(np.sqrt(200 / 60), rel=0.01)


def test_small_windows_are_not_scored():
    lines = {"a": [100] * 30 + [10, 20], "b": [100] * 32}
    scored = contextual.scored_windows(*fleet(lines))
    assert scored["lines"].min() >= contextual.MIN_LINES
    assert scored.filter(pl.col("host") == "a").height == 30


def test_window_must_reach_a_share_of_the_hosts_median_lines():
    lines = {"a": [200] * 30 + [40], "b": [100] * 31}  # 40 < max(30, 0.5 * 200)
    scored = contextual.scored_windows(*fleet(lines))
    assert scored.filter(pl.col("host") == "a").height == 30


def test_host_with_too_few_windows_has_no_fit():
    scored = contextual.scored_windows(*fleet({"a": [100] * 5, "b": [100] * 30}))
    assert set(scored["host"]) == {"b"}


def test_a_host_that_deviates_while_its_peers_do_not_is_flagged():
    lines = {h: [100] * 40 for h in "abcdef"}
    extra = {("a", m): stray(100, 60) for m in (20, 21)}  # 60 % of the lines are one template
    found = flagged(contextual.scored_windows(*fleet(lines, extra)))
    assert found == {("a", 20), ("a", 21)}


def test_a_fleet_wide_shift_is_not_flagged():
    # every host logs the same unusual mix in minute 20: far from the peer centroid, but the
    # peers do the same, so nobody deviates from the others
    lines = {h: [100] * 40 for h in "abcdef"}
    extra = {(h, 20): stray(100, 60) for h in "abcdef"}
    scored = contextual.scored_windows(*fleet(lines, extra))
    shifted = scored.filter(pl.col("bin") == T0 + timedelta(minutes=20))
    assert shifted["distance"].min() > 0.4  # the whole-day centroid would flag all of them
    assert not flagged(scored)


def test_a_normal_host_and_one_stray_line_are_not_flagged():
    lines = {h: [100] * 60 for h in "abcdef"}
    extra = {("a", 30): stray(100, 1)}  # one line of a template that peers never log
    assert not flagged(contextual.scored_windows(*fleet(lines, extra)))


def test_evidence_counts_the_lines_of_the_leading_template():
    lines = {h: [100] * 60 for h in "abcdef"}
    windows, matrix, centroids, peers = fleet(lines, {("a", 30): stray(100, 2)})
    scored = contextual.scored_windows(windows, matrix, centroids, peers)
    one = scored.filter((pl.col("host") == "a") & (pl.col("row") == 30))
    notes, leading = contextual.evidence(one, matrix, centroids, np.arange(K), {})
    assert leading[0] < contextual.MIN_EVIDENCE_LINES  # two lines: dropped by the rule
    assert "T0 " in notes[0] and "2 lines" in notes[0]


@pytest.fixture(scope="module")
def injected(tmp_path_factory: pytest.TempPathFactory) -> tuple[CacheStore, Any]:
    """A cache of a dataset of its own: the busy host changes the structural fits of the rest."""
    data_dir = tmp_path_factory.mktemp("context_data")
    truth = write_dataset(data_dir / "synthetic_preprocessed.csv.zst", SPEC)
    settings = Settings(data_dir, tmp_path_factory.mktemp("context_cache"), "1GB", 2)
    run_build(settings, registry=Registry(), log=lambda _: None)
    built = Built(settings, CacheStore(settings.cache_dir, settings))
    build_with(built, Templates(), Features(), Pca(), Temporal(), Hosts(), Anomalies())
    return built.store, truth


def test_injected_host_window_is_flagged_with_its_template(injected):
    stage, truth = injected
    ctx = DetectContext(stage, stage.settings, lambda _: None)
    found = contextual.Contextual().detect(ctx)
    assert (found["kind"] == "host").all() and (found["raw"] >= contextual.THRESHOLD).all()
    hit = found.filter(
        during(truth.context_start, truth.context_end) & (pl.col("entity") == truth.context_host)
    )
    # clearly above the threshold (not within a unit of it) in every injected minute
    assert hit.height >= SPEC.context_minutes - 1
    assert hit["raw"].min() >= 1.5 * contextual.THRESHOLD
    # no other host and no minute outside the injection is flagged
    assert set(found["entity"]) == {truth.context_host} and found.height == hit.height
    # the evidence names templates by their share, line count and the peers' profile
    pattern = r"T\d+ \d+% \(\d+ lines, \d+% of the window, peer profile \d+\.\d%\)"
    assert hit["evidence"].str.contains(pattern).all()
    # a host without a valid peer group is never scored
    unscored = set(hosts.load_assignments(stage).filter(~pl.col("peer_ok"))["host"])
    assert unscored and not unscored & set(found["entity"])
    assert ctx.meta["unscored_hosts"] == len(unscored)
    assert ctx.meta["unscored_names"] == sorted(unscored)


def test_injected_window_ranks_among_the_contextual_rows(injected):
    stage, truth = injected
    rows = an.load_anomalies(stage).filter(pl.col("type") == "contextual")
    assert rows.height
    rank = context_host_window(truth, rows)
    assert rank is not None and rank < 20


def test_meta_is_json_safe_and_the_page_states_the_unscored_hosts(injected):
    stage, _ = injected
    entry = next(d for d in an.load_meta(stage) if d["key"] == "contextual")
    unscored = int((~hosts.load_assignments(stage)["peer_ok"]).sum())
    info = entry["info"]
    assert info["unscored_hosts"] == unscored > 0 and len(info["unscored_names"]) == unscored
    assert info["scored_windows"] <= info["windows"]
    json.dumps(info, allow_nan=False)  # what the stage wrote is strict JSON
    assert f"{unscored} of 6 hosts have no valid peer group" in an.hint(
        an.Filters(), 1, meta=an.load_meta(stage)
    )
