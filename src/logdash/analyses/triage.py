"""Module 9: anomaly triage with the Jev decision model, called live on click.

The detectors say how unusual a window is; this page asks a decision model whether anyone must
act, what kind of event it is and how bad it is. A click on an anomaly (a grid row or a point in
the scatter) opens a drawer at once (clientside, from the row data) and starts a timer; the server
then reads the level counts and the dominant program of the entity in the window (one DuckDB
query pushed down to the hour partitions), builds the state and sends one request to Jev. The
answers come back with the time of each part, and the drawer moves its bars, rings and gauge
from the previous values to the new ones.

Nothing is precomputed or cached: each click is a new call, so the delay seen is real. The build
stage writes nothing; its version carries the hash of the question spec. The clientside half of
the page is ``app/assets/triage.js``; the values it shows are computed here (``answer_view``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
import duckdb
import plotly.graph_objects as go
import polars as pl
from dash import ClientsideFunction, Dash, Input, Output, State, dcc, html
from dash.development.base_component import Component

from logdash import jev
from logdash.analyses import anomalies
from logdash.analyses.base import ComputeContext, register
from logdash.analyses.explorer import hour_values
from logdash.analyses.features import Lru, segmented
from logdash.analyses.templates import short
from logdash.app import figures, ui
from logdash.app.filters import FILTER_STORE_ID, Filters, from_dict
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.detectors.base import DETECTORS

KEY = "triage"
TEXT_CHARS = 110
MAX_TEMPLATES = 5
TRANSITION_MS = 250
TICK_MS = 50
CHECK_CONFIDENCE = 0.5  # below this confidence an answer gets a "check" badge
CHECK_MARGIN = 0.1  # ... and when the top two probabilities are this close
LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "UNKNOWN")

# ------------------------------------------------------------------------- the questions

TYPE_DESCRIPTIONS = {
    "volume": "the number of lines per minute of a service or host rises far above (spike) or "
    "falls far below (drop) its rolling baseline",
    "severity": "the rate of ERROR or WARNING lines of a service bursts far above its usual rate",
    "structural": "the mix of log templates of a service, a host or the whole fleet in a window "
    "is far from that entity's usual mix",
    "contextual": "one host's mix of log templates in a minute is far from that of its peer "
    "hosts (hosts that run the same services) in the same minute",
    "novel": "a log template is first seen late in the 24 h (novel) or is seen only a handful "
    "of times in all (rare)",
    "sequence": "the order of log templates within one request contains a transition that is "
    "rare across all requests",
}
DETECTOR_DESCRIPTIONS = {
    "volume": "Volume spike or drop: lines per minute of the entity against its rolling median.",
    "severity": "Severity burst: ERROR or WARNING lines per minute of a service against its "
    "baseline rate (Poisson test); bursts close together are merged into one row.",
    "structural_pca": "PCA residual (SPE): the part of a window's template counts that the main "
    "components of normal behaviour do not explain, as a multiple of the limit.",
    "structural_iforest": "Isolation Forest per entity: how easily a window's template counts "
    "are isolated from the entity's other windows (score in [0, 1]).",
    "contextual": "Host against its peers: distance of the host's template mix in a minute to "
    "the mix of its peer hosts in that minute, as a z score of its usual distance.",
    "novel_template": "Novel template: a template first seen in the last part of the 24 h.",
    "rare_template": "Rare template: a template with only a few lines in the whole data set.",
    "sequence": "Request sequence: the rarest transition between consecutive templates of a "
    "request, against all requests.",
}

CONTEXT = (
    "Anomalies found by statistical detectors in 24 h of logs from an OpenStack cloud (162 "
    "hosts, 40 services: nova, neutron, cinder, keystone, glance, barbican, heat, octavia, "
    "haproxy, mariadb, libvirt, openvswitch, fluent, ...). Each item is one detector hit: the "
    "detector and what it measures, the entity (a service, a host, the whole fleet or one API "
    "request), its subsystem, the time window, the detector's evidence, the masked text of the "
    "log templates involved (<IP>, <UUID>, <PATH>, <NUM> and <*> stand for variable parts) and "
    "the line counts per level of the entity in the window. A detector says how unusual a "
    "window is, not whether it matters: a high score, z or p value is not by itself a sign of "
    "harm, and a spike or burst that repeats at regular intervals is a scheduled job. The "
    "answers help an on-call operator triage the list. "
    "Detector types: " + "; ".join(f"{t}: {d}" for t, d in TYPE_DESCRIPTIONS.items()) + "."
)

NATURE = {
    "error_fault": "Errors or exceptions from a component that fails: failed connections, "
    "tracebacks, failing requests.",
    "config": "A configuration problem: a missing device, a wrong setting, a resource that is "
    "not managed or not found.",
    "load": "More or fewer requests or lines than usual, with nothing failing.",
    "periodic": "A recurring job, sync or report: the evidence says the episodes repeat at "
    "regular intervals (for example 'repeating about every 77 min').",
    "lifecycle": "A start, stop, restart, deploy or upgrade, or the first appearance of new code "
    "paths.",
    "noise": "A logging artefact: malformed, duplicated or debug output (for example a line "
    "with raw CSV fields or escape codes), nothing operational.",
    "unclear": "The evidence does not show which of the above it is.",
}
SEVERITY_LEVELS = (
    "Informational: no action, expected behaviour",
    "Low: worth a look when convenient",
    "Medium: a degradation on one host or service, fix this week",
    "High: a service fails or many hosts are affected, fix today",
    "Critical: an outage for tenants, act now",
)
SEVERITY_NAMES = ("informational", "low", "medium", "high", "critical")
SEVERITY_COLORS = ("teal", "lime", "yellow", "orange", "red")

# The three demo rows of the plan, from the current cache: a periodic spike with the maximum
# score, a service fault with a lower rank and a misconfiguration on one host.
SEVERITY_EXAMPLES = [
    {
        "item": {
            "detector": "volume",
            "entity": "neutron-l3-agent",
            "subsystem": "network",
            "evidence": "spike: peak 42925 events/min vs baseline 20, 61131 extra events over 6 "
            "min; 96 episodes in all, repeating about every 77 min",
        },
        "severity": 0,
    },
    {
        "item": {
            "detector": "severity",
            "entity": "barbican-api",
            "subsystem": "identity_secrets",
            "evidence": "ERROR burst: peak 101/min vs baseline 0/min (Poisson P=9.6e-262 at "
            "0.1/min, z=34.5), 975 extra in 7 bursts over 112 min (07:50 to 09:41)",
        },
        "severity": 3,
    },
    {
        "item": {
            "detector": "contextual",
            "entity": "eselda12u01s09",
            "subsystem": "network",
            "evidence": "z 93.8: distance to peers 0.69 (expected 0.03 for 153 lines); "
            "templates: T86 68% could not open network device <*> (No such device); T97 17% "
            "bridge <*> <*> interface <*> on port <NUM>",
        },
        "severity": 2,
    },
]

QUESTIONS: dict[str, dict[str, Any]] = {
    "actionable": {
        "type": "noul",
        "instructions": "Would an on-call operator need to investigate or fix this?",
        "criteria": {
            "true": "A fault, a degradation or a misconfiguration that persists or recurs.",
            "false": "Expected behaviour such as a scheduled or periodic job (episodes that "
            "repeat at regular intervals), a load change, a restart that recovered, a routine "
            "change seen on many hosts at once, or a logging artefact.",
        },
    },
    "user_impact": {
        "type": "noul",
        "instructions": "Does this probably affect tenants or API clients?",
        "criteria": {
            "true": "Failed or slow API requests, instance or volume operations that fail, "
            "errors returned to clients.",
            "false": "Internal only (agents, housekeeping, log shipping, the state of one "
            "host's agent), or no failure. WARNING lines alone are not client errors.",
        },
    },
    "nature": {
        "type": "choice",
        "instructions": "What kind of event is it?",
        "criteria": NATURE,
    },
    "severity": {
        "type": "score",
        "instructions": "How severe is this for the operation of the cloud? Judge the effect "
        "on the cloud, not how unusual the window is: without failing requests or ERROR "
        "lines, a rate or template-mix change is at most low.",
        "criteria": list(SEVERITY_LEVELS),
        "examples": SEVERITY_EXAMPLES,
    },
}
QUESTION_TITLES = {
    "actionable": "Actionable",
    "user_impact": "User impact",
    "nature": "Nature",
    "severity": "Severity",
}


def spec_hash(context: str = CONTEXT, questions: Mapping[str, Any] = QUESTIONS) -> str:
    """Short hash of the context and questions (shown in the drawer, part of the version)."""
    text = json.dumps({"context": context, "questions": questions}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:10]


SPEC_HASH = spec_hash()

# -------------------------------------------------------------------------- subsystems

UNKNOWN = "unknown"
SUBSYSTEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("network", ("neutron-*", "openvswitch")),
    ("compute", ("nova-*", "libvirt", "placement-*")),
    ("storage", ("cinder-*", "glance-*")),
    ("identity_secrets", ("keystone*", "barbican*")),
    ("orchestration", ("heat-*", "octavia-*")),
    ("control_plane", ("haproxy", "mariadb*", "horizon-*")),
    ("observability", ("fluent",)),
)
SUBSYSTEM_NAMES = (*(name for name, _ in SUBSYSTEMS), UNKNOWN)

# Rows judged in this browser session: {row_key: {"severity": expected level, "subsystem": ...}}
Session = dict[str, dict[str, Any]]


def subsystem(program: str | None) -> str:
    """The subsystem of a program (service) by name prefix; ``unknown`` for null or no match."""
    if not program:
        return UNKNOWN
    for name, patterns in SUBSYSTEMS:
        for pattern in patterns:
            if program.startswith(pattern[:-1]) if pattern.endswith("*") else program == pattern:
                return name
    return UNKNOWN


# ------------------------------------------------------------------------------- rows


def row_key(
    detector: str, kind: str, entity: str, bin_: datetime, window_s: int, template_id: int | None
) -> str:
    """Stable key of one anomaly row (the anomaly schema has none)."""
    text = "|".join(str(v) for v in (detector, kind, entity, bin_, window_s, template_id))
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def with_keys(frame: pl.DataFrame) -> pl.DataFrame:
    """The anomaly rows with ``row_key`` and the subsystem known without a query."""
    keys = [
        row_key(*r)
        for r in frame.select(
            "detector", "kind", "entity", "bin", "window_s", "template_id"
        ).iter_rows()
    ]
    subsystems = [
        subsystem(e) if k == "service" else UNKNOWN
        for k, e in frame.select("kind", "entity").iter_rows()
    ]
    return frame.with_columns(
        pl.Series("row_key", keys, pl.String), pl.Series("subsystem", subsystems, pl.String)
    )


_ROWS: Lru[pl.DataFrame] = Lru(2)
_TEMPLATES: Lru[dict[int, str]] = Lru(2)


def clear_caches() -> None:
    _ROWS.items.clear()
    _TEMPLATES.items.clear()


def load_rows(store: CacheStore) -> pl.DataFrame:
    """The anomaly table with keys and subsystems, kept until the file changes."""
    path = store.path(anomalies.KEY, anomalies.FILE)
    key = (str(path), path.stat().st_mtime_ns)
    return _ROWS.get(key, lambda: with_keys(anomalies.load_anomalies(store)))


def load_templates(store: CacheStore) -> dict[int, str]:
    """Masked template text by id (empty when the templates stage is not built)."""
    path = store.path("templates", "templates.parquet")
    if not path.exists():
        return {}
    key = (str(path), path.stat().st_mtime_ns)

    def read() -> dict[int, str]:
        frame = pl.read_parquet(path, columns=["template_id", "template"])
        return dict(zip(frame["template_id"], frame["template"], strict=True))

    return _TEMPLATES.get(key, read)


def minutes(window_s: int) -> float:
    return round(window_s / 60, 1)


def window_text(bin_: datetime, window_s: int, kind: str) -> str:
    if kind == "request":
        return f"{anomalies.stamp(bin_)} UTC (request)"
    return f"{anomalies.stamp(bin_)} UTC, {minutes(window_s):g} min"


def grid_rows(rows: pl.DataFrame, session: Session | None = None) -> list[dict]:
    """Row data of the grid: display columns, the header texts of the drawer and the link.

    A row judged in this session (``session``) shows its severity and the subsystem found on
    click.
    """
    session = session or {}
    out = []
    for r in rows.iter_rows(named=True):
        detector = DETECTORS.get(r["detector"]).title if r["detector"] in DETECTORS else ""
        judged = session.get(r["row_key"], {})
        out.append(
            {
                "row_key": r["row_key"],
                "window": anomalies.stamp(r["bin"]),
                "type": r["type"],
                "kind": r["kind"],
                "entity": r["entity"],
                "subsystem": judged.get("subsystem") or r["subsystem"],
                "score": round(r["score"], 3),
                "judged": judged.get("severity"),
                "evidence_short": short(r["evidence"], TEXT_CHARS),
                "evidence": r["evidence"],
                "head": f"{r['type']} · {detector or r['detector']} · {r['kind']}",
                "when": window_text(r["bin"], r["window_s"], r["kind"]),
                "score_text": f"detector score {r['score']:.2f}",
                "href": anomalies.explorer_url(
                    r["bin"], r["window_s"], r["kind"], r["entity"], r["template_id"]
                ),
            }
        )
    return out


def find_row(rows: pl.DataFrame, key: str) -> dict[str, Any] | None:
    found = rows.filter(pl.col("row_key") == key)
    return found.row(0, named=True) if found.height else None


# ----------------------------------------------------------------------------- state


@dataclass(frozen=True)
class Facts:
    """What the click-time query found: line counts per level and the dominant program."""

    levels: dict[str, int]
    program: str | None


def facts_query(kind: str, entity: str, bin_: datetime, window_s: int) -> tuple[str, list[Any]]:
    """``(sql, params)`` for the level and program counts of an entity in its window.

    A request's lines are found by its id over the whole day (they may fall outside the window,
    and the query is fast); the other kinds are restricted to the window and its hours.
    """
    parts: list[str] = []
    params: list[Any] = []
    if kind == "request":
        parts.append("request_id = ?")
        params.append(entity)
    else:
        end = bin_ + timedelta(seconds=window_s)
        parts += ["ts >= ?", "ts < ?"]
        params += [bin_, end]
        hours = hour_values(bin_, end)
        if hours is not None:
            parts.append(f"hour IN ({', '.join('?' for _ in hours)})")
            params += hours
        column = {"service": "program", "host": "host"}.get(kind)
        if column:
            parts.append(f"{column} = ?")
            params.append(entity)
    sql = (
        "SELECT level, program, count(*) AS n FROM events WHERE "
        + " AND ".join(parts)
        + " GROUP BY level, program"
    )
    return sql, params


def window_facts(
    con: duckdb.DuckDBPyConnection, kind: str, entity: str, bin_: datetime, window_s: int
) -> Facts:
    sql, params = facts_query(kind, entity, bin_, window_s)
    levels: dict[str, int] = {}
    programs: dict[str, int] = {}
    for level, program, n in con.execute(sql, params).fetchall():
        name = level or "UNKNOWN"
        levels[name] = levels.get(name, 0) + int(n)
        if program:
            programs[program] = programs.get(program, 0) + int(n)
    ordered = {lv: levels[lv] for lv in (*LEVELS, *sorted(levels)) if lv in levels}
    dominant = max(sorted(programs), key=lambda p: programs[p]) if programs else None
    return Facts(ordered, dominant)


def row_subsystem(row: Mapping[str, Any], facts: Facts) -> str:
    """A service row's own subsystem; for the other kinds that of the dominant program."""
    return subsystem(row["entity"] if row["kind"] == "service" else facts.program)


