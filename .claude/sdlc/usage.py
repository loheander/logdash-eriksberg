#!/usr/bin/env python3
"""Session usage for the orchestrator: record it, check it, wait for the reset.

    usage.py record      status line command: save rate_limits from stdin, print a line
    usage.py check       print the newest snapshot and whether to pause, as JSON
    usage.py wait        sleep until every window over the limit has reset

Claude Code gives the status line command a JSON object on stdin with
`rate_limits.five_hour` and `rate_limits.seven_day`, each holding `used_percentage`
and `resets_at` (epoch seconds). Rate limits are per account, so `record` keeps one
snapshot for all sessions in SNAPSHOT. `check` pauses when any window is at or over
PAUSE_AT percent and has not reset yet. A window whose `resets_at` has passed counts as
reset, because the snapshot only refreshes when a session redraws its status line.
Without a snapshot, or with one older than STALE_AFTER, `check` reports `unknown`
and does not pause. Stdlib only; run with `python3 -I`.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

SNAPSHOT = Path(os.path.expanduser("~/.claude/usage/rate-limits.json"))
PAUSE_AT = 90.0  # percent of a window's limit
STALE_AFTER = 30 * 60  # seconds
RESUME_MARGIN = 60  # seconds after resets_at before resuming
WINDOWS = ("five_hour", "seven_day")


def record():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        data = {}
    limits = data.get("rate_limits") or {}
    if limits:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=SNAPSHOT.parent, delete=False) as f:
            json.dump({"captured_at": time.time(), "rate_limits": limits}, f)
        os.replace(f.name, SNAPSHOT)
    parts = [
        f"{name.replace('_', ' ')} {limits[name]['used_percentage']:.0f}%"
        for name in WINDOWS
        if isinstance((limits.get(name) or {}).get("used_percentage"), (int, float))
    ]
    print(" · ".join(parts) or "usage unknown")


def check(now=None):
    now = time.time() if now is None else now
    try:
        snap = json.loads(SNAPSHOT.read_text())
    except (OSError, json.JSONDecodeError):
        return {"state": "unknown", "pause": False, "reason": f"no snapshot at {SNAPSHOT}"}
    age = now - snap.get("captured_at", 0)
    windows, over = {}, []
    for name in WINDOWS:
        w = snap.get("rate_limits", {}).get(name) or {}
        used, resets_at = w.get("used_percentage"), w.get("resets_at")
        if not isinstance(used, (int, float)):
            continue
        reset = isinstance(resets_at, (int, float)) and resets_at <= now
        windows[name] = {"used_percentage": used, "resets_at": resets_at, "reset": reset}
        if used >= PAUSE_AT and not reset:
            over.append(name)
    result = {"windows": windows, "age_seconds": round(age)}
    if over:
        resume = max(windows[n]["resets_at"] or now for n in over) + RESUME_MARGIN
        return result | {
            "state": "over",
            "pause": True,
            "reason": ", ".join(f"{n} at {windows[n]['used_percentage']:.0f}%" for n in over),
            "resume_at": resume,
            "resume_at_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(resume)),
        }
    if age > STALE_AFTER:
        return result | {"state": "unknown", "pause": False, "reason": "snapshot is stale"}
    return result | {"state": "ok", "pause": False}


def wait():
    status = check()
    while status["pause"]:
        print(f"paused: {status['reason']}; resuming at {status['resume_at_utc']}", flush=True)
        time.sleep(max(1.0, status["resume_at"] - time.time()))
        status = check()
    print(json.dumps(status))


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "record":
        record()
    elif command == "check":
        print(json.dumps(check(), indent=2))
    elif command == "wait":
        wait()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
