#!/usr/bin/env python3
"""Run resumable D1-label training and paired multi-month dispatch evaluation.

Live Codex calls are never retried by this runner. If a run is interrupted
after it starts and has no complete result, the cell is marked unresolved and
requires an explicit manual audit before any new model request is made.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatch_experiment.decision_contract import CONTRACT_VERSION
from route_experiment.energy_model import EnergyModelConfig, model_metadata


PACKAGE = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1")
RUNNER = ROOT / "scripts/run_nyc_tlc_dispatch_benchmark.py"
TRAINER = ROOT / "scripts/train_nyc_tlc_dispatch_ranker.py"
METHODS = {
    "D0": "D0_greedy",
    "D1": "D1_optimizer",
    "D2": "D2_agent_optimizer",
    "D5": "D5_pure_llm_direct",
    "D6": "D6_agent_codex_context_optimizer",
    "D7": "D7_agent_codex_capacity_shield_optimizer",
    "D8": "D8_agent_codex_nn_capacity_shield_optimizer",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def append_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"timestamp_utc": now(), **event}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"timestamp_utc": now(), **event}, ensure_ascii=False), flush=True)


def result_path(output_dir: Path, case_id: str, fleet_id: str, method: str, seed: int = 0) -> Path:
    return output_dir / case_id / fleet_id / (method + "_seed_" + str(seed) + ".json")


def complete_result(path: Path, method: str, case_id: str, fleet_id: str) -> bool:
    # Full D1 label runs can exceed several GB. Read only the JSON header and
    # summary instead of materializing the entire candidate audit in memory.
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            header = stream.read(2 * 1024 * 1024).decode("utf-8")
            stream.seek(max(0, path.stat().st_size - 4096))
            tail = stream.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if '"run_mode": "complete_simulation"' not in header or '"partial_run": false' not in header:
        return False
    if not tail.rstrip().endswith("}"):
        return False
    expected_values = {
        "method_name": method,
        "case_id": case_id,
        "fleet_id": fleet_id,
    }
    for key, expected in expected_values.items():
        match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"([^"]+)"', header)
        if not match or match.group(1) != expected:
            return False
    summary_key = header.find('"summary"')
    if summary_key < 0:
        return False
    colon = header.find(":", summary_key)
    summary_start = colon + 1
    while summary_start < len(header) and header[summary_start].isspace():
        summary_start += 1
    try:
        summary, _ = json.JSONDecoder().raw_decode(header, summary_start)
    except (ValueError, TypeError):
        return False
    return isinstance(summary, dict) and summary.get("method_name") == method


class Batch:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.package_dir = ROOT / args.package
        self.package_manifest_path = self.package_dir / "benchmark_manifest.json"
        self.package_manifest = load_json(self.package_manifest_path)
        if not self.package_manifest:
            raise FileNotFoundError(self.package_manifest_path)
        self.output_root = ROOT / args.output_dir
        self.evaluation_root = ROOT / args.evaluation_dir if args.evaluation_dir else self.output_root / "evaluation"
        self.ledger = self.output_root / "progress.jsonl"
        self.manifest_path = self.output_root / "experiment_manifest.json"
        self.model_path = self.output_root / "models/d1_pairwise_mlp.json"
        self.training_report_path = self.output_root / "models/d1_pairwise_mlp_training_report.json"
        self.log_root = self.output_root / "execution_logs"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.evaluation_root.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)
        self.experiment_manifest = self._load_or_create_manifest()
        self.events = self._read_events()

    def _load_or_create_manifest(self) -> Dict[str, Any]:
        package_hash = sha256(self.package_manifest_path)
        existing = load_json(self.manifest_path)
        if existing:
            if existing.get("scenario_package_manifest_sha256") != package_hash:
                raise ValueError("result directory is bound to a different frozen scenario package")
            return existing
        source_files = [
            "scripts/run_nyc_tlc_dispatch_multimonth_batch.py",
            "scripts/run_nyc_tlc_dispatch_benchmark.py",
            "dispatch_experiment/agent.py",
            "dispatch_experiment/decision_contract.py",
            "dispatch_experiment/llm.py",
            "dispatch_experiment/models.py",
            "dispatch_experiment/neural_ranker.py",
            "dispatch_experiment/optimizer.py",
            "dispatch_experiment/route_oracle.py",
            "dispatch_experiment/runner.py",
            "route_experiment/energy_model.py",
        ]
        source_hashes = {
            name: sha256(ROOT / name)
            for name in source_files
        }
        value = {
            "experiment_id": "nyc_tlc_dispatch_ev2_lexicographic_v2_seed_{}".format(self.args.seed),
            "created_at_utc": now(),
            "status": "running",
            "backend": "codex_cli",
            "model": self.args.model,
            "reasoning_effort": "low",
            "seed": self.args.seed,
            "retry_policy": "zero retries for live Codex model calls; interrupted deterministic runs may be restarted after preserving their log",
            "scenario_package": self.args.package,
            "scenario_package_manifest_sha256": package_hash,
            "implementation_version": "dispatch-agent-v2",
            "implementation_source_sha256": source_hashes,
            "decision_function_contract_version": CONTRACT_VERSION,
            "energy_model": model_metadata(EnergyModelConfig()),
            "evaluation_output_root": str(self.evaluation_root.relative_to(ROOT)),
            "training_windows": list(self.package_manifest.get("training_case_ids", [])),
            "evaluation_windows": list(self.package_manifest.get("evaluation_case_ids", [])),
            "evaluation_fleets": ["F1", "F2", "F4"],
            "evaluation_methods": list(METHODS.values()),
            "paired_policy": "D0/D1 share one process; D2 is separate; D8 makes one live shared-context request; D6/D7 replay that response only if prompt bytes match; D5 runs live independently",
            "cost_accounting": "Raw Codex CLI JSONL traces are retained per live run; token usage is audited after execution; rate-card equivalents are estimates and actual account billing is unknown",
            "phases": {"training": "pending", "evaluation": "pending", "analysis": "pending"},
            "limitations": [
                "Completed, already-matched TLC trips are used as demand proxies; unserved trip attempts are not observed.",
                "Fleet counts and initial vehicle positions are synthetic zone-level scenarios.",
                "Ride costs use zone OD medians, with any centroid estimate tagged per OD row; this is not street routing or an observed empty-vehicle ETA.",
                "The evaluation uses one simulation/model seed per date/fleet; calendar dates are the independent demand clusters.",
            ],
        }
        ranker_report = load_json(self.training_report_path)
        if self.model_path.is_file() and ranker_report:
            ranker_hash = sha256(self.model_path)
            if ranker_report.get("model_sha256") != ranker_hash:
                raise ValueError("frozen ranker checkpoint does not match its training report")
            value["model"] = {
                "model_id": ranker_report.get("model_id"),
                "path": str(self.model_path.relative_to(ROOT)),
                "sha256": ranker_hash,
                "training_report": str(self.training_report_path.relative_to(ROOT)),
                "training_case_count": ranker_report.get("training_data", {}).get("training_window_count"),
                "training_order_count": ranker_report.get("training_data", {}).get("training_order_id_count"),
                "pairwise_sample_count": ranker_report.get("training_data", {}).get("pairwise_sample_count"),
                "interpretation": ranker_report.get("interpretation"),
            }
        atomic_json(self.manifest_path, value)
        return value

    def _read_events(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        if not self.ledger.exists():
            return grouped
        with self.ledger.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                grouped.setdefault(str(item.get("run_id", "")), []).append(item)
        return grouped

    def checkpoint(self) -> None:
        self.experiment_manifest["updated_at_utc"] = now()
        atomic_json(self.manifest_path, self.experiment_manifest)

    def run_cell(
        self,
        run_id: str,
        command: Sequence[str],
        outputs: Dict[str, Path],
        *,
        model_live: bool,
        label: str,
    ) -> bool:
        all_complete = bool(outputs) and all(
            complete_result(path, method, path.parent.parent.name, path.parent.name)
            for method, path in outputs.items()
        )
        if all_complete:
            append_event(self.ledger, {"event": "reuse_complete", "run_id": run_id,
                                       "results": {name: str(path) for name, path in outputs.items()}})
            return True

        previous = self.events.get(run_id, [])
        previous_starts = [item for item in previous if item.get("event") == "start"]
        if previous_starts and (model_live or any(item.get("live_model_call_possible") for item in previous_starts)):
            append_event(self.ledger, {"event": "blocked", "run_id": run_id,
                                       "reason": "previous live-model attempt has no complete result; automatic retry prohibited"})
            return False
        if previous_starts:
            append_event(self.ledger, {"event": "deterministic_resume", "run_id": run_id,
                                       "reason": "previous attempt was deterministic and made no model request"})
        existing = [path for path in outputs.values() if path.exists()]
        if existing:
            append_event(self.ledger, {"event": "blocked", "run_id": run_id,
                                       "reason": "incomplete result file already exists; refusing overwrite",
                                       "files": [str(path) for path in existing]})
            return False
        if model_live:
            traces = []
            context_methods = {
                METHODS["D6"], METHODS["D7"], METHODS["D8"],
            }
            for method, result_path in outputs.items():
                if method == METHODS["D5"]:
                    trace_dir = result_path.parent / "codex_cli_traces" / method
                elif method in context_methods:
                    trace_dir = result_path.parent / "codex_context_policy_traces" / method
                else:
                    continue
                if trace_dir.exists():
                    traces.extend(str(path) for path in trace_dir.glob("*.jsonl"))
            if traces:
                append_event(self.ledger, {"event": "blocked", "run_id": run_id,
                                           "reason": "raw model trace for this method exists without a complete result; no retry",
                                           "trace_files": traces})
                return False

        attempt = len(previous_starts) + 1
        suffix = "_attempt{}".format(attempt) if attempt > 1 else ""
        log_path = self.log_root / (run_id.replace("/", "_") + suffix + ".log")
        append_event(self.ledger, {"event": "start", "run_id": run_id, "label": label,
                                   "command": list(command), "log": str(log_path),
                                   "live_model_call_possible": model_live})
        start = time.monotonic()
        last_progress = start
        code = None
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                       text=True, env=os.environ.copy())
            while process.poll() is None:
                time.sleep(2)
                elapsed = time.monotonic() - start
                if elapsed - (last_progress - start) >= self.args.progress_interval:
                    append_event(self.ledger, {"event": "progress", "run_id": run_id,
                                               "elapsed_seconds": round(elapsed, 1),
                                               "log_bytes": log_path.stat().st_size})
                    last_progress = time.monotonic()
            code = process.returncode
        result_states = {}
        for method, path in outputs.items():
            valid = complete_result(path, method, path.parent.parent.name, path.parent.name)
            result_states[method] = {
                "path": str(path),
                "exists": path.exists(),
                "sha256": sha256(path) if path.exists() else None,
                "complete": valid,
            }
        ok = code == 0 and all(row["complete"] for row in result_states.values())
        append_event(self.ledger, {"event": "finish", "run_id": run_id, "return_code": code,
                                   "elapsed_seconds": round(time.monotonic() - start, 2),
                                   "results": result_states, "status": "complete" if ok else "failed_no_retry",
                                   "log": str(log_path)})
        self.events.setdefault(run_id, []).append({"event": "finish"})
        return ok

    def command(self, case_id: str, fleet_id: str, methods: Sequence[str], output_dir: Path,
                *, compact: bool, live: bool = False, replay: Optional[Path] = None,
                nn_model: Optional[Path] = None) -> List[str]:
        command = [sys.executable, str(RUNNER), "--package", self.args.package,
                   "--case", case_id, "--fleet-id", fleet_id, "--methods", *methods,
                   "--seed", str(self.args.seed), "--output-dir", str(output_dir.relative_to(ROOT)),
                   "--codex-model", self.args.model]
        if compact:
            command.append("--compact-records")
        if live:
            command.append("--allow-live-api")
        if replay:
            command += ["--codex-policy-replay", str(replay)]
        if nn_model:
            command += ["--nn-ranker-model", str(nn_model)]
        return command

    def train(self) -> bool:
        training_ids = self.package_manifest.get("training_case_ids", [])
        output_dir = self.output_root / "training_d1_labels"
        d1_paths = []
        for case_id in training_ids:
            path = result_path(output_dir, case_id, "F1", METHODS["D1"], self.args.seed)
            d1_paths.append(path)
            run_id = "training/" + case_id + "/F1/D1"
            command = self.command(case_id, "F1", [METHODS["D1"]], output_dir, compact=False)
            command.append("--compact-training-labels")
            if not self.run_cell(run_id, command, {METHODS["D1"]: path}, model_live=False,
                                 label="D1 compact imitation-label run"):
                self.experiment_manifest["phases"]["training"] = "blocked_or_failed"
                self.checkpoint()
                return False

        if self.model_path.exists() and self.training_report_path.exists():
            report = load_json(self.training_report_path) or {}
            if report.get("model_sha256") != sha256(self.model_path):
                raise ValueError("existing ranker report does not match checkpoint checksum")
            append_event(self.ledger, {"event": "reuse_trained_ranker", "model": str(self.model_path),
                                       "sha256": sha256(self.model_path)})
        elif self.model_path.exists() or self.training_report_path.exists():
            raise FileExistsError("only one of ranker checkpoint/training report exists; refusing overwrite")
        else:
            command = [sys.executable, str(TRAINER), "--package", self.args.package,
                       "--output", str(self.model_path.relative_to(ROOT)),
                       "--report", str(self.training_report_path.relative_to(ROOT)),
                       "--seed", "17", *[str(path.relative_to(ROOT)) for path in d1_paths]]
            if not self.run_cell("training/model_fit", command, {}, model_live=False,
                                 label="fit D1-imitation pairwise MLP"):
                # Training writes two artifacts instead of a benchmark result.
                report = load_json(self.training_report_path)
                model = load_json(self.model_path)
                if not report or not model:
                    self.experiment_manifest["phases"]["training"] = "blocked_or_failed"
                    self.checkpoint()
                    return False
        report = load_json(self.training_report_path)
        model = load_json(self.model_path)
        if not report or not model or report.get("model_sha256") != sha256(self.model_path):
            raise ValueError("trained ranker artifact verification failed")
        self.experiment_manifest["model"] = {
            "model_id": report.get("model_id"),
            "path": str(self.model_path.relative_to(ROOT)),
            "sha256": sha256(self.model_path),
            "training_report": str(self.training_report_path.relative_to(ROOT)),
            "training_case_count": report.get("training_data", {}).get("training_window_count"),
            "training_order_count": report.get("training_data", {}).get("training_order_id_count"),
            "pairwise_sample_count": report.get("training_data", {}).get("pairwise_sample_count"),
            "interpretation": report.get("interpretation"),
        }
        self.experiment_manifest["phases"]["training"] = "complete"
        self.checkpoint()
        return True

    def _d8_replay_path(self, path: Path) -> Optional[Path]:
        payload = load_json(path)
        if not payload or payload.get("partial_run") is not False:
            return None
        records = [audit.get("codex_context_policy") for audit in payload.get("round_audits", [])]
        records = [record for record in records if isinstance(record, dict)]
        if len(records) != 1 or records[0].get("status") != "accepted":
            return None
        final_path = records[0].get("final_json_path")
        if not final_path:
            return None
        candidate = Path(final_path)
        return candidate if candidate.is_file() else None

    def _accepted_policy(self, path: Path) -> bool:
        payload = load_json(path)
        if not payload:
            return False
        records = [audit.get("codex_context_policy") for audit in payload.get("round_audits", [])]
        records = [record for record in records if isinstance(record, dict)]
        return len(records) == 1 and records[0].get("status") == "accepted"

    def evaluate(self) -> bool:
        if not self.model_path.is_file() or not self.training_report_path.is_file():
            raise FileNotFoundError("evaluation requires completed D1 labels and the trained D8 ranker")
        report = load_json(self.training_report_path)
        if not report or report.get("model_sha256") != sha256(self.model_path):
            raise ValueError("evaluation ranker checkpoint does not match its training report")
        self.experiment_manifest["model"] = {
            "path": str(self.model_path.relative_to(ROOT)),
            "sha256": sha256(self.model_path),
            "training_report": str(self.training_report_path.relative_to(ROOT)),
            "training_case_count": report.get("training_data", {}).get("training_window_count"),
            "training_order_count": report.get("training_data", {}).get("training_order_id_count"),
            "pairwise_sample_count": report.get("training_data", {}).get("pairwise_sample_count"),
            "interpretation": report.get("interpretation"),
        }
        self.experiment_manifest["phases"]["training"] = "reused_frozen_seed0_ranker"
        self.checkpoint()
        evaluation_dir = self.evaluation_root
        case_ids = self.package_manifest.get("evaluation_case_ids", [])
        fleets = ["F1", "F2", "F4"]
        total = len(case_ids) * len(fleets)
        completed = 0
        for case_id in case_ids:
            for fleet_id in fleets:
                completed += 1
                print(json.dumps({"phase": "evaluation", "cell": completed, "of": total,
                                  "case": case_id, "fleet": fleet_id}, ensure_ascii=False), flush=True)
                baseline_methods = [METHODS["D0"], METHODS["D1"]]
                baseline_outputs = {method: result_path(evaluation_dir, case_id, fleet_id, method, self.args.seed)
                                    for method in baseline_methods}
                if not self.run_cell(
                    "evaluation/{}/{}/D0_D1".format(case_id, fleet_id),
                    self.command(case_id, fleet_id, baseline_methods, evaluation_dir, compact=True),
                    baseline_outputs, model_live=False, label="paired deterministic baselines",
                ):
                    self.experiment_manifest["phases"]["evaluation"] = "blocked_or_failed"
                    self.checkpoint()
                    return False

                method = METHODS["D2"]
                path = result_path(evaluation_dir, case_id, fleet_id, method, self.args.seed)
                if not self.run_cell("evaluation/{}/{}/D2".format(case_id, fleet_id),
                                     self.command(case_id, fleet_id, [method], evaluation_dir, compact=True),
                                     {method: path}, model_live=False, label="D2 Agent optimizer"):
                    self.experiment_manifest["phases"]["evaluation"] = "blocked_or_failed"
                    self.checkpoint()
                    return False

                # Run the full deterministic comparison first. This provides
                # a reproducible D5 call-count estimate before any live LLM
                # evaluation is started.
                if self.args.phase == "deterministic":
                    continue

                d8_method = METHODS["D8"]
                d8_path = result_path(evaluation_dir, case_id, fleet_id, d8_method, self.args.seed)
                self.run_cell(
                    "evaluation/{}/{}/D8_live".format(case_id, fleet_id),
                    self.command(case_id, fleet_id, [d8_method], evaluation_dir, compact=True,
                                 live=True, nn_model=self.model_path),
                    {d8_method: d8_path}, model_live=True, label="D8 live Codex policy plus MLP/shield/optimizer",
                )

                replay = self._d8_replay_path(d8_path)
                if replay is None:
                    # A D8 rejection is recorded as a D8 outcome. D6 is still
                    # run once as its own planned method, then D7 replays D6's
                    # accepted response. This is not a retry of D8's request.
                    d6_method = METHODS["D6"]
                    d6_path = result_path(evaluation_dir, case_id, fleet_id, d6_method, self.args.seed)
                    self.run_cell(
                        "evaluation/{}/{}/D6_live_fallback_source".format(case_id, fleet_id),
                        self.command(case_id, fleet_id, [d6_method], evaluation_dir,
                                     compact=True, live=True),
                        {d6_method: d6_path}, model_live=True,
                        label="D6 live method run because D8 supplied no accepted replay policy",
                    )
                    replay = self._d8_replay_path(d6_path)
                    if replay:
                        d7_method = METHODS["D7"]
                        d7_path = result_path(evaluation_dir, case_id, fleet_id, d7_method, self.args.seed)
                        self.run_cell(
                            "evaluation/{}/{}/D7_replay_from_D6".format(case_id, fleet_id),
                            self.command(case_id, fleet_id, [d7_method], evaluation_dir,
                                         compact=True, replay=replay),
                            {d7_method: d7_path}, model_live=False,
                            label="D7 replay of its accepted D6 context policy",
                        )
                    else:
                        append_event(self.ledger, {"event": "ablation_pair_unavailable",
                                                   "run_id": "evaluation/{}/{}/D6_D7".format(case_id, fleet_id),
                                                   "reason": "neither D8 nor the planned D6 run supplied an accepted replay artifact"})
                else:
                    ablation_methods = [METHODS["D6"], METHODS["D7"]]
                    ablation_outputs = {method: result_path(evaluation_dir, case_id, fleet_id, method, self.args.seed)
                                        for method in ablation_methods}
                    self.run_cell(
                        "evaluation/{}/{}/D6_D7_replay".format(case_id, fleet_id),
                        self.command(case_id, fleet_id, ablation_methods, evaluation_dir,
                                     compact=True, replay=replay),
                        ablation_outputs, model_live=False, label="D6/D7 exact-prompt replay ablations",
                    )

                d5_method = METHODS["D5"]
                d5_path = result_path(evaluation_dir, case_id, fleet_id, d5_method, self.args.seed)
                self.run_cell(
                    "evaluation/{}/{}/D5_live".format(case_id, fleet_id),
                    self.command(case_id, fleet_id, [d5_method], evaluation_dir,
                                 compact=True, live=True),
                    {d5_method: d5_path}, model_live=True, label="D5 pure Codex LLM direct assignment",
                )

        if self.args.phase == "deterministic":
            self.experiment_manifest["phases"]["deterministic_baselines"] = "complete"
        else:
            self.experiment_manifest["phases"]["evaluation"] = "all_planned_cells_attempted_audit_pending"
        self.checkpoint()
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["train", "deterministic", "evaluate", "all"], default="all")
    parser.add_argument("--package", default=str(PACKAGE))
    parser.add_argument("--output-dir", default="outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_v1")
    parser.add_argument("--evaluation-dir", default=None, help="optional shared result root; keep each seed's manifest and no-retry ledger in its own output directory")
    parser.add_argument("--seed", type=int, default=0, help="simulation/model-run seed; use a new output directory for each independent replicate")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--progress-interval", type=int, default=60)
    args = parser.parse_args()
    if args.progress_interval < 10:
        parser.error("--progress-interval must be at least 10 seconds")
    batch = Batch(args)
    batch.experiment_manifest["status"] = "running"
    batch.checkpoint()
    if args.phase in {"train", "all"} and not batch.train():
        batch.experiment_manifest["status"] = "blocked_or_failed"
        batch.checkpoint()
        return 2
    if args.phase in {"deterministic", "evaluate", "all"} and not batch.evaluate():
        batch.experiment_manifest["status"] = "blocked_or_failed"
        batch.checkpoint()
        return 3
    batch.experiment_manifest["status"] = (
        "deterministic_baselines_ready"
        if args.phase == "deterministic"
        else "completed_runs_analysis_pending"
    )
    batch.checkpoint()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
