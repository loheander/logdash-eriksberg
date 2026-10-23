"""Spot check of semantic tags (issue #24): a reproducible stratified sample
for a person to mark, and agreement from the marked sheet. Reads the cache only; never
calls a model.
"""

from __future__ import annotations

import csv
import io
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

SAMPLE_SIZE = 30
SEED = 24  # the parent issue
STRATA = ("actionable", "noise", "other")
TAGS = ("actionable", "component", "noise", "summary")
AGREE_COLUMNS = tuple(f"agree_{t}" for t in TAGS)
SHEET_COLUMNS = (
    "template_id",
    "service",
    "count",
    "template",
    *TAGS,
    *AGREE_COLUMNS,
    "note",
)
MARKS: dict[str, bool] = {"agree": True, "disagree": False}
DEFAULT_SHEET = Path("spot-checks/tags-spot-check.csv")
DEFAULT_REPORT = Path("spot-checks/tags-spot-check-results.md")
META_KEYS = (
    "model",
    "prompt_version",
    "seed",
    "size",
    "templates",
    "untagged",
    "population",
    "sampled",
)


def stratum(actionable: bool, noise: bool) -> str:
    if actionable:
        return "actionable"
    return "noise" if noise else "other"


def population(tags: pl.DataFrame, templates: pl.DataFrame, counts: pl.DataFrame) -> pl.DataFrame:
    """Tagged templates with service, line count and stratum, sorted by template_id."""
    top = (
        counts.group_by("template_id", "program")
        .agg(pl.col("n").sum())
        .sort(["template_id", "n", "program"], descending=[False, True, False])
        .group_by("template_id", maintain_order=True)
        .first()
        .select("template_id", pl.col("program").alias("service"))
    )
    return (
        tags.filter(pl.col("tagged"))
        .join(
            templates.select("template_id", "template", "count"),
            on=["template_id", "template"],
            how="left",
        )
        .join(top, on="template_id", how="left")
        .with_columns(
            pl.when(pl.col("actionable"))
            .then(pl.lit("actionable"))
            .when(pl.col("noise"))
            .then(pl.lit("noise"))
            .otherwise(pl.lit("other"))
            .alias("stratum")
        )
        .select(
            "template_id",
            "service",
            "count",
            "template",
            "actionable",
            "component",
            "noise",
            "summary",
            "stratum",
        )
        .sort("template_id")
    )


def allocate(sizes: Mapping[str, int], n: int) -> dict[str, int]:
    """Round-robin places over STRATA until n are given or every stratum is full."""
    quota = dict.fromkeys(STRATA, 0)
    given = 0
    while given < n:
        progressed = False
        for s in STRATA:
            if given < n and quota[s] < sizes.get(s, 0):
                quota[s] += 1
                given += 1
                progressed = True
        if not progressed:
            break
    return quota


def sample(pop: pl.DataFrame, n: int = SAMPLE_SIZE, seed: int = SEED) -> pl.DataFrame:
    """Stratified, reproducible sample; candidates are ordered by template text."""
    by_stratum = {
        s: sorted(pop.filter(pl.col("stratum") == s)["template"].to_list()) for s in STRATA
    }
    quota = allocate({s: len(v) for s, v in by_stratum.items()}, n)
    picked: set[str] = set()
    for s in STRATA:
        picked |= set(random.Random(f"{seed}:{s}").sample(by_stratum[s], quota[s]))
    return pop.filter(pl.col("template").is_in(picked)).sort("template_id")


def _strata_counts(frame: pl.DataFrame) -> dict[str, int]:
    return {s: frame.filter(pl.col("stratum") == s).height for s in STRATA}


def sheet_meta(
    tags: pl.DataFrame,
    pop: pl.DataFrame,
    picked: pl.DataFrame,
    tags_meta: Mapping[str, Any],
    *,
    seed: int,
    n: int,
) -> dict[str, Any]:
    return {
        "model": tags_meta.get("model"),
        "prompt_version": tags_meta.get("prompt_version"),
        "seed": seed,
        "size": n,
        "templates": pop.height,
        "untagged": tags.height - pop.height,
        "population": _strata_counts(pop),
        "sampled": _strata_counts(picked),
    }


def strata_line(meta: Mapping[str, Any]) -> str:
    sampled, pop = meta["sampled"], meta["population"]
    return ", ".join(f"{s} {sampled[s]} of {pop[s]}" for s in STRATA)


def sidecar_path(sheet: Path) -> Path:
    return sheet.with_suffix(".json")


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("yes" if value else "no")


