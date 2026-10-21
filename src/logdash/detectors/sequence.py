"""Unusual request sequences: a request whose consecutive templates the data hardly ever shows.

This is the *sequence* type of ``PLAN.md`` module 6 (kind ``request``). The lines of each request
id are ordered by ``(ts, template_id)`` and every consecutive pair ``(src, dst)`` is a transition,
fitted as a first-order Markov chain over all scored requests (``2 <= lines <= MAX_LINES``).
Longer requests are not scored: on the real data 91 % of the lines with a request id belong to a
few hundred ids of the periodic agent loops (up to 347,715 lines), which would own the model;
their number, by program, is in the detector's note on the page. Those programs get no sequence
scoring at all (a per-id loop-aware model is future work).

Ranking by mean negative log-likelihood is length-confounded and tie-ridden (96 % of the scored
requests have one transition), so the score is a tail mass. For a transition seen ``c`` times out
of ``tot`` from its source state, ``p = sum(c' for the next states with c' <= c) / tot`` is the
probability that a transition drawn from that state is at least as rare. A request's ``p`` is the
smallest of its transitions combined over its ``L`` distinct ``(src, dst)`` pairs (Sidak:
``1 - (1 - p_min) ** L``; a pair repeated in the request is one test, and the transitions of a
chain are dependent, for which Sidak is conservative, so it is an approximation), and
``raw = -log10(p)``. ``ALPHA`` is the per-request level, a strict one on purpose: ``p`` is at least
``1 / tot`` per transition, so a source state seen fewer than ``1 / ALPHA`` times cannot flag
anything (more for ``L > 1``: ``p`` is about ``L * p_min``). On the real data only 5 of 70 source
states are seen often enough, and the detector keeps well under ``ALPHA`` of the requests. The mean
smoothed NLL (``(c + 0.5) / (tot + 0.5 K)``, ``K`` templates) is in the evidence.

At most ``MAX_PER_PAIR`` requests are listed per rarest transition: the most extreme one and
requests evenly spread over the time order of the group (so a burst is not shown as its first five).
Their evidence says how many requests share the transition and over which time span; the rest are
counted in the note.
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
TOP_PROGRAMS = 4  # programs named in the note for the requests that are not scored
FORMULA = 2  # bump when the scoring or the evidence changes (part of the detector version)

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
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Transitions of the scored requests and counts of the requests left out.

    ``by_program`` lists ``(program, requests, lines)`` of the too long requests, biggest first.

    ``assignments`` is the path of ``assignments.parquet`` (events carry no template id). Ties on
    ``ts`` are ordered by template id, so the result does not depend on the file order.
    """
    path = assignments.replace("'", "''")
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE seq_lines AS
            SELECT e.request_id, e.ts, a.template_id, e.program
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
    by_program = con.execute(
        """WITH long_ids AS (
               SELECT request_id FROM seq_lines GROUP BY request_id HAVING count(*) > ?)
           SELECT coalesce(program, '?') AS program, count(DISTINCT request_id), count(*) AS n
           FROM seq_lines JOIN long_ids USING (request_id)
           GROUP BY 1 ORDER BY n DESC, 1""",
        [MAX_LINES],
    ).fetchall()
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
        "by_program": [(str(p), int(ids), int(n)) for p, ids, n in by_program],
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
    """Requests at or above the threshold with their rarest transition, ``raw = -log10(p)``.

    ``p_min`` is the rarest transition's tail mass, ``p`` the request's (Sidak over the ``L``
    distinct ``(src, dst)`` pairs of the request, ``transitions``).
    """
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
            transitions=pl.struct("src", "dst").n_unique(),
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


