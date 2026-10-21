import polars as pl
import pytest

from logdash.cache.store import CacheStore
from logdash.config import Settings
from logdash.ingest.reader import ingest


def test_roundtrips(tmp_path):
    store = CacheStore(tmp_path / "cache")
    df = pl.DataFrame({"a": [1, 2, 3]})
    store.write_parquet("mod", "sub/x.parquet", df)
    assert store.exists("mod", "sub/x.parquet")
    assert store.read_parquet("mod", "sub/x.parquet").equals(df)
    assert store.count_parquet_rows("mod") == {"sub/x.parquet": 3}
    store.write_json("mod", "m.json", {"k": [1, 2]})
    assert store.read_json("mod", "m.json") == {"k": [1, 2]}
    assert not store.exists("mod", "nope")


def test_reset_module(tmp_path):
    store = CacheStore(tmp_path)
    store.write_json("mod", "m.json", {})
    out = store.reset_module("mod")
    assert out.exists() and list(out.iterdir()) == []


def test_connect_requires_events(tmp_path):
    with pytest.raises(FileNotFoundError):
        CacheStore(tmp_path).connect()


def test_connect_events_view(synthetic, tmp_path):
    settings = Settings(synthetic.data_dir, tmp_path, "1GB", 2)
    store = CacheStore(tmp_path, settings)
    ingest(settings, store.events_dir)
    con = store.connect()
    assert con.cursor().execute("SELECT count(*) FROM events").fetchone()[0] == (
        synthetic.truth.n_rows
    )
    assert con.execute("SELECT min(hour) FROM events").fetchone()[0] == "00"
