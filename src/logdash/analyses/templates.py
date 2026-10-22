"""Module 3: log templates (Drain3 on unique masked lines, never on raw rows).

``events`` carries ``masked_hash = hash(mask(payload))``. Build time:

1. group ``events`` by ``masked_hash`` in DuckDB (count, first/last seen, one sample);
2. mask the one sample per group and feed the unique masked lines, most frequent
   first, to Drain3;
3. map ``masked_hash`` to ``template_id`` (``assignments.parquet``) and join that small
   table back onto ``events`` in DuckDB for ``template_counts.parquet`` (per service) and
   ``template_counts_host.parquet`` (per host), both per template and minute;
4. sample raw payloads of the top templates and extract the masked-out values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
import duckdb
import plotly.graph_objects as go
import polars as pl
from dash import Dash, Input, Output
from dash.development.base_component import Component
from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from logdash.analyses.base import ComputeContext, register
from logdash.app import figures, theme, ui
from logdash.app.filters import (
    FILTER_STORE_ID,
    Filters,
    filter_frame,
    from_dict,
    page_url,
    parse_time,
)
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.ingest.masking import MASK_RULES, mask_sql

KEY = "templates"
SETTING_NAMES = (
    "drain_sim_th",
    "drain_depth",
    "template_top_k",
    "param_sample",
    "rare_threshold",
    "novel_fraction",
)
MISSING = "(missing)"
GRID_ROWS = 200
LIST_ROWS = 100
MAX_VALUES = 500  # distinct parameter values kept per template position
PARAM_BARS = 20
SAMPLE_OVERSHOOT = 3  # sample this many times the wanted rows, then cap exactly
TEXT_CHARS = 160

_PARAM_RE = re.compile(
    "|".join(f"(?P<k{i}>{pattern})" for i, (pattern, _) in enumerate(MASK_RULES)), re.ASCII
)
_KINDS = [repl.strip("<>") for _, repl in MASK_RULES]


# --------------------------------------------------------------------------- compute


def mine_templates(
    lines: Sequence[str], sim_th: float, depth: int
) -> tuple[list[int], dict[int, str]]:
    """Drain3 cluster id per line (in order) and the final template text per cluster id.

    Masking is already done upstream, so Drain's own masking is left empty.
    """
    config = TemplateMinerConfig()
    config.drain_sim_th = sim_th
    config.drain_depth = depth
    config.profiling_enabled = False
    config.masking_instructions = []
    drain = TemplateMiner(config=config).drain
    ids = [drain.add_log_message(line)[0].cluster_id for line in lines]
    return ids, {cid: cluster.get_template() for cid, cluster in drain.id_to_cluster.items()}


def renumber_by_count(
    hashes: Sequence[int], counts: Sequence[int], cluster_ids: Sequence[int]
) -> tuple[pl.DataFrame, dict[int, int]]:
    """``assignments`` frame (masked_hash, template_id) with ids 1.. ordered by event count."""
    df = pl.DataFrame(
        {"masked_hash": hashes, "n": counts, "cid": cluster_ids},
        schema={"masked_hash": pl.UInt64, "n": pl.Int64, "cid": pl.Int64},
    )
    order = df.group_by("cid").agg(pl.col("n").sum()).sort(["n", "cid"], descending=[True, False])
    mapping = {cid: i + 1 for i, cid in enumerate(order["cid"].to_list())}
    assignments = df.select(
        "masked_hash",
        pl.col("cid").replace_strict(mapping, return_dtype=pl.Int64).alias("template_id"),
    )
    return assignments, mapping


def extract_params(payload: str) -> list[tuple[str, str]]:
    """``(kind, value)`` for every token the masking rules would replace, in order."""
    out = []
    for match in _PARAM_RE.finditer(payload):
        index = int(match.lastgroup[1:]) if match.lastgroup else 0
        out.append((_KINDS[index], match.group()))
    return out


def params_frame(samples: pl.DataFrame) -> pl.DataFrame:
    """Aggregate sampled ``(template_id, payload)`` rows into value counts per position."""
    rows = [
        (tid, pos, kind, value)
        for tid, payload in zip(samples["template_id"], samples["payload"], strict=True)
        for pos, (kind, value) in enumerate(extract_params(payload))
    ]
    df = pl.DataFrame(
        rows,
        schema={
            "template_id": pl.Int64,
            "position": pl.Int64,
            "kind": pl.String,
            "value": pl.String,
        },
        orient="row",
    )
    counts = df.group_by("template_id", "position", "kind", "value").agg(pl.len().alias("n"))
    ranked = counts.with_columns(
        pl.col("n").rank("ordinal", descending=True).over("template_id", "position").alias("rank")
    )
    return ranked.filter(pl.col("rank") <= MAX_VALUES).drop("rank").sort("template_id", "position")


def unique_lines_sql() -> str:
    return f"""
        SELECT masked_hash, {mask_sql("sample")} AS masked, n, first_seen, last_seen
        FROM (SELECT masked_hash, any_value(payload) AS sample, count(*) AS n,
                     min(ts) AS first_seen, max(ts) AS last_seen
              FROM events WHERE masked_hash IS NOT NULL GROUP BY masked_hash)
        ORDER BY n DESC, masked_hash"""


def compute_all(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    out_dir: Path,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Run the whole pipeline; returns row counts of the outputs."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE uniq AS {unique_lines_sql()}")
    uniq = con.execute("SELECT masked_hash, masked, n FROM uniq").pl()
    log(f"templates: {uniq.height} unique masked lines")
    ids, texts = mine_templates(
        uniq["masked"].to_list(), settings.drain_sim_th, settings.drain_depth
    )
    assignments, mapping = renumber_by_count(
        uniq["masked_hash"].to_list(), uniq["n"].to_list(), ids
    )
    log(f"templates: {len(texts)} templates")
    names = pl.DataFrame(
        {"template_id": [mapping[c] for c in texts], "template": list(texts.values())},
        schema={"template_id": pl.Int64, "template": pl.String},
    )
    con.register("assign_arrow", assignments.to_arrow())
    con.execute("CREATE OR REPLACE TEMP TABLE assign AS SELECT * FROM assign_arrow")
    stats = con.execute(
        """SELECT a.template_id, sum(u.n)::BIGINT AS count, min(u.first_seen) AS first_seen,
                  max(u.last_seen) AS last_seen, count(*)::BIGINT AS n_variants
           FROM uniq u JOIN assign a USING (masked_hash) GROUP BY a.template_id"""
    ).pl()
    templates = stats.join(names, on="template_id").select(
        "template_id", "template", "count", "first_seen", "last_seen", "n_variants"
    ).sort("template_id")  # fmt: skip
    templates.write_parquet(out_dir / "templates.parquet", compression="zstd")
    assignments.write_parquet(out_dir / "assignments.parquet", compression="zstd")
    con.execute(
        f"""COPY (SELECT a.template_id, time_bucket(INTERVAL '1 minute', e.ts) AS bin,
                         coalesce(e.program, '{MISSING}') AS program, count(*)::BIGINT AS n
                  FROM events e JOIN assign a USING (masked_hash)
                  GROUP BY ALL ORDER BY bin)
            TO '{out_dir}/template_counts.parquet' (FORMAT parquet, COMPRESSION zstd)"""
    )
    con.execute(
        f"""COPY (SELECT a.template_id, time_bucket(INTERVAL '1 minute', e.ts) AS bin,
                         coalesce(e.host, '{MISSING}') AS host, count(*)::BIGINT AS n
                  FROM events e JOIN assign a USING (masked_hash)
                  GROUP BY ALL ORDER BY bin)
            TO '{out_dir}/template_counts_host.parquet' (FORMAT parquet, COMPRESSION zstd)"""
    )
    params = sample_params(con, templates, settings)
    (out_dir / "params").mkdir(exist_ok=True)
    params.write_parquet(out_dir / "params" / "values.parquet", compression="zstd")
    span = con.execute("SELECT min(ts), max(ts) FROM events").fetchone() or (None, None)
    (out_dir / "meta.json").write_text(
        json.dumps({"ts_min": span[0], "ts_max": span[1]}, default=str)
    )
    return {"unique_lines": uniq.height, "templates": templates.height, "params": params.height}


def sample_params(
    con: duckdb.DuckDBPyConnection, templates: pl.DataFrame, settings: Settings
) -> pl.DataFrame:
    """Sample raw payloads of the top templates in DuckDB and extract their parameters."""
    top = templates.sort("count", descending=True).head(settings.template_top_k)
    wanted = settings.param_sample
    probs = top.select(
        "template_id",
        (pl.lit(wanted * SAMPLE_OVERSHOOT) / pl.col("count")).clip(upper_bound=1.0).alias("p"),
    )
    con.register("prob_arrow", probs.to_arrow())
    samples = con.execute(
        f"""SELECT a.template_id, e.payload
            FROM events e JOIN assign a USING (masked_hash) JOIN prob_arrow p USING (template_id)
            WHERE (hash(epoch_ms(e.ts), e.payload) % 1000000) < p.p * 1000000
            QUALIFY row_number() OVER (PARTITION BY a.template_id
                                       ORDER BY hash(epoch_ms(e.ts), e.payload)) <= {int(wanted)}"""
    ).pl()
    return params_frame(samples)


# ---------------------------------------------------------------------- pure helpers


def rare_templates(templates: pl.DataFrame, threshold: int) -> pl.DataFrame:
    return templates.filter(pl.col("count") <= threshold).sort("count", "template_id")


def novel_templates(
    templates: pl.DataFrame, ts_min: datetime, ts_max: datetime, fraction: float
) -> pl.DataFrame:
    """Templates first seen in the last ``fraction`` of the data span, earliest first."""
    cutoff = ts_max - (ts_max - ts_min) * fraction
    return templates.filter(pl.col("first_seen") > cutoff).sort("first_seen", "template_id")


def short(text: str, limit: int = TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def volume_figure(
    counts: pl.DataFrame,
    lo: datetime,
    hi: datetime,
    template_id: int | None,
    scheme: str | None,
) -> go.Figure:
    """Events per bin of one template over the visible range."""
    if counts.is_empty():
        return figures.empty_figure("no events for this template in the range", scheme)
    size = figures.choose_bin_seconds(lo.timestamp(), hi.timestamp())
    size = max(size, 60)
    binned = (
        counts.with_columns(pl.col("bin").dt.truncate(f"{size}s"))
        .group_by("bin")
        .agg(pl.col("n").sum())
    )
    full = figures.fill_bins(binned, lo.replace(second=0), hi, size)
    fig = go.Figure(go.Bar(x=full["bin"].to_list(), y=full["n"].to_list()))
    fig.update_yaxes(title=f"events per {size // 60} min")
    figures.add_note(fig, f"template {template_id}", scheme)
    return theme.style(fig, scheme, height=320)


def param_options(params: pl.DataFrame, template_id: int | None) -> list[dict[str, str]]:
    """Select options ``position|kind`` for the parameters of one template."""
    if template_id is None:
        return []
    part = params.filter(pl.col("template_id") == template_id)
    pairs = part.select("position", "kind").unique().sort("position")
    return [{"value": f"{p}|{k}", "label": f"#{p + 1} ({k})"} for p, k in pairs.iter_rows()]


def param_figure(
    params: pl.DataFrame, template_id: int | None, choice: str | None, scheme: str | None
) -> go.Figure:
    """Distribution of one parameter: log-spaced histogram for numbers, top values otherwise."""
    if not choice or "|" not in choice:
        return figures.empty_figure("this template has no extracted parameters", scheme)
    position, kind = choice.split("|", 1)
    part = params.filter(
        (pl.col("template_id") == template_id)
        & (pl.col("position") == int(position))
        & (pl.col("kind") == kind)
    )
    if part.is_empty():
        return figures.empty_figure("no sampled values", scheme)
    if kind == "NUM":
        nums = part.with_columns(pl.col("value").cast(pl.Float64, strict=False)).drop_nulls("value")
        fig = go.Figure(
            go.Histogram(
                x=nums["value"].to_list(), y=nums["n"].to_list(), histfunc="sum", nbinsx=40
            )
        )
        fig.update_xaxes(title="value")
        fig.update_yaxes(title="sampled lines")
    else:
        top = part.sort("n", descending=True).head(PARAM_BARS)
        fig = go.Figure(
            go.Bar(
                x=top["n"].to_list(),
                y=[figures.shorten_middle(str(v)) for v in top["value"]],
                orientation="h",
                customdata=top["value"].to_list(),
                hovertemplate="%{customdata}: %{x:,}<extra></extra>",
            )
        )
        fig.update_yaxes(autorange="reversed", type="category")
        fig.update_xaxes(title="sampled lines")
    figures.add_note(fig, f"parameter #{int(position) + 1} ({kind})", scheme)
    return theme.style(fig, scheme, height=320)


def grid_rows(templates: pl.DataFrame, limit: int = GRID_ROWS) -> list[dict[str, Any]]:
    top = templates.sort("count", descending=True).head(limit)
    return [
        {
            "template_id": r["template_id"],
            "template": short(r["template"]),
            "count": r["count"],
            "variants": r["n_variants"],
            "first_seen": f"{r['first_seen']:%Y-%m-%d %H:%M:%S}",
            "last_seen": f"{r['last_seen']:%Y-%m-%d %H:%M:%S}",
        }
        for r in top.iter_rows(named=True)
    ]


def list_table(df: pl.DataFrame) -> Component:
    """Template list (rare or novel) with an explorer link per row."""
    if df.is_empty():
        return dmc.Text("none")
    df = df.head(LIST_ROWS)
    shown = df.select(
        "template_id",
        pl.col("template").map_elements(short, return_dtype=pl.String),
        "count",
        pl.col("first_seen").dt.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return ui.link_table(shown, [page_url("/explorer", template=t) for t in df["template_id"]])


def selected_id(rows: list[dict[str, Any]] | None) -> int | None:
    return int(rows[0]["template_id"]) if rows else None


def volume_for(
    store: CacheStore, filters: Filters, template_id: int | None, scheme: str | None
) -> go.Figure:
    meta = store.read_json(KEY, "meta.json")
    lo = parse_time(filters.start) or datetime.fromisoformat(meta["ts_min"])
    hi = parse_time(filters.end) or datetime.fromisoformat(meta["ts_max"]) + timedelta(minutes=1)
    counts = (
        pl.scan_parquet(store.path(KEY, "template_counts.parquet"))
        .filter(
            (pl.col("template_id") == template_id) & (pl.col("bin") >= lo) & (pl.col("bin") < hi)
        )
        .collect()
    )
    counts = filter_frame(counts, filters, service="program")
    return volume_figure(counts, lo, hi, template_id, scheme)


@register
class Templates:
    key = KEY
    title = "Templates"
    version = "2"
    depends_on: tuple[str, ...] = ()
    setting_names = SETTING_NAMES

    def compute(self, ctx: ComputeContext) -> None:
        for name, count in compute_all(ctx.con, ctx.settings, ctx.out_dir, ctx.progress).items():
            ctx.note_rows(name, count)

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        settings = store.settings
        templates = store.read_parquet(KEY, "templates.parquet")
        meta = store.read_json(KEY, "meta.json")
        lo, hi = datetime.fromisoformat(meta["ts_min"]), datetime.fromisoformat(meta["ts_max"])
        rows = grid_rows(templates)
        grid = dag.AgGrid(
            id="templates-grid",
            rowData=rows,
            columnDefs=[
                {"field": "template_id", "headerName": "ID", "width": 80},
                {"field": "template", "flex": 1, "minWidth": 300},
                {
                    "field": "count",
                    "width": 120,
                    "valueFormatter": {"function": "d3.format(',')(params.value)"},
                },
                {"field": "variants", "width": 100},
                {"field": "first_seen", "width": 170},
                {"field": "last_seen", "width": 170},
            ],
            defaultColDef={"sortable": True, "resizable": True},
            dashGridOptions={
                "rowSelection": {
                    "mode": "singleRow",
                    "checkboxes": False,
                    "enableClickSelection": True,
                }
            },
            selectedRows=rows[:1],
            style={"height": 380},
        )
        return ui.page(
            "Log templates",
            ui.card("Top templates (click a row)", grid),
            ui.grid(
                [
                    ui.card(
                        "Selected template volume",
                        dmc.Anchor(
                            "view in explorer", id="templates-link", href="/explorer", refresh=False
                        ),
                        ui.graph("templates-volume", 320),
                    ),
                    ui.card(
                        "Parameter distribution",
                        dmc.Select(id="templates-param", data=[], size="xs", w=240, mb="xs"),
                        ui.graph("templates-params", 320),
                    ),
                ],
                cols={"base": 1, "lg": 2},
            ),
            ui.card(
                f"Rare templates (at most {settings.rare_threshold} events)",
                list_table(rare_templates(templates, settings.rare_threshold)),
            ),
            ui.card(
                f"Novel templates (first seen in the last {settings.novel_fraction:.0%})",
                list_table(novel_templates(templates, lo, hi, settings.novel_fraction)),
            ),
            description=(
                "Drain3 over unique masked lines. Service and time filters apply to the volume "
                "chart; the tables are not filtered."
            ),
        )

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        @app.callback(
            Output("templates-volume", "figure"),
            Output("templates-link", "href"),
            Input("templates-grid", "selectedRows"),
            Input(FILTER_STORE_ID, "data"),
            theme.theme_input(),
        )
        def draw_volume(
            rows: list[dict[str, Any]] | None, filters: dict[str, Any] | None, theme: str | None
        ) -> tuple[go.Figure, str]:
            tid = selected_id(rows)
            return volume_for(store, from_dict(filters), tid, theme), page_url(
                "/explorer", template=tid
            )

        @app.callback(
            Output("templates-param", "data"),
            Output("templates-param", "value"),
            Input("templates-grid", "selectedRows"),
        )
        def pick_params(
            rows: list[dict[str, Any]] | None,
        ) -> tuple[list[dict[str, str]], str | None]:
            options = param_options(self.params(store), selected_id(rows))
            return options, options[0]["value"] if options else None

        @app.callback(
            Output("templates-params", "figure"),
            Input("templates-param", "value"),
            Input("templates-grid", "selectedRows"),
            theme.theme_input(),
        )
        def draw_params(
            choice: str | None, rows: list[dict[str, Any]] | None, theme: str | None
        ) -> go.Figure:
            return param_figure(self.params(store), selected_id(rows), choice, theme)

    @staticmethod
    def params(store: CacheStore) -> pl.DataFrame:
        return pl.read_parquet(store.path(KEY, "params") / "values.parquet")
