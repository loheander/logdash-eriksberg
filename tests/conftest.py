from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from logdash.analyses.base import Analysis, Registry
from logdash.build import run_build
from logdash.cache.store import CacheStore
from logdash.config import Settings
from tests.synthetic import GroundTruth, SyntheticSpec, write_dataset


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches OpenRouter: the key is unset and any real HTTP request fails.

    Tests of the Jev client and the triage page pass a fake poster or opener instead.
    """
    import urllib.request

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a test tried to make a real HTTP request")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


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
