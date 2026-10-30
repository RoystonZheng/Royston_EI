#!/usr/bin/env python3
"""Compare two independently repeated fixed-delay seed-4 FASTEST pilots."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parents[1]
PILOTS = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/pilot_v3_fixed_delay"
OUTPUT = PILOTS / "pilot_reproducibility_audit.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(element: ET.Element) -> Any:
    return {
        "tag": element.tag,
        "attributes": sorted(element.attrib.items()),
        "text": (element.text or "").strip(),
        "children": [canonical(child) for child in list(element)],
    }


def load_run(label: str) -> tuple[dict[str, Any], list[Any]]:
    directory = PILOTS / f"seed_4_FASTEST_{label}"
    tripinfo_path = directory / "tripinfo.xml"
    root = ET.parse(tripinfo_path).getroot()
    rows = root.findall("tripinfo")
    exp = [row for row in rows if row.get("id", "").startswith("exp_")]
    arrived = [row for row in exp if row.get("arrival") not in (None, "", "-1.00")]
    unfinished = [row for row in exp if row.get("arrival") == "-1.00" or row.get("vaporized")]
    bg = [row for row in rows if row.get("id", "").startswith("bg_")]
    bg_unfinished = [row for row in bg if row.get("arrival") == "-1.00" or row.get("vaporized")]
    log = (PILOTS / f"seed_4_FASTEST_{label}.log").read_text(encoding="utf-8")
    data = {
        "label": label,
        "output_directory": str(directory.relative_to(PACKAGE)),
        "tripinfo_sha256": sha256(tripinfo_path),
        "background_demand_sha256": sha256(directory / "background_demand.json"),
        "vehicle_route_file_sha256": sha256(directory / "ev_type.rou.xml"),
        "tripinfo_rows": len(rows),
        "experimental_tripinfo_rows": len(exp),
        "experimental_arrivals": len(arrived),
        "unfinished_experimental_ids": [row.get("id", "") for row in unfinished],
        "background_tripinfo_rows": len(bg),
        "unfinished_background_ids": [row.get("id", "") for row in bg_unfinished],
        "hit_24h_simulation_cap": "SUMO run exceeded 24 simulated hours" in log,
    }
    return data, [canonical(row) for row in rows]


def main() -> None:
    a, rows_a = load_run("a")
    b, rows_b = load_run("b")
    protocol = PACKAGE / "derived/experiments/protocols/r1_dynamic_sumo_protocol_v3.json"
    trace = PACKAGE / "derived/experiments/r1_dynamic_sumo_v1/planning_latency_traces_v1/seed_4/FASTEST.json"
    payload = {
        "experiment_id": "porto_r1_seed4_fastest_fixed_delay_repeatability_pilot",
        "interpretation": "Diagnostic repetitions only; excluded from formal_v3_fixed_delay aggregates.",
        "comparison_basis": "Ordered tripinfo records with every tripinfo and nested child attribute compared; generated-file timestamp and SUMO config text are outside the comparison.",
        "protocol_v3_sha256": sha256(protocol),
        "latency_trace_sha256": sha256(trace),
        "runs": [a, b],
        "tripinfo_record_sequences_identical": rows_a == rows_b,
        "tripinfo_record_count_identical": len(rows_a) == len(rows_b),
        "conclusion": "The repeated seed-4 FASTEST pilot reproduced the same 24-hour cap and the same four unfinished experimental trips." if rows_a == rows_b and a["hit_24h_simulation_cap"] and b["hit_24h_simulation_cap"] else "The repeated pilot did not reproduce identical tripinfo records; inspect the raw outputs before interpreting.",
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"audit": str(OUTPUT), "identical": payload["tripinfo_record_sequences_identical"], "rows": len(rows_a), "failed_legs": a["unfinished_experimental_ids"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
