"""Spot check of semantic tags: sampling, the sheet, reading marks, agreement."""

from __future__ import annotations

import csv
import json
import random

import polars as pl
import pytest

from logdash.analyses.tags import TAGS_SCHEMA
from logdash.tagging import spotcheck as sc


def make_tags(n_actionable, n_noise, n_other, n_untagged=0):
    """tags, templates and counts frames with distinct texts and ids."""
    rows = []
    kinds = [(True, False)] * n_actionable + [(False, True)] * n_noise + [(False, False)] * n_other
    for i, (act, noise) in enumerate(kinds):
        rows.append(
            {
                "template_id": i,
                "template": f"tpl {i}",
                "tagged": True,
                "actionable": act,
                "component": "network",
                "noise": noise,
                "summary": f"summary {i}",
            }
        )
    for j in range(n_untagged):
        i = len(kinds) + j
        rows.append(
            {
                "template_id": i,
                "template": f"tpl {i}",
                "tagged": False,
                "actionable": None,
                "component": None,
                "noise": None,
                "summary": None,
            }
        )
    tags = pl.DataFrame(rows, schema=TAGS_SCHEMA)
    ids = tags["template_id"].to_list()
    templates = pl.DataFrame(
        {
            "template_id": ids,
            "template": tags["template"].to_list(),
            "count": [i + 1 for i in ids],
        }
    )
    counts = pl.DataFrame(
        [
            {
                "template_id": i,
                "bin": 0,
                "program": prog,
                "n": (5 if (prog == "nova") == (i % 2 == 0) else 1),
            }
            for i in ids
            for prog in ("nova", "neutron")
        ]
    )
    return tags, templates, counts


def pop_of(*args):
    tags, templates, counts = make_tags(*args)
    return sc.population(tags, templates, counts)


def test_stratum():
    assert sc.stratum(True, True) == "actionable"
    assert sc.stratum(False, True) == "noise"
    assert sc.stratum(False, False) == "other"


@pytest.mark.parametrize(
    ("sizes", "n", "expected"),
    [
        ({"actionable": 50, "noise": 50, "other": 200}, 30, (10, 10, 10)),
        ({"actionable": 3, "noise": 50, "other": 200}, 30, (3, 14, 13)),
        ({"actionable": 0, "noise": 2, "other": 5}, 30, (0, 2, 5)),
        ({"noise": 4}, 30, (0, 4, 0)),
    ],
)
def test_allocate_round_robin(sizes, n, expected):
    got = sc.allocate(sizes, n)
    assert tuple(got[s] for s in sc.STRATA) == expected


def test_population_joins_and_strata():
    tags, templates, counts = make_tags(1, 1, 1, n_untagged=1)
    tags = tags.with_columns(
        pl.when(pl.col("template_id") == 0).then(True).otherwise(pl.col("noise")).alias("noise")
    )
    tags = tags.with_columns(
        pl.when(pl.col("template_id") == 2)
        .then(pl.lit("changed text"))
        .otherwise(pl.col("template"))
        .alias("template")
    )
    # template 1: a tie between programs, broken alphabetically
    counts = counts.with_columns(
        pl.when(pl.col("template_id") == 1).then(3).otherwise(pl.col("n")).alias("n")
    )
    pop = sc.population(tags, templates, counts)
    assert pop["template_id"].to_list() == [0, 1, 2]  # untagged dropped
    rows = {r["template_id"]: r for r in pop.iter_rows(named=True)}
    assert rows[0]["stratum"] == "actionable"  # actionable and noise both true
    assert rows[0]["service"] == "nova"
    assert rows[1]["service"] == "neutron"
    assert rows[1]["stratum"] == "noise"
    assert rows[2]["count"] is None


def test_sample_is_reproducible_and_stratified():
    pop = pop_of(40, 40, 120)
    a = sc.sample(pop)
    assert a.equals(sc.sample(pop))
    assert not a.equals(sc.sample(pop, seed=25))
    assert a.height == 30
    assert {s: a.filter(pl.col("stratum") == s).height for s in sc.STRATA} == {
        "actionable": 10,
        "noise": 10,
        "other": 10,
    }
    assert a["template_id"].to_list() == sorted(a["template_id"].to_list())
    shuffled = pop.sample(fraction=1.0, shuffle=True, seed=3)
    assert sc.sample(shuffled).equals(a)
    renumbered = pop.with_columns((pl.col("template_id") + 1000).alias("template_id"))
    assert set(sc.sample(renumbered)["template"]) == set(a["template"])


def test_sample_takes_all_when_few():
    assert sc.sample(pop_of(4, 4, 4)).height == 12


def sheet_for(tmp_path, n_untagged=0):
    tags, templates, counts = make_tags(10, 10, 10, n_untagged)
    pop = sc.population(tags, templates, counts)
    picked = sc.sample(pop)
    meta = sc.sheet_meta(
        tags, pop, picked, {"model": "m/x", "prompt_version": 3}, seed=sc.SEED, n=sc.SAMPLE_SIZE
    )
    path = tmp_path / "sheets" / "s.csv"
    sidecar = sc.write_sheet(path, picked, meta)
    return path, sidecar, meta


