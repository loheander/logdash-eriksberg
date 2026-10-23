"""The ``logdash spot-check`` command."""

from __future__ import annotations

import csv

import pytest

from logdash.cache.store import CacheStore
from logdash.cli import main
from logdash.config import Settings
from logdash.tagging import spotcheck
from tests.unit.test_spotcheck import make_tags


def spot_cache(cache):
    store = CacheStore(cache, Settings())
    tags, templates, counts = make_tags(12, 12, 12)
    store.write_parquet("tags", "tags.parquet", tags)
    store.write_json("tags", "meta.json", {"model": "m/x", "prompt_version": 3})
    store.write_parquet("templates", "templates.parquet", templates)
    store.write_parquet("templates", "template_counts.parquet", counts)


def test_cli_spot_check_sample_and_score(cache_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    spot_cache(cache_dir)
    assert main(["spot-check", "sample", "--cache", str(cache_dir)]) == 0
    assert "tags stage is missing" in capsys.readouterr().err
    sheet = tmp_path / spotcheck.DEFAULT_SHEET
    assert sheet.exists()
    assert spotcheck.sidecar_path(sheet).exists()
    before = sheet.read_bytes()
    assert main(["spot-check", "sample", "--cache", str(cache_dir)]) == 2
    assert "pass --force" in capsys.readouterr().err
    assert sheet.read_bytes() == before
    assert main(["spot-check", "sample", "--cache", str(cache_dir), "--force"]) == 0

    with open(sheet, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        for col in spotcheck.AGREE_COLUMNS:
            row[col] = "agree"
    rows[0]["agree_noise"] = "disagree"
    rows[0]["note"] = "hmm"
    rows[1]["agree_summary"] = ""
    with open(sheet, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=spotcheck.SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    capsys.readouterr()
    assert main(["spot-check", "score"]) == 0
    captured = capsys.readouterr()
    assert "## Semantic tags spot check" in captured.out
    assert "1 of 120 marks are empty" in captured.err
    report = (tmp_path / spotcheck.DEFAULT_REPORT).read_text(encoding="utf-8")
    assert "| **overall** | 118 | 1 | 1 |" in report
    assert "hmm" in report


def test_cli_spot_check_small_population(cache_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    spot_cache(cache_dir)
    assert main(["spot-check", "sample", "--cache", str(cache_dir), "--size", "50"]) == 0
    assert "only 36 tagged templates could be sampled (asked for 50)" in capsys.readouterr().out


def test_cli_spot_check_errors(cache_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["spot-check", "sample", "--cache", str(cache_dir)]) == 2
    assert "logdash build --only tags" in capsys.readouterr().err
    assert main(["spot-check", "score"]) == 2
    assert "error:" in capsys.readouterr().err

    spot_cache(cache_dir)
    assert main(["spot-check", "sample", "--cache", str(cache_dir)]) == 0
    sheet = tmp_path / spotcheck.DEFAULT_SHEET
    with open(sheet, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows[0]["agree_noise"] = "maybe"
    with open(sheet, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=spotcheck.SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    capsys.readouterr()
    assert main(["spot-check", "score"]) == 2
    assert "not agree or disagree" in capsys.readouterr().err


def test_cli_spot_check_requires_action():
    with pytest.raises(SystemExit) as exc:
        main(["spot-check"])
    assert exc.value.code == 2
