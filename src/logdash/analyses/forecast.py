"""Module 8: the forecasting lab's data: a spike label and input features per host and minute.

Nothing is trained here. The stage stores what a spike forecast learns from, and the page shows
what it found. The design is ADR003 (``adrs/ADR003-spike-forecasting-lab.md``).

Stored files (``cache/forecast/``):

- ``frame.parquet``: one row per host and minute of the whole span (empty minutes are zeros, not
  missing), ordered by host and then by minute, so a host's series is one contiguous block:
  ``bin, host, spike`` and one Float32 column per feature. Hosts are those of
  ``temporal/host_60.parquet`` except ``(missing)`` (malformed rows). Columns:
  ``tpl_<template_id>`` (``log1p`` of the host's lines of one of the ``TOP_TEMPLATES`` templates
  per minute), ``lvl_<level>`` (the share of each level in ``LEVELS``, 0 in an empty minute),
  ``svc_<service>`` (``log1p`` of the lines of one of the ``TOP_SERVICES`` busiest services),
  ``peer_rate`` (``log1p`` of the mean rate of the host's peers, or the fleet median of the
  minute for a host without a valid peer group), ``lag_rate`` (``log1p`` of the host's own rate)
  and ``lag_z`` (its trailing robust z: no future minute enters it);
- ``spike_counts.parquet``: ``bin, hosts``, the number of hosts in a spike minute, per minute;
- ``meta.json``: the span, hosts and minutes, the column names of each group, the templates and
  services behind the columns, the episode count, the hosts without a peer group, the split and
  the positive rate per horizon and period.

Definitions shared with the later stories of the lab (import them from here):

- *spike minute*: a minute inside an episode of ``temporal.find_bursts(x, burst_k)`` on the
  host's dense 1-minute series. These are the volume detector's host episodes before
  ``merge_periodic``;
- *target* for horizon ``H`` (``targets``): ``y(t)`` is true when any minute in ``(t, t+H]`` is a
  spike minute;
- *split* (``split_bounds``, ``period_index``): train is the first 7/12 of the span, validation
  the next 1/12 and test the last 1/3. A sample whose target window reaches into the next period
  is dropped (``sample_mask``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import dash_mantine_components as dmc
import diskcache
import duckdb
import numpy as np
import plotly.graph_objects as go
import polars as pl
from dash import Dash, DiskcacheManager, Input, Output, State, ctx, dcc, html, no_update
from dash.development.base_component import Component

from logdash.analyses import templates as templates_mod
from logdash.analyses import temporal
from logdash.analyses.base import ComputeContext, register
from logdash.analyses.temporal import MISSING, find_bursts, rolling_baseline, span
from logdash.app import figures, ui
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore
from logdash.forecast import metrics as forecast_metrics
from logdash.forecast import params as forecast_params
from logdash.forecast import runs
from logdash.ingest.normalize import LEVELS

KEY = "forecast"
STEP_S = 60
TOP_TEMPLATES = 20
TOP_SERVICES = 20
HORIZONS = (1, 5, 15)  # minutes, shown on the page
GROUPS = ("templates", "levels", "services", "peers", "lags")  # checkbox values of later stories
PERIODS = ("train", "validation", "test")
TEMPLATE_TEXT = 80  # characters of a template's text kept in meta.json
PERIOD_COLORS = {
    "train": figures.PALETTE[0],
    "validation": figures.PALETTE[3],
    "test": figures.PALETTE[1],
}
HOST_MIX_SQL = f"""
    SELECT time_bucket(INTERVAL '1 minute', ts) AS bin, coalesce(host, '{MISSING}') AS host,
           level, coalesce(program, '{MISSING}') AS program, count(*)::BIGINT AS n
    FROM events GROUP BY ALL"""

DESCRIPTION = (
    "What a spike forecast learns from: the spike label and the input features of every host and "
    "minute. A spike minute lies inside a volume spike of its host (the volume detector's "
    "episodes, before periodic repeats are merged). The forecasting lab always uses every host and "
    "the whole span: the global filters do not apply on this page. Run scores forecasts on the "
    "test period in a background job."
)
RATE_NOTE = (
    "Share of samples whose next H minutes contain a spike minute of the host. Samples whose "
    "target window reaches into the next period are dropped."
)
HORIZON_ID = "forecast-horizon"
RUN_BUTTON_ID = "forecast-run"
CANCEL_BUTTON_ID = "forecast-cancel"
PROGRESS_ID = "forecast-progress"
PROGRESS_LABEL_ID = "forecast-progress-label"
MESSAGE_ID = "forecast-message"
RUN_STORE_ID = "forecast-run-id"
THRESHOLD_ID = "forecast-threshold"
RESULTS_ID = "forecast-results"
DEFAULT_THRESHOLD = 0.5
POLL_MS = 500  # how often the page asks for the job's progress
MODEL_LABELS = {"persistence": "Persistence (repeat the last value)"}  # later stories add theirs
RESULT_COLUMNS = ("model", "accuracy", "F1", "AUROC", "PR-AUC", "positive rate")
NA_NO_SPIKES = "n/a (no spikes in the test period)"
NA_NO_SAMPLES = "n/a (no test samples)"
NA_NO_NEGATIVES = "n/a (no spike-free samples in the test period)"
RUN_NOTE = (
    "A run predicts, for every host and every minute of the test period, whether a spike minute "
    "follows within the horizon. The persistence baseline repeats the last value: it predicts a "
    "spike when the host had a spike minute in the last H minutes. The run executes in the "
    "background; the rest of the dashboard keeps answering."
)
ACCURACY_NOTE = (
    "About 1 % of the samples have a spike ahead, so a model that never predicts a spike already "
    "reaches an accuracy near 0.99: accuracy is misleading here. PR-AUC and F1 are the numbers "
    "to read. The threshold applies to F1 and accuracy only."
)
NO_RUN_TEXT = "No run yet. Pick a horizon and press Run."
MISSING_RUN_TEXT = "Run {run_id} is no longer in the runs directory. Press Run to start a new one."
STARTED_TEXT = "Run started."
CANCELLED_TEXT = "The run was cancelled. No results were saved."

# --------------------------------------------------------------------------- compute


@dataclass(frozen=True)
class Sources:
    """Upstream tables the stage reads (one place, so tests can hand-make them)."""

    host_60: pl.DataFrame  # temporal/host_60.parquet: bin, host, n
    rate_bins: pl.Series  # temporal/rate_60.parquet["bin"] (for the span, as the volume detector)
    template_counts: pl.DataFrame  # templates/template_counts_host.parquet
    template_texts: pl.DataFrame  # templates/templates.parquet: template_id, template
    host_mix: pl.DataFrame  # HOST_MIX_SQL over events: bin, host, level, program, n
    peers: pl.DataFrame  # hosts/peers.parquet: host, peer, distance


def load_sources(con: duckdb.DuckDBPyConnection, store: CacheStore) -> Sources:
    return Sources(
        host_60=store.read_parquet(temporal.KEY, "host_60.parquet"),
        rate_bins=store.read_parquet(temporal.KEY, "rate_60.parquet")["bin"],
        template_counts=store.read_parquet(templates_mod.KEY, "template_counts_host.parquet"),
        template_texts=store.read_parquet(templates_mod.KEY, "templates.parquet"),
        host_mix=con.execute(HOST_MIX_SQL).pl(),
        peers=store.read_parquet("hosts", "peers.parquet"),
    )


def naive_bins(df: pl.DataFrame) -> pl.DataFrame:
    """``df`` with ``bin`` as naive UTC ``Datetime("us")`` (guards a time-zone aware source)."""
    dtype = df.schema["bin"]
    if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None:
        df = df.with_columns(pl.col("bin").dt.convert_time_zone("UTC").dt.replace_time_zone(None))
    return df.with_columns(pl.col("bin").cast(pl.Datetime("us")))


def span_minutes(sources: Sources) -> tuple[datetime | None, int]:
    """First minute and number of minutes, exactly as the volume detector takes them."""
    if sources.host_60.is_empty():
        return None, 0
    t0, t1 = span(pl.concat([sources.rate_bins, sources.host_60["bin"]]))
    return t0, int((t1 - t0).total_seconds() // STEP_S) + 1


def frame_hosts(host_60: pl.DataFrame) -> list[str]:
    """Sorted hosts of the frame: every host but the malformed-row placeholder."""
    return sorted(h for h in host_60["host"].unique().to_list() if h != MISSING)


def dense_cube(
    df: pl.DataFrame,
    key: str | None,
    keys: Sequence[Any],
    hosts: Sequence[str],
    t0: datetime,
    minutes: int,
) -> np.ndarray:
    """``df["n"]`` summed into a ``(keys, hosts, minutes)`` array (zeros where nothing is).

    With ``key=None`` the result has one key and every row counts. Rows whose key or host is
    not listed, or whose minute is outside the span, are left out.
    """
    n_keys = 1 if key is None else len(keys)
    cube = np.zeros((n_keys, len(hosts), minutes))
    if df.is_empty() or not len(keys) or not len(hosts):
        return cube
    host_map = {h: i for i, h in enumerate(hosts)}
    cols = [
        df["host"].replace_strict(host_map, default=None, return_dtype=pl.Int64).alias("h"),
        ((df["bin"] - t0).dt.total_seconds() // STEP_S).cast(pl.Int64).alias("m"),
        df["n"].cast(pl.Float64).alias("n"),
    ]
    if key is None:
        cols.append(pl.Series("k", np.zeros(df.height, dtype=np.int64)))
    else:
        key_map = {k: i for i, k in enumerate(keys)}
        cols.append(df[key].replace_strict(key_map, default=None, return_dtype=pl.Int64).alias("k"))
    idx = pl.DataFrame(cols).drop_nulls().filter((pl.col("m") >= 0) & (pl.col("m") < minutes))
    np.add.at(
        cube, (idx["k"].to_numpy(), idx["h"].to_numpy(), idx["m"].to_numpy()), idx["n"].to_numpy()
    )
    return cube


def spike_mask(x: np.ndarray, burst_k: float) -> tuple[np.ndarray, int]:
    """Minutes inside an episode of ``find_bursts(x, burst_k)`` and the number of episodes."""
    mask = np.zeros(len(x), dtype=bool)
    found = find_bursts(x, burst_k)
    for f in found:
        mask[f["first"] : f["last"] + 1] = True
    return mask, len(found)


def lag_features(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``log1p`` of the rate and its trailing robust z ``(x - median) / scale``.

    The baseline window ends at the minute, so no future minute enters; ``scale >= 1``, so
    there is no division by zero.
    """
    median, scale = rolling_baseline(x, trailing=True)
    return np.log1p(x), (x - median) / scale


