import duckdb
import pytest

from logdash.ingest.masking import mask, mask_sql

CASES = [
    ("-A chain -s 10.130.0.0/16 -d 172.18.2.6/32 -j ACCEPT", "-A chain -s <IP> -d <IP> -j ACCEPT"),
    ("Port dc31305b-3d07-4807-8d38-32b724294212 updated", "Port <UUID> updated"),
    ("key 95d0469a78fb4d7844c98e48cdb7d0df1a65e898 and 0xdeadBEEF", "key <HEX> and <HEX>"),
    ("iteration:123 elapsed:4.56 s", "iteration:<NUM> elapsed:<NUM> s"),
    ("open /var/lib/nova/instances/x.log failed", "open <PATH> failed"),
    ("node12 stays", "node12 stays"),
    ("", ""),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_mask(text, expected):
    assert mask(text) == expected


def test_sql_agrees_with_python():
    con = duckdb.connect()
    texts = [t for t, _ in CASES] + [
        "[21/May/2023:02:01:10.665] 1.2.3 v1.2.3 id=42, x=0.5%",
        "Router 0f1e2d3c-1111-2222-3333-444455556666 updated in 12 ms from 10.0.0.1:8080",
    ]
    for text in texts:
        sql = mask_sql("?")
        assert con.execute(f"SELECT {sql}", [text]).fetchone()[0] == mask(text), text
