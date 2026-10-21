from pathlib import Path

from logdash.config import Settings


def test_defaults_from_empty_env():
    s = Settings.from_env({})
    assert (s.data_dir, s.cache_dir) == (Path("data"), Path("cache"))
    assert (s.memory_limit, s.threads) == ("8GB", 8)


def test_from_env():
    s = Settings.from_env(
        {
            "LOGDASH_DATA": "/d",
            "LOGDASH_CACHE": "/c",
            "LOGDASH_MEMORY_LIMIT": "2GB",
            "LOGDASH_THREADS": "3",
        }
    )
    assert s == Settings(Path("/d"), Path("/c"), "2GB", 3)


def test_overrides_ignore_none_and_coerce_paths():
    s = Settings().with_overrides(data_dir="x", cache_dir=None, threads=2)
    assert s.data_dir == Path("x") and s.cache_dir == Path("cache") and s.threads == 2
    assert Settings().with_overrides(cache_dir="y").cache_dir == Path("y")


def test_data_files_sorted(tmp_path):
    for name in ("b.csv.zst", "a.csv.zst", "c.txt"):
        (tmp_path / name).touch()
    files = Settings(data_dir=tmp_path).data_files()
    assert [f.name for f in files] == ["a.csv.zst", "b.csv.zst"]


def test_fingerprint_params_ignore_resources():
    assert Settings(threads=1).fingerprint_params() == Settings(threads=9).fingerprint_params()