TEMPLATE_REF = re.compile(r"\bT(\d+)\b")


def template_ids(template_id: int | None, evidence: str) -> list[int]:
    """The row's template and each ``T<n>`` named in the evidence, at most ``MAX_TEMPLATES``."""
    ids = [] if template_id is None else [int(template_id)]
    ids += [int(m) for m in TEMPLATE_REF.findall(evidence)]
    return list(dict.fromkeys(ids))[:MAX_TEMPLATES]


def build_state(
    row: Mapping[str, Any], facts: Facts, templates: Mapping[int, str]
) -> dict[str, Any]:
    """The item sent to the model: named fields, the evidence in full."""
    detector = row["detector"]
    return {
        "detector": detector,
        "detector_description": DETECTOR_DESCRIPTIONS.get(
            detector, TYPE_DESCRIPTIONS.get(row["type"], "")
        ),
        "type": row["type"],
        "kind": row["kind"],
        "entity": row["entity"],
        "subsystem": row_subsystem(row, facts),
        "window_start": f"{anomalies.stamp(row['bin'])} UTC",
        "window_minutes": minutes(row["window_s"]),
        "evidence": row["evidence"],
        "templates": {
            f"T{t}": templates[t]
            for t in template_ids(row["template_id"], row["evidence"])
            if t in templates
        },
        "levels": facts.levels,
    }


