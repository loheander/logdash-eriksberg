"""Cache manifest: per-stage fingerprints, timings and row counts, plus staleness checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import logdash

CHUNK = 8 * 1024 * 1024


@dataclass
class Entry:
    """Manifest record of one build stage."""

    fingerprint: str
    version: str
    created_at: str
    duration_s: float
    rows: dict[str, int] = field(default_factory=dict)


def file_fingerprint(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Identify a data file by name, size and mtime, or by content hash when strict."""
    stat = path.stat()
    info: dict[str, Any] = {"name": path.name, "size": stat.st_size}
    if strict:
        info["blake2b"] = hash_file(path)
    else:
        info["mtime_ns"] = stat.st_mtime_ns
    return info


def hash_file(path: Path) -> str:
    digest = hashlib.blake2b()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def data_fingerprint(files: Iterable[Path], *, strict: bool = False) -> str:
    return digest_of([file_fingerprint(f, strict=strict) for f in sorted(files)])


def digest_of(obj: Any) -> str:
    """Stable sha256 of a JSON-serialisable object."""
    blob = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def stage_fingerprint(
    *,
    version: str,
    data_fp: str,
    params: Mapping[str, Any],
    upstream: Mapping[str, str],
) -> str:
    """Fingerprint of one stage from its inputs, settings, version and upstream stages."""
    return digest_of(
        {
            "package": logdash.__version__,
            "version": version,
            "data": data_fp,
            "params": dict(params),
            "upstream": dict(upstream),
        }
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Manifest:
    """JSON-backed mapping from stage key to ``Entry``."""

    def __init__(self, path: Path, entries: dict[str, Entry] | None = None) -> None:
        self.path = path
        self.entries: dict[str, Entry] = entries or {}

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.exists():
            return cls(path)
        raw = json.loads(path.read_text())
        return cls(path, {k: Entry(**v) for k, v in raw.get("stages", {}).items()})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"stages": {k: asdict(v) for k, v in sorted(self.entries.items())}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)

    def get(self, key: str) -> Entry | None:
        return self.entries.get(key)

    def set(self, key: str, entry: Entry) -> None:
        self.entries[key] = entry

    def remove(self, key: str) -> None:
        self.entries.pop(key, None)

    def is_stale(self, key: str, fingerprint: str) -> bool:
        entry = self.entries.get(key)
        return entry is None or entry.fingerprint != fingerprint
