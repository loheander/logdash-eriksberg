"""Sequence detector on hand-made transitions and events."""

import math
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl
import pytest

from logdash.detectors import sequence as sq
from logdash.detectors.sequence import (
    MAX_LINES,
    MAX_PER_PAIR,
    SCHEMA,
    THRESHOLD,
    TRANSITION_SCHEMA,
    fetch_transitions,
    fit_pairs,
    score_requests,
)

T0 = datetime(2023, 5, 21, 12, 0)
TEMPLATES = pl.DataFrame(
    {"template_id": [1, 2, 3], "template": ["one", "two", "three"]},
    schema={"template_id": pl.Int64, "template": pl.String},
)


def transitions(*requests: tuple[str, list[int]]) -> pl.DataFrame:
    """(request id, template ids in order) -> the transition table, one second per step."""
    rows = [
        (rid, T0, T0 + timedelta(seconds=len(ids) - 1), len(ids), ids[i], ids[i + 1])
        for rid, ids in requests
        for i in range(len(ids) - 1)
    ]
    return pl.DataFrame(rows, schema=TRANSITION_SCHEMA, orient="row")


def population(common: int, rare: int, state: int = 1) -> list[tuple[str, list[int]]]:
    """``common`` requests going from ``state`` to 2 and ``rare`` going to 3."""
    return [(f"c{state}_{i}", [state, 2]) for i in range(common)] + [
        (f"r{state}_{i}", [state, 3]) for i in range(rare)
    ]


def test_tail_mass_counts_transitions_at_least_as_rare():
    pairs = fit_pairs(transitions(*population(90, 6), ("x", [1, 4]), ("y", [1, 5])), 5)
    p = {r["dst"]: r["p"] for r in pairs.iter_rows(named=True)}
    assert p[2] == pytest.approx(1.0)  # the most common next state: every transition is as rare
    assert p[3] == pytest.approx(8 / 98)  # 6 and the two singletons
    assert p[4] == p[5] == pytest.approx(2 / 98)  # tied singletons share the tail
    assert set(pairs["tot"]) == {98}


def test_smoothed_nll_is_in_the_evidence_not_the_score():
    rows = score_requests(transitions(*population(990, 5), ("x", [1, 4])), TEMPLATES, 3)
    one = rows.filter(pl.col("entity") == "x")
    assert f"mean NLL {-math.log(1.5 / 997.5):.2f}" in one["evidence"][0]
    assert "seen 1/996 times (P=0.0010)" in one["evidence"][0]
    assert one["raw"][0] == pytest.approx(math.log10(996))  # p = 1 / 996, L = 1


def test_only_rare_transitions_are_returned_with_their_request_window():
    rows = score_requests(transitions(*population(500, 3)), TEMPLATES, 3)
    assert rows.schema == SCHEMA and sorted(rows["entity"]) == ["r1_0", "r1_1", "r1_2"]
    assert (rows["kind"] == "request").all() and (rows["window_s"] == 1).all()
    assert (rows["bin"] == T0).all() and (rows["raw"] >= THRESHOLD).all()
    assert rows["template_id"].to_list() == [3] * 3
    assert "T1 -> T3 seen 3/503 times" in rows["evidence"][0]
    assert "one -> three" in rows["evidence"][0]


def test_a_common_variant_is_not_flagged():
    # 30 % of the requests take the other branch: its tail mass is 0.3, far from ALPHA
    rows = score_requests(transitions(*population(70, 30)), TEMPLATES, 3)
    assert rows.is_empty() and rows.schema == SCHEMA


def test_a_state_seen_few_times_cannot_flag_anything():
    rows = score_requests(transitions(*population(98, 1)), TEMPLATES, 3)  # P = 1 / 99 > ALPHA
    assert rows.is_empty()
    assert score_requests(transitions(*population(100, 1)), TEMPLATES, 3).height == 1


