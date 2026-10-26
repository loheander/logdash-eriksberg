from pathlib import Path

import pytest

from logdash import config
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


@pytest.mark.parametrize("bad", ["0", "1", "-0.1", "1.5"])
def test_novel_fraction_must_be_between_zero_and_one(bad):
    with pytest.raises(ValueError, match="novel_fraction must be between 0 and 1"):
        Settings.from_env({"LOGDASH_NOVEL_FRACTION": bad})
    with pytest.raises(ValueError, match="novel_fraction"):
        Settings().with_overrides(novel_fraction=float(bad))


def test_tag_model_from_env_and_default():
    from logdash.config import DEFAULT_TAG_MODEL

    assert Settings.from_env({}).tag_model == DEFAULT_TAG_MODEL
    assert Settings.from_env({"LOGDASH_TAG_MODEL": "x/y"}).tag_model == "x/y"
    assert Settings.from_env({"LOGDASH_TAG_MODEL": ""}).tag_model == DEFAULT_TAG_MODEL


def test_key_from_env_and_dotenv(tmp_path, monkeypatch):
    from logdash import config

    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path / "missing.env")
    assert Settings.from_env().openrouter_api_key is None
    assert not Settings.from_env().tagging_enabled
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=sk-secret\n")
    monkeypatch.setattr(config, "DOTENV_PATH", dotenv)
    s = Settings.from_env()
    assert s.openrouter_api_key == "sk-secret" and s.tagging_enabled
    assert "sk-secret" not in repr(s)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    assert Settings.from_env().openrouter_api_key == "sk-env"
    assert Settings.from_env({}).openrouter_api_key is None  # explicit env: no .env


def test_read_dotenv_parsing(tmp_path):
    from logdash.config import read_dotenv

    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nA=1\nexport B = two \nC='quoted'\nD=\"dq\"\nno equals here\nE=\nF='\n"
    )
    assert read_dotenv(path) == {"A": "1", "B": "two", "C": "quoted", "D": "dq", "E": "", "F": "'"}


def test_runs_dir_from_env_and_default(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_RUNS", "runs")
    assert Settings().runs_dir == Path("runs") == Settings.from_env({}).runs_dir
    assert Settings.from_env({"LOGDASH_RUNS": "/r"}).runs_dir == Path("/r")
    assert Settings().with_overrides(runs_dir="x").runs_dir == Path("x")
    assert "runs_dir" not in Settings().analysis_params(("burst_k",))
