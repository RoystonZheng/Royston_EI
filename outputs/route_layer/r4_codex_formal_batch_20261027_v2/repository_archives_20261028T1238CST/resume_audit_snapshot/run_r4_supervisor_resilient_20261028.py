#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

AUDIT = Path(r"/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_batch_20261027_v2/resume_audit_20261028T034707Z")
PROJECT = Path(r"/Users/didi/Documents/personalcodexfile/第二次课")
BATCH = Path(r"/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_batch_20261027_v2")
PILOT = Path(r"/Users/didi/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/r4_codex_formal_task9_pilot_20261027_v3/final_audit.json")
SUPERVISOR = AUDIT / "run_r4_serial_formal_supervisor_resume.py"
EVENTS = BATCH / "parallel_workers" / "serial_supervisor_wrapper.jsonl"
OUTPUT = BATCH / "parallel_workers" / "serial_supervisor_wrapper_output.log"

def log(event):
    event["at_utc"] = datetime.now(timezone.utc).isoformat()
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")

cmd = [sys.executable, str(SUPERVISOR), "--project-root", str(PROJECT),
       "--batch-root", str(BATCH), "--pilot-audit", str(PILOT),
       "--usage-baseline-percent", "9"]
while True:
    with OUTPUT.open("a", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=str(AUDIT), stdout=out, stderr=subprocess.STDOUT)
        log({"event":"supervisor_process_started","pid":proc.pid,
             "project_request_cap":None,"usage_stop_percent":None})
        rc = proc.wait()
    log({"event":"supervisor_process_exited","pid":proc.pid,"returncode":rc})
    if rc == -signal.SIGTERM:
        log({"event":"supervisor_restart_after_sigterm","delay_seconds":10,
             "unknown_calls_are_not_replayed":True})
        time.sleep(10)
        continue
    raise SystemExit(rc)
