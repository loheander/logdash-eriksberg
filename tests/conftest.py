from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

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
