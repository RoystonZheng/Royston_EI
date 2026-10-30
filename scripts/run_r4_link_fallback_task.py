#!/usr/bin/env python3
"""Continue one fresh R4 fallback-link task sequentially, with zero retries."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_r4_codex_pilot_continuation.py"


def read_state(path: Path) -> dict:
    return json.loads((path / "state.json").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    out = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    out = out.resolve()
    log_dir = out / "driver"
    log_dir.mkdir(exist_ok=True)
    progress_path = log_dir / "progress.jsonl"

    while True:
        before = read_state(out)
        if before.get("status") != "ready":
            return 0 if before.get("status") == "completed_route" else 2
        before_count = int(before.get("new_requests_used", 0))
        step_number = before_count + 1
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        command = [sys.executable, str(RUNNER), "--one-step", "--output-dir", str(out)]
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        after = read_state(out)
        advanced_by = int(after.get("new_requests_used", 0)) - before_count
        step_record = {
            "step_number": step_number,
            "started_at_utc": started,
            "finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "argv": command,
            "returncode": result.returncode,
            "requests_before": before_count,
            "requests_after": int(after.get("new_requests_used", 0)),
            "attempts_count": len(after.get("attempts", [])),
            "status": after.get("status"),
            "last_attempt": (after.get("attempts") or [{}])[-1],
        }
        (log_dir / ("step_{0:04d}.stdout.json".format(step_number))).write_text(
            result.stdout, encoding="utf-8"
        )
        (log_dir / ("step_{0:04d}.stderr.txt".format(step_number))).write_text(
            result.stderr, encoding="utf-8"
        )
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(step_record, ensure_ascii=False, sort_keys=True) + "\n")

        # Any ambiguous delivery, CLI error, or state non-advance stops here.
        # This driver never retries a request.
        if result.returncode != 0 or advanced_by != 1:
            print(json.dumps({"stopped": True, "reason": "request_error_or_state_not_advanced", **step_record}, ensure_ascii=False))
            return 3
        if after.get("status") != "ready":
            print(json.dumps({"stopped": True, "reason": "terminal_route_state", **step_record}, ensure_ascii=False))
            return 0 if after.get("status") == "completed_route" else 2


if __name__ == "__main__":
    raise SystemExit(main())