def test_requests_are_combined_over_their_transitions():
    reqs = [
        *population(700, 2),
        ("multi", [1, 3, 2]),  # 1->3 is seen 3 of 703 times; 3->2 is a state seen once
    ]
    row = score_requests(transitions(*reqs), TEMPLATES, 3).filter(pl.col("entity") == "multi")
    p = 1 - (1 - 3 / 703) ** 2  # Sidak over the two transitions
    assert row["raw"][0] == pytest.approx(-math.log10(p))
    assert "T1 -> T3 seen 3/703 times" in row["evidence"][0] and "3 lines" in row["evidence"][0]
    assert row["window_s"][0] == 2


def test_one_rare_transition_lists_at_most_max_per_pair_requests():
    reqs = [*population(2000, 12), *[(f"d{i}", [2, 1]) for i in range(2)]]
    rows = score_requests(transitions(*reqs), TEMPLATES, 3)
    assert rows.height == MAX_PER_PAIR  # 12 requests share 1->3; the pair 2->1 is common
    ids = sorted(f"r1_{i}" for i in range(12))  # equal strength and time: by request id
    assert rows["entity"].to_list() == ids[:MAX_PER_PAIR]


def test_empty_input_returns_the_schema():
    assert score_requests(transitions(), TEMPLATES, 3).schema == SCHEMA


# ---------------------------------------------------------------- reading the cache


def connection(lines: list[tuple[str | None, int, int, bool]]) -> duckdb.DuckDBPyConnection:
    """(request id, seconds, masked_hash, malformed) -> an ``events`` table."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE events (request_id VARCHAR, ts TIMESTAMP, masked_hash BIGINT, "
        "malformed BOOLEAN)"
    )
    for rid, sec, mh, bad in lines:
        con.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?)", [rid, T0 + timedelta(seconds=sec), mh, bad]
        )
    return con


def assignments(tmp_path: Path) -> str:
    path = tmp_path / "assignments.parquet"
    pl.DataFrame({"masked_hash": [10, 20, 30], "template_id": [1, 2, 3]}).write_parquet(path)
    return str(path)


def test_fetch_orders_ties_by_template_and_leaves_out_unscorable_requests(tmp_path):
    long_request = [("long", i, 10, False) for i in range(MAX_LINES + 1)]
    con = connection(
        [
            ("a", 0, 30, False),  # same ts as the next line: template 2 first, then 3
            ("a", 0, 20, False),
            ("a", 5, 10, False),
            ("single", 0, 10, False),
            ("", 0, 10, False),
            ("", 1, 20, False),  # no request id
            (None, 0, 10, False),
            (None, 1, 20, False),
            ("bad", 0, 10, True),
            ("bad", 1, 20, True),
            *long_request,
        ]
    )
    frame, counts = fetch_transitions(con, assignments(tmp_path))
    assert frame.schema == pl.Schema(TRANSITION_SCHEMA)
    got = frame.sort("first", "src")
    assert got["request_id"].to_list() == ["a", "a"]
    assert list(zip(got["src"], got["dst"], strict=True)) == [(2, 3), (3, 1)]
    assert got["n"].to_list() == [3, 3] and got["last"][0] == T0 + timedelta(seconds=5)
    assert counts == {"too_long": 1, "long_lines": MAX_LINES + 1, "scored": 1}


def test_a_request_of_exactly_max_lines_is_scored(tmp_path):
    con = connection([("r", i, 10 if i % 2 else 20, False) for i in range(MAX_LINES)])
    frame, counts = fetch_transitions(con, assignments(tmp_path))
    assert frame.height == MAX_LINES - 1 and counts["too_long"] == 0
    assert sq.score_requests(frame, TEMPLATES, 3).is_empty()  # one request: nothing is rare


def test_no_events_with_a_request_id_gives_no_transitions(tmp_path):
    frame, counts = fetch_transitions(connection([]), assignments(tmp_path))
    assert frame.is_empty() and counts == {"too_long": 0, "long_lines": 0, "scored": 0}