def peer_rates(
    rates: np.ndarray, hosts: Sequence[str], peers: pl.DataFrame
) -> tuple[np.ndarray, list[str]]:
    """Mean rate of each host's peers per minute, from ``rates`` of shape ``(hosts, minutes)``.

    A host with no peer in the frame (not in ``peers``, or none of its peers present) gets the
    median over all frame hosts of the minute. Returns the rates (not logged) and the sorted
    hosts that got the fleet median.
    """
    index = {h: i for i, h in enumerate(hosts)}
    fleet = np.median(rates, axis=0) if len(hosts) else np.zeros(rates.shape[1])
    lists: dict[str, list[int]] = {}
    for host, peer in peers.select("host", "peer").iter_rows():
        if host in index and peer in index:
            lists.setdefault(host, []).append(index[peer])
    out = np.zeros_like(rates, dtype=float)
    no_peer: list[str] = []
    for host, i in index.items():
        if lists.get(host):
            out[i] = rates[lists[host]].mean(axis=0)
        else:
            out[i] = fleet
            no_peer.append(host)
    return out, sorted(no_peer)


def top_templates(
    counts: pl.DataFrame, texts: pl.DataFrame, n: int = TOP_TEMPLATES
) -> pl.DataFrame:
    """``template_id, lines, text`` of the ``n`` templates with most lines (not ``(missing)``)."""
    ranked = (
        counts.filter(pl.col("host") != MISSING)
        .group_by("template_id")
        .agg(pl.col("n").sum().alias("lines"))
        .sort(["lines", "template_id"], descending=[True, False])
        .head(n)
    )
    names = dict(texts.select("template_id", "template").iter_rows())
    text = [templates_mod.short(names.get(i, ""), TEMPLATE_TEXT) for i in ranked["template_id"]]
    return ranked.with_columns(pl.Series("text", text, dtype=pl.String))


