#!/usr/bin/env python3
"""Wait for the isolated EV2 v4 batch, then run both official and ledger audits."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED_ROOT = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_ev2_segment_ledger_v4_20261027/seed_0"
)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_logged(command: list[str], log_path: Path) -> Dict[str, Any]:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    log_path.write_text(
        "COMMAND: " + " ".join(command) + "\n\nSTDOUT:\n" + result.stdout
        + "\nSTDERR:\n" + result.stderr,
        encoding="utf-8",
    )
    return {"command": command, "return_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr, "log": str(log_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-root", default=str(DEFAULT_SEED_ROOT))
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    seed_root = ROOT / args.seed_root
    manifest_path = seed_root / "experiment_manifest.json"
    while True:
        manifest = load_json(manifest_path)
        if manifest.get("status") != "running":
            break
        time.sleep(max(10, args.poll_seconds))

    analysis_dir = seed_root / "final_analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    official = run_logged(
        [sys.executable, "scripts/audit_nyc_tlc_dispatch_multimonth_results.py",
         "--package", manifest["scenario_package"],
         "--results", str(Path(args.seed_root) / "final_analysis" / "paired_analysis"),
         "--seed-results", "0=" + args.seed_root],
        analysis_dir / "official_audit.log",
    )
    ledger = run_logged(
        [sys.executable, "scripts/audit_nyc_tlc_dispatch_ev2_segment_ledger_v4.py",
         "--package", manifest["scenario_package"], "--seed-root", args.seed_root],
        analysis_dir / "ledger_audit.log",
    )
    summary = {
        "schema_version": "nyc-tlc-dispatch-ev2-v4-finalization-v1",
        "finalized_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "batch_status": manifest.get("status"),
        "batch_phases": manifest.get("phases"),
        "official_pair_audit": {key: official[key] for key in ("return_code", "log")},
        "segment_ledger_audit": {key: ledger[key] for key in ("return_code", "log")},
        "all_audits_passed": (
            manifest.get("status") == "completed_runs_analysis_pending"
            and official["return_code"] == 0 and ledger["return_code"] == 0
        ),
    }
    summary_path = analysis_dir / "finalization_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["all_audits_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
