from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from logdash import config
from logdash.analyses.base import Analysis, Registry
from logdash.analyses.forecast import Forecast
from logdash.analyses.hosts import Hosts
from logdash.analyses.templates import Templates
from logdash.analyses.temporal import Temporal
from logdash.build import run_build
from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.tagging import openrouter
from tests.synthetic import GroundTruth, SyntheticSpec, write_dataset


@dataclass(frozen=True)
class SyntheticData:
    data_dir: Path
    csv_path: Path
    truth: GroundTruth


@pytest.fixture(scope="session")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> SyntheticData:
    """Session-wide synthetic dataset in a temporary data directory (read-only use)."""
    data_dir = tmp_path_factory.mktemp("data")
    path = data_dir / "synthetic_preprocessed.csv.zst"
    truth = write_dataset(path, SyntheticSpec())
    return SyntheticData(data_dir, path, truth)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@dataclass(frozen=True)
class Built:
    settings: Settings
    store: CacheStore


@pytest.fixture(scope="session")
def built(synthetic: SyntheticData, tmp_path_factory: pytest.TempPathFactory) -> Built:
    """Session-wide cache with the ``events`` stage built from the synthetic data."""
    settings = Settings(synthetic.data_dir, tmp_path_factory.mktemp("cache"), "1GB", 2)
    run_build(settings, registry=Registry(), log=lambda _: None)
    return Built(settings, CacheStore(settings.cache_dir, settings))


def build_with(built: Built, *analyses: Analysis) -> Registry:
    """Add analyses to a fresh registry and build them into the shared session cache."""
    registry = Registry()
    for analysis in analyses:
        registry.add(analysis)
    run_build(built.settings, registry=registry, log=lambda _: None)
    return registry


# error_rows=0: the ERROR burst is a host volume spike of its own, and the checks on the forecast
# stage say that every spike minute belongs to an injected spike
FORECAST_SPEC = SyntheticSpec(forecast_spikes=24, error_rows=0)


@pytest.fixture(scope="session")
def forecast_cache(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Built, Registry, GroundTruth]:
    """A cache of a dataset of its own: injected spikes with precursors (shared data has none)."""
    data_dir = tmp_path_factory.mktemp("forecast_data")
    truth = write_dataset(data_dir / "synthetic_preprocessed.csv.zst", FORECAST_SPEC)
    settings = Settings(data_dir, tmp_path_factory.mktemp("forecast_cache"), "1GB", 2)
    run_build(settings, registry=Registry(), log=lambda _: None)
    built = Built(settings, CacheStore(settings.cache_dir, settings))
    registry = build_with(built, Temporal(), Templates(), Hosts(), Forecast())
    return built, registry, truth


@pytest.fixture(scope="session", autouse=True)
def _runs_in_tmp(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Forecast runs and the background-job cache go to a temporary directory, never ./runs.

    Session scope: module and session fixtures build ``Settings`` before function fixtures run.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "DEFAULT_RUNS", str(tmp_path_factory.getbasetemp() / "runs"))
    mp.delenv("LOGDASH_RUNS", raising=False)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _no_model_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Never read the developer's .env or reach OpenRouter from a test."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LOGDASH_TAG_MODEL", raising=False)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path_factory.getbasetemp() / "no.env")

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("tests must not call OpenRouter")

    monkeypatch.setattr(openrouter, "post_json", refuse)