# --------------------------------------------------------------------- answer display


@dataclass(frozen=True)
class ViewOutput:
    """One output the clientside ``arrive`` callback sets from ``answer_view``."""

    id: str
    prop: str


def numeric_id(qid: str, part: str) -> str:
    return f"triage-{qid}-{part}"


def view_outputs() -> list[ViewOutput]:
    """The props the answers set, in the order of ``answer_view``'s ``view`` list."""
    out: list[ViewOutput] = []
    for qid, q in QUESTIONS.items():
        if q["type"] == "noul":
            out += [
                ViewOutput(f"triage-{qid}-bar", "value"),
                ViewOutput(f"triage-{qid}-bar", "color"),
            ]
        elif q["type"] == "choice":
            for option in q["criteria"]:
                base = f"triage-{qid}-{option}"
                out += [
                    ViewOutput(f"{base}-row", "style"),
                    ViewOutput(f"{base}-bar", "value"),
                    ViewOutput(f"{base}-bar", "color"),
                ]
        else:
            out += [
                ViewOutput("triage-needle", "style"),
                ViewOutput(f"triage-{qid}-label", "children"),
            ]
            for level in range(len(q["criteria"])):
                out.append(ViewOutput(f"triage-{qid}-{level}-bar", "value"))
        out += [
            ViewOutput(f"triage-{qid}-ring", "sections"),
            ViewOutput(f"triage-{qid}-check", "style"),
            ViewOutput(f"triage-{qid}-section", "style"),
        ]
    return out


