"""Run directories: ``<runs_dir>/<run_id>/{run.json, scores.npz}``, written once, never changed."""

from __future__ import annotations

import functools
import json
import re
import secrets
import shutil
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

RUN_FILE = "run.json"
SCORES_FILE = "scores.npz"
JOBS_DIR = "jobs"  # the DiskcacheManager's directory under runs_dir
TMP_PREFIX = ".tmp-"
PARTIAL_MAX_AGE_S = 3600
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")


def score_key(model: str) -> str:
    return f"score_{model}"


def new_run_id(now: datetime | None = None) -> str:
    """``YYYYMMDDTHHMMSSZ-xxxxxx``: UTC time and 6 random hex digits."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def valid_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None


def write_run(
    runs_dir: Path, run_id: str, info: Mapping[str, Any], arrays: Mapping[str, np.ndarray]
) -> Path:
    """Write ``<runs_dir>/<run_id>/`` atomically: a temporary directory, then one rename."""
    if not valid_run_id(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    final = runs_dir / run_id
    if final.exists():
        raise FileExistsError(str(final))
    tmp = runs_dir / f"{TMP_PREFIX}{run_id}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        np.savez_compressed(tmp / SCORES_FILE, **arrays)  # type: ignore[arg-type]  # numpy stubs
        (tmp / RUN_FILE).write_text(json.dumps(info, indent=2, allow_nan=False))
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def cleanup_partial(
    runs_dir: Path, max_age_s: float = PARTIAL_MAX_AGE_S, now: float | None = None
) -> None:
    """Remove ``.tmp-*`` directories older than ``max_age_s`` (left by killed jobs)."""
    if not runs_dir.is_dir():
        return
    now = time.time() if now is None else now
    for path in runs_dir.glob(f"{TMP_PREFIX}*"):
        if path.is_dir() and now - path.stat().st_mtime > max_age_s:
            shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True)
class Run:
    run_id: str
    info: dict[str, Any]  # run.json
    arrays: dict[str, np.ndarray]  # every array of scores.npz, materialised

    @property
    def labels(self) -> np.ndarray:
        return self.arrays["labels"]

    @property
    def models(self) -> list[str]:
        return list(self.info["models"])

    def scores(self, model: str) -> np.ndarray:
        return self.arrays[score_key(model)]


@functools.lru_cache(maxsize=8)
def _read(path: str) -> Run:
    info = json.loads(Path(path, RUN_FILE).read_text())
    with np.load(Path(path, SCORES_FILE), allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    for key in ("labels", *(score_key(m) for m in info["models"])):
        if key not in arrays:
            raise KeyError(key)
    return Run(Path(path).name, info, arrays)


def load_run(runs_dir: Path, run_id: str | None) -> Run | None:
    """The run, memoised (runs never change); None when the id is invalid or the run is
    missing or unreadable. Misses are not memoised."""
    if not valid_run_id(run_id):
        return None
    try:
        return _read(str(runs_dir / str(run_id)))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None


def clear_caches() -> None:
    _read.cache_clear()
