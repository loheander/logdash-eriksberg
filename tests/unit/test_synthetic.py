import csv
import hashlib
import io

import zstandard

from tests.synthetic import (
    COLUMNS,
    FORECAST_HOSTS,
    HOSTS,
    PRECURSOR_RATE,
    SyntheticSpec,
    generate_rows,
    rows_to_csv,
    utc,
    write_dataset,
)


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
    rare = [r for r in rows if r["Payload"].startswith("Watchdog rebooted")]
    assert len(rare) == 2 and {r["Hostname"] for r in rare} == {HOSTS[3]}
    by_req = [r for r in rows if r["request_id"] == truth.swapped_request_ids[0]]
    assert len(by_req) == 4


def test_forecast_spikes_off_leaves_the_data_unchanged():
    # sha256 of the default dataset before forecast spikes existed
    rows, _ = generate_rows(SyntheticSpec())
    digest = hashlib.sha256(rows_to_csv(rows).encode()).hexdigest()
    assert digest == "0baa89decad8b682c4c422195dcc3cd01621a8b1bf611a3e3ef6a087528b2f49"


def test_forecast_spikes_and_precursors_are_injected():
    rows, truth = generate_rows(SyntheticSpec(forecast_spikes=24, error_rows=0))
    assert len(truth.forecast_onsets) == 24
    assert truth.forecast_onsets == sorted(truth.forecast_onsets)
    assert all(t.second == 0 and t.microsecond == 0 for t in truth.forecast_onsets)
    assert truth.forecast_hosts == [h.split(".", 1)[0] for h in FORECAST_HOSTS] * 6
    precursors = [r for r in rows if r["Payload"].startswith("Memory pressure on hypervisor")]
    assert len(precursors) == 24 * 3 * PRECURSOR_RATE
    assert {r["log_level"] for r in precursors} == {"WARNING"}
