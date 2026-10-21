import pytest

from logdash.cache.manifest import (
    Entry,
    Manifest,
    data_fingerprint,
    digest_of,
    file_fingerprint,
    stage_fingerprint,
)


def test_file_fingerprint_modes(tmp_path):
    f = tmp_path / "a.csv.zst"
    f.write_bytes(b"abc")
    fast = file_fingerprint(f)
    strict = file_fingerprint(f, strict=True)
    assert fast["size"] == 3 and "mtime_ns" in fast and "blake2b" not in fast
    assert "blake2b" in strict and "mtime_ns" not in strict


def test_data_fingerprint_tracks_changes(tmp_path):
    f = tmp_path / "a.csv.zst"
    f.write_bytes(b"abc")
    before = data_fingerprint([f], strict=True)
    assert data_fingerprint([f], strict=True) == before
    f.write_bytes(b"abd")
    assert data_fingerprint([f], strict=True) != before


def test_strict_ignores_mtime(tmp_path):
    import os

    f = tmp_path / "a.csv.zst"
    f.write_bytes(b"abc")
    before_strict, before_fast = data_fingerprint([f], strict=True), data_fingerprint([f])
    os.utime(f, ns=(1, 1))
    assert data_fingerprint([f], strict=True) == before_strict
    assert data_fingerprint([f]) != before_fast


def base(**over):
    args = {"version": "1", "data_fp": "d", "params": {"a": 1}, "upstream": {"events": "x"}}
    return stage_fingerprint(**{**args, **over})


@pytest.mark.parametrize(
    "change",
    [
        {"version": "2"},
        {"data_fp": "e"},
        {"params": {"a": 2}},
        {"upstream": {"events": "y"}},
    ],
)
def test_stage_fingerprint_sensitive(change):
    assert base() == base()
    assert base(**change) != base()


def test_digest_is_order_independent():
    assert digest_of({"a": 1, "b": 2}) == digest_of({"b": 2, "a": 1})


def test_manifest_roundtrip_and_staleness(tmp_path):
    path = tmp_path / "sub" / "manifest.json"
    assert Manifest.load(path).entries == {}
    m = Manifest(path)
    assert m.is_stale("k", "fp")
    m.set("k", Entry("fp", "1", "now", 1.5, {"rows": 3}))
    m.save()
    loaded = Manifest.load(path)
    assert loaded.get("k") == m.get("k")
    assert not loaded.is_stale("k", "fp") and loaded.is_stale("k", "other")
    loaded.remove("k")
    loaded.remove("k")
    assert loaded.get("k") is None