VIEW_OUTPUTS = view_outputs()


def probabilities(answer: Mapping[str, Any]) -> dict[str, float]:
    """Option or level probabilities; a noul's are ``true`` = P(yes) and ``false``."""
    if answer["type"] == "noul":
        p = float(answer["value"])
        return {"true": p, "false": 1.0 - p}
    return {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}


def confidence(answer: Mapping[str, Any]) -> float:
    """The API's confidence; a noul has none, so its distance from a coin flip, ``|2p - 1|``."""
    if answer["type"] == "noul":
        return abs(2 * float(answer["value"]) - 1)
    if "confidence" in answer:
        return float(answer["confidence"])
    probs = probabilities(answer)
    return max(probs.values()) if probs else 0.0


def needs_check(answer: Mapping[str, Any]) -> bool:
    """Low confidence, or the top two probabilities within ``CHECK_MARGIN``."""
    top = sorted(probabilities(answer).values(), reverse=True)
    close = len(top) >= 2 and top[0] - top[1] <= CHECK_MARGIN
    return confidence(answer) < CHECK_CONFIDENCE or close


def needle_style(value: float, levels: int) -> dict[str, Any]:
    """The gauge needle at the expected level (0 = far left, the top level = far right)."""
    angle = -90 + 180 * min(max(value, 0.0), levels - 1) / (levels - 1)
    return {**NEEDLE_BASE, "transform": f"rotate({angle:.1f}deg)"}


def section_style(muted: bool) -> dict[str, Any]:
    return {"opacity": 0.5 if muted else 1, "transition": f"opacity {TRANSITION_MS}ms ease-out"}


def answer_view(answers: Mapping[str, Mapping[str, Any]]) -> tuple[list[Any], dict[str, Any]]:
    """Values for ``VIEW_OUTPUTS`` and the numbers to count up (``{id: [value, decimals]}``)."""
    view: list[Any] = []
    numbers: dict[str, list[float | int]] = {}
    for qid, q in QUESTIONS.items():
        answer = answers[qid]
        muted = needs_check(answer)
        accent = "gray" if muted else "blue"
        if q["type"] == "noul":
            p = float(answer["value"])
            view += [100 * p, accent]
            numbers[numeric_id(qid, "num")] = [round(p, 3), 2]
        elif q["type"] == "choice":
            probs = probabilities(answer)
            order = sorted(q["criteria"], key=lambda o: (-probs.get(o, 0.0), list(q["criteria"])))
            for option in q["criteria"]:
                p = probs.get(option, 0.0)
                chosen = option == answer["value"]
                view += [
                    {"order": order.index(option), "display": "flex"},
                    100 * p,
                    accent if chosen else "gray.4",
                ]
                numbers[numeric_id(qid, f"{option}-num")] = [round(p, 3), 2]
        else:
            levels = len(q["criteria"])
            value = float(answer["value"])
            nearest = min(levels - 1, max(0, math.floor(value + 0.5)))  # as the grid rounds
            view += [needle_style(value, levels), SEVERITY_NAMES[nearest]]
            probs = probabilities(answer)
            for level in range(levels):
                p = probs.get(str(level), 0.0)
                view.append(100 * p)
                numbers[numeric_id(qid, f"{level}-num")] = [round(p, 3), 2]
            numbers[numeric_id(qid, "num")] = [round(value, 3), 2]
        conf = confidence(answer)
        view += [
            [{"value": 100 * conf, "color": accent}],
            {"visibility": "visible" if muted else "hidden"},
            section_style(muted),
        ]
        numbers[numeric_id(qid, "conf")] = [round(conf, 3), 2]
    return view, numbers


# ------------------------------------------------------------------------------- judge


