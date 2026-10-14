"""Shared JSON-safe data contracts for the route-layer experiment.

The module intentionally uses Python 3.9-compatible typing so the first
offline experiment can run on the available host without third-party
dependencies.
"""

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, List, Optional


class JsonModel:
    """Provide a stable JSON-compatible representation for a data record."""

    def to_dict(self) -> Dict[str, Any]:
        return _json_safe(asdict(self))


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


@dataclass
class Node(JsonModel):
    node_id: str
    x: int
    y: int
    landmark_name: Optional[str] = None
    landmark_aliases: List[str] = field(default_factory=list)
    legal_pickup: bool = False
    legal_dropoff: bool = False


@dataclass
class Edge(JsonModel):
    edge_id: str
    from_node: str
    to_node: str
    length_m: float
    base_speed_mps: float
    one_way: bool = False
    congestion_level: int = 0
    signal_delay_sec: float = 0.0


@dataclass
class RoadsideFeature(JsonModel):
    """A semantic roadside object that graph-only planners may ignore."""

    feature_id: str
    feature_type: str
    node_id: str
    label: str
    aliases: List[str] = field(default_factory=list)
    side_of_road: str = "unknown"
    orientation: str = "unknown"
    semantic_role: str = "roadside_reference"
    paired_node_id: Optional[str] = None


@dataclass
class RoadMap(JsonModel):
    map_id: str
    version: str
    size: int
    nodes: List[Node]
    edges: List[Edge]
    boundary: Dict[str, int]
    road_state_version: str = "road_state_v1"
    roadside_features: List[RoadsideFeature] = field(default_factory=list)


@dataclass
class VehicleOrderTask(JsonModel):
    task_id: str
    vehicle_id: str
    order_id: str
    start_node_id: str
    pickup_node_id: Optional[str]
    dropoff_node_id: Optional[str]
    pickup_text: Optional[str] = None
    dropoff_text: Optional[str] = None
    task_type: str = "exact_node"
    true_pickup_node_id: Optional[str] = None
    true_dropoff_node_id: Optional[str] = None


@dataclass
class RouteInputPackage(JsonModel):
    package_id: str
    map_id: str
    map_version: str
    road_state_version: str
    random_seed: int
    start_node_id: str
    pickup_node_id: Optional[str]
    dropoff_node_id: Optional[str]
    task_type: str
    pickup_text: Optional[str]
    dropoff_text: Optional[str]
    nodes: List[Node]
    edges: List[Edge]
    expansion_count: int
    serialized_token_estimate: int
    boundary: Dict[str, int] = field(default_factory=dict)
    blocked_edge_ids: List[str] = field(default_factory=list)
    alternative_route_refs: List[str] = field(default_factory=list)
    roadside_features: List[RoadsideFeature] = field(default_factory=list)


@dataclass
class RouteCandidate(JsonModel):
    route_id: str
    node_sequence: List[str] = field(default_factory=list)
    edge_sequence: List[str] = field(default_factory=list)
    route_source: str = ""
    route_reachability: bool = False
    route_distance_m: Optional[float] = None
    route_duration_sec: Optional[float] = None
    validation_status: str = "unvalidated"
    validation_errors: List[str] = field(default_factory=list)


@dataclass
class ValidationResult(JsonModel):
    valid: bool
    status: str
    errors: List[str] = field(default_factory=list)
    pickup_exact_match: bool = False
    dropoff_exact_match: bool = False
    legal_pickup: bool = False
    legal_dropoff: bool = False
    boundary_violation: bool = False
    direction_valid: bool = False


@dataclass
class ResourceUsage(JsonModel):
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    llm_latency_ms: float = 0.0
    compute_time_ms: float = 0.0
    energy_kwh: float = 0.0


@dataclass
class MonetaryCost(JsonModel):
    api_cost_amount: float = 0.0
    compute_cost_amount: float = 0.0
    electricity_cost_amount: float = 0.0
    total_monetary_cost: float = 0.0
    currency: str = "CNY"


@dataclass
class EvaluationResult(JsonModel):
    base_travel_time_sec: float = 0.0
    congestion_delay_sec: float = 0.0
    signal_delay_sec: float = 0.0
    total_travel_time_sec: float = 0.0
    energy_moving_kwh: float = 0.0
    energy_idle_signal_kwh: float = 0.0
    energy_congestion_kwh: float = 0.0
    energy_total_kwh: float = 0.0
    route_distance_m: float = 0.0
    simulation_estimate: bool = True
    energy_model_version: str = "energy_v1"


