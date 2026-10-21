"""Fingerprints and the manifest that records which stages are built and from what."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHUNK = 1 << 20


@dataclass(frozen=True)
class Entry:
    """One built stage: its fingerprint, module version, build time and output row counts."""

    fingerprint: str
    version: str
    created_at: str
    duration_s: float
    rows: dict[str, int] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def digest_of(value: Any) -> str:
    """Stable hash of a JSON-serialisable value (key order does not matter)."""
    text = json.dumps(value, sort_keys=True, default=str)
    return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def file_fingerprint(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Name and size plus mtime, or with ``strict`` a content hash instead of the mtime."""
    stat = path.stat()
    info: dict[str, Any] = {"name": path.name, "size": stat.st_size}
    if not strict:
        info["mtime_ns"] = stat.st_mtime_ns
        return info
    hasher = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            hasher.update(chunk)
    info["blake2b"] = hasher.hexdigest()
    return info


def data_fingerprint(files: Sequence[Path], *, strict: bool = False) -> str:
    return digest_of([file_fingerprint(f, strict=strict) for f in files])


def stage_fingerprint(
    *, version: str, data_fp: str, params: dict[str, Any], upstream: dict[str, str]
) -> str:
    """Hash of everything a stage's output depends on."""
    return digest_of({"version": version, "data": data_fp, "params": params, "upstream": upstream})


class Manifest:
    """``manifest.json``: stage key to ``Entry``."""

    def __init__(self, path: Path, entries: dict[str, Entry] | None = None) -> None:
        self.path = path
        self.entries: dict[str, Entry] = entries or {}

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.exists():
            return cls(path)
        raw = json.loads(path.read_text())
        return cls(path, {k: Entry(**v) for k, v in raw.items()})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in self.entries.items()}
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def get(self, key: str) -> Entry | None:
        return self.entries.get(key)

    def set(self, key: str, entry: Entry) -> None:
        self.entries[key] = entry

    def remove(self, key: str) -> None:
        self.entries.pop(key, None)

    def is_stale(self, key: str, fingerprint: str) -> bool:
        entry = self.entries.get(key)
        return entry is None or entry.fingerprint != fingerprint
