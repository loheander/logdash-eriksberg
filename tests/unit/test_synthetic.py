import csv
import io

import zstandard

from tests.synthetic import COLUMNS, SyntheticSpec, generate_rows, rows_to_csv, utc, write_dataset


def _read(path):
    text = zstandard.ZstdDecompressor().stream_reader(path.open("rb")).read().decode()
    return list(csv.DictReader(io.StringIO(text))), text


def test_header_and_row_count(synthetic):
    rows, text = _read(synthetic.csv_path)
    assert text.splitlines()[0] == ",".join(COLUMNS)
    assert len(rows) == synthetic.truth.n_rows
    assert 20_000 <= len(rows) <= 50_000


def test_deterministic(tmp_path):
    spec = SyntheticSpec(n_rows=500, spike_rows=50)
    a, _ = generate_rows(spec)
    b, _ = generate_rows(spec)
    assert rows_to_csv(a) == rows_to_csv(b)
    write_dataset(tmp_path / "x.csv.zst", spec)
    assert (tmp_path / "x.csv.zst").stat().st_size > 0


def test_expected_features(synthetic):
    rows, _ = _read(synthetic.csv_path)
    levels = {r["log_level"] for r in rows}
    assert {"warn", "WARN", "Warning", "info", "INFO", ""} <= levels
    assert any(r["Hostname"].endswith(".xerces.lan") for r in rows)
    assert any(r["Pid"].endswith(".0") for r in rows)
    malformed = [r for r in rows if not r["Payload"] and not r["Hostname"]]
    assert len(malformed) == synthetic.truth.n_malformed
    assert utc(synthetic.truth.start).hour == 0


def test_anomalies_injected(synthetic):
    rows, _ = _read(synthetic.csv_path)
    truth = synthetic.truth
    silent = [
        r
        for r in rows
        if r["Hostname"] == truth.silent_host
        and truth.gap_start.timestamp() < int(r["Timestamp"]) < truth.gap_end.timestamp() - 1
    ]
    assert silent == []
    novel = [r for r in rows if r["Payload"].startswith("Unexpected bgp")]
    assert novel and min(r["@timestamp"] for r in novel) >= truth.novel_first_seen.strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    by_req = [r for r in rows if r["request_id"] == truth.out_of_order_request_ids[0]]
    assert len(by_req) == 4
