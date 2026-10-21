"""Contextual detector: host windows against peers, small windows, unscored hosts, meta."""

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy import sparse

from logdash.analyses import anomalies as an
from logdash.analyses import hosts
from logdash.analyses.hosts import Centroids
from logdash.detectors import contextual
from logdash.detectors.base import DetectContext
from tests.unit.test_anomalies import during, stage  # noqa: F401 - the stage fixture

T0 = datetime(2023, 5, 21)


def hand_made(counts: list[list[int]], host: str = "a"):
    """Windows of one host with the given counts over two templates; peers are 50/50."""
    windows = pl.DataFrame(
        {
            "host": [host] * len(counts),
            "bin": [T0 + timedelta(minutes=i) for i in range(len(counts))],
        }
    )
    centroids = Centroids((host,), np.array([[np.sqrt(0.5), np.sqrt(0.5)]]))
    baseline = pl.DataFrame(
        {"host": [host], "median_lines": [100.0], "median": [0.05], "mad": [0.01]}
    )
    return windows, sparse.csr_matrix(np.array(counts, dtype=float)), centroids, baseline


def test_small_windows_are_not_scored_and_noisy_ones_get_a_wider_scale():
    # all-in-one-template windows are equally far from the peers; only the size differs
    windows, matrix, centroids, baseline = hand_made([[100, 0], [60, 0], [10, 0], [2, 0], [50, 50]])
    scored = contextual.scored_windows(windows, matrix, centroids, baseline)
    # 10 and 2 lines are below MIN_LINES, 50/50 is at the peers
    assert scored["row"].to_list() == [0, 1, 4]
    z = dict(zip(scored["row"], scored["z"], strict=True))
    assert z[4] == pytest.approx(-5.0)  # distance 0 against median 0.05, mad 0.01
    # the 60-line window has the same distance but a scale sqrt(100 / 60) wider
    assert z[0] / z[1] == pytest.approx(np.sqrt(100 / 60))


def test_window_must_reach_a_share_of_the_hosts_median_lines():
    windows, matrix, centroids, baseline = hand_made([[40, 0], [60, 0]])
    assert contextual.scored_windows(windows, matrix, centroids, baseline)["row"].to_list() == [1]


def test_injected_host_window_is_flagged_with_its_template(stage, synthetic):  # noqa: F811
    truth = synthetic.truth
    ctx = DetectContext(stage, stage.settings, lambda _: None)
    found = contextual.Contextual().detect(ctx)
    assert (found["kind"] == "host").all() and (found["raw"] >= contextual.THRESHOLD).all()
    hit = found.filter(
        during(truth.context_start, truth.context_end) & (pl.col("entity") == truth.context_host)
    )
    assert hit.height >= 3
    # the evidence names templates by their share of the window against the peers' share
    assert hit["evidence"].str.contains(r"T\d+ \d+% \(\d+% of lines vs \d+% at peers\)").all()
    # a host without a valid peer group is never scored
    assigned = hosts.load_assignments(stage)
    unscored = set(assigned.filter(~pl.col("peer_ok"))["host"])
    assert unscored and not unscored & set(found["entity"])
    assert ctx.meta["unscored_hosts"] == len(unscored)


def test_meta_and_page_state_the_unscored_hosts(stage):  # noqa: F811
    entry = next(d for d in an.load_meta(stage) if d["key"] == "contextual")
    unscored = int((~hosts.load_assignments(stage)["peer_ok"]).sum())
    assert entry["info"]["unscored_hosts"] == unscored > 0
    assert entry["info"]["scored_windows"] <= entry["info"]["windows"]
    assert f"{unscored} of 6 hosts have no valid peer group" in an.hint(
        an.Filters(), 1, meta=an.load_meta(stage)
    )
