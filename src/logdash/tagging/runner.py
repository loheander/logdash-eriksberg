"""Tag many templates with retries, a worker pool and a memo of valid answers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from logdash.cache.manifest import digest_of
from logdash.tagging.model import (
    PROMPT_VERSION,
    FatalError,
    InvalidAnswer,
    Tag,
    TagModel,
    TransientError,
)

MAX_ATTEMPTS = 3
BACKOFF_S = 2.0
WORKERS = 4
PROGRESS_EVERY = 25
MEMO_SCHEMA = {
    "key": pl.String,
    "actionable": pl.Boolean,
    "component": pl.String,
    "noise": pl.Boolean,
    "summary": pl.String,
}


def memo_key(model_id: str, template: str) -> str:
    return digest_of([PROMPT_VERSION, model_id, template])


def load_memo(path: Path, log: Callable[[str], None] = print) -> dict[str, Tag]:
    """Missing file -> {}; unreadable or wrong columns -> {} and one log line."""
    if not path.exists():
        return {}
    try:
        frame = pl.read_parquet(path)
        if frame.schema != pl.Schema(MEMO_SCHEMA):
            raise ValueError("unexpected columns")
        return {
            r["key"]: Tag(r["actionable"], r["component"], r["noise"], r["summary"])
            for r in frame.iter_rows(named=True)
        }
    except Exception as exc:
        log(f"tags: ignoring unreadable memo ({exc})")
        return {}


def save_memo(path: Path, memo: Mapping[str, Tag]) -> None:
    """Rows sorted by key; write path.with_suffix('.tmp') then replace; mkdir parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "key": k,
            "actionable": t.actionable,
            "component": t.component,
            "noise": t.noise,
            "summary": t.summary,
        }
        for k, t in sorted(memo.items())
    ]
    tmp = path.with_suffix(".tmp")
    pl.DataFrame(rows, schema=MEMO_SCHEMA).write_parquet(tmp)
    tmp.replace(path)


@dataclass
class Outcome:
    tags: dict[str, Tag]  # by template text, memo hits included
    failed: dict[str, str]  # template text -> reason
    calls: int = 0  # model.tag invocations
    aborted: str | None = None  # first FatalError message


def tag_templates(
    texts: Sequence[str],
    model: TagModel,
    memo: dict[str, Tag],
    model_id: str,
    *,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    workers: int = WORKERS,
) -> Outcome:
    """Tag the unique texts; memo hits cost nothing. Never raises for a model failure."""
    unique = list(dict.fromkeys(texts))
    out = Outcome({}, {})
    misses: list[str] = []
    for text in unique:
        hit = memo.get(memo_key(model_id, text))
        if hit is None:
            misses.append(text)
        else:
            out.tags[text] = hit
    if not misses:
        return out

    abort = threading.Event()
    lock = threading.Lock()
    done = 0

    def finish() -> None:
        nonlocal done
        with lock:
            done += 1
            if done % PROGRESS_EVERY == 0:
                log(f"tagged {done}/{len(misses)}")

    def tag_one(text: str) -> None:
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if abort.is_set():
                with lock:
                    out.failed[text] = f"aborted: {out.aborted}"
                return
            with lock:
                out.calls += 1
            try:
                tag = model.tag(text)
            except InvalidAnswer as exc:
                last = exc
            except TransientError as exc:
                last = exc
                sleep(BACKOFF_S * attempt)
            except FatalError as exc:
                with lock:
                    if out.aborted is None:
                        out.aborted = str(exc)
                    out.failed[text] = f"{type(exc).__name__}: {exc}"
                abort.set()
                return
            except Exception as exc:
                with lock:
                    out.failed[text] = repr(exc)
                return
            else:
                with lock:
                    memo[memo_key(model_id, text)] = tag
                    out.tags[text] = tag
                return
        with lock:
            out.failed[text] = f"{type(last).__name__}: {last}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(_guarded, tag_one, finish, t) for t in misses]:
            future.result()
    return out


def _guarded(fn: Callable[[str], None], finish: Callable[[], None], text: str) -> None:
    try:
        fn(text)
    finally:
        finish()
