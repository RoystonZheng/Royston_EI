#!/usr/bin/env python3
"""Create a post-hoc source-SHA binding addendum for the 58 existing R4 EV2 rows.

This is an offline provenance audit. It never recalculates EV2 or invokes a model.
The original sidecars are intentionally left immutable.
"""

from __future__ import annotations

import hashlib
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


WORKSPACE = Path(__file__).resolve().parents[1]
ROUTE_OUTPUTS = WORKSPACE / "outputs/route_layer"
APP_BATCH_ROOT = Path(
    "[LOCAL_HOME]/Library/Application Support/CodexR4Formal20261027/outputs/route_layer/"
    "r4_codex_formal_batch_20261027_v2"
)
PARENT_MANIFEST = APP_BATCH_ROOT / "manifest.json"
ENERGY_MODEL = WORKSPACE / "route_experiment/energy_model.py"
BASE_SIDECAR = (
    ROUTE_OUTPUTS / "r4_ev2_payload_phase_authoritative_20261029T2215CST/manifest.json"
)
DELTA_SIDECAR = (
    ROUTE_OUTPUTS
    / "r4_ev2_payload_phase_authoritative_20261029T2320CST_delta_from57/manifest.json"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    return json.loads(raw), raw


def resolved(path_text: str) -> Path:
    return Path(path_text).expanduser().resolve()


def check_hash(path: Path, expected: str | None) -> dict:
    exists = path.is_file()
    actual = sha256(path) if exists else None
    return {
        "path": str(path),
        "exists": exists,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "matches": bool(exists and expected and actual == expected),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        help="Existing addendum directory to revise, or a new directory to create.",
    )
    args = parser.parse_args()
    parent, parent_raw = load_json(PARENT_MANIFEST)
    parent_sha = hashlib.sha256(parent_raw).hexdigest()
    base, base_raw = load_json(BASE_SIDECAR)
    delta, delta_raw = load_json(DELTA_SIDECAR)
    module_sha = sha256(ENERGY_MODEL)
    module_stat = ENERGY_MODEL.stat()

    parent_route_complete = {
        row["task_id"]: row
        for row in parent.get("tasks", [])
        if row.get("status") == "route_complete"
    }
    sidecar_sources: dict[str, dict] = {}
    source_manifests = [
        (BASE_SIDECAR, base, base_raw),
        (DELTA_SIDECAR, delta, delta_raw),
    ]
    per_manifest = []

    for manifest_path, manifest, manifest_raw in source_manifests:
        rows = manifest.get("runs", [])
        per_manifest.append(
            {
                "path": str(manifest_path),
                "sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "row_count": len(rows),
                "source_run_count": len(
                    {row.get("source_run_dir") for row in rows if row.get("source_run_dir")}
                ),
                "batch_manifest_sha256_at_scan": manifest.get(
                    "batch_manifest_sha256_at_scan"
                ),
                "energy_model_source_sha256_field_present": any(
                    str(key).lower()
                    in {
                        "source_sha256",
                        "source_file_sha256",
                        "module_sha256",
                        "energy_model_source_sha256",
                    }
                    for key in manifest.get("energy_model", {}).keys()
                ),
            }
        )
        for row in rows:
            source_run = row.get("source_run_dir")
            if not source_run:
                raise SystemExit(f"Missing source_run_dir in {manifest_path}: {row}")
            if source_run in sidecar_sources:
                raise SystemExit(f"Duplicate source run across sidecars: {source_run}")
            sidecar_sources[source_run] = {
                "task_id": row.get("task_id"),
                "seed": row.get("seed"),
                "source_run_dir": source_run,
                "sidecar_output_path": row.get("output"),
                "sidecar_output_sha256_expected": row.get("output_sha256"),
                "state_sha256_expected": row.get("state_sha256")
                or row.get("source_state_sha256"),
                "final_audit_sha256_expected": row.get("final_audit_sha256"),
                "preflight_sha256_expected": row.get("preflight_sha256"),
                "energy_profile_sha256": row.get("energy_profile_sha256")
                or manifest.get("energy_profile_sha256")
                or manifest.get("energy_model", {}).get("profile_sha256"),
                "source_sidecar_manifest": str(manifest_path),
            }

    run_audits = []
    failures = []
    for source_run, row in sorted(sidecar_sources.items()):
        source_dir = resolved(source_run)
        output_path = resolved(row["sidecar_output_path"])
        output_document = json.loads(output_path.read_text(encoding="utf-8"))
        evidence = output_document.get("evidence", {})
        task = parent_route_complete.get(row["task_id"])
        task_matches_parent = bool(task and resolved(task["output_dir"]) == source_dir)
        expected_state = row["state_sha256_expected"] or evidence.get("state_sha256")
        expected_final = (
            row["final_audit_sha256_expected"]
            or evidence.get("final_audit_sha256")
        )
        expected_preflight = (
            row["preflight_sha256_expected"]
            or evidence.get("preflight_sha256")
            or (task.get("preflight_sha256") if task else None)
        )
        checks = {
            "sidecar_output": check_hash(
                output_path, row["sidecar_output_sha256_expected"]
            ),
            "source_state": check_hash(
                source_dir / "state.json", expected_state
            ),
        }
        task_final_rel = task.get("final_audit_path") if task else None
        if task_final_rel:
            task_final_path = Path(task_final_rel)
            if not task_final_path.is_absolute():
                task_final_path = APP_BATCH_ROOT / task_final_path
        else:
            task_final_path = source_dir / "final_audit.json"
        checks["final_audit"] = check_hash(
            task_final_path, expected_final
        )

        preflight_path = (
            resolved(task["preflight_path"])
            if task and task.get("preflight_path")
            else None
        )
        if preflight_path:
            checks["preflight"] = check_hash(preflight_path, expected_preflight)
        else:
            checks["preflight"] = {
                "path": None,
                "exists": False,
                "expected_sha256": expected_preflight,
                "actual_sha256": None,
                "matches": False,
            }
        evidence_preflight_path = evidence.get("preflight_path")
        checks["ev2_evidence_preflight"] = (
            check_hash(resolved(evidence_preflight_path), expected_preflight)
            if evidence_preflight_path
            else {
                "path": None,
                "exists": False,
                "expected_sha256": expected_preflight,
                "actual_sha256": None,
                "matches": False,
            }
        )
        checks["ev2_output_evidence_links"] = {
            "source_run_dir_matches": output_document.get("source_run_dir")
            == source_run,
            "state_path_matches": bool(
                evidence.get("state_path")
                and resolved(evidence["state_path"]) == source_dir / "state.json"
            ),
            "final_audit_path_matches": bool(
                evidence.get("final_audit_path")
                and resolved(evidence["final_audit_path"]) == task_final_path
            ),
            "preflight_reference_hash_matches": bool(
                checks["preflight"]["actual_sha256"]
                and checks["preflight"]["actual_sha256"]
                == checks["ev2_evidence_preflight"]["actual_sha256"]
            ),
            "energy_profile_sha256_matches": output_document.get("energy_profile_sha256")
            == row["energy_profile_sha256"],
        }
        checks["ev2_output_evidence_links"]["matches"] = all(
            value
            for key, value in checks["ev2_output_evidence_links"].items()
            if key != "matches"
        )

        ok = task_matches_parent and all(check["matches"] for check in checks.values())
        if not ok:
            failures.append(
                {
                    "task_id": row["task_id"],
                    "source_run_dir": source_run,
                    "task_matches_current_parent": task_matches_parent,
                    "checks": checks,
                }
            )
        run_audits.append(
            {
                "task_id": row["task_id"],
                "seed": row["seed"],
                "source_run_dir": source_run,
                "task_status_in_parent_snapshot": task.get("status") if task else None,
                "task_output_dir_in_parent": task.get("output_dir") if task else None,
                "task_matches_current_parent": task_matches_parent,
                "sidecar_output_path": str(output_path),
                "energy_model_source_path": str(ENERGY_MODEL),
                "energy_model_source_sha256": module_sha,
                "energy_profile_sha256": row["energy_profile_sha256"],
                "checks": checks,
                "status": "passed" if ok else "failed",
            }
        )

    source_dirs = set(sidecar_sources)
    parent_dirs = {
        resolved(row["output_dir"])
        for row in parent_route_complete.values()
        if row.get("output_dir")
    }
    source_set_digest = hashlib.sha256(
        ("\n".join(sorted(source_dirs)) + "\n").encode("utf-8")
    ).hexdigest()
    parent_set_digest = hashlib.sha256(
        ("\n".join(sorted(map(str, parent_dirs))) + "\n").encode("utf-8")
    ).hexdigest()

    current_local = datetime.now(ZoneInfo("Asia/Shanghai"))
    created_utc = datetime.now(timezone.utc).isoformat()
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else ROUTE_OUTPUTS
        / (
            "r4_ev2_payload_phase_source_binding_"
            + current_local.strftime("%Y%m%dT%H%M%S%Z")
        )
    )
    output_dir.mkdir(parents=True, exist_ok=bool(args.output_dir))
    status = (
        "passed_posthoc_source_binding"
        if not failures
        and len(source_dirs) == 58
        and len(parent_dirs) == 58
        and source_dirs == {str(path) for path in parent_dirs}
        else "failed"
    )
    report = {
        "schema_version": "r4_ev2_payload_phase_source_binding_addendum_v1",
        "status": status,
        "created_at_utc": created_utc,
        "created_at_local": current_local.isoformat(),
        "audit_script": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256(Path(__file__).resolve()),
        },
        "scope": "Offline provenance addendum for existing R4 EV2 payload-phase rows; no recalculation.",
        "model_calls": 0,
        "route_algorithm_rerun": False,
        "original_sidecars_modified": False,
        "historical_execution_identity_proven": False,
        "historical_execution_identity_limitation": (
            "The original EV2 sidecar manifests identify ev_longitudinal_v2 and the profile SHA, "
            "but do not record the energy_model.py source SHA. This addendum binds the existing "
            "rows to the inspected source snapshot post hoc; file mtime predating the sidecars "
            "is corroborating metadata, not cryptographic proof of which bytes were imported "
            "during the original recalculation."
        ),
        "energy_model_source": {
            "path": str(ENERGY_MODEL),
            "sha256": module_sha,
            "size_bytes": module_stat.st_size,
            "mtime_utc": datetime.fromtimestamp(
                module_stat.st_mtime, timezone.utc
            ).isoformat(),
            "energy_profile_sha256": "b4878499ec45879552a35f56978cedcfeb78df4ab841a3514ec1741833eb7bee",
        },
        "source_sidecars": per_manifest,
        "source_run_union": {
            "count": len(source_dirs),
            "unique_count": len(source_dirs),
            "intersection_count_between_sidecars": len(
                set(
                    row["source_run_dir"]
                    for row in base.get("runs", [])
                )
                & set(
                    row["source_run_dir"]
                    for row in delta.get("runs", [])
                )
            ),
            "source_run_dirs_sha256_sorted_newline_delimited": source_set_digest,
            "exactly_matches_current_parent_route_complete_set": source_dirs
            == {str(path) for path in parent_dirs},
            "current_parent_route_complete_count": len(parent_dirs),
            "current_parent_route_complete_set_sha256_sorted_newline_delimited": parent_set_digest,
            "parent_manifest_path": str(PARENT_MANIFEST),
            "parent_manifest_sha256_at_audit": parent_sha,
            "parent_status_at_audit": parent.get("status"),
            "parent_updated_at_utc_at_audit": parent.get("updated_at_utc"),
        },
        "integrity_summary": {
            "sidecar_output_hashes_valid": sum(
                r["checks"]["sidecar_output"]["matches"] for r in run_audits
            ),
            "source_state_hashes_valid": sum(
                r["checks"]["source_state"]["matches"] for r in run_audits
            ),
            "source_final_audit_hashes_valid": sum(
                r["checks"]["final_audit"]["matches"] for r in run_audits
            ),
            "source_preflight_hashes_valid": sum(
                r["checks"]["preflight"]["matches"] for r in run_audits
            ),
            "ev2_evidence_preflight_hashes_valid": sum(
                r["checks"]["ev2_evidence_preflight"]["matches"]
                for r in run_audits
            ),
            "rows_bound_to_energy_model_source_sha": sum(
                r["energy_model_source_sha256"] == module_sha for r in run_audits
            ),
            "failure_count": len(failures),
        },
        "runs": run_audits,
        "failures": failures,
    }
    report_path = output_dir / "source_binding_addendum.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_sha = sha256(report_path)
    readme = (
        "# R4 EV2 payload-phase source binding addendum\n\n"
        f"- Status: `{status}`\n"
        f"- Existing rows: {len(run_audits)}; source-run union: {len(source_dirs)} unique\n"
        f"- Current route-complete set match: "
        f"{report['source_run_union']['exactly_matches_current_parent_route_complete_set']}\n"
        f"- `energy_model.py` SHA-256: `{module_sha}`\n"
        f"- Sidecar output/state/final-audit/preflight hash checks: "
        f"{report['integrity_summary']['sidecar_output_hashes_valid']}/58, "
        f"{report['integrity_summary']['source_state_hashes_valid']}/58, "
        f"{report['integrity_summary']['source_final_audit_hashes_valid']}/58, "
        f"{report['integrity_summary']['source_preflight_hashes_valid']}/58\n"
        "- Model calls and route reruns: 0\n\n"
        "This is a post-hoc binding addendum. The original sidecars omitted the source-module "
        "SHA, so this record does not claim cryptographic proof of the exact module bytes "
        "imported at the historical recalculation time. Original sidecar files remain unchanged.\n"
    )
    readme_path = output_dir / "README.md"
    readme_path.write_text(readme, encoding="utf-8")
    (output_dir / "SHA256SUMS").write_text(
        f"{report_sha}  source_binding_addendum.json\n"
        f"{sha256(readme_path)}  README.md\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": status,
        "output_dir": str(output_dir),
        "report_sha256": report_sha,
        "parent_sha256": parent_sha,
        "route_complete_count": len(parent_dirs),
        "source_run_union_count": len(source_dirs),
        "source_run_union_matches_parent": report["source_run_union"]["exactly_matches_current_parent_route_complete_set"],
        "integrity_summary": report["integrity_summary"],
        "failure_count": len(failures),
    }, ensure_ascii=False, indent=2))
    if status != "passed_posthoc_source_binding":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
