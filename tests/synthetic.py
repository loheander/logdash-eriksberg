"""Deterministic synthetic OpenStack-style log data with injected anomalies."""

from __future__ import annotations

import csv
import io
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import zstandard

COLUMNS = [
    "Timestamp",
    "@timestamp",
    "id",
    "Pid",
    "log_level",
    "python_module",
    "request_id",
    "user_id",
    "tenant_id",
    "domain_id",
    "user_domain",
    "project_domain",
    "Payload",
    "Hostname",
    "Logger",
    "programname",
]

TZ = timezone(timedelta(hours=2))
START = datetime(2023, 5, 21, 2, 1, 10, tzinfo=TZ)

HOSTS = [f"eselda{n:02d}u{m:02d}s{k:02d}" for n, m, k in [(6, 28, 1), (7, 2, 3), (9, 2, 11)]]
HOSTS += ["eselda13u21s06.xerces.lan", "eselda14u05s02", "eselda15u07s09"]
SILENT_HOST = HOSTS[2]
SPIKE_HOST = HOSTS[0]

LEVELS = ["INFO", "info", "WARNING", "warning", "warn", "WARN", "Warning", "ERROR", "DEBUG", ""]
LEVEL_WEIGHTS = [30, 20, 10, 8, 3, 3, 2, 1, 10, 13]

# (programname, logger, python_module, templates); {ip}, {cidr}, {uuid}, {n}, {hex} are filled in.
SERVICES = [
    (
        "neutron-openvswitch-agent",
        "openstack.neutron",
        "neutron.agent.common.ovs_lib",
        [
            "Port {uuid} updated. Details: {{'device': '{hex}', 'port_id': '{uuid}'}}",
            "Agent rpc_loop - iteration:{n} started",
            "Agent rpc_loop - iteration:{n} completed. Elapsed:{n}.{n} ",
        ],
    ),
    (
        "neutron-l3-agent",
        "openstack.neutron",
        "neutron.agent.linux.iptables_manager",
        [
            "Duplicate iptables rule detected. Line: -A neutron-l3-agent-POSTROUTING "
            "-s {cidr} -d {ip}/32 -m policy --dir out --pol ipsec -j ACCEPT",
            "Router {uuid} updated in {n} ms",
        ],
    ),
    (
        "haproxy",
        "",
        "",
        [
            "{ip}:{n} [21/May/2023:02:01:10.665] frontend~ backend/node{n} 0/0/1/{n}/{n} 200 {n}",
            "Server backend/node{n} is UP, check duration: {n}ms",
        ],
    ),
    (
        "libvirt",
        "",
        "",
        [
            "virPCIVPDResourceUpdateKeyword:401 : Tried to update an unsupported keyword YA: "
            "skipping.",
        ],
    ),
]

NOVEL_TEMPLATE = "Unexpected bgp session reset for peer {ip} after {n} seconds"
REQUEST_STEPS = [
    "Received request for instance {uuid}",
    "Validating quota for tenant {hex}",
    "Scheduling instance on node{n}",
    "Instance {uuid} spawned in {n} seconds",
]
REQUEST_HOST = HOSTS[1]
CONTEXT_HOST = HOSTS[5]
CONTEXT_TEMPLATE = 0  # in the window the host logs only this template of each service


@dataclass(frozen=True)
class SyntheticSpec:
    n_rows: int = 30_000
    hours: float = 6.0
    seed: int = 7
    n_malformed: int = 12
    spike_rows: int = 2_500
    n_requests: int = 150
    n_out_of_order: int = 5
    context_minutes: int = 6  # host-specific template-mix deviation
    context_busy_rows: int = 12_000  # extra lines of that host: busy enough for 1-minute windows


@dataclass
class GroundTruth:
    """Where the anomalies were injected, for detector and analysis tests."""

    start: datetime
    end: datetime
    spike_start: datetime
    spike_end: datetime
    spike_host: str
    silent_host: str
    gap_start: datetime
    gap_end: datetime
    novel_template: str
    novel_first_seen: datetime
    request_ids: list[str] = field(default_factory=list)
    out_of_order_request_ids: list[str] = field(default_factory=list)
    n_rows: int = 0
    n_malformed: int = 0
    context_host: str = CONTEXT_HOST
    context_start: datetime = START
    context_end: datetime = START


def _fill(rng: random.Random, template: str) -> str:
    """Replace placeholders with random IPs, CIDRs, UUIDs, hex strings and numbers."""
    out = template
    while "{ip}" in out:
        out = out.replace("{ip}", ".".join(str(rng.randint(1, 254)) for _ in range(4)), 1)
    while "{cidr}" in out:
        ip = f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.0/{rng.choice([16, 23, 24])}"
        out = out.replace("{cidr}", ip, 1)
    while "{uuid}" in out:
        out = out.replace("{uuid}", str(uuid.UUID(int=rng.getrandbits(128), version=4)), 1)
    while "{hex}" in out:
        out = out.replace("{hex}", f"{rng.getrandbits(128):032x}", 1)
    while "{n}" in out:
        out = out.replace("{n}", str(rng.randint(0, 5000)), 1)
    return out.replace("{{", "{").replace("}}", "}")


