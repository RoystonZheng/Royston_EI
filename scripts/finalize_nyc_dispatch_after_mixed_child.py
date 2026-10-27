#!/usr/bin/env python3
"""Wait for the approved in-flight mixed NYC cell, then finalize offline indexes.

This helper does not launch a dispatch run or contact a model. Once the already
running D8 child exits, it stops the paused legacy batch, reruns the offline
recalculation index builder and integrity audit, and records the final status.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
CHILD_PID = 85820
PARENT_PID = 26615
CAFFEINATE_PID = 26616
CHILD_MARKER = "scripts/run_nyc_tlc_dispatch_benchmark.py"
PARENT_MARKER = "scripts/run_nyc_tlc_dispatch_multimonth_batch.py"
CASE_MARKER = "EV_20240521_210000_q50"
FLEET_MARKER = "F4"
RECALC_DIR = Path(
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_multimonth_energy_v2_offline_20261026"
)
RECALC_SCRIPT = ROOT / "scripts/recalculate_nyc_dispatch_energy_offline.py"
AUDIT_SCRIPT = ROOT / "scripts/audit_nyc_dispatch_energy_recalculation.py"
RECLASS_PATH = ROOT / (
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_multimonth_replicates_v1/"
    "energy_model_reclassification_20261026.json"
)
PAPER_PATH = ROOT / "output/paper/EI论文_阶段性可写稿_20261024.md"
MEMORY_PATH = ROOT / "output/paper/研究记忆库.md"
LOG_PATH = ROOT / (
    "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
    "dispatch_formal_multimonth_energy_v2_offline_20261026/"
    "post_child_finalization.log"
)


def local_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def log(message: str) -> None:
    stamp = local_now().isoformat(timespec="seconds")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp} {message}\n")
    print(f"{stamp} {message}", flush=True)


def process_command(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    command = result.stdout.strip()
    return command or None


def child_is_our_process() -> bool:
    command = process_command(CHILD_PID)
    return bool(
        command
        and CHILD_MARKER in command
        and CASE_MARKER in command
        and FLEET_MARKER in command
        and "D8_agent_codex_nn_capacity_shield_optimizer" in command
    )


def stop_if_matching(pid: int, marker: str, sig: int) -> None:
    command = process_command(pid)
    if command and marker in command and "--seed 3" in command:
        try:
            os.kill(pid, sig)
            log(f"sent signal {sig} to PID {pid} after identity check")
        except ProcessLookupError:
            pass
    elif command:
        log(f"left PID {pid} untouched because identity no longer matches")


def run_offline(script: Path) -> None:
    command = [sys.executable, str(script), "--output-dir", str(RECALC_DIR)]
    if script == RECALC_SCRIPT:
        command = [
            sys.executable,
            str(script),
            "--seeds", "0", "1", "2", "3",
            "--output-dir", str(RECALC_DIR),
        ]
    log("starting offline command: " + " ".join(command))
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        raise RuntimeError(f"offline command failed ({result.returncode}): {script.name}")


def update_records(audit: dict, result_manifest: dict, d8_output_present: bool) -> None:
    reclass = json.loads(RECLASS_PATH.read_text(encoding="utf-8"))
    now = local_now()
    stamp = now.strftime("%Y-%m-%d %H:%M CST")
    reclass["updated_at_local"] = now.isoformat(timespec="seconds")
    reclass["current_execution"] = (
        "The previously started D8/F4 child has exited. Its output-presence status is "
        + ("result file present" if d8_output_present else "no complete D8 result file observed")
        + ". The whole EV_20240521_210000_q50/F4 mixed-code cell remains excluded. "
        "The paused seed 3 parent batch and its caffeinate process were stopped after "
        "the child exited. Offline indexes and integrity audit were rebuilt and passed; "
        "no new formal rerun or model call was made. Seed 4 was not started."
    )
    reclass["in_flight_mixed_cell"]["benchmark_child_status"] = (
        "child exited; "
        + ("D8 result file preserved and excluded with the mixed cell" if d8_output_present
           else "no complete D8 result JSON observed")
        + "; entire cell excluded from paired analysis"
    )
    offline = reclass["offline_recalculation"]
    offline["completed_at_local"] = audit["created_at_local"]
    offline["source_results_scanned"] = result_manifest["input_result_count"]
    offline["verified_existing_output_files"] = result_manifest["recalculated_count"]
    offline["output_coverage_complete"] = result_manifest["output_coverage_complete"]
    offline["excluded_source_results"] = result_manifest["excluded_count"]
    offline["unrecomputable_complete_inputs"] = result_manifest["unrecomputable_count"]
    offline["expected_missing_method_results"] = result_manifest["expected_missing_result_count"]
    offline["integrity_audit_status"] = audit["status"]
    for summary in offline["seed_summaries"]:
        current = next(item for item in audit["seed_summaries"] if item["seed"] == summary["seed"])
        summary["source_results_scanned"] = current["source_result_count"]
        summary["verified_outputs"] = current["recalculated_output_count"]
        summary["paired_completed_cells"] = current["paired_completed_cells"]
        summary["missing_methods"] = current["missing_expected_method_result_count"]
    RECLASS_PATH.write_text(
        json.dumps(reclass, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    paper = PAPER_PATH.read_text(encoding="utf-8")
    waiting = (
        "seed 3 父进程 PID 26615 保持暂停，已启动的 D8 子进程 PID 85820 仍在计算该混合格；"
        "该格排除，seed 4 未启动。"
    )
    completed = (
        "seed 3 混合格的 D8 子进程已结束并完成文件核对；"
        "整个混合代码格仍排除，旧批次已停止，seed 4 未启动。"
    )
    paper = paper.replace(waiting, completed, 1)
    paper = paper.replace(
        "截至 2026-10-26 15:23 CST",
        "截至 " + now.strftime("%Y-%m-%d %H:%M CST"),
        1,
    )
    PAPER_PATH.write_text(paper, encoding="utf-8")

    memory_entry = (
        f"\n- {stamp}（NYC seed3 混合单元结束与离线总索引复核）：既有 D8/F4 子进程已退出；"
        + ("结果 JSON 已保留，并按冻结规则与同格旧 D0/D1/D2 一并排除。" if d8_output_present
           else "未发现完整 D8 结果 JSON；混合格仍按规则整体排除。")
        + f"重新生成的离线总 manifest 扫描 {result_manifest['input_result_count']} 份源结果，"
        f"验证 {result_manifest['recalculated_count']} 份 v2 能耗文件，排除 {result_manifest['excluded_count']} 份，"
        f"预期缺失 {result_manifest['expected_missing_result_count']} 份；独立 audit 状态 `{audit['status']}`，"
        f"errors={len(audit['errors'])}。暂停的旧 seed3 批次与 caffeinate 已停止，seed4 未启动；"
        "没有新增模型调用或策略重跑。完整性审计与索引文件位于 "
        "`outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/dispatch_formal_multimonth_energy_v2_offline_20261026/`。"
        "证据类型：子进程终态、源文件清单、离线重算 manifest/index 与独立完整性审计；"
        "该事后能耗估算仍不等于 v2 感知的派单策略实验。\n"
    )
    with MEMORY_PATH.open("a", encoding="utf-8") as stream:
        stream.write(memory_entry)


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not child_is_our_process():
        log("target child is already absent or its PID no longer matches; proceeding with offline finalization")
    else:
        log(f"waiting for already-running D8 child PID {CHILD_PID}; no new dispatch run will be started")
        while child_is_our_process():
            time.sleep(30)
        log("target D8 child exited")

    # Stop only the exact, already-paused seed 3 parent and its caffeinate child.
    stop_if_matching(CAFFEINATE_PID, "caffeinate -dimsu", signal.SIGTERM)
    stop_if_matching(PARENT_PID, PARENT_MARKER, signal.SIGKILL)

    d8_candidates = list(
        (ROOT / "outputs/dispatch_layer/nyc_tlc_hvfhv_2024/results/"
         "dispatch_formal_multimonth_replicates_v1/seed_3/evaluation").rglob(
            "D8_agent_codex_nn_capacity_shield_optimizer_seed_3.json"
        )
    )
    d8_output_present = any(
        CASE_MARKER in str(path) and FLEET_MARKER in str(path) for path in d8_candidates
    )

    run_offline(RECALC_SCRIPT)
    run_offline(AUDIT_SCRIPT)
    manifest_path = ROOT / RECALC_DIR / "manifest.json"
    audit_path = ROOT / RECALC_DIR / "integrity_audit.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("status") != "passed":
        raise RuntimeError("final integrity audit did not pass")
    if not manifest.get("output_coverage_complete"):
        raise RuntimeError("final manifest reports incomplete output artifact coverage")
    update_records(audit, manifest, d8_output_present)
    log(
        "finalization complete: scanned={}, verified={}, excluded={}, missing={}, audit={}".format(
            manifest["input_result_count"],
            manifest["recalculated_count"],
            manifest["excluded_count"],
            manifest["expected_missing_result_count"],
            audit["status"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