def spread(n: int, k: int) -> list[int]:
    """``k`` of ``n`` positions, the first and last included, evenly spaced."""
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def cap_per_pair(flagged: pl.DataFrame) -> pl.DataFrame:
    """At most ``MAX_PER_PAIR`` requests per (src, dst), with the size and span of their group.

    One transition that is rare but not unique (51 identical requests on the real data) would
    otherwise fill the top of the list with the same finding. The strongest request of a group
    (earliest on ties) is kept, then the others are chosen evenly over the group's time order
    (first and last included), so a burst is not shown as its first requests. The columns
    ``group_n``, ``group_first`` and ``group_last`` describe the whole group.
    """
    kept = []
    ordered = flagged.sort("first", "request_id")
    for _, group in ordered.group_by(["src", "dst"], maintain_order=True):
        ids = group["request_id"].to_list()
        best = group.sort(["raw", "first", "request_id"], descending=[True, False, False])
        chosen = {ids.index(best["request_id"][0])}
        for i in spread(group.height, MAX_PER_PAIR):
            if len(chosen) < MAX_PER_PAIR:
                chosen.add(i)
        kept.append(
            group[sorted(chosen)].with_columns(
                group_n=pl.lit(group.height, pl.Int64),
                group_first=pl.lit(group["first"].min()),
                group_last=pl.lit(group["first"].max()),
            )
        )
    if not kept:
        when = flagged.schema["first"]
        return flagged.head(0).with_columns(
            group_n=pl.lit(None, pl.Int64),
            group_first=pl.lit(None, when),
            group_last=pl.lit(None, when),
        )
    return pl.concat(kept).sort(["raw", "first", "request_id"], descending=[True, False, False])


def request_rows(flagged: pl.DataFrame, templates: pl.DataFrame) -> pl.DataFrame:
    text = {
        r["template_id"]: short(r["template"], TEXT_CHARS) for r in templates.iter_rows(named=True)
    }
    rows = []
    for r in flagged.iter_rows(named=True):
        group = ""
        if r["group_n"] > 1:
            span = f"{r['group_first']:%m-%d %H:%M} to {r['group_last']:%m-%d %H:%M}"
            group = f"; one of {r['group_n']} requests with it, {span}"
        rows.append(
            {
                "bin": r["first"],
                "window_s": max(1, math.ceil((r["last"] - r["first"]).total_seconds())),
                "kind": "request",
                "entity": r["request_id"],
                "raw": r["raw"],
                "evidence": (
                    f"T{r['src']} -> T{r['dst']} seen {r['c']}/{r['tot']} times "
                    f"(rarest transition P={r['p_min']:.4f} of {r['transitions']} distinct, "
                    f"request p={r['p']:.4f}); mean NLL {r['nll']:.2f}; {r['n']} lines; "
                    f"{text.get(r['src'], '?')} -> {text.get(r['dst'], '?')}{group}"
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


def note(counts: dict[str, Any], transitions: pl.DataFrame, dropped: int, n_templates: int) -> str:
    """Coverage of the model for the page: what can flag, and what is not scored at all."""
    states = fit_pairs(transitions, n_templates).group_by("src").agg(tot=pl.col("c").sum())
    able = states.filter(pl.col("tot") >= 1 / ALPHA)
    text = (
        f"{counts['scored']} requests of {MIN_LINES} to {MAX_LINES} lines scored; only the "
        f"{able.height} of {states.height} source states seen at least {math.ceil(1 / ALPHA)} "
        f"times can flag ({able['tot'].sum()} of {states['tot'].sum()} transitions). "
        f"{counts['too_long']} longer requests ({counts['long_lines']} lines) are not scored"
    )
    if counts["by_program"]:
        top = ", ".join(
            f"{p} {ids} requests / {n} lines" for p, ids, n in counts["by_program"][:TOP_PROGRAMS]
        )
        more = len(counts["by_program"]) - TOP_PROGRAMS
        rest = f", {more} more" if more > 0 else ""
        text += (
            " (periodic agent loops; by program, a request counts for each program it has "
            f"lines in: {top}{rest})"
        )
    return (
        text
        + f". {dropped} further requests repeat a transition already listed {MAX_PER_PAIR} times."
    )


@register
class Sequence:
    key = "sequence"
    type = "sequence"
    title = "Request sequence"
    # constants and the formula change the output
    version = f"{FORMULA}:{MIN_LINES}:{MAX_LINES}:{ALPHA}:{MAX_PER_PAIR}:{TEXT_CHARS}"
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
        ctx.meta["note"] = note(counts, transitions, flagged.height - rows.height, templates.height)
        ctx.log(f"sequence: {counts['scored']} requests scored, {flagged.height} flagged")
        return rows