def top_services(host_mix: pl.DataFrame, n: int = TOP_SERVICES) -> pl.DataFrame:
    """``service, lines`` of the ``n`` busiest programs (not ``(missing)`` hosts or programs)."""
    return (
        host_mix.filter((pl.col("host") != MISSING) & (pl.col("program") != MISSING))
        .group_by("program")
        .agg(pl.col("n").sum().alias("lines"))
        .rename({"program": "service"})
        .sort(["lines", "service"], descending=[True, False])
        .head(n)
    )


def split_bounds(minutes: int) -> tuple[int, int]:
    """End (exclusive) minute index of train and of validation: 7/12 and 8/12 of the span."""
    return minutes * 7 // 12, minutes * 8 // 12


def period_index(minutes: int) -> np.ndarray:
    """0 (train), 1 (validation) or 2 (test) for every minute index."""
    a, b = split_bounds(minutes)
    out = np.zeros(minutes, dtype=np.int64)
    out[a:b] = 1
    out[b:] = 2
    return out


def targets(spike: np.ndarray, horizon: int) -> np.ndarray:
    """``y[..., t] = any(spike[..., t+1 : t+1+horizon])``; False where the window passes the end."""
    n = spike.shape[-1]
    zero = np.zeros((*spike.shape[:-1], 1), dtype=np.int64)
    cum = np.concatenate([zero, np.cumsum(spike, axis=-1, dtype=np.int64)], axis=-1)
    y = np.zeros(spike.shape, dtype=bool)
    if horizon < n:
        y[..., : n - horizon] = (cum[..., 1 + horizon : n + 1] - cum[..., 1 : n - horizon + 1]) > 0
    return y


