from logdash.build import run_build
from logdash.cache.store import CacheStore
from logdash.cli import main
from logdash.config import Settings


def settings_for(synthetic, cache):
    return Settings(synthetic.data_dir, cache, "1GB", 2)


def test_build_then_fresh_then_force(synthetic, cache_dir):
    s = settings_for(synthetic, cache_dir)
    logs: list[str] = []
    assert [r.status for r in run_build(s, log=logs.append)] == ["built"]
    entry = CacheStore(cache_dir).manifest().get("events")
    assert entry is not None and entry.rows["rows"] == synthetic.truth.n_rows
    assert [r.status for r in run_build(s, log=logs.append)] == ["fresh"]
    assert [r.status for r in run_build(s, force=True, log=logs.append)] == ["built"]
    assert any("up to date" in line for line in logs)


def test_strict_mode_rebuilds_on_changed_fingerprint(synthetic, cache_dir):
    s = settings_for(synthetic, cache_dir)
    run_build(s, log=lambda _: None)
    assert [r.status for r in run_build(s, strict=True, log=lambda _: None)] == ["built"]


def test_cli_build_status_serve(synthetic, cache_dir, capsys):
    common = ["--data", str(synthetic.data_dir), "--cache", str(cache_dir), "--threads", "2"]
    assert main(["status", *common]) == 0
    assert "missing" in capsys.readouterr().out
    assert main(["build", *common, "--memory-limit", "1GB"]) == 0
    assert main(["status", *common]) == 0
    assert "fresh" in capsys.readouterr().out
    (synthetic.data_dir / "other.csv.zst").write_bytes(b"x")
    try:
        assert main(["status", *common]) == 0
        assert "stale" in capsys.readouterr().out
    finally:
        (synthetic.data_dir / "other.csv.zst").unlink()


def test_cli_without_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out


def test_version_flag(capsys):
    import pytest

    from logdash import __version__

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
