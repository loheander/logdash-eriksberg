"""Module 2: temporal analysis (event rate, host heatmap, periodicity, bursts and gaps).

Build time: counts per bin are aggregated in DuckDB into ``rate_<s>.parquet``
(bin x level x service; 1 s, 10 s, 1 min, 5 min) and ``host_<s>.parquet``
(bin x host; 10 s, 1 min, 5 min; 1 s per host would be ~26 M rows). Spectra and
burst/gap detection run on those small tables with NumPy/SciPy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import dash_mantine_components as dmc
import duckdb
import numpy as np
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output, State, ctx, no_update
from dash.development.base_component import Component
from scipy import ndimage, signal

from logdash.analyses.base import ComputeContext, register
from logdash.app import figures, ui
from logdash.app.filters import (
    FILTER_STORE_ID,
    Filters,
    filter_frame,
    from_dict,
    page_url,
    parse_time,
    with_time_range,
)
from logdash.app.shell import THEME_STORE_ID
from logdash.cache.store import CacheStore
from logdash.ingest.normalize import LEVELS

KEY = "temporal"
RATE_BINS = (1, 10, 60, 300)  # level x service tables
HOST_BINS = (10, 60, 300)  # host tables
MISSING = "(missing)"
ALL = "(all)"
SPLITS = {"level": "level", "service": "program", "host": "host"}
TOP_SPLIT = 10
ROLL_WINDOW = 61  # 1-minute bins (one hour) for the rolling median and MAD
MIN_BURST = 10  # a burst needs at least this many events in its peak minute
MIN_EXCESS = 300  # ... and at least this many events above its baseline over the whole episode
MERGE_BINS = 2  # bursts separated by at most this many quiet minutes form one episode
MAX_LAG = 3600  # autocorrelation lags in seconds
SPECTRUM_POINTS = 300
MIN_SPECTRUM_EVENTS = 50
PEAKS = 5
MAX_ANOMALY_ROWS = 100
HEAT_MAX_BINS = 3000
SETTING_NAMES = ("burst_k", "gap_minutes")


# --------------------------------------------------------------------------- compute


def _bucket(expr: str, seconds: int) -> str:
    if seconds == 1:
        return f"date_trunc('second', {expr})"
    return f"time_bucket(INTERVAL '{seconds} seconds', {expr})"


def write_rate_tables(con: duckdb.DuckDBPyConnection, out_dir: Path) -> None:
    """Write all rate and host tables from two scans of ``events``; coarser bins roll up."""
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE r1 AS
        SELECT {_bucket("ts", 1)} AS bin, level, coalesce(program, '{MISSING}') AS program,
               count(*) AS n
        FROM events GROUP BY ALL"""
    )
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE h10 AS
        SELECT {_bucket("ts", 10)} AS bin, coalesce(host, '{MISSING}') AS host, count(*) AS n
        FROM events GROUP BY ALL"""
    )
    for seconds in RATE_BINS:
        con.execute(
            f"""COPY (SELECT {_bucket("bin", seconds)} AS bin, level, program, sum(n)::BIGINT AS n
                      FROM r1 GROUP BY ALL ORDER BY bin)
                TO '{out_dir}/rate_{seconds}.parquet' (FORMAT parquet, COMPRESSION zstd)"""
        )
    for seconds in HOST_BINS:
        con.execute(
            f"""COPY (SELECT {_bucket("bin", seconds)} AS bin, host, sum(n)::BIGINT AS n
                      FROM h10 GROUP BY ALL ORDER BY bin)
                TO '{out_dir}/host_{seconds}.parquet' (FORMAT parquet, COMPRESSION zstd)"""
        )
    con.execute("DROP TABLE r1")
    con.execute("DROP TABLE h10")


def span(bins: pl.Series) -> tuple[datetime, datetime]:
    """First and last timestamp of a non-empty datetime series."""
    lo, hi = bins.min(), bins.max()
    assert isinstance(lo, datetime) and isinstance(hi, datetime)
    return lo, hi


def dense_series(
    bins: pl.Series, counts: pl.Series, t0: datetime, step: int, length: int
) -> np.ndarray:
    """Counts per bin as a dense float array of ``length`` bins starting at ``t0``."""
    out = np.zeros(length)
    if bins.len() == 0:
        return out
    idx = ((bins - t0).dt.total_seconds() // step).to_numpy().astype(int)
    keep = (idx >= 0) & (idx < length)
    np.add.at(out, idx[keep], counts.to_numpy()[keep].astype(float))
    return out


def autocorrelation(x: np.ndarray, max_lag: int = MAX_LAG) -> np.ndarray:
    """Normalised autocorrelation at lags ``0..max_lag`` (FFT based); empty if constant."""
    x = x - x.mean()
    n = len(x)
    if n < 2 or not np.any(x):
        return np.zeros(0)
    size = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(x, size)
    acf = np.fft.irfft(spec * np.conj(spec), size)[: min(max_lag, n - 1) + 1]
    return acf / acf[0]


def periodogram(x: np.ndarray, fs: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """``(period in seconds, power)`` without the zero frequency; empty if constant."""
    x = x - x.mean()
    if len(x) < 4 or not np.any(x):
        return np.zeros(0), np.zeros(0)
    freq, power = signal.periodogram(x, fs=fs, detrend=False)
    return 1.0 / freq[1:], power[1:]


def log_bin_spectrum(
    period: np.ndarray, power: np.ndarray, points: int = SPECTRUM_POINTS
) -> tuple[np.ndarray, np.ndarray]:
    """Reduce a periodogram to at most ``points`` log-spaced period bins (max power per bin)."""
    if len(period) <= points:
        return period, power
    edges = np.geomspace(period.min(), period.max() * 1.0001, points + 1)
    which = np.clip(np.digitize(period, edges) - 1, 0, points - 1)
    best = np.full(points, -1.0)
    np.maximum.at(best, which, power)
    centers = np.sqrt(edges[:-1] * edges[1:])
    keep = best >= 0
    return centers[keep], best[keep]


def spectral_tables(rate_1s: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Autocorrelation, periodogram and top peaks per service (and for all events)."""
    if rate_1s.is_empty():
        empty = {"service": pl.String}
        return {
            "acf": pl.DataFrame(schema={**empty, "lag": pl.Int64, "acf": pl.Float64}),
            "spectrum": pl.DataFrame(schema={**empty, "period": pl.Float64, "power": pl.Float64}),
            "peaks": pl.DataFrame(
                schema={**empty, "rank": pl.Int64, "period": pl.Float64, "power": pl.Float64}
            ),
        }
    t0, t1 = span(rate_1s["bin"])
    length = int((t1 - t0).total_seconds()) + 1
    per = rate_1s.group_by("program", "bin").agg(pl.col("n").sum())
    series = {ALL: per.group_by("bin").agg(pl.col("n").sum())}
    totals = per.group_by("program").agg(pl.col("n").sum())
    for program in totals.filter(pl.col("n") >= MIN_SPECTRUM_EVENTS)["program"]:
        series[program] = per.filter(pl.col("program") == program)
    acf_rows, spec_rows, peak_rows = [], [], []
    for name, frame in series.items():
        x = dense_series(frame["bin"], frame["n"], t0, 1, length)
        acf = autocorrelation(x)
        period, power = periodogram(x)
        if not len(acf) or not len(period):
            continue
        acf_rows.append(pl.DataFrame({"service": name, "lag": np.arange(len(acf)), "acf": acf}))
        grid_p, grid_w = log_bin_spectrum(period, power)
        spec_rows.append(pl.DataFrame({"service": name, "period": grid_p, "power": grid_w}))
        top = np.argsort(power)[::-1][:PEAKS]
        peak_rows.append(
            pl.DataFrame(
                {
                    "service": name,
                    "rank": np.arange(1, len(top) + 1),
                    "period": period[top],
                    "power": power[top],
                }
            )
        )
    return {
        "acf": pl.concat(acf_rows) if acf_rows else spectral_tables(rate_1s.clear())["acf"],
        "spectrum": pl.concat(spec_rows)
        if spec_rows
        else spectral_tables(rate_1s.clear())["spectrum"],
        "peaks": pl.concat(peak_rows) if peak_rows else spectral_tables(rate_1s.clear())["peaks"],
    }


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive ``(first, last)`` index pairs of consecutive True values."""
    padded = np.concatenate([[False], mask.astype(bool), [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b) - 1) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def rolling_baseline(x: np.ndarray, window: int = ROLL_WINDOW) -> tuple[np.ndarray, np.ndarray]:
    """Rolling median and robust scale (1.4826 MAD, floored by Poisson noise and 1)."""
    median = ndimage.median_filter(x, size=window, mode="nearest")
    mad = ndimage.median_filter(np.abs(x - median), size=window, mode="nearest")
    scale = np.maximum(1.4826 * mad, np.sqrt(np.maximum(median, 1.0)))
    return median, scale


def find_bursts(x: np.ndarray, k: float, min_peak: int = MIN_BURST) -> list[dict[str, Any]]:
    """Runs of bins above rolling median + k robust sigmas; one dict per run."""
    median, scale = rolling_baseline(x)
    flagged = (x > median + k * scale) & (x >= min_peak)
    if MERGE_BINS:
        flagged = ndimage.binary_closing(flagged, structure=np.ones(MERGE_BINS + 1, dtype=bool))
    found = []
    for first, last in runs(flagged):
        peak = first + int(np.argmax(x[first : last + 1]))
        excess = float(x[first : last + 1].sum() - median[first : last + 1].sum())
        if excess < MIN_EXCESS:
            continue
        found.append(
            {
                "first": first,
                "last": last,
                "peak": float(x[peak]),
                "baseline": float(median[peak]),
                "score": float((x[peak] - median[peak]) / scale[peak]),
                "events": float(x[first : last + 1].sum()),
                "excess": excess,
            }
        )
    return found


def find_gaps(x: np.ndarray, min_bins: int) -> list[dict[str, Any]]:
    """Runs of at least ``min_bins`` empty bins."""
    nonzero = x[x > 0]
    baseline = float(np.median(nonzero)) if len(nonzero) else 0.0
    return [
        {
            "first": a,
            "last": b,
            "peak": 0.0,
            "baseline": baseline,
            "score": float(b - a + 1),
            "events": 0.0,
            "excess": baseline * (b - a + 1),
        }
        for a, b in runs(x == 0)
        if b - a + 1 >= min_bins
    ]


ANOMALY_SCHEMA: dict[str, Any] = {
    "kind": pl.String,
    "entity_type": pl.String,
    "entity": pl.String,
    "start": pl.Datetime("us"),
    "end": pl.Datetime("us"),
    "peak": pl.Float64,
    "baseline": pl.Float64,
    "score": pl.Float64,
    "events": pl.Float64,
    "excess": pl.Float64,  # events above (burst) or below (gap) the rolling baseline
}


def detect_anomalies(
    rate_60: pl.DataFrame, host_60: pl.DataFrame, burst_k: float, gap_minutes: int
) -> pl.DataFrame:
    """Bursts (total, per service, per host) and gaps (total, per host) on 1-minute bins."""
    if rate_60.is_empty():
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    t0, t1 = span(pl.concat([rate_60["bin"], host_60["bin"]]))
    length = int((t1 - t0).total_seconds() // 60) + 1
    rows: list[dict[str, Any]] = []

    def add(kind: str, etype: str, entity: str, found: list[dict[str, Any]]) -> None:
        for f in found:
            rows.append(
                {
                    "kind": kind,
                    "entity_type": etype,
                    "entity": entity,
                    "start": t0 + timedelta(minutes=f["first"]),
                    "end": t0 + timedelta(minutes=f["last"] + 1),
                    "peak": f["peak"],
                    "baseline": f["baseline"],
                    "score": f["score"],
                    "events": f["events"],
                    "excess": f["excess"],
                }
            )

    def series(frame: pl.DataFrame) -> np.ndarray:
        return dense_series(frame["bin"], frame["n"], t0, 60, length)

    total = rate_60.group_by("bin").agg(pl.col("n").sum())
    add("burst", "all", ALL, find_bursts(series(total), burst_k))
    add("gap", "all", ALL, find_gaps(series(total), gap_minutes))
    for (program,), frame in rate_60.group_by(["program"], maintain_order=True):
        add("burst", "service", str(program), find_bursts(series(frame), burst_k))
    for (host,), frame in host_60.group_by(["host"], maintain_order=True):
        if host == MISSING:  # malformed rows, not a machine
            continue
        x = series(frame)
        add("burst", "host", str(host), find_bursts(x, burst_k))
        add("gap", "host", str(host), find_gaps(x, gap_minutes))
    if not rows:
        return pl.DataFrame(schema=ANOMALY_SCHEMA)
    return pl.DataFrame(rows, schema=ANOMALY_SCHEMA).sort("excess", descending=True)


# ------------------------------------------------------------------- page data access


def lazy_table(store: CacheStore, name: str) -> pl.LazyFrame:
    return pl.scan_parquet(store.path(KEY, name))


def visible_range(filters: Filters, meta: dict[str, Any]) -> tuple[datetime, datetime]:
    """Visible time range: the time filter where set, else the whole data span."""
    lo = parse_time(filters.start) or datetime.fromisoformat(meta["ts_min"])
    hi = parse_time(filters.end) or datetime.fromisoformat(meta["ts_max"]) + timedelta(seconds=1)
    return lo, max(hi, lo + timedelta(seconds=1))


def pick_table(filters: Filters, split: str, bin_s: int) -> tuple[str, int]:
    """File name and effective bin size; host data is used for host splits and host filters."""
    if split == "host" or filters.hosts:
        size = next((b for b in HOST_BINS if b >= bin_s), HOST_BINS[-1])
        return f"host_{size}.parquet", size
    size = next((b for b in RATE_BINS if b >= bin_s), RATE_BINS[-1])
    return f"rate_{size}.parquet", size


def load_rate(
    store: CacheStore, filters: Filters, split: str, lo: datetime, hi: datetime, bin_s: int
) -> tuple[pl.DataFrame, int, str]:
    """``(bin, series, n)`` for the visible range, the effective bin size and a note."""
    name, size = pick_table(filters, split, bin_s)
    df = lazy_table(store, name).filter((pl.col("bin") >= lo) & (pl.col("bin") < hi)).collect()
    notes = []
    column: str | None
    if name.startswith("host_"):
        df = filter_frame(df, filters, host="host")
        column = "host" if split == "host" else None
        if column is None:
            notes.append(f"split by {split} unavailable while hosts are filtered")
        if filters.levels or filters.services:
            notes.append("level and service filters ignored")
    else:
        df = filter_frame(df, filters, level="level", service="program")
        column = SPLITS[split]
    if column is None:
        df = df.group_by("bin").agg(pl.col("n").sum()).with_columns(series=pl.lit("events"))
    else:
        df = df.rename({column: "series"})
    note = "; ".join(notes)
    return df.group_by("bin", "series").agg(pl.col("n").sum()), size, note


def fold_series(df: pl.DataFrame, top: int = TOP_SPLIT) -> pl.DataFrame:
    """Keep the ``top`` largest series and fold the rest into ``other``."""
    kept = figures.top_n_other(df.rename({"series": "label"}), "label", "n", top)["label"].to_list()
    return (
        df.with_columns(
            pl.when(pl.col("series").is_in(kept)).then(pl.col("series")).otherwise(pl.lit("other"))
        )
        .group_by("bin", "series")
        .agg(pl.col("n").sum())
    )


def level_order(names: list[str]) -> list[str]:
    known = [n for n in LEVELS if n in names]
    return known + sorted(n for n in names if n not in LEVELS)


def rate_figure(
    df: pl.DataFrame,
    lo: datetime,
    hi: datetime,
    bin_s: int,
    split: str,
    scheme: str | None,
    note: str = "",
) -> go.Figure:
    if df.is_empty():
        return figures.empty_figure("no events in the visible range", scheme)
    df = fold_series(df)
    first = datetime.fromtimestamp(lo.replace(tzinfo=UTC).timestamp() // bin_s * bin_s, UTC)
    grid = pl.datetime_range(
        first.replace(tzinfo=None), hi, interval=f"{bin_s}s", eager=True, time_unit="us"
    ).to_frame("bin")
    names = (
        df.group_by("series").agg(pl.col("n").sum()).sort("n", descending=True)["series"].to_list()
    )
    if split == "level":
        names = level_order(names)
    fig = go.Figure()
    for name in names:
        part = (
            grid.join(df.filter(pl.col("series") == name), on="bin", how="left")
            .with_columns(pl.col("n").fill_null(0))
            .sort("bin")
        )
        fig.add_scatter(
            x=part["bin"].to_list(), y=part["n"].to_list(), name=name, mode="lines",
            line={"shape": "hv", "width": 1.2},
        )  # fmt: skip
    fig.update_yaxes(title=f"events per {_bin_label(bin_s)}")
    fig.update_xaxes(range=[lo, hi])
    figures.add_note(fig, f"bin {_bin_label(bin_s)}" + (f" ({note})" if note else ""))
    return figures.style(fig, scheme, dragmode="zoom", height=380)


def _bin_label(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


HEAT_MAX_HEIGHT = 720
REL_CLIP = 3.0
REL_TICKS = ("×1/8", "×1/4", "×1/2", "×1", "×2", "×4", "×8")


def heatmap_height(n_hosts: int) -> int:
    """Pixel height: one row per host up to ``HEAT_MAX_HEIGHT``; labels thin out beyond that."""
    return min(HEAT_MAX_HEIGHT, max(320, 14 * n_hosts + 120))


def relative_ratio(z: np.ndarray) -> np.ndarray:
    """Each row's counts divided by that row's median (floored at 1); gaps give 0."""
    med = np.maximum(np.median(z, axis=1, keepdims=True), 1.0)
    return z / med


def relative_scale(z: np.ndarray, clip: float = REL_CLIP) -> np.ndarray:
    """log2 of the per-row ratio to the row median, clipped to ``[-clip, clip]``.

    Zero counts (gaps) are mapped to ``-clip``, the strongest negative value.
    """
    with np.errstate(divide="ignore"):
        out = np.log2(relative_ratio(z))
    return np.clip(out, -clip, clip)


def heatmap_figure(
    df: pl.DataFrame, bin_s: int, scheme: str | None, scale: str = "relative"
) -> go.Figure:
    """Host x time heatmap from a ``(bin, host, n)`` frame.

    ``relative``: log2 of the ratio to each host's median, diverging around 0.
    ``absolute``: log1p of the counts.
    """
    if df.is_empty():
        return figures.empty_figure("no events in the visible range", scheme)
    hosts = sorted(df["host"].unique().to_list())
    t0, t1 = span(df["bin"])
    length = int((t1 - t0).total_seconds() // bin_s) + 1
    z = np.vstack(
        [
            dense_series(sub["bin"], sub["n"], t0, bin_s, length)
            for host in hosts
            for sub in [df.filter(pl.col("host") == host)]
        ]
    )
    x = [t0 + timedelta(seconds=bin_s * i) for i in range(length)]
    relative = scale != "absolute"
    scaling: dict[str, Any] = (
        {
            "z": relative_scale(z),
            "zmin": -REL_CLIP,
            "zmax": REL_CLIP,
            "zmid": 0,
            "colorscale": "RdBu_r",
            "colorbar": {
                "title": "vs host median",
                "tickvals": list(range(-3, 4)),
                "ticktext": list(REL_TICKS),
            },
        }
        if relative
        else {"z": np.log1p(z), "colorscale": "Viridis", "colorbar": {"title": "log(1+n)"}}
    )
    fig = go.Figure(
        go.Heatmap(
            x=x,
            y=hosts,
            customdata=np.dstack([z, relative_ratio(z)]),
            hovertemplate=(
                "%{y} %{x}: %{customdata[0]:,.0f} events (×%{customdata[1]:.2f} median)"
                "<extra></extra>"
            ),
            **scaling,
        )
    )
    return figures.style(fig, scheme, height=heatmap_height(len(hosts)))


def acf_figure(acf: pl.DataFrame, service: str | None, scheme: str | None) -> go.Figure:
    part = acf.filter(pl.col("service") == service)
    if part.height < 2:
        return figures.empty_figure("no autocorrelation for this service", scheme)
    fig = go.Figure(go.Scatter(x=part["lag"].to_list(), y=part["acf"].to_list(), mode="lines"))
    fig.update_xaxes(title="lag (seconds)")
    fig.update_yaxes(title="autocorrelation", range=[-1, 1])
    return figures.style(
        fig, scheme, title=f"{service}: autocorrelation of events per second", showlegend=False
    )


def spectrum_figure(
    spectrum: pl.DataFrame, peaks: pl.DataFrame, service: str | None, scheme: str | None
) -> go.Figure:
    part = spectrum.filter(pl.col("service") == service).sort("period")
    if part.is_empty():
        return figures.empty_figure("no periodogram for this service", scheme)
    fig = go.Figure(
        go.Scatter(
            x=part["period"].to_list(), y=part["power"].to_list(), mode="lines", name="periodogram"
        )
    )
    top = peaks.filter(pl.col("service") == service)
    fig.add_scatter(
        x=top["period"].to_list(), y=top["power"].to_list(), mode="markers", name="peaks",
        marker={"size": 8, "symbol": "diamond"},
    )  # fmt: skip
    fig.update_xaxes(type="log", title="period (seconds)")
    fig.update_yaxes(type="log", title="power")
    return figures.style(fig, scheme, title=f"{service}: periodogram (events per second)")


def brush_range(relayout: dict[str, Any] | None) -> tuple[datetime, datetime] | str | None:
    """Time range from Plotly ``relayoutData``; ``"reset"`` for autorange, else ``None``."""
    if not relayout:
        return None
    if relayout.get("xaxis.autorange"):
        return "reset"
    lo, hi = relayout.get("xaxis.range[0]"), relayout.get("xaxis.range[1]")
    if lo is None or hi is None:
        pair = relayout.get("xaxis.range")
        lo, hi = (pair[0], pair[1]) if isinstance(pair, list) and len(pair) == 2 else (None, None)
    start, end = parse_time(lo), parse_time(hi)
    if start is None or end is None or (end - start).total_seconds() < 1:
        return None
    return start, end


def anomaly_view(df: pl.DataFrame, filters: Filters) -> pl.DataFrame:
    """Anomalies overlapping the time range and matching host/service filters."""
    lo, hi = parse_time(filters.start), parse_time(filters.end)
    if lo:
        df = df.filter(pl.col("end") > lo)
    if hi:
        df = df.filter(pl.col("start") < hi)
    keep = pl.col("entity_type") == "all"
    if filters.hosts:
        keep = keep | ((pl.col("entity_type") == "host") & pl.col("entity").is_in(filters.hosts))
    else:
        keep = keep | (pl.col("entity_type") == "host")
    if filters.services:
        keep = keep | (
            (pl.col("entity_type") == "service") & pl.col("entity").is_in(filters.services)
        )
    else:
        keep = keep | (pl.col("entity_type") == "service")
    return df.filter(keep)


def explorer_href(row: dict[str, Any]) -> str:
    """Link into the explorer for one anomaly row."""
    params: dict[str, object] = {
        "start": row["start"].replace(microsecond=0).isoformat(sep=" "),
        "end": row["end"].replace(microsecond=0).isoformat(sep=" "),
    }
    if row["entity_type"] == "host":
        params["host"] = row["entity"]
    elif row["entity_type"] == "service":
        params["service"] = row["entity"]
    return page_url("/explorer", **params)


def anomaly_table(df: pl.DataFrame) -> Component:
    if df.is_empty():
        return dmc.Text("no bursts or gaps found for the current filters")
    df = df.head(MAX_ANOMALY_ROWS)
    shown = df.select(
        "kind", "entity_type", "entity",
        pl.col("start").dt.strftime("%Y-%m-%d %H:%M"),
        pl.col("end").dt.strftime("%Y-%m-%d %H:%M"),
        ((pl.col("end") - pl.col("start")).dt.total_minutes()).alias("minutes"),
        pl.col("peak").round(0).cast(pl.Int64),
        pl.col("baseline").round(1),
        pl.col("score").round(1),
        pl.col("excess").round(0).cast(pl.Int64),
    )  # fmt: skip
    return ui.link_table(shown, [explorer_href(r) for r in df.iter_rows(named=True)])


@register
class Temporal:
    key = KEY
    title = "Temporal"
    version = "2"
    depends_on: tuple[str, ...] = ()
    setting_names = SETTING_NAMES

    def compute(self, ctx: ComputeContext) -> None:
        write_rate_tables(ctx.con, ctx.out_dir)
        rates = {s: pl.read_parquet(ctx.out_dir / f"rate_{s}.parquet") for s in (1, 60)}
        host_60 = pl.read_parquet(ctx.out_dir / "host_60.parquet")
        for name, df in spectral_tables(rates[1]).items():
            df.write_parquet(ctx.out_dir / f"{name}.parquet", compression="zstd")
        anomalies = detect_anomalies(
            rates[60], host_60, ctx.settings.burst_k, ctx.settings.gap_minutes
        )
        anomalies.write_parquet(ctx.out_dir / "anomalies.parquet", compression="zstd")
        span = ctx.con.execute("SELECT min(ts), max(ts) FROM events").fetchone() or (None, None)
        services = sorted(rates[60]["program"].unique().to_list())
        hosts = sorted(host_60["host"].unique().to_list())
        meta = {"ts_min": span[0], "ts_max": span[1], "services": services, "hosts": hosts}
        (ctx.out_dir / "meta.json").write_text(json.dumps(meta, default=str))
        ctx.progress(f"{anomalies.height} bursts and gaps")

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        peaks = store.read_parquet(KEY, "peaks.parquet")
        services = [s for s in peaks["service"].unique().sort().to_list() if s != ALL]
        meta = store.read_json(KEY, "meta.json")
        return ui.page(
            "Temporal analysis",
            dmc.Group(
                [
                    dmc.Text("Split by", size="sm"),
                    dmc.SegmentedControl(
                        id="temporal-split", data=list(SPLITS), value="level", size="xs"
                    ),
                    dmc.Button("Reset time range", id="temporal-reset", size="xs", variant="light"),
                ]
            ),
            ui.card(
                "Event rate (drag to zoom; the range becomes the global time filter)",
                ui.graph("temporal-rate", 380),
            ),  # fmt: skip
            ui.card(
                "Host x time heatmap",
                dmc.Group(
                    [
                        dmc.SegmentedControl(
                            id="temporal-hbin",
                            data=[
                                {"label": "1 min", "value": "60"},
                                {"label": "5 min", "value": "300"},
                            ],
                            value="300",
                            size="xs",
                        ),
                        dmc.Text("Scale", size="sm"),
                        dmc.SegmentedControl(
                            id="temporal-hscale",
                            data=["relative", "absolute"],
                            value="relative",
                            size="xs",
                        ),
                    ],
                    mb="xs",
                ),
                ui.graph("temporal-heatmap", heatmap_height(len(meta["hosts"]))),
            ),
            ui.card(
                "Periodicity",
                dmc.Select(
                    id="temporal-service",
                    data=[ALL, *services],
                    value=ALL,
                    label="Service",
                    size="xs",
                    w=300,
                    mb="xs",
                ),
                ui.grid(
                    [ui.graph("temporal-acf", 320), ui.graph("temporal-spectrum", 320)],
                    cols={"base": 1, "lg": 2},
                ),
            ),
            ui.card("Bursts and gaps", dmc.Box(id="temporal-anomalies")),
            description=(
                "Rates come from pre-binned counts; the global time range and level, service and "
                "host filters apply. Host filters use host-level counts, which carry no level "
                "or service."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            Output("temporal-rate", "figure"),
            Input(FILTER_STORE_ID, "data"),
            Input("temporal-split", "value"),
            Input(THEME_STORE_ID, "data"),
        )
        def draw_rate(
            filters: dict[str, Any] | None, split: str | None, theme: str | None
        ) -> go.Figure:
            return self.rate(store, from_dict(filters), split or "level", theme)

        @app.callback(
            Output(FILTER_STORE_ID, "data", allow_duplicate=True),
            Input("temporal-rate", "relayoutData"),
            Input("temporal-reset", "n_clicks"),
            State(FILTER_STORE_ID, "data"),
            prevent_initial_call=True,
        )
        def brush(
            relayout: dict[str, Any] | None, _clicks: int | None, filters: dict[str, Any] | None
        ) -> Any:
            return self.brushed(ctx.triggered_id, relayout, filters)

        @app.callback(
            Output("temporal-heatmap", "figure"),
            Input(FILTER_STORE_ID, "data"),
            Input("temporal-hbin", "value"),
            Input("temporal-hscale", "value"),
            Input(THEME_STORE_ID, "data"),
        )
        def draw_heatmap(
            filters: dict[str, Any] | None,
            hbin: str | None,
            hscale: str | None,
            theme: str | None,
        ) -> go.Figure:
            return self.heatmap(
                store, from_dict(filters), int(hbin or 300), theme, hscale or "relative"
            )

        @app.callback(
            Output("temporal-acf", "figure"),
            Output("temporal-spectrum", "figure"),
            Input("temporal-service", "value"),
            Input(THEME_STORE_ID, "data"),
        )
        def draw_spectra(service: str | None, theme: str | None) -> tuple[go.Figure, go.Figure]:
            acf = store.read_parquet(KEY, "acf.parquet")
            spectrum = store.read_parquet(KEY, "spectrum.parquet")
            peaks = store.read_parquet(KEY, "peaks.parquet")
            return (
                acf_figure(acf, service, theme),
                spectrum_figure(spectrum, peaks, service, theme),
            )

        @app.callback(Output("temporal-anomalies", "children"), Input(FILTER_STORE_ID, "data"))
        def draw_anomalies(filters: dict[str, Any] | None) -> Component:
            anomalies = store.read_parquet(KEY, "anomalies.parquet")
            return anomaly_table(anomaly_view(anomalies, from_dict(filters)))

    @staticmethod
    def rate(store: CacheStore, filters: Filters, split: str, theme: str | None) -> go.Figure:
        meta = store.read_json(KEY, "meta.json")
        lo, hi = visible_range(filters, meta)
        bin_s = figures.choose_bin_seconds(lo.timestamp(), hi.timestamp())
        df, size, note = load_rate(store, filters, split, lo, hi, bin_s)
        return rate_figure(df, lo, hi, size, split, theme, note)

    @staticmethod
    def heatmap(
        store: CacheStore,
        filters: Filters,
        bin_s: int,
        theme: str | None,
        scale: str = "relative",
    ) -> go.Figure:
        meta = store.read_json(KEY, "meta.json")
        lo, hi = visible_range(filters, meta)
        if (hi - lo).total_seconds() / bin_s > HEAT_MAX_BINS:
            bin_s = 300
        name = f"host_{bin_s}.parquet"
        df = lazy_table(store, name).filter((pl.col("bin") >= lo) & (pl.col("bin") < hi)).collect()
        return heatmap_figure(filter_frame(df, filters, host="host"), bin_s, theme, scale)

    @staticmethod
    def brushed(
        trigger: str | None, relayout: dict[str, Any] | None, filters: dict[str, Any] | None
    ) -> Any:
        if trigger == "temporal-reset":
            return with_time_range(filters, None, None)
        picked = brush_range(relayout)
        if picked == "reset":
            return with_time_range(filters, None, None)
        if isinstance(picked, tuple):
            return with_time_range(filters, *picked)
        return no_update