def sample_mask(minutes: int, horizon: int) -> np.ndarray:
    """True where ``t + horizon`` is inside the span and in the same period as ``t``."""
    period = period_index(minutes)
    ok = np.zeros(minutes, dtype=bool)
    if horizon < minutes:
        ok[: minutes - horizon] = period[: minutes - horizon] == period[horizon:]
    return ok


def positive_rates(
    spike: np.ndarray, horizons: Sequence[int] = HORIZONS
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{str(h): {period: {"samples", "positives", "rate"}}}`` over all hosts (rate or None)."""
    hosts, minutes = spike.shape
    period = period_index(minutes)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for h in horizons:
        y = targets(spike, h)
        keep = sample_mask(minutes, h)
        cells: dict[str, dict[str, Any]] = {}
        for p, name in enumerate(PERIODS):
            sel = keep & (period == p)
            samples = int(sel.sum()) * hosts
            positives = int(y[:, sel].sum())
            cells[name] = {
                "samples": samples,
                "positives": positives,
                "rate": positives / samples if samples else None,
            }
        out[str(h)] = cells
    return out


def empty_meta(burst_k: float) -> dict[str, Any]:
    return {
        "start": None,
        "end": None,
        "minutes": 0,
        "hosts": 0,
        "host_minutes": 0,
        "burst_k": burst_k,
        "groups": {g: [] for g in GROUPS},
        "templates": [],
        "services": [],
        "episodes": 0,
        "spike_minutes": 0,
        "spike_hosts": 0,
        "no_peer_hosts": [],
        "split": {"train_end": 0, "validation_end": 0, "periods": {}},
        "horizons": list(HORIZONS),
        "positive_rate": positive_rates(np.zeros((0, 0), dtype=bool)),
    }


def _empty_outputs(burst_k: float) -> tuple[pl.DataFrame, dict[str, Any], pl.DataFrame]:
    frame = pl.DataFrame(schema={"bin": pl.Datetime("us"), "host": pl.String, "spike": pl.Boolean})
    counts = pl.DataFrame(schema={"bin": pl.Datetime("us"), "hosts": pl.Int64})
    return frame, empty_meta(burst_k), counts


def build(sources: Sources, burst_k: float) -> tuple[pl.DataFrame, dict[str, Any], pl.DataFrame]:
    """The frame, its meta and the per-minute spike counts (see the module docstring)."""
    sources = Sources(
        naive_bins(sources.host_60),
        sources.rate_bins.cast(pl.Datetime("us")),
        naive_bins(sources.template_counts),
        sources.template_texts,
        naive_bins(sources.host_mix),
        sources.peers,
    )
    t0, minutes = span_minutes(sources)
    hosts = frame_hosts(sources.host_60)
    if t0 is None or minutes == 0 or not hosts:
        return _empty_outputs(burst_k)
    h = len(hosts)

    rates = dense_cube(sources.host_60, None, [None], hosts, t0, minutes)[0]
    spike = np.zeros((h, minutes), dtype=bool)
    episodes = 0
    for i in range(h):
        spike[i], found = spike_mask(rates[i], burst_k)
        episodes += found

    features: dict[str, np.ndarray] = {}
    groups: dict[str, list[str]] = {g: [] for g in GROUPS}

    def add(group: str, name: str, values: np.ndarray) -> None:
        features[name] = values
        groups[group].append(name)

    top = top_templates(sources.template_counts, sources.template_texts)
    ids = top["template_id"].to_list()
    cube = np.log1p(dense_cube(sources.template_counts, "template_id", ids, hosts, t0, minutes))
    tpl_meta = []
    for k, (tid, text, lines) in enumerate(top.select("template_id", "text", "lines").iter_rows()):
        add("templates", f"tpl_{tid}", cube[k])
        tpl_meta.append({"column": f"tpl_{tid}", "template_id": tid, "text": text, "lines": lines})

    by_level = sources.host_mix.group_by("bin", "host", "level").agg(pl.col("n").sum())
    cube = dense_cube(by_level, "level", LEVELS, hosts, t0, minutes)
    total = cube.sum(axis=0)
    share = np.divide(cube, total, out=np.zeros_like(cube), where=total > 0)
    for k, level in enumerate(LEVELS):
        add("levels", f"lvl_{level.lower()}", share[k])

    svc = top_services(sources.host_mix)
    names = svc["service"].to_list()
    chosen = sources.host_mix.filter(pl.col("program").is_in(names))
    by_program = chosen.group_by("bin", "host", "program").agg(pl.col("n").sum())
    cube = np.log1p(dense_cube(by_program, "program", names, hosts, t0, minutes))
    svc_meta = []
    for k, (name, lines) in enumerate(svc.select("service", "lines").iter_rows()):
        add("services", f"svc_{name}", cube[k])
        svc_meta.append({"column": f"svc_{name}", "service": name, "lines": lines})

    raw, no_peer = peer_rates(rates, hosts, sources.peers)
    add("peers", "peer_rate", np.log1p(raw))

    lag_rate = np.zeros_like(rates)
    lag_z = np.zeros_like(rates)
    for i in range(h):
        lag_rate[i], lag_z[i] = lag_features(rates[i])
    add("lags", "lag_rate", lag_rate)
    add("lags", "lag_z", lag_z)

    for name, values in features.items():
        if not np.isfinite(values).all():
            raise ValueError(f"forecast: non-finite feature values in {name}")

    t0_64 = np.datetime64(t0, "us")
    minute_bins = t0_64 + np.arange(minutes) * np.timedelta64(STEP_S, "s")
    frame = pl.DataFrame(
        {
            "bin": np.tile(minute_bins, h),
            "host": np.repeat(np.array(hosts, dtype=object), minutes),
            "spike": spike.reshape(-1),
            **{n: v.reshape(-1).astype(np.float32) for n, v in features.items()},
        },
        schema_overrides={"bin": pl.Datetime("us")},
    )
    counts = pl.DataFrame(
        {"bin": minute_bins, "hosts": spike.sum(axis=0).astype(np.int64)},
        schema_overrides={"bin": pl.Datetime("us")},
    )

    a, b = split_bounds(minutes)
    at = [0, a, b, minutes]

    def stamp(m: int) -> str:
        return str(np.datetime_as_string(t0_64 + np.timedelta64(m * STEP_S, "s"), unit="s"))

    meta: dict[str, Any] = {
        "start": stamp(0),
        "end": stamp(minutes),
        "minutes": minutes,
        "hosts": h,
        "host_minutes": h * minutes,
        "burst_k": burst_k,
        "groups": groups,
        "templates": tpl_meta,
        "services": svc_meta,
        "episodes": episodes,
        "spike_minutes": int(spike.sum()),
        "spike_hosts": int(spike.any(axis=1).sum()),
        "no_peer_hosts": no_peer,
        "split": {
            "train_end": a,
            "validation_end": b,
            "periods": {p: [stamp(at[i]), stamp(at[i + 1])] for i, p in enumerate(PERIODS)},
        },
        "horizons": list(HORIZONS),
        "positive_rate": positive_rates(spike),
    }
    return frame, meta, counts


# --------------------------------------------------------------------------- page


def spike_figure(counts: pl.DataFrame, meta: dict[str, Any], theme: str | None) -> go.Figure:
    """Hosts in a spike per minute, with the three periods shaded."""
    if counts.is_empty():
        return figures.empty_figure("No data in the cache", theme)
    fig = go.Figure(
        go.Scatter(
            x=counts["bin"],
            y=counts["hosts"],
            mode="lines",
            line_shape="hv",
            name="hosts in a spike",
            hovertemplate="%{x|%H:%M}: %{y} hosts<extra></extra>",
        )
    )
    for period, (start, end) in meta["split"]["periods"].items():
        fig.add_vrect(
            x0=start,
            x1=end,
            fillcolor=PERIOD_COLORS[period],
            opacity=0.12,
            layer="below",
            line_width=0,
            annotation_text=period,
            annotation_position="top left",
        )
    return figures.style(
        fig, theme, showlegend=False, yaxis_title="hosts in a spike", xaxis_title="time (UTC)"
    )


def period_text(meta: dict[str, Any]) -> str:
    """The three periods with their bounds and lengths in minutes."""
    parts = []
    for period, (start, end) in meta["split"]["periods"].items():
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        minutes = int((b - a).total_seconds() // STEP_S)
        parts.append(f"{period} {a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M} ({minutes} min)")
    return "; ".join(parts) + " (UTC)." if parts else ""


def rate_table(meta: dict[str, Any]) -> pl.DataFrame:
    """Positive rate per horizon (rows) and period (columns), as text."""
    rows = []
    for h in meta["horizons"]:
        cells = meta["positive_rate"][str(h)]
        row = {"horizon": f"{h} min"}
        for p in PERIODS:
            rate = cells[p]["rate"] if p in cells else None
            row[p] = "-" if rate is None else f"{100 * rate:.2f} %"
        rows.append(row)
    return pl.DataFrame(rows, schema=dict.fromkeys(("horizon", *PERIODS), pl.String))


def fmt_score(value: float | None, m: Mapping[str, Any]) -> str:
    """3 decimals, or why the number is undefined (no samples, no spikes, no spike-free samples)."""
    if value is not None:
        return f"{value:.3f}"
    if m["samples"] == 0:
        return NA_NO_SAMPLES
    if m["positives"] == 0:
        return NA_NO_SPIKES
    return NA_NO_NEGATIVES


def results_table(run: runs.Run, threshold: float) -> pl.DataFrame:
    """One row per model of the run, in ``run.models`` order; String columns ``RESULT_COLUMNS``."""
    rows = []
    for model in run.models:
        m = run.info["metrics"][model]
        t = forecast_metrics.threshold_metrics(run.labels, run.scores(model), threshold)
        rate = m["positive_rate"]
        rows.append(
            {
                "model": MODEL_LABELS.get(model, model),
                "accuracy": fmt_score(t["accuracy"], m),
                "F1": fmt_score(t["f1"], m),
                "AUROC": fmt_score(m["auroc"], m),
                "PR-AUC": fmt_score(m["pr_auc"], m),
                "positive rate": "-" if rate is None else f"{100 * rate:.2f} %",
            }
        )
    return pl.DataFrame(rows, schema=dict.fromkeys(RESULT_COLUMNS, pl.String))


def results_view(run: runs.Run | None, run_id: str | None, threshold: float) -> Component:
    if not run_id:
        return dmc.Text(NO_RUN_TEXT, size="sm", c="dimmed")
    if run is None:
        return dmc.Text(MISSING_RUN_TEXT.format(run_id=run_id), size="sm", c="dimmed")
    m = run.info["metrics"][run.models[0]]
    return dmc.Stack(
        [
            dmc.Text(
                f"Run {run.run_id}: horizon {run.info['settings']['horizon']} min, "
                f"{ui.fmt_int(m['samples'])} test samples, {ui.fmt_int(m['positives'])} with a "
                f"spike ahead. Threshold {threshold:.2f}.",
                size="sm",
            ),
            ui.table(results_table(run, threshold)),
        ],
        gap="xs",
    )


def run_note(trigger: str | None) -> str:
    return CANCELLED_TEXT if trigger == CANCEL_BUTTON_ID else STARTED_TEXT


@register
class Forecast:
    key = KEY
    title = "Forecasting"
    version = "1"
    depends_on: tuple[str, ...] = ("temporal", "templates", "hosts")
    setting_names: tuple[str, ...] = ("burst_k",)

    def compute(self, ctx: ComputeContext) -> None:
        store = CacheStore(ctx.out_dir.parent, ctx.settings)
        frame, meta, counts = build(load_sources(ctx.con, store), ctx.settings.burst_k)
        store.write_parquet(KEY, "frame.parquet", frame)
        store.write_parquet(KEY, "spike_counts.parquet", counts)
        store.write_json(KEY, "meta.json", meta)
        ctx.note_rows("episodes", meta["episodes"])
        ctx.note_rows("spike_minutes", meta["spike_minutes"])
        ctx.progress(
            f"{meta['host_minutes']} host-minutes, {meta['episodes']} spike episodes "
            f"on {meta['spike_hosts']} hosts"
        )

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        meta = store.read_json(KEY, "meta.json")
        return ui.page(
            "Forecasting",
            ui.grid(
                [
                    ui.stat_card("host-minutes", ui.fmt_int(meta["host_minutes"])),
                    ui.stat_card("hosts", ui.fmt_int(meta["hosts"])),
                    ui.stat_card("spike episodes", ui.fmt_int(meta["episodes"])),
                    ui.stat_card("spike minutes", ui.fmt_int(meta["spike_minutes"])),
                ],
                cols={"base": 2, "md": 4},
            ),
            ui.card(
                "Spike minutes over time (hosts in a spike, per minute)",
                ui.graph("forecast-spikes", 320),
                dmc.Text(period_text(meta), size="sm", c="dimmed"),
            ),
            ui.card(
                "Positive rate per prediction horizon",
                dmc.Text(RATE_NOTE, size="sm", c="dimmed", mb="xs"),
                ui.table(rate_table(meta)),
            ),
            ui.card(
                "Run a forecast",
                dmc.Text(RUN_NOTE, size="sm", c="dimmed", mb="xs"),
                dmc.Text("Prediction horizon (minutes)", size="sm"),
                dmc.Slider(
                    id=HORIZON_ID,
                    min=forecast_params.MIN_HORIZON,
                    max=forecast_params.MAX_HORIZON,
                    step=1,
                    value=forecast_params.DEFAULT_HORIZON,
                    marks=[{"value": v, "label": str(v)} for v in (1, 5, 10, 15)],
                    mb="xl",
                ),
                dmc.Group(
                    [
                        dmc.Button("Run", id=RUN_BUTTON_ID),
                        dmc.Button("Cancel", id=CANCEL_BUTTON_ID, variant="default", disabled=True),
                    ]
                ),
                dmc.Progress(id=PROGRESS_ID, value=0, size="lg", mt="md"),
                dmc.Text(id=PROGRESS_LABEL_ID, size="sm", c="dimmed"),
                dmc.Text(id=MESSAGE_ID, size="sm"),
                dcc.Store(id=RUN_STORE_ID, storage_type="session"),
            ),
            ui.card(
                "Results on the test period",
                dmc.Text(ACCURACY_NOTE, size="sm", c="dimmed", mb="xs"),
                dmc.Text("Threshold (a score at or above it predicts a spike)", size="sm"),
                dmc.Slider(
                    id=THRESHOLD_ID,
                    min=0.05,
                    max=0.95,
                    step=0.05,
                    precision=2,
                    value=DEFAULT_THRESHOLD,
                    marks=[{"value": v, "label": f"{v:.1f}"} for v in (0.1, 0.5, 0.9)],
                    mb="xl",
                ),
                html.Div(id=RESULTS_ID),
            ),
            description=DESCRIPTION,
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(Output("forecast-spikes", "figure"), Input(THEME_STORE_ID, "data"))
        def draw_spikes(theme: str | None) -> go.Figure:
            counts = store.read_parquet(KEY, "spike_counts.parquet")
            return spike_figure(counts, store.read_json(KEY, "meta.json"), theme)

        from logdash.forecast import job as forecast_job  # job imports this module: import it here

        runs_dir = store.settings.runs_dir
        manager = DiskcacheManager(diskcache.Cache(str(runs_dir / runs.JOBS_DIR)))
        running = [  # later stories append their own entries
            (Output(RUN_BUTTON_ID, "disabled"), True, False),
            (Output(CANCEL_BUTTON_ID, "disabled"), False, True),
        ]

        @app.callback(
            Output(RUN_STORE_ID, "data"),
            Output(MESSAGE_ID, "children"),
            Input(RUN_BUTTON_ID, "n_clicks"),
            State(HORIZON_ID, "value"),
            background=True,
            manager=manager,
            progress=[Output(PROGRESS_ID, "value"), Output(PROGRESS_LABEL_ID, "children")],
            progress_default=[0, ""],  # keep the same length as progress
            running=running,
            cancel=[Input(CANCEL_BUTTON_ID, "n_clicks")],
            interval=POLL_MS,
            prevent_initial_call=True,
        )
        def run_forecast(
            set_progress: forecast_job.ProgressFn, _clicks: int | None, horizon: int | None
        ) -> tuple[Any, str]:
            try:
                params = forecast_params.RunParams(
                    horizon=int(horizon or forecast_params.DEFAULT_HORIZON)
                )
                run_id = forecast_job.run_forecast_job(set_progress, store, runs_dir, params)
            except (OSError, ValueError) as err:
                return no_update, f"The run failed: {err}"
            return run_id, f"Run {run_id} finished (horizon {params.horizon} min)."

        @app.callback(
            Output(MESSAGE_ID, "children", allow_duplicate=True),
            Input(RUN_BUTTON_ID, "n_clicks"),
            Input(CANCEL_BUTTON_ID, "n_clicks"),
            prevent_initial_call=True,
        )
        def note_run(_run: int | None, _cancel: int | None) -> str:
            return run_note(ctx.triggered_id)

        @app.callback(
            Output(RESULTS_ID, "children"),
            Input(RUN_STORE_ID, "data"),
            Input(THRESHOLD_ID, "value"),
        )
        def show_results(run_id: str | None, threshold: float | None) -> Component:
            t = round(float(DEFAULT_THRESHOLD if threshold is None else threshold), 2)
            return results_view(runs.load_run(runs_dir, run_id), run_id, t)