def test_write_sheet_and_read_back(tmp_path):
    path, sidecar, meta = sheet_for(tmp_path, n_untagged=2)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with open(path, encoding="utf-8-sig", newline="") as fh:
        table = list(csv.DictReader(fh))
    assert tuple(table[0]) == sc.SHEET_COLUMNS
    assert len(table) == 30
    assert {r["actionable"] for r in table} <= {"yes", "no"}
    assert {r["noise"] for r in table} <= {"yes", "no"}
    assert all(not r[c] for r in table for c in (*sc.AGREE_COLUMNS, "note"))
    rows = sc.read_sheet(path)
    assert all(m is None for r in rows for m in r.marks.values())
    side = json.loads(sidecar.read_text())
    assert tuple(side) == sc.META_KEYS
    assert side["untagged"] == 2
    assert sc.read_sidecar(sidecar) == meta
    path2, _, _ = sheet_for(tmp_path / "again", n_untagged=2)
    assert path2.read_bytes() == raw


def sheet_text(rows, delimiter=","):
    header = delimiter.join(sc.SHEET_COLUMNS)
    lines = [header]
    for tid, marks, note in rows:
        cells = [str(tid), "nova", "3", "tpl", "yes", "network", "no", "sum", *marks, note]
        lines.append(delimiter.join(cells))
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("delimiter", [";", "\t"])
def test_read_sheet_variants(tmp_path, delimiter):
    text = sheet_text(
        [
            (1, ["Agree", " y ", "D", "no"], "  two   words "),
            (2, ["", "", "", ""], ""),
        ],
        delimiter,
    )
    text += delimiter * (len(sc.SHEET_COLUMNS) - 1) + "\n"  # blank spreadsheet row
    path = tmp_path / "s.csv"
    path.write_text(text, encoding="utf-8-sig")
    rows = sc.read_sheet(path)
    assert [r.template_id for r in rows] == [1, 2]
    assert list(rows[0].marks.values()) == [True, True, False, False]
    assert rows[0].note == "two words"
    assert all(m is None for m in rows[1].marks.values())


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (sheet_text([(1, ["maybe", "", "", ""], "")]), r":2: agree_actionable"),
        (sheet_text([(1, ["", "", "", ""], ""), (2, ["", "", "x", ""], "")]), r":3: agree_noise"),
        ("template_id,template\n1,x\n", "missing column"),
        (
            sheet_text([(1, ["", "", "", ""], ""), ("one", ["", "", "", ""], "")]),
            r":3: template_id",
        ),
        (sheet_text([(1, ["", "", "", ""], ""), (1, ["", "", "", ""], "")]), r":3: .*twice"),
    ],
)
def test_read_sheet_errors(tmp_path, text, match):
    path = tmp_path / "s.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        sc.read_sheet(path)


def marked(tid, marks, note="", values=None):
    values = values or {
        "actionable": "yes",
        "component": "network",
        "noise": "no",
        "summary": "s",
    }
    return sc.MarkedRow(tid, values, dict(zip(sc.TAGS, marks, strict=True)), note)


META = {
    "model": "m/x",
    "prompt_version": 3,
    "seed": 24,
    "size": 30,
    "templates": 281,
    "untagged": 0,
    "population": {"actionable": 40, "noise": 100, "other": 141},
    "sampled": {"actionable": 10, "noise": 10, "other": 10},
}


def test_score_and_report():
    rows = [
        marked(1, [True, True, True, True]),
        marked(17, [False, True, None, False], "bad | pipe here"),
        marked(18, [True, False, True, True], "wrong area"),
    ]
    result = sc.score(rows)
    by_tag = {s.tag: s for s in result.per_tag}
    assert (by_tag["actionable"].agree, by_tag["actionable"].disagree) == (2, 1)
    assert by_tag["noise"].unmarked == 1
    assert by_tag["noise"].rate == 1.0
    assert result.overall.agree == 8
    assert result.overall.disagree == 3
    assert result.overall.unmarked == 1
    assert result.all_agreed == 1
    assert result.disagreements == (
        (17, ("actionable=yes", "summary"), "bad | pipe here"),
        (18, ("component=network",), "wrong area"),
    )
    text = sc.render_report(result, META | {"untagged": 4})
    assert "| actionable | 2 | 1 | 0 | 67% |" in text
    assert "| **overall** | 8 | 3 | 1 | **73%** |" in text
    assert "actionable 10 of 40, noise 10 of 100, other 10 of 141" in text
    assert "4 untagged template(s) could not be sampled." in text
    assert "| 17 | actionable=yes, summary | bad \\| pipe here |" in text
    assert text.endswith("|\n")
    clean = sc.render_report(sc.score([marked(1, [True] * 4)]), META)
    assert "No disagreements." in clean
    assert "untagged" not in clean
    assert sc.pct(None) == "n/a"
    assert sc.TagScore("x", 0, 0, 2).rate is None


def test_read_sidecar_missing_key(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({k: 1 for k in sc.META_KEYS if k != "seed"}))
    with pytest.raises(ValueError, match="seed"):
        sc.read_sidecar(path)


def test_string_seed_is_deterministic():
    assert random.Random("24:noise").random() == random.Random("24:noise").random()
