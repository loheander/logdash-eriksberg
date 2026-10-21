"""Analysis contract, compute context and registry with topological ordering."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import duckdb

from logdash.config import Settings

if TYPE_CHECKING:
    from dash import Dash
    from dash.development.base_component import Component

    from logdash.cache.store import CacheStore

RESERVED_KEYS = frozenset({"events"})


@runtime_checkable
class Analysis(Protocol):
    """One dashboard page: precompute in ``compute``, render from the cache afterwards."""

    key: str  # cache subdirectory and URL path
    title: str  # navigation label
    version: str  # bump to invalidate this module's cache
    depends_on: tuple[str, ...]  # upstream analyses

    def compute(self, ctx: ComputeContext) -> None: ...

    def layout(self, store: CacheStore) -> Component: ...

    def register_callbacks(self, app: Dash, store: CacheStore) -> None: ...


@dataclass
class ComputeContext:
    """Everything ``Analysis.compute`` needs: events view, settings, output dir, progress."""

    key: str
    con: duckdb.DuckDBPyConnection
    settings: Settings
    out_dir: Path
    log: Callable[[str], None] = print
    rows: dict[str, int] = field(default_factory=dict)

    def progress(self, message: str) -> None:
        self.log(f"{self.key}: {message}")

    def note_rows(self, name: str, count: int) -> None:
        """Record an extra row count (besides Parquet files) in the manifest."""
        self.rows[name] = count


class RegistryError(Exception):
    """Base class for registry problems."""


class DuplicateKeyError(RegistryError):
    pass


class MissingDependencyError(RegistryError):
    pass


class DependencyCycleError(RegistryError):
    pass


class UnknownAnalysisError(RegistryError):
    pass


class Registry:
    """Ordered collection of analyses keyed by ``Analysis.key``."""

    def __init__(self) -> None:
        self._items: dict[str, Analysis] = {}

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def add(self, analysis: Analysis) -> Analysis:
        if analysis.key in RESERVED_KEYS:
            raise DuplicateKeyError(f"{analysis.key!r} is reserved")
        if analysis.key in self._items:
            raise DuplicateKeyError(f"analysis {analysis.key!r} already registered")
        self._items[analysis.key] = analysis
        return analysis

    def get(self, key: str) -> Analysis:
        try:
            return self._items[key]
        except KeyError:
            raise UnknownAnalysisError(f"unknown analysis {key!r}") from None

    def clear(self) -> None:
        self._items.clear()

    def ordered(self, keys: Iterable[str] | None = None) -> list[Analysis]:
        """Analyses in dependency order (registration order breaks ties).

        With ``keys``, return only those analyses and their transitive dependencies.
        """
        wanted = self._closure(keys) if keys is not None else set(self._items)
        for key in wanted:
            for dep in self._items[key].depends_on:
                if dep not in self._items:
                    raise MissingDependencyError(f"{key!r} depends on unknown {dep!r}")
        return [self._items[k] for k in self._toposort(wanted)]

    def _closure(self, keys: Iterable[str]) -> set[str]:
        seen: set[str] = set()
        stack = [self.get(k).key for k in keys]
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            for dep in self._items[key].depends_on:
                if dep not in self._items:
                    raise MissingDependencyError(f"{key!r} depends on unknown {dep!r}")
                stack.append(dep)
        return seen

    def _toposort(self, wanted: set[str]) -> list[str]:
        order: list[str] = []
        state: dict[str, int] = {}  # 1 = visiting, 2 = done

        def visit(key: str, path: tuple[str, ...]) -> None:
            if state.get(key) == 2:
                return
            if state.get(key) == 1:
                cycle = " -> ".join((*path[path.index(key) :], key))
                raise DependencyCycleError(f"dependency cycle: {cycle}")
            state[key] = 1
            for dep in self._items[key].depends_on:
                visit(dep, (*path, key))
            state[key] = 2
            order.append(key)

        for key in self._items:
            if key in wanted:
                visit(key, ())
        return order


REGISTRY = Registry()


def register[T](cls: type[T]) -> type[T]:
    """Class decorator: instantiate the analysis and add it to the global registry."""
    REGISTRY.add(cls())  # type: ignore[arg-type]
    return cls
