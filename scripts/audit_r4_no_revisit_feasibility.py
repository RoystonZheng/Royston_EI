#!/usr/bin/env python3
"""Audit simple-route feasibility witnesses for frozen R4 tasks; never calls a model."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for path in (ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from prepare_r4_codex_semantic_full_route_pilot import (
    _find_no_revisit_reference_route,
    _route_nodes_to_moves,
)
from preflight_r4_codex_all_frozen_tasks import (
    DEFAULT_SEED_ROOT,
    _task_from_raw,
    read_json,
)
from preflight_r4_codex_full_route_candidate import _raw_sources
from route_experiment.models import PackageConfig, RoadMap, VehicleOrderTask
from route_experiment.r4_compact_route import validate_r4_compact_route_proposal
from route_experiment.routing import build_route_input_package, run_traditional_route
from run_r4_codex_formal_checkpoint import load_map


DEFAULT_CANDIDATE = ROOT / "outputs/route_layer/r4_codex_full_route_candidate_preflight_20261023_v5"
DEFAULT_FROZEN_PREFLIGHT = ROOT / "outputs/route_layer/r4_codex_all_frozen_tasks_preflight_20261022_v2"
DEFAULT_OUTPUT = ROOT / "outputs/route_layer/r4_no_revisit_feasibility_audit_20261023_v1"


def audit_task(
    road_map: RoadMap,
    task: VehicleOrderTask,
    blocked_edge_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Find and validate a sequential-Dijkstra no-revisit witness for one task."""

    package = build_route_input_package(
        road_map,
        task,
        PackageConfig(),
        blocked_edge_ids=blocked_edge_ids,
    )
    baseline = run_traditional_route(
        road_map,
        task,
        "local_dijkstra",
        PackageConfig(),
        blocked_edge_ids=blocked_edge_ids,
    )
    baseline_route = baseline.routes[0] if baseline.routes else None
    baseline_repeat_count = (
        len(baseline_route.node_sequence) - len(set(baseline_route.node_sequence))
        if baseline_route
        else None
    )
    witness = _find_no_revisit_reference_route(
        package,
        task.start_node_id,
        task.pickup_node_id,
        task.dropoff_node_id,
    )
    result: dict[str, Any] = {
        "baseline_status": baseline.status,
        "baseline_route_is_simple": (
            baseline_repeat_count == 0 if baseline_repeat_count is not None else None
        ),
        "baseline_repeated_node_count": baseline_repeat_count,
        "witness_status": "no_sequential_witness_found",
        "witness_edge_count": None,
        "validator_accepted": False,
        "validator_violation_count": None,
        "witness_node_sequence": [],
        "witness_edge_sequence": [],
        "witness_move_sequence": "",
        "witness_scope": "Sequential Dijkstra feasibility witness only; not a global optimum proof.",
    }
    if witness is None:
        return result

    moves = _route_nodes_to_moves(witness.node_sequence)
    validation = validate_r4_compact_route_proposal(
        road_map,
        start_node_id=task.start_node_id,
        pickup_node_id=task.pickup_node_id,
        dropoff_node_id=task.dropoff_node_id,
        blocked_edge_ids=package.blocked_edge_ids,
        payload={
            "status": "completed",
            "move_sequence": moves,
            "step_check_sequence": "1" * len(moves),
            "failure_reason_code": "none",
        },
    )
    accepted = bool(validation.get("accepted"))
    result.update(
        {
            "witness_status": "validated_witness" if accepted else "witness_validator_rejected",
            "witness_edge_count": len(witness.edge_sequence),
            "validator_accepted": accepted,
            "validator_violation_count": len(validation.get("violations", [])),
            "witness_node_sequence": list(witness.node_sequence),
            "witness_edge_sequence": list(witness.edge_sequence),
            "witness_move_sequence": moves,
        }
    )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_dataset(
    seed_root: Path,
    candidate_dir: Path,
    frozen_preflight_dir: Path,
) -> dict[str, Any]:
    candidate_path = candidate_dir / "summary.json"
    frozen_path = frozen_preflight_dir / "summary.json"
    candidate = read_json(candidate_path)
    frozen = read_json(frozen_path)
    if candidate.get("model_requests_sent") != 0 or candidate.get("chain_reachable_task_count") != 129:
        raise RuntimeError("r4_candidate_preflight_scope_or_request_gate_failed")
    if frozen.get("formal_batch_started") is not False or frozen.get("task_count") != 150:
        raise RuntimeError("frozen_stepwise_preflight_scope_or_status_failed")

    frozen_rows = {(int(row["seed"]), row["task_id"]): row for row in frozen["task_rows"]}
    rows_by_seed: dict[int, list[dict[str, Any]]] = {
        seed: [row for row in candidate["task_rows"] if int(row["seed"]) == seed]
        for seed in range(10)
    }
    rows: list[dict[str, Any]] = []
    for seed in range(10):
        seed_dir = seed_root / f"seed_{seed}"
        map_path = seed_dir / f"map_100x100_seed_{seed}.json"
        road_map = load_map(map_path)
        order_by_id, scenario_by_id = _raw_sources(seed_root, seed)
        for candidate_row in rows_by_seed[seed]:
            task_id = candidate_row["task_id"]
            frozen_row = frozen_rows[(seed, task_id)]
            if frozen_row.get("route_chain_reachable") is not True:
                raise RuntimeError(f"frozen_task_no_longer_routeable:{seed}:{task_id}")
            if candidate_row["sample_group"] == "orders":
                raw_task = order_by_id[task_id]
                blocked: tuple[str, ...] = ()
            else:
                scenario, raw_task = scenario_by_id[task_id]
                blocked = tuple(scenario.get("blocked_edge_ids", ()))
            task = _task_from_raw(
                raw_task,
                frozen_row["pickup_node_id"],
                frozen_row["dropoff_node_id"],
            )
            task_audit = audit_task(road_map, task, blocked)
            rows.append(
                {
                    "seed": seed,
                    "task_id": task_id,
                    "task_type": task.task_type,
                    "sample_group": candidate_row["sample_group"],
                    "scenario_type": candidate_row.get("scenario_type"),
                    "start_node_id": task.start_node_id,
                    "pickup_node_id": task.pickup_node_id,
                    "dropoff_node_id": task.dropoff_node_id,
                    "blocked_edge_count": len(blocked),
                    **task_audit,
                }
            )

    witness_lengths = [
        int(row["witness_edge_count"])
        for row in rows
        if row["witness_status"] == "validated_witness"
    ]
    counters = Counter(row["witness_status"] for row in rows)
    baseline_repeat_count = sum(row["baseline_route_is_simple"] is False for row in rows)
    return {
        "schema_version": "r4_no_revisit_feasibility_audit_v1",
        "status": "offline_feasibility_audit_complete_no_model_requests",
        "model_requests_sent": 0,
        "formal_r4_started": False,
        "frozen_task_count": frozen.get("task_count"),
        "candidate_chain_reachable_task_count": len(rows),
        "sequential_witness_found_and_validated_count": counters["validated_witness"],
        "witness_validator_rejected_count": counters["witness_validator_rejected"],
        "no_sequential_witness_found_count": counters["no_sequential_witness_found"],
        "baseline_local_dijkstra_routes_with_revisits": baseline_repeat_count,
        "revisited_baseline_tasks_with_replacement_witness": sum(
            row["baseline_route_is_simple"] is False
            and row["witness_status"] == "validated_witness"
            for row in rows
        ),
        "witness_edge_count_stats": {
            "count": len(witness_lengths),
            "min": min(witness_lengths) if witness_lengths else None,
            "median": sorted(witness_lengths)[(len(witness_lengths) - 1) // 2]
            if witness_lengths
            else None,
            "max": max(witness_lengths) if witness_lengths else None,
            "within_r4_512_edge_limit": sum(length <= 512 for length in witness_lengths),
        },
        "source_inputs": {
            "candidate_preflight": str(candidate_path.relative_to(ROOT)),
            "candidate_preflight_sha256": _sha256(candidate_path),
            "frozen_preflight": str(frozen_path.relative_to(ROOT)),
            "frozen_preflight_sha256": _sha256(frozen_path),
            "seed_root": str(seed_root.relative_to(ROOT)),
        },
        "limitations": [
            "找到并通过共用 validator 的见证证明任务存在合法不重访路径；未找到只表示本策略未找到，不能判为不可达。",
            "见证由第一段避免提前经过 dropoff 的最短路，加上避开第一段已访问节点的第二段最短路构成；不是联合全局最优解。",
            "见证路径用于离线可行性分类，不作为 R4 模型输入、fallback、正式路线结果或路线性能数据。",
            "本审计不计算车辆行驶电耗，也不修改传统实验原始文件。",
        ],
        "task_rows": rows,
    }


def render(summary: dict[str, Any], output_dir: Path) -> None:
    rows = []
    for row in summary["task_rows"]:
        group = "订单" if row["sample_group"] == "orders" else "正式场景/" + str(row["scenario_type"])
        route_nodes = " → ".join(row["witness_node_sequence"])
        rows.append(
            "<tr><td>{seed}</td><td>{task}</td><td>{group}</td><td>{baseline}</td>"
            "<td>{status}</td><td>{length}</td><td>{validation}</td><td><details><summary>路线节点</summary><code>{path}</code></details></td></tr>".format(
                seed=row["seed"],
                task=html.escape(row["task_id"]),
                group=html.escape(group),
                baseline=("跨段重复" if row["baseline_route_is_simple"] is False else "无重复"),
                status=("已找到见证" if row["witness_status"] == "validated_witness" else row["witness_status"]),
                length=(row["witness_edge_count"] if row["witness_edge_count"] is not None else "—"),
                validation=("通过" if row["validator_accepted"] else "未通过/未找到"),
                path=html.escape(route_nodes),
            )
        )
    index = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>R4 全程不重访可行性审计</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;margin:24px;color:#1f2937}}h1{{margin-bottom:8px}}.notice{{padding:12px;background:#fff7e6;border:1px solid #e9cf92;border-radius:8px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid #ddd;padding:8px;text-align:left;vertical-align:top}}th{{background:#eef2ff;position:sticky;top:0}}code{{display:block;max-height:160px;overflow:auto;white-space:normal}}details{{max-width:420px}}</style>
<h1>R4 全程不重访可行性审计</h1><p>状态：纯本地确定性搜索；模型请求 0；不是正式路线实验结果。</p><div class='notice'>见证路线由顺序 Dijkstra 策略构造并通过整条路线 validator。它只证明当前端点存在合法路线，不是联合全局最优路线，也没有输入给 R4 模型。</div>
<p>冻结可达任务：{total}；validator 接受见证：{valid}；原分段 local Dijkstra 跨段重复：{repeated}；其中有替代见证：{replacements}；最长见证：{max_edges} 边。</p>
<p><a href='summary.json'>机器可读记录</a> · <a href='../../traditional_no_revisit_audit_20261023_v2/index.html'>传统组原始路线审计</a> · <a href='../r4_codex_full_route_candidate_preflight_20261023_v5/index.html'>R4 冻结任务输入预检</a></p>
<table><thead><tr><th>种子</th><th>任务</th><th>组别</th><th>原参考路线</th><th>见证状态</th><th>边数</th><th>共用校验</th><th>见证节点序列</th></tr></thead><tbody>{rows}</tbody></table></html>""".format(
        total=summary["candidate_chain_reachable_task_count"],
        valid=summary["sequential_witness_found_and_validated_count"],
        repeated=summary["baseline_local_dijkstra_routes_with_revisits"],
        replacements=summary["revisited_baseline_tasks_with_replacement_witness"],
        max_edges=summary["witness_edge_count_stats"]["max"],
        rows="".join(rows),
    )
    (output_dir / "index.html").write_text(index, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-root", type=Path, default=DEFAULT_SEED_ROOT)
    parser.add_argument("--candidate-preflight", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--frozen-preflight", type=Path, default=DEFAULT_FROZEN_PREFLIGHT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("refusing_to_overwrite_existing_feasibility_audit")
    result = audit_dataset(args.seed_root, args.candidate_preflight, args.frozen_preflight)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    render(result, args.output_dir)
    print(json.dumps({key: value for key, value in result.items() if key != "task_rows"}, ensure_ascii=False, indent=2))
    print(f"output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
