"""Module: semantic template tags (a language model labels each template once).

Runs after ``templates`` at build time only (ADR001). Answers are memoised by template
text in ``cache/tags_memo`` (outside this stage's directory, which is wiped on every
rebuild), so an unchanged template is never sent to the model twice. Without
``OPENROUTER_API_KEY`` the stage records ``available: false`` and writes no tags.
Later pages read the tags with ``read_tags`` and ``read_meta``; nothing here is shown
on a page and the dashboard never calls the model.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import polars as pl

from logdash.analyses import templates as tpl
from logdash.analyses.base import ComputeContext, register
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.tagging.model import PROMPT_VERSION, FatalError, Tag, TagModel
from logdash.tagging.openrouter import OpenRouterModel
from logdash.tagging.runner import load_memo, save_memo, tag_templates

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

KEY = "tags"
FILE = "tags.parquet"
META = "meta.json"
MEMO_KEY = "tags_memo"
MEMO_FILE = "memo.parquet"
MAX_FAILURES_KEPT = 20
TAGS_SCHEMA = {
    "template_id": pl.Int64,
    "template": pl.String,
    "tagged": pl.Boolean,
    "actionable": pl.Boolean,
    "component": pl.String,
    "noise": pl.Boolean,
    "summary": pl.String,
}
ModelFactory = Callable[[Settings], TagModel]


def openrouter_factory(settings: Settings) -> TagModel:
    if not settings.openrouter_api_key:
        raise FatalError("no OPENROUTER_API_KEY")
    return OpenRouterModel(settings.openrouter_api_key, settings.tag_model)


def tags_frame(templates: pl.DataFrame, tags: Mapping[str, Tag]) -> pl.DataFrame:
    """One row per template by template_id; untagged rows have null tags and tagged False."""
    rows = []
    for tid, text in templates.sort("template_id").select("template_id", "template").iter_rows():
        tag = tags.get(text)
        rows.append(
            {
                "template_id": tid,
                "template": text,
                "tagged": tag is not None,
                "actionable": tag.actionable if tag else None,
                "component": tag.component if tag else None,
                "noise": tag.noise if tag else None,
                "summary": tag.summary if tag else None,
            }
        )
    return pl.DataFrame(rows, schema=TAGS_SCHEMA)


def read_meta(store: CacheStore) -> dict[str, Any]:
    """meta.json, or {"available": False, "reason": "not built"} if missing."""
    if not store.exists(KEY, META):
        return {"available": False, "reason": "not built"}
    meta: dict[str, Any] = store.read_json(KEY, META)
    return meta


def read_tags(store: CacheStore) -> pl.DataFrame | None:
    """tags.parquet, or None when tags are absent (no key, not built). Reads the cache only."""
    if not store.exists(KEY, FILE):
        return None
    return store.read_parquet(KEY, FILE)


@register
class Tags:
    key = KEY
    title = "Semantic tags"
    version = f"1:{PROMPT_VERSION}"
    depends_on: tuple[str, ...] = (tpl.KEY,)
    setting_names = ("tag_model", "tagging_enabled")
    page = False
    calls_model = True

    def __init__(
        self,
        model_factory: ModelFactory = openrouter_factory,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_factory = model_factory
        self.sleep = sleep

    def compute(self, ctx: ComputeContext) -> None:
        store = CacheStore(ctx.out_dir.parent, ctx.settings)
        if not ctx.settings.tagging_enabled:
            ctx.progress("skipped (no OPENROUTER_API_KEY)")
            store.write_json(KEY, META, {"available": False, "reason": "no OPENROUTER_API_KEY"})
            ctx.note_rows("tagged", 0)
            return
        templates = store.read_parquet(tpl.KEY, "templates.parquet").select(
            "template_id", "template"
        )
        memo_path = store.path(MEMO_KEY, MEMO_FILE)
        memo = load_memo(memo_path, ctx.progress)
        try:
            outcome = tag_templates(
                templates["template"].to_list(),
                self.model_factory(ctx.settings),
                memo,
                ctx.settings.tag_model,
                log=ctx.progress,
                sleep=self.sleep,
            )
        finally:
            save_memo(memo_path, memo)
        frame = tags_frame(templates, outcome.tags)
        store.write_parquet(KEY, FILE, frame)
        n = frame.height
        tagged = int(frame["tagged"].sum())
        failures = [
            {"template_id": tid, "reason": outcome.failed.get(text, "unknown")}
            for tid, text, ok in frame.select("template_id", "template", "tagged").iter_rows()
            if not ok
        ][:MAX_FAILURES_KEPT]
        store.write_json(
            KEY,
            META,
            {
                "available": True,
                "model": ctx.settings.tag_model,
                "prompt_version": PROMPT_VERSION,
                "templates": n,
                "tagged": tagged,
                "untagged": n - tagged,
                "calls": outcome.calls,
                "aborted": outcome.aborted,
                "failures": failures,
            },
        )
        ctx.note_rows("tagged", tagged)
        ctx.note_rows("untagged", n - tagged)
        ctx.note_rows("model_calls", outcome.calls)
        ctx.progress(f"tagged {tagged} of {n} templates, {outcome.calls} model call(s)")
        if n - tagged:
            ctx.progress("run `logdash build --only tags --force` to retry untagged templates")

    def layout(self, store: CacheStore, **_query: Any) -> Component:
        raise RuntimeError("the tags stage has no page")

    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        """No page, no callbacks: the dashboard never calls the model."""
