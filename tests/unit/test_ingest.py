import duckdb
import pytest

from logdash.config import Settings
from logdash.ingest.normalize import EVENT_COLUMNS, LEVELS
from logdash.ingest.reader import connect, create_raw_view, events_glob, ingest


@pytest.fixture(scope="module")
def events(synthetic, tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    settings = Settings(data_dir=synthetic.data_dir, cache_dir=cache, memory_limit="1GB", threads=2)
    result = ingest(settings, cache / "events")
    return settings, cache / "events", result


def query(events_dir, sql):
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(
        "CREATE VIEW events AS SELECT * FROM "
        f"read_parquet('{events_glob(events_dir)}', hive_partitioning=true, "
        "hive_types_autocast=false)"
    )
    return con.execute(sql).fetchall()


def test_row_counts(events, synthetic):
    _, _, result = events
    assert result.rows == synthetic.truth.n_rows
    assert result.malformed == synthetic.truth.n_malformed
    assert result.hours >= 6


def test_partition_layout(events):
    _, events_dir, _ = events
    names = sorted(p.name for p in events_dir.iterdir())
    assert names[0] == "hour=00"
    assert all(len(n) == len("hour=00") for n in names)
    assert not events_dir.with_name("events.tmp").exists()


def test_schema(events):
    _, events_dir, _ = events
    cols = [r[0] for r in query(events_dir, "DESCRIBE events")]
    assert set(cols) == set(EVENT_COLUMNS)


def test_levels_hosts_pid(events):
    _, events_dir, _ = events
    levels = {r[0] for r in query(events_dir, "SELECT DISTINCT level FROM events")}
    assert levels <= set(LEVELS) and {"INFO", "WARNING", "UNKNOWN"} <= levels
    hosts = {r[0] for r in query(events_dir, "SELECT DISTINCT host FROM events")}
    assert not any(h and "." in h for h in hosts)
    assert "eselda13u21s06" in hosts
    ((pid_type,),) = query(events_dir, "SELECT typeof(pid) FROM events LIMIT 1")
    assert pid_type == "BIGINT"
    assert query(events_dir, "SELECT count(*) FROM events WHERE pid >= 1000")[0][0] > 0


def test_timestamps_utc_ms_and_sorted(events, synthetic):
    _, events_dir, _ = events
    ((lo, hi),) = query(events_dir, "SELECT min(ts), max(ts) FROM events")
    assert lo.hour == 0 and lo.minute == 1  # 02:01 CEST is 00:01 UTC
    assert hi.hour == 6
    assert query(events_dir, "SELECT count(*) FROM events WHERE ts IS NULL")[0][0] == 0
    for path in sorted(events_dir.glob("hour=*/*.parquet")):
        ts = [r[0] for r in duckdb.sql(f"SELECT ts FROM '{path}'").fetchall()]
        assert ts == sorted(ts)
    ((ms,),) = query(events_dir, "SELECT count(*) FROM events WHERE ts != date_trunc('ms', ts)")
    assert ms == 0


def test_malformed_and_cleaning(events):
    _, events_dir, _ = events
    rows = query(
        events_dir,
        "SELECT payload, host, program, request_id, user_domain FROM events WHERE malformed",
    )
    assert all(r == (None,) * 5 for r in rows)
    assert query(events_dir, "SELECT count(*) FROM events WHERE user_domain = '-'")[0][0] == 0


def test_reingest_replaces(events):
    settings, events_dir, first = events
    again = ingest(settings, events_dir)
    assert again == first


def test_missing_files(tmp_path):
    con = connect(Settings(data_dir=tmp_path, cache_dir=tmp_path))
    with pytest.raises(FileNotFoundError):
        create_raw_view(con, [])