def judge(
    con: duckdb.DuckDBPyConnection,
    row: Mapping[str, Any],
    templates: Mapping[int, str],
    settings: Settings,
    key: str | None,
    poster: jev.Poster = jev.http_post,
) -> dict[str, Any]:
    """Build the state of one row and ask Jev; a failure is an ``error``, never a default.

    The result keeps the exact ``request`` body and the raw ``response`` (None when no answer
    came back), shown by "show raw prompt".
    """
    t0 = time.perf_counter()
    result: dict[str, Any] = {"key": row["row_key"], "state_ms": None, "jev_ms": None}
    try:
        facts = window_facts(con, row["kind"], row["entity"], row["bin"], row["window_s"])
    except duckdb.Error as error:
        return {**result, "error": f"the state query failed: {error}"}
    state = build_state(row, facts, templates)
    body = jev.request_body(settings.jev_model, CONTEXT, QUESTIONS, state)
    result |= {
        "state_ms": (time.perf_counter() - t0) * 1000,
        "subsystem": state["subsystem"],
        "request": body,
        "response": None,
    }
    if key is None:
        return {**result, "error": f"{jev.KEY_ENV} is not set, so the model cannot be asked."}
    t1 = time.perf_counter()
    try:
        result["response"] = jev.post_with_retry(poster, key, body, settings.jev_timeout_s)
        decision = jev.parse_decision(result["response"], QUESTIONS, settings.jev_model)
    except jev.JevError as error:
        return {**result, "jev_ms": (time.perf_counter() - t1) * 1000, "error": str(error)}
    view, numbers = answer_view(decision.answers)
    return {
        **result,
        "jev_ms": (time.perf_counter() - t1) * 1000,
        "error": None,
        "cost": decision.cost,
        "model": decision.model,
        "severity": round(float(decision.answers["severity"]["value"]), 2),
        "answers": decision.answers,
        "view": view,
        "numbers": numbers,
    }


# ------------------------------------------------------------------------------ figures


def scatter_figure(rows: pl.DataFrame, scheme: str | None) -> go.Figure:
    """Detector score against time, one trace per subsystem; a point's customdata is its key."""
    if rows.is_empty():
        return figures.empty_figure("no anomalies in the selection", scheme)
    fig = go.Figure()
    for i, name in enumerate(SUBSYSTEM_NAMES):
        part = rows.filter(pl.col("subsystem") == name)
        if part.is_empty():
            continue
        color = "#adb5bd" if name == UNKNOWN else figures.PALETTE[i % len(figures.PALETTE)]
        fig.add_trace(
            go.Scattergl(
                x=part["bin"].to_list(),
                y=part["score"].to_list(),
                customdata=part["row_key"].to_list(),
                mode="markers",
                name=name,
                marker={"size": 7, "color": color},
                text=anomalies.hover(part),
                hovertemplate="%{text}<extra></extra>",
            )
        )
    fig.update_yaxes(title="detector score", range=[-0.02, 1.02])
    figures.add_note(fig, "click a point to ask the model; unknown: subsystem found on click")
    return figures.style(fig, scheme, height=320)


def draw_triage(
    store: CacheStore,
    filters: Filters,
    types: list[str],
    sort: str,
    scheme: str | None,
    session: Session | None,
) -> tuple[list[dict], go.Figure, str]:
    problem = anomalies.unavailable(store)
    if problem is not None:
        return [], figures.empty_figure(problem, scheme), problem
    rows = anomalies.select(load_rows(store), filters, types)
    found = {k: v["subsystem"] for k, v in (session or {}).items() if v.get("subsystem")}
    if found:  # the subsystems found on click colour their points too
        rows = rows.with_columns(
            pl.col("row_key").replace_strict(found, default=pl.col("subsystem")).alias("subsystem")
        )
    ordered = rows if sort == "score" else anomalies.sort_rows(rows, sort)
    text = (
        f"{rows.height:,} anomalies in the selection, ordered by {sort}. The level filter does "
        "not apply here."
    )
    return grid_rows(ordered, session), scatter_figure(rows, scheme), text


# ------------------------------------------------------------------------------- layout

NEEDLE_BASE = {
    "position": "absolute",
    "left": "calc(50% - 1.5px)",
    "bottom": 0,
    "width": 3,
    "height": 92,
    "borderRadius": 2,
    "background": "var(--mantine-color-text)",
    "transformOrigin": "50% 100%",
    "transition": f"transform {TRANSITION_MS}ms ease-out",
}
DIM = {"opacity": 0.35, "transition": f"opacity {TRANSITION_MS}ms ease-out"}
SHOWN = {"opacity": 1, "transition": f"opacity {TRANSITION_MS}ms ease-out"}
HIDDEN = {"display": "none"}
SEVERITY_FORMAT = (
    "params.value == null ? '' : "
    f"{list(SEVERITY_NAMES)}[Math.min({len(SEVERITY_NAMES) - 1}, Math.max(0, "
    "Math.round(params.value)))] + ' ' + params.value.toFixed(1)"
)


def number(id_: str, value: str = "0.00", **style: Any) -> Component:
    """A number the clientside ``count`` callback counts up (Dash never re-renders it)."""
    return html.Span(value, id=id_, style={"fontVariantNumeric": "tabular-nums", **style})


def confidence_ring(qid: str) -> Component:
    return dmc.Stack(
        [
            dmc.RingProgress(
                id=f"triage-{qid}-ring",
                className="triage-ring",
                size=46,
                thickness=5,
                roundCaps=True,
                sections=[{"value": 0, "color": "blue"}],
                label=dmc.Text(number(f"triage-{qid}-conf"), size="9px", ta="center"),
            ),
            dmc.Badge(
                "check",
                id=f"triage-{qid}-check",
                color="orange",
                size="xs",
                variant="light",
                style={"visibility": "hidden"},
            ),
        ],
        gap=2,
        align="center",
    )


def section(qid: str, body: Component) -> Component:
    return html.Div(
        dmc.Group(
            [
                dmc.Stack([dmc.Text(QUESTION_TITLES[qid], fw=600, size="sm"), body], gap=4, flex=1),
                confidence_ring(qid),
            ],
            align="flex-start",
            wrap="nowrap",
        ),
        id=f"triage-{qid}-section",
        style=section_style(False),
    )


def progress(id_: str, color: str = "blue") -> Component:
    return dmc.Progress(id=id_, value=0, color=color, size="lg", transitionDuration=TRANSITION_MS)


