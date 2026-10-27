"""Versioned decision-function descriptions written into dispatch results."""

from __future__ import annotations

from typing import Any, Dict, Optional


CONTRACT_VERSION = "nyc_dispatch_decision_contract_v4"

_METHOD_STEPS = {
    "D0_greedy": [
        "Keep only candidates satisfying route, commitment, one-to-one, and max-wait hard constraints.",
        "Sort by request arrival time, predicted pickup wait, empty distance, order ID, then vehicle ID.",
        "Accept the first candidate for each unused vehicle and order.",
    ],
    "D1_optimizer": [
        "Keep only hard-feasible vehicle-order candidates.",
        "Solve a one-to-one minimum-cost bipartite flow with the exact lexicographic objective recorded per round.",
    ],
    "D2_agent_optimizer": [
        "Each vehicle agent builds pickup and ride candidates and applies the bounded manual linear-clip context function.",
        "If the context function removes every hard-feasible candidate for an agent, restore that agent's hard-feasible candidates.",
        "Apply the exact lexicographic global optimizer to the remaining candidates.",
    ],
    "D3_agent_llm_context_optimizer": [
        "Each vehicle agent builds hard-feasible candidates and applies the frozen offline context-policy surrogate.",
        "If the context function removes every hard-feasible candidate for an agent, restore that agent's hard-feasible candidates.",
        "Apply the exact lexicographic global optimizer; this method uses no live LLM call.",
    ],
    "D4_agent_llm_feedback_optimizer": [
        "Apply the frozen offline feedback-rule surrogate to review candidates without changing hard constraints.",
        "Apply the bounded context filter and restore hard-feasible candidates if a vehicle would otherwise have none.",
        "Apply the exact lexicographic global optimizer; this method uses no live LLM call.",
    ],
    "D5_pure_llm_direct": [
        "Codex CLI proposes vehicle-order assignments from the shared candidate facts, including pickup and passenger-segment EV v2 signed-net energy.",
        "The deterministic validator rejects unknown, duplicate, unreachable, over-wait, or commitment-violating assignments.",
        "No global mathematical optimizer is applied after the LLM response.",
    ],
    "D6_agent_codex_context_optimizer": [
        "Codex CLI supplies bounded linear-clip context parameters for vehicle agents; parameters cannot alter hard constraints.",
        "Agents retain candidates whose predicted pickup wait is within the generated threshold.",
        "If a vehicle has no context-approved candidate, restore its hard-feasible candidates.",
        "Apply the exact lexicographic global optimizer.",
    ],
    "D7_agent_codex_capacity_shield_optimizer": [
        "Apply the D6 Codex context policy.",
        "Restore rejected hard-feasible candidates in frozen priority order until the filtered graph preserves the original maximum one-to-one matching cardinality.",
        "Apply the exact lexicographic global optimizer.",
    ],
    "D8_agent_codex_nn_capacity_shield_optimizer": [
        "Apply the D7 Codex context policy.",
        "Score hard-feasible candidates with the frozen D1-imitation MLP and retain the configured Top-K candidates per vehicle, prioritizing context-approved candidates.",
        "Apply the maximum-cardinality shield so pruning cannot reduce feasible matching capacity.",
        "Apply the exact lexicographic global optimizer.",
    ],
}


def decision_function_record(
    method_name: str,
    *,
    optimizer_metadata: Optional[Dict[str, Any]] = None,
    pickup_energy_proxy_mode: str = "centroid_start_stop_ev2_v1",
) -> Dict[str, Any]:
    """Return the versioned policy contract and optional round-specific scales."""
    if method_name not in _METHOD_STEPS:
        raise ValueError("unknown_method_for_decision_function_contract")
    uses_global_optimizer = method_name not in {"D0_greedy", "D5_pure_llm_direct"}
    if method_name == "D0_greedy":
        final_rule = "deterministic_greedy_v1"
    elif method_name == "D5_pure_llm_direct":
        final_rule = "codex_direct_assignment_with_deterministic_validator_v1"
    else:
        final_rule = "global_min_cost_flow_lexicographic_v2"
    energy_dependency = (
        "not_used_by_the_assignment_rule"
        if method_name == "D0_greedy"
        else (
            "pickup_and_ride_signed_net_energy_are_visible_to_the_llm_but_no_deterministic_energy_objective_is_applied"
            if method_name == "D5_pure_llm_direct"
            else "pickup_and_ride_ev_v2_signed_net_energy_sum_is_the_last_global_optimizer_objective_tier"
        )
    )
    record: Dict[str, Any] = {
        "schema": CONTRACT_VERSION,
        "method_name": method_name,
        "hard_constraints": [
            "pickup and ride routes are reachable",
            "predicted pickup wait is at most max_pickup_wait_sec",
            "one vehicle serves at most one order per dispatch round",
            "one order is assigned to at most one vehicle per dispatch round",
            "existing vehicle commitments and reservation limits are preserved",
        ],
        "method_steps": list(_METHOD_STEPS[method_name]),
        "final_assignment_rule": final_rule,
        "uses_global_optimizer": uses_global_optimizer,
        "energy_dependency": energy_dependency,
        "energy_input_contract": {
            "model_version": "ev_longitudinal_v2",
            "pickup_leg": (
                "synthetic_zone_centroid_distance_time_with_ev_v2_start_stop_motion_profile"
                if pickup_energy_proxy_mode == "centroid_start_stop_ev2_v1"
                else "legacy_empty_edge_sequence_zero"
            ),
            "ride_leg": "direct_observed_zone_od_edge_conditions_with_ev_v2",
            "missing_positive_distance_edge_sequence": "fail_closed_unless_explicit_synthetic_pickup_proxy",
            "optimizer_energy_basis": "sum_of_signed_net_pickup_and_ride_kwh",
            "segment_billing_basis": "sum_signed_link_energy_within_segment_then_max(net_kwh,0)",
            "observed_fare_basis": "NYC TLC base_passenger_fare in USD; signed values retained; historical proxy, not realized revenue",
            "fare_usage": "observed fare is recorded on the passenger service segment; partial joins remain explicitly null and never remove candidates",
            "method_cost_scope": "Codex/API usage and compute cost are recorded once per method, never apportioned to route segments",
            "monetary_score_status": "not_applied_to_assignment; any time-money route-choice score is blocked without frozen electricity tariff, method cost rates, and a common alternative-route pool",
        },
    }
    if uses_global_optimizer:
        record["global_optimizer"] = {
            "version": "global_min_cost_flow_lexicographic_v2",
            "priority_order": [
                "maximize_served_orders",
                "minimize_total_pickup_wait",
                "minimize_total_empty_distance",
                "minimize_total_ev_energy",
            ],
            "per_round_integer_scales": (
                dict(optimizer_metadata) if optimizer_metadata else "recorded_in_each_round_audit"
            ),
        }
    return record
