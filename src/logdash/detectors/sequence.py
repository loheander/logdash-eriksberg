"""Unusual request sequences: a request whose consecutive templates the data hardly ever shows.

This is the *sequence* type of ``PLAN.md`` module 6 (kind ``request``). The lines of each request
id are ordered by ``(ts, template_id)`` and every consecutive pair ``(src, dst)`` is a transition,
fitted as a first-order Markov chain over all scored requests (``2 <= lines <= MAX_LINES``).
Longer requests are not scored: on the real data 91 % of the lines with a request id belong to a
few hundred ids of the periodic agent loops (up to 347,715 lines), which would own the model;
their number is in the detector's note on the page.

Ranking by mean negative log-likelihood is length-confounded and tie-ridden (96 % of the scored
requests have one transition), so the score is a tail mass. For a transition seen ``c`` times out
of ``tot`` from its source state, ``p = sum(c' for the next states with c' <= c) / tot`` is the
probability that a transition drawn from that state is at least as rare. A request's ``p`` is the
smallest of its transitions combined over its ``L`` transitions (Sidak:
``1 - (1 - p_min) ** L``), and ``raw = -log10(p)``. ``ALPHA`` is the per-request level, a strict
one on purpose: a state seen fewer than ``1 / ALPHA`` times cannot flag anything, and on the real
data it keeps well under ``ALPHA`` of the requests. The mean smoothed NLL
(``(c + 0.5) / (tot + 0.5 K)``, ``K`` templates) is in the evidence.
At most ``MAX_PER_PAIR`` requests are listed per transition (the rest are counted in the note).
"""

from __future__ import annotations

import math
from typing import Any

import duckdb
import polars as pl

from logdash.analyses import templates as tpl
from logdash.analyses.templates import short
from logdash.detectors.base import DetectContext, register

MIN_LINES = 2  # a request needs a transition
MAX_LINES = 100  # longer requests are periodic loops; the real p99 is 3
ALPHA = 0.01  # request level; raw is -log10 of the p-value
THRESHOLD = -math.log10(ALPHA)
TEXT_CHARS = 60
MAX_PER_PAIR = 5  # requests listed per rare transition

SCHEMA: dict[str, Any] = {
    "bin": pl.Datetime("us"),
    "window_s": pl.Int64,
    "kind": pl.String,
    "entity": pl.String,
    "raw": pl.Float64,
    "evidence": pl.String,
    "template_id": pl.Int64,
}

TRANSITION_SCHEMA: dict[str, Any] = {
    "request_id": pl.String,
    "first": pl.Datetime("us"),
    "last": pl.Datetime("us"),
    "n": pl.Int64,
    "src": pl.Int64,
    "dst": pl.Int64,
}