def noul_block(qid: str) -> Component:
    bar = html.Div(
        [
            progress(f"triage-{qid}-bar"),
            html.Div(
                style={
                    "position": "absolute",
                    "left": "50%",
                    "top": -3,
                    "bottom": -3,
                    "width": 2,
                    "background": "var(--mantine-color-text)",
                    "opacity": 0.6,
                }
            ),
        ],
        style={"position": "relative", "flex": 1},
    )
    body = dmc.Stack(
        [
            dmc.Text(QUESTIONS[qid]["instructions"], size="xs", c="dimmed"),
            dmc.Group(
                [bar, dmc.Text(["P(yes) ", number(f"triage-{qid}-num")], size="xs", w=80)],
                wrap="nowrap",
                gap="xs",
            ),
        ],
        gap=4,
    )
    return section(qid, body)


def choice_block(qid: str) -> Component:
    rows = [
        html.Div(
            [
                dmc.Text(option, size="xs", w=86),
                html.Div(progress(f"triage-{qid}-{option}-bar", "gray.4"), style={"flex": 1}),
                dmc.Text(number(f"triage-{qid}-{option}-num"), size="xs", w=34, ta="right"),
            ],
            id=f"triage-{qid}-{option}-row",
            style={"order": i, "display": "flex"},
            className="triage-choice-row",
            title=text,
        )
        for i, (option, text) in enumerate(QUESTIONS[qid]["criteria"].items())
    ]
    body = html.Div(rows, style={"display": "flex", "flexDirection": "column", "gap": 4})
    return section(qid, body)


def gauge(qid: str) -> Component:
    """A semicircle from 0 to the top level with one coloured band per level and a needle.

    The needle is a plain element whose ``transform`` changes, so a CSS transition moves it
    (a Plotly indicator is redrawn instead of animated).
    """
    levels = len(QUESTIONS[qid]["criteria"])
    step = 180 / levels
    bands = ", ".join(
        f"var(--mantine-color-{c}-5) {i * step:g}deg {(i + 1) * step:g}deg"
        for i, c in enumerate(SEVERITY_COLORS[:levels])
    )
    dial = html.Div(
        [
            html.Div(
                style={
                    "width": 200,
                    "height": 200,
                    "borderRadius": "50%",
                    "background": f"conic-gradient(from 270deg, {bands}, transparent 180deg)",
                }
            ),
            html.Div(
                style={
                    "position": "absolute",
                    "left": 26,
                    "top": 26,
                    "width": 148,
                    "height": 148,
                    "borderRadius": "50%",
                    "background": "var(--mantine-color-body)",
                }
            ),
            html.Div(id="triage-needle", style=needle_style(0, levels)),
            html.Div(
                style={
                    "position": "absolute",
                    "left": "calc(50% - 6px)",
                    "bottom": -6,
                    "width": 12,
                    "height": 12,
                    "borderRadius": "50%",
                    "background": "var(--mantine-color-text)",
                }
            ),
        ],
        style={"position": "relative", "width": 200, "height": 100, "overflow": "hidden"},
    )
    return dmc.Stack(
        [
            dial,
            dmc.Text(
                [number(f"triage-{qid}-num"), " ", html.Span("–", id=f"triage-{qid}-label")],
                size="sm",
                fw=600,
            ),
        ],
        gap=4,
        align="center",
    )


def score_block(qid: str) -> Component:
    bars = [
        html.Div(
            dmc.Group(
                [
                    dmc.Text(f"{level} {SEVERITY_NAMES[level]}", size="xs", w=100),
                    html.Div(
                        progress(f"triage-{qid}-{level}-bar", SEVERITY_COLORS[level]),
                        style={"flex": 1},
                    ),
                    dmc.Text(number(f"triage-{qid}-{level}-num"), size="xs", w=34, ta="right"),
                ],
                wrap="nowrap",
                gap="xs",
            ),
            title=text,
        )
        for level, text in enumerate(QUESTIONS[qid]["criteria"])
    ]
    return section(qid, dmc.Stack([gauge(qid), *bars], gap=4))


BLOCKS = {"noul": noul_block, "choice": choice_block, "score": score_block}


def drawer(model: str) -> Component:
    header = dmc.Stack(
        [
            dmc.Text("", id="triage-h-head", size="xs", c="dimmed"),
            dmc.Group(
                [
                    dmc.Title("", id="triage-h-entity", order=4, style={"wordBreak": "break-all"}),
                    dmc.Badge(UNKNOWN, id="triage-h-subsystem", variant="light"),
                ],
                gap="xs",
            ),
            dmc.Text("", id="triage-h-when", size="xs"),
            dmc.Text("", id="triage-h-score", size="xs"),
            dmc.Text("", id="triage-h-evidence", size="xs", style={"wordBreak": "break-word"}),
            dmc.Group(
                [
                    dmc.Anchor(
                        "raw lines in the explorer",
                        id="triage-h-explorer",
                        href="/explorer",
                        size="xs",
                        refresh=False,
                    ),
                    dmc.Anchor("all anomalies", href="/anomalies", size="xs", refresh=False),
                    html.Span(
                        dmc.Text("show raw prompt", size="xs", style=LINK),
                        id="triage-raw-open",
                        n_clicks=0,
                    ),
                ],
                gap="md",
            ),  # fmt: skip
        ],
        gap=4,
    )
    status = dmc.Group(
        [
            dmc.Stack(
                [
                    dmc.Text("", id="triage-timer", size="sm", fw=500),
                    dmc.Text("", id="triage-split", size="xs", c="dimmed"),
                ],
                gap=0,
            ),
            dmc.Button("ask again", id="triage-again", size="compact-xs", variant="default"),
        ],
        justify="space-between",
    )
    answers = html.Div(
        dmc.Stack([BLOCKS[q["type"]](qid) for qid, q in QUESTIONS.items()], gap="md"),
        id="triage-answers",
        style=SHOWN,
    )
    return dmc.Drawer(
        dmc.Stack(
            [
                header,
                dmc.Divider(),
                status,
                dmc.Alert("", id="triage-error", color="red", title="No answer", style=HIDDEN),
                answers,
                dmc.Divider(),
                dmc.Text(f"model {model} · spec {SPEC_HASH}", size="xs", c="dimmed"),
            ],
            gap="sm",
        ),
        id="triage-drawer",
        position="right",
        size=460,
        opened=False,
        keepMounted=True,
        withOverlay=False,
        lockScroll=False,
        closeOnClickOutside=False,
        title=dmc.Text("Triage", fw=600),
    )