def _row(ts: datetime, rng: random.Random, host: str, service: tuple, payload: str) -> dict:
    program, logger, module, _ = service
    has_ctx = bool(module) and rng.random() < 0.5
    return {
        "Timestamp": str(int(ts.timestamp())),
        "@timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond * 1000:09d}+02:00",
        "id": str(rng.randint(3000, 3600)),
        "Pid": f"{rng.randint(1000, 9000)}.0",
        "log_level": rng.choices(LEVELS, LEVEL_WEIGHTS)[0],
        "python_module": module,
        "request_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)) if has_ctx else "",
        "user_id": f"{rng.getrandbits(160):040x}" if has_ctx else "",
        "tenant_id": f"{rng.getrandbits(128):032x}" if has_ctx else "",
        "domain_id": "",
        "user_domain": "-" if has_ctx else "",
        "project_domain": "-" if has_ctx else "",
        "Payload": payload,
        "Hostname": host,
        "Logger": logger,
        "programname": program,
    }


def _random_time(rng: random.Random, lo: datetime, hi: datetime) -> datetime:
    return lo + timedelta(microseconds=rng.randrange(int((hi - lo).total_seconds() * 1e6)))


def generate_rows(spec: SyntheticSpec) -> tuple[list[dict], GroundTruth]:
    """Build all rows (sorted by time, malformed rows included) and the ground truth."""
    rng = random.Random(spec.seed)
    end = START + timedelta(hours=spec.hours)
    spike_start = START + (end - START) * 0.30
    spike_end = spike_start + timedelta(seconds=60)
    gap_start = START + (end - START) * 0.50
    gap_end = gap_start + (end - START) * 0.20
    novel_from = end - timedelta(minutes=10)
    truth = GroundTruth(
        START, end, spike_start, spike_end, SPIKE_HOST, SILENT_HOST, gap_start, gap_end,
        NOVEL_TEMPLATE, novel_from,
    )  # fmt: skip

    context_start = START + (end - START) * 0.15
    context_end = context_start + timedelta(minutes=spec.context_minutes)
    truth.context_start, truth.context_end = context_start, context_end
    shifted: list[tuple[int, datetime, tuple]] = []
    rows: list[dict] = []
    while len(rows) < spec.n_rows:
        ts = _random_time(rng, START, end)
        host = rng.choice(HOSTS)
        if host == SILENT_HOST and gap_start <= ts < gap_end:
            continue
        service = rng.choice(SERVICES)
        rows.append(_row(ts, rng, host, service, _fill(rng, rng.choice(service[3]))))
        if host == CONTEXT_HOST and context_start <= ts < context_end:
            shifted.append((len(rows) - 1, ts, service))

    # A host-specific template-mix deviation. One host is busier than the rest (more lines in
    # the fleet's mix: a 1-minute window needs enough lines to be scored), and for a few minutes
    # it logs the first template of every service (each service keeps its line count, so service
    # volumes and the services' own template mix hardly change). A separate generator keeps
    # the rest of the data as it was.
    other = random.Random(spec.seed + 1000)
    for _ in range(spec.context_busy_rows):
        ts = _random_time(other, START, end)
        service = other.choice(SERVICES)
        rows.append(_row(ts, other, CONTEXT_HOST, service, _fill(other, other.choice(service[3]))))
        if context_start <= ts < context_end:
            shifted.append((len(rows) - 1, ts, service))
    for i, ts, service in shifted:
        payload = service[3][CONTEXT_TEMPLATE]
        rows[i] = _row(ts, other, CONTEXT_HOST, service, _fill(other, payload))

    spike_service = SERVICES[0]
    for _ in range(spec.spike_rows):
        ts = _random_time(rng, spike_start, spike_end)
        payload = _fill(rng, spike_service[3][1])
        rows.append(_row(ts, rng, SPIKE_HOST, spike_service, payload))

    first = _random_time(rng, novel_from, novel_from + timedelta(seconds=30))
    for i in range(20):
        ts = first if i == 0 else _random_time(rng, first, end)
        rows.append(_row(ts, rng, HOSTS[4], SERVICES[2], _fill(rng, NOVEL_TEMPLATE)))
    truth.novel_first_seen = first

    request_service = ("nova-api", "openstack.nova", "nova.api.openstack", [])
    for r in range(spec.n_requests):
        rid = f"req-{uuid.UUID(int=rng.getrandbits(128), version=4)}"
        steps = list(REQUEST_STEPS)
        if r < spec.n_out_of_order:
            steps[1], steps[2] = steps[2], steps[1]
            truth.out_of_order_request_ids.append(rid)
        truth.request_ids.append(rid)
        ts = _random_time(rng, START, end - timedelta(minutes=1))
        for step in steps:
            ts += timedelta(milliseconds=rng.randint(5, 400))
            row = _row(ts, rng, REQUEST_HOST, request_service, _fill(rng, step))
            row["request_id"], row["log_level"] = rid, "INFO"
            rows.append(row)

    rows.sort(key=lambda r: r["@timestamp"])
    for _ in range(spec.n_malformed):
        ts = _random_time(rng, START, end)
        bad = dict.fromkeys(COLUMNS, "")
        bad["Timestamp"] = str(int(ts.timestamp()))
        bad["@timestamp"] = _row(ts, rng, "", SERVICES[0], "")["@timestamp"]
        rows.insert(rng.randrange(len(rows) + 1), bad)

    truth.n_rows, truth.n_malformed = len(rows), spec.n_malformed
    return rows, truth


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def write_dataset(path: Path, spec: SyntheticSpec | None = None) -> GroundTruth:
    """Write a zstd-compressed CSV with the real schema to ``path``."""
    rows, truth = generate_rows(spec or SyntheticSpec())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor(level=3).compress(rows_to_csv(rows).encode()))
    return truth


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)
