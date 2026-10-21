"""JSON-safe contracts for the dispatch-agent experiment.

The contracts intentionally use only the Python standard library.  They keep
the simulation auditable: every decision records the state snapshot, the
candidate facts, the hard-constraint result and the selected fallback.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


class JsonModel:
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DispatchConfig(JsonModel):
    map_size: int = 20
    vehicle_count: int = 10
    order_count: int = 100
    horizon_sec: int = 3600
    max_pickup_wait_sec: float = 300.0
    post_arrival_boarding_timeout_sec: float = 180.0
    passenger_no_show_rate: float = 0.05
    pickup_sampling_mode: str = "equal_probability_area_weighted_hotspots_v1"
    max_next_reservation_per_vehicle: int = 1
    moving_kwh_per_km: float = 0.18
    electricity_price_per_kwh: float = 2.29
    method_name: str = "D4_agent_llm_feedback_optimizer"
    context_function_mode: str = "manual_linear_clip"
    feedback_mode: str = "offline_rule"
    seed: int = 0
    schema_version: str = "dispatch-agent-v1"
    llm_backend: str = "codex_cli"
    llm_model: str = "gpt-5.6-luna"


@dataclass
class Order(JsonModel):
    order_id: str
    arrival_time_sec: float
    pickup_node_id: str
    dropoff_node_id: str
    hotspot_id: str
    passenger_no_show: bool = False
    status: str = "future"
    assigned_vehicle_id: Optional[str] = None
    assignment_time_sec: Optional[float] = None
    pickup_arrival_time_sec: Optional[float] = None
    boarding_time_sec: Optional[float] = None
    dropoff_time_sec: Optional[float] = None
    cancel_time_sec: Optional[float] = None
    cancel_reason: Optional[str] = None


@dataclass
class Vehicle(JsonModel):
    vehicle_id: str
    initial_position_node_id: str
    position_node_id: str
    state: str = "idle"
    current_order_id: Optional[str] = None
    reserved_order_id: Optional[str] = None
    current_pickup_node_id: Optional[str] = None
    current_dropoff_node_id: Optional[str] = None
    available_time_sec: float = 0.0
    planned_pickup_time_sec: Optional[float] = None
    planned_dropoff_time_sec: Optional[float] = None
    boarding_deadline_time_sec: Optional[float] = None
    last_assignment_time_sec: Optional[float] = None
    total_busy_sec: float = 0.0
    total_empty_distance_m: float = 0.0
    total_energy_kwh: float = 0.0


@dataclass
class RouteLeg(JsonModel):
    reachable: bool
    distance_m: float = 0.0
    duration_sec: float = 0.0
    node_sequence: List[str] = field(default_factory=list)
    edge_sequence: List[str] = field(default_factory=list)
    reason: Optional[str] = None


@dataclass
class Candidate(JsonModel):
    candidate_id: str
    dispatch_round_id: str
    vehicle_id: str
    order_id: str
    candidate_type: str
    route_reachability: bool
    pickup_leg: RouteLeg
    ride_leg: RouteLeg
    vehicle_available_time_sec: float
    predicted_pickup_time_sec: float
    predicted_dropoff_time_sec: float
    predicted_wait_sec: float
    empty_distance_m: float
    total_distance_m: float
    energy_kwh: float
    current_commitment_ok: bool
    one_to_one_ok: bool
    time_non_overlap_ok: bool
    max_wait_ok: bool
    hard_constraint_ok: bool
    feature_vector: Dict[str, float] = field(default_factory=dict)
    context_threshold_sec: Optional[float] = None
    context_pass: bool = True
    feedback_decision: Optional[str] = None
    feedback_risk: Optional[float] = None
    feedback_reason: Optional[str] = None
    rejection_reason: Optional[str] = None


@dataclass
class CandidateDecision(JsonModel):
    candidate_id: str
    agent_id: str
    filter_stage: str
    agent_decision: str
    hard_constraint_pass: bool
    context_threshold_sec: Optional[float]
    context_pass: bool
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class Assignment(JsonModel):
    assignment_id: str
    candidate_id: str
    vehicle_id: str
    order_id: str
    assignment_type: str
    predicted_pickup_time_sec: float
    predicted_dropoff_time_sec: float
    predicted_wait_sec: float


@dataclass
class DispatchInput(JsonModel):
    dispatch_round_id: str
    experiment_run_id: str
    snapshot_time_sec: float
    trigger_type: str
    vehicles: List[Vehicle]
    orders: List[Order]
    candidates: List[Candidate]
    max_pickup_wait_sec: float
    post_arrival_boarding_timeout_sec: float
    one_to_one_rule: bool = True
    max_next_reservation_per_vehicle: int = 1
    objective_priority: List[str] = field(
        default_factory=lambda: [
            "preserve_commitments",
            "maximize_served_orders",
            "minimize_wait",
            "minimize_empty_distance",
            "minimize_energy",
        ]
    )
    schema_version: str = "dispatch-agent-v1"
    future_order_visibility: bool = False


@dataclass
class DispatchOutput(JsonModel):
    decision_package_id: str
    dispatch_round_id: str
    method_name: str
    method_version: str
    assignments: List[Assignment] = field(default_factory=list)
    unserved_order_ids: List[str] = field(default_factory=list)
    selected_candidate_ids: List[str] = field(default_factory=list)
    candidate_decisions: List[CandidateDecision] = field(default_factory=list)
    optimizer_status: str = "not_used"
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    validator_status: str = "valid"
    hard_constraint_errors: List[str] = field(default_factory=list)
    agent_time_ms: float = 0.0
    function_time_ms: float = 0.0
    llm_time_ms: float = 0.0
    optimizer_time_ms: float = 0.0
    total_decision_time_ms: float = 0.0
    llm_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    api_cost: Optional[float] = 0.0
    api_cost_usd: Optional[float] = None
    audit: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatchRoundRecord(JsonModel):
    dispatch_round_id: str
    trigger_type: str
    snapshot_time_sec: float
    input: DispatchInput
    output: DispatchOutput
    candidates: List[Candidate]


@dataclass
class ExperimentSummary(JsonModel):
    experiment_run_id: str
    method_name: str
    seed: int
    map_id: str
    vehicle_count: int
    order_count: int
    served_orders: int = 0
    unserved_orders: int = 0
    cancelled_orders: int = 0
    service_rate: float = 0.0
    average_pickup_wait_sec: Optional[float] = None
    p95_pickup_wait_sec: Optional[float] = None
    max_pickup_wait_sec: Optional[float] = None
    total_empty_distance_m: float = 0.0
    total_energy_kwh: float = 0.0
    total_dispatch_rounds: int = 0
    total_llm_calls: int = 0
    total_llm_input_tokens: Optional[int] = None
    total_llm_cached_input_tokens: Optional[int] = None
    total_llm_output_tokens: Optional[int] = None
    total_llm_tokens: Optional[int] = None
    llm_usage_status: str = "not_recorded"
    total_codex_cli_invocations: int = 0
    total_api_cost: Optional[float] = 0.0
    total_api_cost_usd: Optional[float] = None
    total_decision_time_ms: float = 0.0
    optimizer_time_ms: float = 0.0
    candidate_count_before_filter: int = 0
    candidate_count_after_filter: int = 0
    fallback_count: int = 0
    commitment_violations: int = 0
    status_counts: Dict[str, int] = field(default_factory=dict)