# A text that looks like the Anchors next to it but runs a callback instead of following a link.
LINK = {"color": "var(--mantine-color-anchor)", "cursor": "pointer"}

PRE = {
    "whiteSpace": "pre-wrap",
    "wordBreak": "break-word",
    "fontSize": 12,
    "maxHeight": 320,
    "overflow": "auto",
    "margin": 0,
    "padding": 8,
    "borderRadius": 4,
    "background": "var(--mantine-color-default-hover)",
}


def raw_modal() -> Component:
    """What was sent to and returned by Jev for the selected row, filled clientside."""
    block = lambda title, id_: dmc.Stack(  # noqa: E731
        [dmc.Text(title, fw=600, size="sm"), html.Pre("", id=id_, style=PRE)], gap=4
    )
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.Text("", id="triage-raw-note", size="xs", c="dimmed"),
                block("Context (shared by every item)", "triage-raw-context"),
                block("Prompt (model, item and questions; the context is shown above)",
                      "triage-raw-prompt"),
                block("Output (the response as returned)", "triage-raw-output"),
            ],
            gap="sm",
        ),
        id="triage-raw",
        title=dmc.Text("Raw prompt", fw=600),
        size="xl",
        opened=False,
        zIndex=400,  # above the drawer
    )  # fmt: skip


def grid() -> Component:
    severity_styles = [
        {
            "condition": f"params.value != null && Math.round(params.value) == {level}",
            "style": {
                "backgroundColor": f"var(--mantine-color-{color}-light)",
                "fontWeight": 600,
            },
        }
        for level, color in enumerate(SEVERITY_COLORS)
    ]
    return dag.AgGrid(
        id="triage-grid",
        rowData=[],
        getRowId="params.data.row_key",
        columnDefs=[
            {"field": "window", "width": 165},
            {"field": "type", "width": 105},
            {"field": "kind", "width": 85},
            {"field": "entity", "width": 190, "tooltipField": "entity"},
            {"field": "subsystem", "width": 135},
            {"field": "score", "width": 85},
            {
                "field": "judged",
                "headerName": "severity",
                "width": 120,
                "valueFormatter": {"function": SEVERITY_FORMAT},
                "cellStyle": {"styleConditions": severity_styles},
            },
            {
                "field": "evidence_short",
                "headerName": "evidence",
                "flex": 1,
                "minWidth": 400,
                "tooltipField": "evidence",
            },
        ],
        defaultColDef={"resizable": True, "sortable": False},
        dashGridOptions={"rowSelection": {"mode": "singleRow", "checkboxes": False,
                                          "enableClickSelection": True},
                         "tooltipShowDelay": 300},
        style={"height": 520},
    )  # fmt: skip


def answer_outputs() -> list[Output]:
    """Outputs of the clientside ``arrive`` callback: fixed ones, then ``VIEW_OUTPUTS``."""
    fixed = [
        Output("triage-tick", "disabled", allow_duplicate=True),
        Output("triage-timer", "children", allow_duplicate=True),
        Output("triage-split", "children"),
        Output("triage-error", "style", allow_duplicate=True),
        Output("triage-error", "children", allow_duplicate=True),
        Output("triage-answers", "style", allow_duplicate=True),
        Output("triage-h-subsystem", "children", allow_duplicate=True),
        Output("triage-session", "data"),
        Output("triage-numbers", "data"),
    ]
    return fixed + [Output(o.id, o.prop) for o in VIEW_OUTPUTS]


FIXED_OUTPUTS = 9


def sort_value(value: str | None) -> str:
    return anomalies.sort_value(value)


def session_values(value: Any) -> Session:
    """The well-formed entries of the session store (``{row_key: {severity, subsystem}}``)."""
    if not isinstance(value, dict):
        return {}
    out: Session = {}
    for key, entry in value.items():
        if isinstance(entry, dict) and isinstance(entry.get("severity"), int | float):
            sub = entry.get("subsystem")
            out[str(key)] = {
                "severity": float(entry["severity"]),
                "subsystem": sub if sub in SUBSYSTEM_NAMES else None,
            }
    return out


