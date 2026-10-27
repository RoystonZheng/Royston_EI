#!/usr/bin/env python3
"""Measure held-out D1-label fidelity of the frozen dispatch MLP ranker."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import sys
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dispatch_experiment.neural_ranker import NeuralCandidateRanker, candidate_feature_row


DEFAULT_PACKAGE = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/dispatch_formal_multimonth_v1")
DEFAULT_SEED0 = Path("outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_v1")
FLEETS = ["F1", "F2", "F4"]


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0]) if rows else ["case_id", "service_date", "fleet_id", "seed"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: List[float], p: float) -> float:
    values = sorted(values)
    if not values:
        return float("nan")
    index = (len(values) - 1) * p
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (index - low)


def equal_date_effects(rows: List[Dict[str, Any]], metric: str, fleet: str) -> Dict[str, Dict[int, List[float]]]:
    date_seed: Dict[str, Dict[int, List[float]]] = {}
    for row in rows:
        if fleet != "ALL" and row["fleet_id"] != fleet:
            continue
        value = row.get(metric)
        if value is not None:
            date_seed.setdefault(row["service_date"], {}).setdefault(row["seed"], []).append(float(value))
    return date_seed


def date_seed_effects(date_seed: Dict[str, Dict[int, List[float]]]) -> List[float]:
    output = []
    for per_seed in date_seed.values():
        seed_means = [sum(values) / len(values) for values in per_seed.values() if values]
        if seed_means:
            output.append(sum(seed_means) / len(seed_means))
    return output


def two_way_bootstrap(
    date_seed: Dict[str, Dict[int, List[float]]], *, iterations: int, seed: int
) -> tuple[float, float]:
    dates = sorted(date_seed)
    seeds = sorted({seed_id for per_seed in date_seed.values() for seed_id in per_seed})
    if not dates or not seeds:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sampled_dates = [rng.choice(dates) for _ in dates]
        sampled_seeds = [rng.choice(seeds) for _ in seeds]
        date_means = []
        for date in sampled_dates:
            seed_means = []
            for seed_id in sampled_seeds:
                values = date_seed.get(date, {}).get(seed_id, [])
                if values:
                    seed_means.append(sum(values) / len(values))
            if seed_means:
                date_means.append(sum(seed_means) / len(seed_means))
        if date_means:
            estimates.append(sum(date_means) / len(date_means))
    return percentile(estimates, 0.025), percentile(estimates, 0.975)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default=str(DEFAULT_PACKAGE))
    parser.add_argument("--model", required=True)
    parser.add_argument("--seed-results", nargs="*", default=None,
                        help="D1 result roots as SEED=PATH pairs; default is frozen seed 0")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--iterations", type=int, default=10000)
    args = parser.parse_args()
    if args.seed_results is None:
        seed_roots = {0: DEFAULT_SEED0}
    else:
        seed_roots = {}
        for item in args.seed_results:
            try:
                seed_text, path_text = item.split("=", 1)
                seed_id = int(seed_text)
            except (ValueError, TypeError):
                parser.error("--seed-results entries must be SEED=PATH")
            if seed_id in seed_roots:
                parser.error("each seed may be listed only once")
            seed_roots[seed_id] = ROOT / path_text
        if not seed_roots:
            parser.error("--seed-results requires at least one SEED=PATH entry")

    package = ROOT / args.package
    model_path = ROOT / args.model
    manifest = read_json(package / "benchmark_manifest.json")
    report_path = model_path.parent / "d1_pairwise_mlp_training_report.json"
    training_report = read_json(report_path)
    if not manifest or not training_report:
        raise FileNotFoundError("frozen package manifest or ranker training report is missing")
    training_ids = set(manifest.get("training_case_ids", []))
    evaluation_ids = list(manifest.get("evaluation_case_ids", []))
    overlap = training_ids.intersection(evaluation_ids)
    if overlap:
        raise ValueError("training_and_evaluation_case_ids_overlap")
    model = NeuralCandidateRanker(model_path)
    if training_report.get("model_sha256") != model.checkpoint_sha256:
        raise ValueError("ranker_checkpoint_training_report_hash_mismatch")
    max_negatives = int(training_report.get("training_data", {}).get("max_negatives_per_positive", 64))

    cell_rows = []
    missing = []
    for seed_id, result_root in sorted(seed_roots.items()):
        evaluation = result_root / "evaluation"
        for case_id in evaluation_ids:
            service_date = "-".join([case_id[3:7], case_id[7:9], case_id[9:11]])
            for fleet in FLEETS:
                path = evaluation / case_id / fleet / ("D1_optimizer_seed_" + str(seed_id) + ".json")
                payload = read_json(path)
                rounds = payload.get("round_audits", []) if payload else []
                recorded_seed = (payload.get("summary", {}).get("seed", payload.get("config", {}).get("seed"))
                                 if payload else None)
                valid_rounds = bool(rounds) and all(
                    item.get("validator_status") == "valid" and
                    not item.get("hard_constraint_errors") and
                    item.get("one_to_one_valid") is not False
                    for item in rounds
                )
                if (not payload or payload.get("partial_run") is not False or
                        payload.get("run_mode") != "complete_simulation" or
                        payload.get("method_name") != "D1_optimizer" or
                        recorded_seed != seed_id or not valid_rounds):
                    missing.append({"case_id": case_id, "fleet_id": fleet, "seed": seed_id,
                                    "path": str(path), "reason": "missing_incomplete_seed_mismatch_or_invalid_D1"})
                    continue
                config = payload.get("config", {})
                max_wait = float(config.get("max_pickup_wait_sec", 300.0))
                horizon = float(config.get("horizon_sec", 3600.0))
                pair_correct = 0.0
                pair_count = 0
                selected_edges = 0
                selected_top1 = 0
                label_lookup_misses = 0
                selected_id_length_mismatch_rounds = 0
                audit_rounds = 0
                for audit in payload.get("round_audits", []):
                    candidates = [item for item in (audit.get("common_candidate_table") or [])
                                  if item.get("hard_constraint_ok")]
                    if not candidates:
                        continue
                    rows = [candidate_feature_row(item, max_wait_sec=max_wait, horizon_sec=horizon)
                            for item in candidates]
                    scores = model.score_rows(rows)
                    score_by_id = {item["candidate_id"]: score for item, score in zip(candidates, scores)}
                    by_pair = {}
                    by_vehicle = {}
                    for item in candidates:
                        by_pair.setdefault((item.get("vehicle_id"), item.get("order_id")), []).append(item)
                        by_vehicle.setdefault(item.get("vehicle_id"), []).append(item)
                    selected_vehicle_ids = audit.get("selected_vehicle_ids", [])
                    selected_order_ids = audit.get("selected_order_ids", [])
                    if len(selected_vehicle_ids) != len(selected_order_ids):
                        selected_id_length_mismatch_rounds += 1
                        continue
                    selected_pairs = list(zip(selected_vehicle_ids, selected_order_ids))
                    selected_ids = set()
                    for vehicle_id, order_id in selected_pairs:
                        options = by_pair.get((vehicle_id, order_id), [])
                        if len(options) != 1:
                            label_lookup_misses += 1
                            continue
                        selected_ids.add(options[0]["candidate_id"])
                    for vehicle_id, order_id in selected_pairs:
                        options = by_pair.get((vehicle_id, order_id), [])
                        if len(options) != 1:
                            continue
                        positive = options[0]
                        positive_id = positive["candidate_id"]
                        positive_score = score_by_id[positive_id]
                        negatives = [item for item in candidates
                                     if item["candidate_id"] not in selected_ids and
                                     (item.get("vehicle_id") == vehicle_id or item.get("order_id") == order_id)]
                        negatives.sort(key=lambda item: hashlib.sha256(
                            (str(audit.get("dispatch_round_id", "")) + "|" + str(positive_id) + "|" +
                             str(item.get("candidate_id", ""))).encode("utf-8")
                        ).hexdigest())
                        for negative in negatives[:max_negatives]:
                            delta = positive_score - score_by_id[negative["candidate_id"]]
                            pair_correct += float(delta > 0)
                            pair_count += 1
                        vehicle_candidates = by_vehicle.get(vehicle_id, [])
                        top = min(vehicle_candidates,
                                  key=lambda item: (-score_by_id[item["candidate_id"]], item["candidate_id"]))
                        selected_edges += 1
                        selected_top1 += int(top["candidate_id"] == positive_id)
                    audit_rounds += 1
                cell_rows.append({
                    "case_id": case_id, "service_date": service_date, "fleet_id": fleet, "seed": seed_id,
                    "evaluated_rounds": audit_rounds, "selected_d1_edges": selected_edges,
                    "pairwise_conflict_pairs": pair_count,
                    "pairwise_d1_preference_accuracy": pair_correct / pair_count if pair_count else None,
                    "selected_edge_top1_agreement": selected_top1 / selected_edges if selected_edges else None,
                    "d1_label_lookup_misses": label_lookup_misses,
                    "selected_id_length_mismatch_rounds": selected_id_length_mismatch_rounds,
                    "interpretation": "held-out imitation fidelity to D1 labels; not dispatch performance",
                })

    summaries = []
    for fleet in ["ALL"] + FLEETS:
        for metric in ["pairwise_d1_preference_accuracy", "selected_edge_top1_agreement"]:
            by_date_seed = equal_date_effects(cell_rows, metric, fleet)
            effects = date_seed_effects(by_date_seed)
            if not effects:
                continue
            low, high = two_way_bootstrap(by_date_seed, iterations=args.iterations,
                                          seed=20261025 + len(summaries))
            summaries.append({
                "fleet_id": fleet, "metric": metric,
                "heldout_date_count": len(effects), "seed_count": len(seed_roots),
                "available_date_seed_fleet_cells": sum(
                    1 for row in cell_rows if row[metric] is not None and
                    (fleet == "ALL" or row["fleet_id"] == fleet)),
                "mean_equal_date_effect": sum(effects) / len(effects),
                "sd_across_date_effects": (sum((value - sum(effects) / len(effects)) ** 2
                                                for value in effects) / max(1, len(effects) - 1)) ** 0.5,
                "two_way_date_seed_bootstrap_95ci_low": low,
                "two_way_date_seed_bootstrap_95ci_high": high,
                "bootstrap_iterations": args.iterations,
                "interpretation": "held-out agreement with D1 optimizer pairwise labels; does not establish rider service benefit",
            })
    output = ROOT / args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "heldout_ranker_cell_metrics.csv", cell_rows)
    write_csv(output / "heldout_ranker_summary.csv", summaries)
    (output / "heldout_ranker_audit.json").write_text(
        json.dumps({
            "schema_version": "nyc-tlc-dispatch-ranker-holdout-v1",
            "ranker_model_sha256": model.checkpoint_sha256,
            "training_report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "training_case_ids": sorted(training_ids), "evaluation_case_ids": evaluation_ids,
            "seed_roots": {str(seed): str(path) for seed, path in sorted(seed_roots.items())},
            "complete_holdout_d1_runs": len(cell_rows),
            "expected_holdout_d1_runs": len(evaluation_ids) * len(FLEETS) * len(seed_roots),
            "missing_or_invalid_d1_runs": missing,
            "summaries": summaries,
            "limitations": [
                "This is an offline test of MLP agreement with D1 labels on the frozen held-out D1 candidate graphs.",
                "Pairwise candidates and dispatch rounds within a date are dependent; intervals resample dates and seeds, not candidate pairs.",
                "Agreement with D1 is imitation fidelity, not evidence of better service or real-world operational validity.",
            ],
        }, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output), "complete_runs": len(cell_rows),
                      "expected_runs": len(evaluation_ids) * len(FLEETS) * len(seed_roots),
                      "missing_runs": len(missing)}, ensure_ascii=False, indent=2))
    return 0 if not missing else 2


if __name__ == "__main__":
    raise SystemExit(main())
