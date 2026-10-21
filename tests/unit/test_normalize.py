import duckdb
import pytest

from logdash.ingest.normalize import (
    LEVEL_MAP,
    LEVELS,
    level_sql,
    normalize_level,
    strip_host_domain,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("info", "INFO"),
        ("INFO", "INFO"),
        ("warn", "WARNING"),
        ("WARN", "WARNING"),
        ("Warning", "WARNING"),
        (" error ", "ERROR"),
        ("CRITICAL", "CRITICAL"),
        ("debug", "DEBUG"),
        ("", "UNKNOWN"),
        (None, "UNKNOWN"),
        ("weird", "UNKNOWN"),
    ],
)
def test_normalize_level(raw, expected):
    assert normalize_level(raw) == expected
    assert expected in LEVELS


def test_level_sql_agrees_with_python():
    con = duckdb.connect()
    for raw in [*LEVEL_MAP, "WARN", "Warning", "", "x", "-"]:
        got = con.execute(f"SELECT {level_sql('?')}", [raw]).fetchone()[0]
        assert got == normalize_level(raw)


def test_strip_host_domain():
    assert strip_host_domain("eselda13u21s06.xerces.lan") == "eselda13u21s06"
    assert strip_host_domain("eselda06u28") == "eselda06u28"
    assert strip_host_domain(None) is None