@dataclass
class RouteMethodResult(JsonModel):
    task_id: str
    method_name: str
    status: str = "completed"
    routes: List[RouteCandidate] = field(default_factory=list)
    best_route_id: Optional[str] = None
    valid_route_count: int = 0
    invalid_route_count: int = 0
    failure_reason: Optional[str] = None
    validation: Optional[ValidationResult] = None
    evaluation: Optional[EvaluationResult] = None
    resources: ResourceUsage = field(default_factory=ResourceUsage)
    monetary_cost: MonetaryCost = field(default_factory=MonetaryCost)
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw_output: Any = None


@dataclass
class CoverageReport(JsonModel):
    feasible_case: bool = False
    detour_case: bool = False
    unreachable_case: bool = False
    boundary_case: bool = False
    illegal_endpoint_case: bool = False
    semantic_landmark_case: bool = False
    legal_pickup_count: int = 0
    legal_dropoff_count: int = 0


@dataclass
class ScenarioRecord(JsonModel):
    scenario_id: str
    random_seed: int
    road_map: RoadMap
    tasks: List[VehicleOrderTask]
    coverage: CoverageReport = field(default_factory=CoverageReport)
    feature_scenarios: List["RouteFeatureScenario"] = field(default_factory=list)
    scenario_type: str = "generic"
    blocked_edge_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FormalScenarioSet(JsonModel):
    """One seed's formal coverage set and its independently runnable cases."""

    set_id: str
    random_seed: int
    road_map: RoadMap
    scenarios: List[ScenarioRecord]
    coverage: CoverageReport = field(default_factory=CoverageReport)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteFeatureScenario(JsonModel):
    """A reproducible visual/experimental case for semantic road features."""

    scenario_id: str
    title: str
    scenario_type: str
    description: str
    start_node_id: str
    pickup_candidate_node_ids: List[str]
    baseline_selected_node_id: Optional[str]
    agent_selected_node_id: Optional[str]
    dropoff_node_id: Optional[str]
    feature_ids: List[str]
    semantic_query: str
    baseline_visibility: str = "graph_only"
    agent_visibility: str = "road_feature_input_protocol_v1"
    expected_baseline_behavior: str = ""
    expected_agent_behavior: str = ""
    expected_route_effect: str = ""


@dataclass
class MapConfig(JsonModel):
    size: int = 100
    congestion_speed_multipliers: Dict[int, float] = field(
        default_factory=lambda: {0: 1.0, 1: 0.8, 2: 0.6}
    )
    signal_probability: float = 0.08
    one_way_probability: float = 0.15
    road_sign_count: int = 10
    utility_pole_count: int = 10


@dataclass
class PackageConfig(JsonModel):
    initial_margin: int = 10
    expansion_step: int = 5
    max_expansions: int = 3
    tokens_per_four_chars: float = 1.0


@dataclass
class EvaluationConfig(JsonModel):
    congestion_speed_multipliers: Dict[int, float] = field(
        default_factory=lambda: {0: 1.0, 1: 0.8, 2: 0.6}
    )
    moving_kwh_per_km: float = 0.18
    signal_idle_kwh_per_sec: float = 0.00003
    congestion_extra_kwh_per_km_level: float = 0.01
    energy_model_version: str = "energy_v1"


@dataclass
class PriceConfig(JsonModel):
    api_price_per_token: float = 0.0
    compute_price_per_ms: float = 0.0
    electricity_price_per_kwh: float = 0.0
    currency: str = "CNY"
    pricing_version: str = "price_v1"
    billing_period: str = "simulation"
    pricing_status: str = "unconfirmed"
    price_source: str = "unconfirmed"
    price_source_url: str = ""
    reference_station: str = ""
    reference_rate_type: str = ""
    observed_at: str = ""
    price_basis: str = ""


@dataclass
class RunConfig(JsonModel):
    map_size: int = 100
    seeds: List[int] = field(default_factory=lambda: list(range(10)))
    exact_ratio: float = 0.7
    llm_mode: str = "disabled"
    output_dir: str = "outputs/route_layer"
    methods: List[str] = field(
        default_factory=lambda: ["full_map_dijkstra", "dijkstra", "a_star"]
    )


@dataclass
class RawPlannerResponse(JsonModel):
    ok: bool
    payload: Any = None
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0


@dataclass
class RoadFeatureInputProtocol(JsonModel):
    protocol_name: str
    protocol_version: str
    units: Dict[str, str]
    context: Dict[str, Any]
    route_query: Dict[str, Any]
    entities: List[Dict[str, Any]]
    features: List[Dict[str, Any]]
    relations: List[Dict[str, Any]]
    constraints: List[Dict[str, Any]]
    semantic_index: List[Dict[str, Any]]
    output_contract: Dict[str, Any]