def fetch_transitions(
    con: duckdb.DuckDBPyConnection, assignments: str
) -> tuple[pl.DataFrame, dict[str, int]]:
    """Transitions of the scored requests and counts of the requests left out.

    ``assignments`` is the path of ``assignments.parquet`` (events carry no template id). Ties on
    ``ts`` are ordered by template id, so the result does not depend on the file order.
    """
    path = assignments.replace("'", "''")
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE seq_lines AS
            SELECT e.request_id, e.ts, a.template_id
            FROM events e JOIN read_parquet('{path}') a USING (masked_hash)
            WHERE e.request_id IS NOT NULL AND e.request_id <> '' AND NOT e.malformed"""
    )
    sizes = con.execute(
        """SELECT count(*) FILTER (WHERE n >= ?) AS with_transitions,
                  count(*) FILTER (WHERE n > ?) AS too_long,
                  coalesce(sum(n) FILTER (WHERE n > ?), 0) AS long_lines
           FROM (SELECT request_id, count(*) AS n FROM seq_lines GROUP BY request_id)""",
        [MIN_LINES, MAX_LINES, MAX_LINES],
    ).fetchone() or (0, 0, 0)
    frame = con.execute(
        """WITH sized AS (
               SELECT request_id FROM seq_lines GROUP BY request_id
               HAVING count(*) BETWEEN ? AND ?)
           SELECT request_id, first, last, n, src, dst FROM (
               SELECT request_id, template_id AS src,
                      lead(template_id) OVER w AS dst,
                      min(ts) OVER (PARTITION BY request_id) AS first,
                      max(ts) OVER (PARTITION BY request_id) AS last,
                      count(*) OVER (PARTITION BY request_id) AS n
               FROM seq_lines JOIN sized USING (request_id)
               WINDOW w AS (PARTITION BY request_id ORDER BY ts, template_id))
           WHERE dst IS NOT NULL""",
        [MIN_LINES, MAX_LINES],
    ).pl()
    con.execute("DROP TABLE seq_lines")
    counts = {
        "too_long": int(sizes[1]),
        "long_lines": int(sizes[2]),
        "scored": frame["request_id"].n_unique(),
    }
    return frame.cast(pl.Schema(TRANSITION_SCHEMA)), counts


def fit_pairs(transitions: pl.DataFrame, n_templates: int) -> pl.DataFrame:
    """Per (src, dst): ``c``, ``tot`` (of src), tail mass ``p`` and the smoothed ``nll``."""
    pairs = (
        transitions.group_by("src", "dst")
        .agg(c=pl.len())
        .sort("src", "c", "dst")
        .with_columns(
            tot=pl.col("c").sum().over("src"),
            cum=pl.col("c").cum_sum().over("src"),
        )
        .with_columns(tail=pl.col("cum").max().over("src", "c"))
    )
    return pairs.select(
        "src",
        "dst",
        "c",
        "tot",
        p=pl.col("tail") / pl.col("tot"),
        nll=-((pl.col("c") + 0.5) / (pl.col("tot") + 0.5 * n_templates)).log(),
    )


def flag_requests(transitions: pl.DataFrame, n_templates: int) -> pl.DataFrame:
    """Requests at or above the threshold with their rarest transition, ``raw = -log10(p)``."""
    pairs = fit_pairs(transitions, n_templates)
    return (
        transitions.join(pairs, on=["src", "dst"])
        .sort("request_id", "p", "dst")
        .group_by("request_id", maintain_order=True)
        .agg(
            pl.col("first").first(),
            pl.col("last").first(),
            pl.col("n").first(),
            nll=pl.col("nll").mean(),
            transitions=pl.len(),
            src=pl.col("src").first(),
            dst=pl.col("dst").first(),
            c=pl.col("c").first(),
            tot=pl.col("tot").first(),
            p_min=pl.col("p").first(),
        )
        .with_columns(p=1 - (1 - pl.col("p_min")) ** pl.col("transitions"))
        .with_columns(raw=-pl.col("p").log10())
        .filter(pl.col("raw") >= THRESHOLD)  # the rest is dropped by the framework anyway
    )


def cap_per_pair(flagged: pl.DataFrame) -> pl.DataFrame:
    """At most ``MAX_PER_PAIR`` requests per (src, dst), the strongest and earliest first.

    One transition that is rare but not unique (51 identical requests on the real data) would
    otherwise fill the top of the list with the same finding.
    """
    return flagged.sort(["raw", "first", "request_id"], descending=[True, False, False]).filter(
        pl.int_range(pl.len()).over("src", "dst") < MAX_PER_PAIR
    )


def request_rows(flagged: pl.DataFrame, templates: pl.DataFrame) -> pl.DataFrame:
    text = {
        r["template_id"]: short(r["template"], TEXT_CHARS) for r in templates.iter_rows(named=True)
    }
    rows = []
    for r in flagged.iter_rows(named=True):
        rows.append(
            {
                "bin": r["first"],
                "window_s": max(1, math.ceil((r["last"] - r["first"]).total_seconds())),
                "kind": "request",
                "entity": r["request_id"],
                "raw": r["raw"],
                "evidence": (
                    f"T{r['src']} -> T{r['dst']} seen {r['c']}/{r['tot']} times "
                    f"(P={r['p_min']:.4f}); mean NLL {r['nll']:.2f}; {r['n']} lines; "
                    f"{text.get(r['src'], '?')} -> {text.get(r['dst'], '?')}"
                ),
                "template_id": r["dst"],
            }
        )
    return pl.DataFrame(rows, schema=SCHEMA)


def score_requests(
    transitions: pl.DataFrame, templates: pl.DataFrame, n_templates: int
) -> pl.DataFrame:
    """One detector row per flagged request (after the per-pair cap)."""
    return request_rows(cap_per_pair(flag_requests(transitions, n_templates)), templates)


@register
class Sequence:
    key = "sequence"
    type = "sequence"
    title = "Request sequence"
    version = "1"
    setting_names: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = (tpl.KEY,)
    threshold = THRESHOLD

    def detect(self, ctx: DetectContext) -> pl.DataFrame:
        templates = ctx.store.read_parquet(tpl.KEY, "templates.parquet")
        con = ctx.store.connect()
        try:
            transitions, counts = fetch_transitions(
                con, str(ctx.store.path(tpl.KEY, "assignments.parquet"))
            )
        finally:
            con.close()
        flagged = flag_requests(transitions, templates.height)
        rows = request_rows(cap_per_pair(flagged), templates)
        ctx.meta["note"] = (
            f"{counts['scored']} requests of {MIN_LINES} to {MAX_LINES} lines scored; "
            f"{counts['too_long']} longer requests ({counts['long_lines']} lines, periodic "
            f"loops) are not. {flagged.height - rows.height} further requests repeat a "
            f"transition already listed {MAX_PER_PAIR} times."
        )
        ctx.log(f"sequence: {counts['scored']} requests scored, {flagged.height} flagged")
        return rows