@register
class Triage:
    key = KEY
    title = "Triage"
    version = f"1:{SPEC_HASH}"
    depends_on: tuple[str, ...] = ("anomalies", "templates")

    def __init__(
        self, poster: jev.Poster | None = None, env: Mapping[str, str] | None = None
    ) -> None:
        self.poster = jev.http_post if poster is None else poster
        self.env = env  # None: the process environment, read at each call

    def compute(self, ctx: ComputeContext) -> None:
        # Nothing to precompute: the model is asked live. The version carries the spec hash.
        ctx.note_rows("questions", len(QUESTIONS))

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        model = store.settings.jev_model
        banner: list[Component] = []
        if jev.api_key(self.env) is None:
            banner.append(
                dmc.Alert(
                    f"{jev.KEY_ENV} is not set: the list works, but the model cannot be asked. "
                    "Set it and restart `logdash serve`.",
                    id="triage-banner",
                    color="yellow",
                    title="No API key",
                )
            )
        controls = dmc.Group(
            [
                dmc.MultiSelect(
                    id="triage-types",
                    label="Type (empty = all)",
                    data=anomalies.load_types(store),
                    value=[],
                    size="xs",
                    w=360,
                    clearable=True,
                ),
                dmc.Stack(
                    [
                        dmc.Text("Sort table by", size="xs", fw=500),
                        segmented(
                            "triage-sort", [(s, s) for s in anomalies.SORTS], anomalies.DEFAULT_SORT
                        ),
                    ],
                    gap=4,
                ),
            ],
            align="flex-end",
            gap="lg",
        )
        return ui.page(
            "Anomaly triage",
            *banner,
            controls,
            ui.card("Detector score over time, by subsystem", ui.graph("triage-scatter", 320)),
            ui.card("Anomalies (click a row to ask the model)", grid()),
            dmc.Text(id="triage-hint", size="sm", c="dimmed"),
            drawer(model),
            dcc.Store(id="triage-selected"),
            dcc.Store(id="triage-result"),
            dcc.Store(id="triage-numbers"),
            dcc.Store(id="triage-counted"),
            dcc.Store(id="triage-session", storage_type="session", data={}),
            dcc.Store(
                id="triage-config",
                data={"model": model.split("/")[-1], "view_n": len(VIEW_OUTPUTS)},
            ),
            dcc.Interval(id="triage-tick", interval=TICK_MS, disabled=True),
            raw_modal(),
            description=(
                "The detectors say how unusual a window is, not whether anyone must act. A "
                f"click on a row or a point sends one request to {model} with the row's "
                "detector, entity, subsystem, window, evidence, template texts and level counts "
                "(sent to OpenRouter and TypeSafe as they are). The drawer shows the delay and "
                "the answers: actionable and user impact as P(yes), the nature of the event and "
                "the severity from 0 to 4, each with its confidence. Nothing is cached, so each "
                "click is a new call."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        state: dict[str, duckdb.DuckDBPyConnection] = {}

        def connection() -> duckdb.DuckDBPyConnection:
            if "con" not in state:
                state["con"] = store.connect()
            return state["con"].cursor()

        @app.callback(
            Output("triage-grid", "rowData"),
            Output("triage-scatter", "figure"),
            Output("triage-hint", "children"),
            Input("triage-types", "value"),
            Input("triage-sort", "value"),
            Input(FILTER_STORE_ID, "data"),
            Input(THEME_STORE_ID, "data"),
            State("triage-session", "data"),
        )
        def draw(
            types: list[str] | None,
            sort: str | None,
            filters: dict[str, Any] | None,
            theme: str | None,
            session: dict[str, Any] | None,
        ) -> tuple[list[dict], go.Figure, str]:
            return draw_triage(
                store,
                from_dict(filters),
                anomalies.type_values(types),
                sort_value(sort),
                theme,
                session_values(session),
            )

        @app.callback(Output("triage-result", "data"), Input("triage-selected", "data"))
        def ask(selected: dict[str, Any] | None) -> dict[str, Any] | None:
            if not isinstance(selected, dict) or not isinstance(selected.get("key"), str):
                return None
            n = selected.get("n")
            if anomalies.unavailable(store) is not None:
                return {"key": selected["key"], "n": n, "error": "anomalies are not built"}
            row = find_row(load_rows(store), selected["key"])
            if row is None:
                return {"key": selected["key"], "n": n, "error": "unknown anomaly row"}
            result = judge(
                connection(),
                row,
                load_templates(store),
                store.settings,
                jev.api_key(self.env),
                self.poster,
            )
            return {**result, "n": n}

        app.clientside_callback(
            ClientsideFunction("triage", "select"),
            Output("triage-selected", "data"),
            Input("triage-grid", "cellClicked"),
            Input("triage-scatter", "clickData"),
            Input("triage-again", "n_clicks"),
            State("triage-selected", "data"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            ClientsideFunction("triage", "open"),
            Output("triage-drawer", "opened"),
            Output("triage-h-head", "children"),
            Output("triage-h-entity", "children"),
            Output("triage-h-subsystem", "children"),
            Output("triage-h-when", "children"),
            Output("triage-h-score", "children"),
            Output("triage-h-evidence", "children"),
            Output("triage-h-explorer", "href"),
            Output("triage-tick", "disabled"),
            Output("triage-timer", "children"),
            Output("triage-split", "children", allow_duplicate=True),
            Output("triage-error", "style"),
            Output("triage-error", "children"),
            Output("triage-answers", "style"),
            Input("triage-selected", "data"),
            State("triage-grid", "rowData"),
            State("triage-config", "data"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            ClientsideFunction("triage", "tick"),
            Output("triage-timer", "children", allow_duplicate=True),
            Input("triage-tick", "n_intervals"),
            State("triage-config", "data"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            ClientsideFunction("triage", "arrive"),
            *answer_outputs(),
            Input("triage-result", "data"),
            State("triage-selected", "data"),
            State("triage-session", "data"),
            State("triage-config", "data"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            ClientsideFunction("triage", "count"),
            Output("triage-counted", "data"),
            Input("triage-numbers", "data"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            ClientsideFunction("triage", "judged"),
            Output("triage-grid", "rowTransaction"),
            Input("triage-session", "data"),
            State("triage-grid", "rowData"),
            prevent_initial_call=True,
        )
        app.clientside_callback(
            ClientsideFunction("triage", "raw"),
            Output("triage-raw", "opened"),
            Output("triage-raw-note", "children"),
            Output("triage-raw-context", "children"),
            Output("triage-raw-prompt", "children"),
            Output("triage-raw-output", "children"),
            Input("triage-raw-open", "n_clicks"),
            State("triage-result", "data"),
            State("triage-selected", "data"),
            prevent_initial_call=True,
        )

        try:  # load the rows now so that the first request does not read the file
            if anomalies.unavailable(store) is None:
                load_rows(store)
        except (OSError, ValueError):
            pass
