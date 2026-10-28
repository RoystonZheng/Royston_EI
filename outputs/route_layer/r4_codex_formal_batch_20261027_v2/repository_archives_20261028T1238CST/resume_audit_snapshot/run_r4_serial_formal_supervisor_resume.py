#!/usr/bin/env python3
"""Fail-closed replacement for the retired stop-none R4 supervisor.

The formal matrix now requires an external fresh quota snapshot before and
after each one-request invocation. This legacy serial supervisor cannot
provide that guarantee and must not launch model work.
"""
import json
print(json.dumps({"status":"blocked_legacy_supervisor_no_per_request_quota_gate","model_requests_started":0,"instruction":"Use the separately quota-guarded one-request executor."}, ensure_ascii=False))
raise SystemExit(78)
