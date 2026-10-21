import duckdb
import polars as pl
import pytest

from logdash.app.filters import (
    Filters,
    build_filters,
    filter_frame,
    from_dict,
    to_dict,
    where_clause,
)


def test_build_filters_inclusive_end_date():
    data = build_filters(["2023-05-21", "2023-05-21"], ["INFO"], None, [])
    assert data["start"] == "2023-05-21 00:00:00" and data["end"] == "2023-05-22 00:00:00"
    assert data["levels"] == ("INFO",) and data["hosts"] == ()


def test_build_filters_empty_and_partial():
    assert not from_dict(build_filters(None, None, None, None)).active
    assert not from_dict(build_filters([], [], [], [])).active
    only_start = from_dict(build_filters(["2023-05-21"], None, None, None))
    assert only_start.start and only_start.end is None


def test_from_dict_tolerates_garbage():
    f = from_dict({"start": 5, "levels": "INFO", "hosts": ["a", 3], "end": ""})
    assert f == Filters(hosts=("a", "3"))
    assert from_dict(None) == Filters()
    assert from_dict(to_dict(Filters(levels=("INFO",)))) == Filters(levels=("INFO",))


def test_where_clause_parameterised_and_executable():
    f = Filters("2023-05-21 00:00:00", "2023-05-22 00:00:00", ("INFO", "ERROR"), ("x'; DROP",), ())
    sql, params = where_clause(f)
    assert "DROP" not in sql and sql.count("?") == len(params) == 5
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE events AS SELECT TIMESTAMP '2023-05-21 10:00:00' AS ts, 'INFO' AS level, "
        "'x''; DROP' AS program, 'h' AS host"
    )
    assert con.execute(f"SELECT count(*) FROM events {sql}", params).fetchone() == (1,)


def test_where_clause_empty_and_skipped_columns():
    assert where_clause(Filters()) == ("", [])
    sql, params = where_clause(
        Filters("2023-05-21", None, ("INFO",), (), ("h",)), ts=None, level=None
    )
    assert sql == "WHERE host IN (?)" and params == ["h"]


def test_where_clause_rejects_bad_identifier():
    with pytest.raises(ValueError):
        where_clause(Filters(levels=("INFO",)), level="level; --")


def test_filter_frame():
    df = pl.DataFrame({"level": ["INFO", "ERROR"], "svc": ["a", "b"], "n": [1, 2]})
    out = filter_frame(df, Filters(levels=("ERROR",)), level="level", service="svc")
    assert out["n"].to_list() == [2]
    assert filter_frame(df, Filters(), level="level").height == 2
    assert filter_frame(df, Filters(services=("a",)), service="svc")["n"].to_list() == [1]
    assert filter_frame(df, Filters(hosts=("z",)), host=None).height == 2


def test_time_helpers_and_page_url():
    from datetime import datetime

    from logdash.app.filters import page_url, parse_time, with_time_range

    assert parse_time("2023-05-21 01:02:03") == datetime(2023, 5, 21, 1, 2, 3)
    assert parse_time("2023-05-21T01:02:03+02:00") == datetime(2023, 5, 21, 1, 2, 3)
    assert parse_time("junk") is None and parse_time(None) is None
    out = with_time_range({"hosts": ["a"]}, datetime(2023, 5, 21, 1), None)
    assert out["start"] == "2023-05-21 01:00:00" and out["end"] is None and out["hosts"] == ("a",)
    assert page_url("/x", a="b c", none=None, empty="") == "/x?a=b+c"
    assert page_url("/x") == "/x"


def test_merge_filters_keeps_brushed_time():
    from logdash.app.shell import merge_filters

    current = {"start": "2023-05-21 01:00:00", "end": "2023-05-21 02:00:00"}
    kept = merge_filters("filter-levels", current, [], ["INFO"], [], [])
    assert kept["start"] == "2023-05-21 01:00:00" and kept["levels"] == ("INFO",)
    cleared = merge_filters("filter-dates", current, [], [], [], [])
    assert cleared["start"] is None
    assert merge_filters(None, None, [], [], [], [])["start"] is None