def write_sheet(path: Path, picked: pl.DataFrame, meta: Mapping[str, Any]) -> Path:
    """Write the CSV (UTF-8 with BOM) and the JSON sidecar; return the sidecar path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SHEET_COLUMNS)
        for row in picked.iter_rows(named=True):
            writer.writerow(
                [
                    row["template_id"],
                    row["service"] or "",
                    "" if row["count"] is None else row["count"],
                    row["template"],
                    _yes_no(row["actionable"]),
                    row["component"] or "",
                    _yes_no(row["noise"]),
                    row["summary"] or "",
                    *([""] * len(AGREE_COLUMNS)),
                    "",
                ]
            )
    sidecar = sidecar_path(path)
    sidecar.write_text(json.dumps(dict(meta), indent=2) + "\n", encoding="utf-8")
    return sidecar


def parse_mark(value: str | None, where: str) -> bool | None:
    """`agree` -> True, `disagree` -> False, empty -> None (case and outer
    whitespace ignored). Anything else is an error: yes/no would clash with the
    yes/no tag columns."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if v in MARKS:
        return MARKS[v]
    raise ValueError(f"{where}: {value!r} is not agree or disagree")


@dataclass(frozen=True)
class MarkedRow:
    template_id: int
    values: dict[str, str]
    marks: dict[str, bool | None]
    note: str


def read_sheet(path: Path) -> list[MarkedRow]:
    text = path.read_text(encoding="utf-8-sig")
    header = text.splitlines()[0] if text else ""
    delimiter = next((d for d in (",", ";", "\t") if d in header), ",")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    missing = [c for c in SHEET_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
    rows: list[MarkedRow] = []
    seen: set[int] = set()
    for line, rec in enumerate(reader, start=2):
        raw = (rec["template_id"] or "").strip()
        if not raw:
            continue
        try:
            tid = int(raw)
        except ValueError:
            raise ValueError(f"{path}:{line}: template_id {raw!r} is not a number") from None
        if tid in seen:
            raise ValueError(f"{path}:{line}: template_id {tid} appears twice")
        seen.add(tid)
        marks = {t: parse_mark(rec[f"agree_{t}"], f"{path}:{line}: agree_{t}") for t in TAGS}
        rows.append(
            MarkedRow(
                template_id=tid,
                values={t: rec[t] or "" for t in TAGS},
                marks=marks,
                note=" ".join((rec["note"] or "").split()),
            )
        )
    return rows


def read_sidecar(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    missing = [k for k in META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"{path}: missing key(s) {', '.join(missing)}")
    return meta


@dataclass(frozen=True)
class TagScore:
    tag: str
    agree: int
    disagree: int
    unmarked: int

    @property
    def rate(self) -> float | None:
        total = self.agree + self.disagree
        return self.agree / total if total else None


@dataclass(frozen=True)
class Agreement:
    per_tag: tuple[TagScore, ...]
    overall: TagScore
    rows: int
    all_agreed: int
    disagreements: tuple[tuple[int, tuple[str, ...], str], ...]


def score(rows: Sequence[MarkedRow]) -> Agreement:
    per_tag = []
    for tag in TAGS:
        marks = [r.marks[tag] for r in rows]
        per_tag.append(
            TagScore(
                tag,
                sum(m is True for m in marks),
                sum(m is False for m in marks),
                sum(m is None for m in marks),
            )
        )
    overall = TagScore(
        "overall",
        sum(s.agree for s in per_tag),
        sum(s.disagree for s in per_tag),
        sum(s.unmarked for s in per_tag),
    )
    disagreements = []
    for r in rows:
        bad = tuple(
            "summary" if t == "summary" else f"{t}={r.values[t]}"
            for t in TAGS
            if r.marks[t] is False
        )
        if bad:
            disagreements.append((r.template_id, bad, r.note))
    return Agreement(
        per_tag=tuple(per_tag),
        overall=overall,
        rows=len(rows),
        all_agreed=sum(all(r.marks[t] is True for t in TAGS) for r in rows),
        disagreements=tuple(disagreements),
    )


def pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_report(result: Agreement, meta: Mapping[str, Any]) -> str:
    untagged = (
        f" {meta['untagged']} untagged template(s) could not be sampled."
        if meta["untagged"] > 0
        else ""
    )
    lines = [
        "## Semantic tags spot check",
        "",
        f"Model `{meta['model']}`, prompt version {meta['prompt_version']}. "
        f"{result.rows} templates marked, sampled with seed {meta['seed']} "
        f"from {meta['templates']} tagged templates ({strata_line(meta)}).{untagged}",
        "",
        "| Tag | Agree | Disagree | Unmarked | Agreement |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in result.per_tag:
        lines.append(f"| {s.tag} | {s.agree} | {s.disagree} | {s.unmarked} | {pct(s.rate)} |")
    o = result.overall
    lines.append(f"| **overall** | {o.agree} | {o.disagree} | {o.unmarked} | **{pct(o.rate)}** |")
    lines += [
        "",
        f"Templates with every tag agreed: {result.all_agreed} of {result.rows}.",
        "",
        "### Disagreements",
        "",
    ]
    if result.disagreements:
        lines += ["| Template | Tags | Note |", "|---|---|---|"]
        for tid, tags, note in result.disagreements:
            lines.append(f"| {tid} | {', '.join(tags)} | {_cell(note)} |")
    else:
        lines.append("No disagreements.")
    return "\n".join(lines) + "\n"
