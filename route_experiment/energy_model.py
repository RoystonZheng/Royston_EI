"""Brand-neutral EV longitudinal energy model shared by route and dispatch.

The model uses a generic reference EV and explicit motion traces. It does not
use a Tesla vehicle-consumption curve or a per-kilometre energy adder.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


MODEL_VERSION = "ev_longitudinal_v2"


@dataclass(frozen=True)
class EnergyModelConfig:
    profile_id: str = "generic_reference_ev_v1"
    curb_mass_kg: float = 1900.0
    passenger_load_kg: float = 75.0
    rotating_mass_kg: float = 50.0
    rolling_resistance_coefficient: float = 0.009
    drag_coefficient: float = 0.26
    frontal_area_m2: float = 2.35
    air_density_kg_m3: float = 1.225
    propulsion_efficiency: float = 0.90
    regenerative_efficiency: float = 0.65
    auxiliary_power_w: float = 800.0
    max_acceleration_mps2: float = 1.2
    max_deceleration_mps2: float = 2.0
    regenerative_power_cap_w: float = 50000.0
    gravity_mps2: float = 9.80665
    model_version: str = MODEL_VERSION

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MotionStep:
    duration_sec: float
    speed_start_mps: float
    speed_end_mps: float
    slope_rad: Optional[float] = None
    acceleration_mps2: Optional[float] = None


@dataclass
class EnergyResult:
    distance_m: float = 0.0
    drive_time_sec: float = 0.0
    signal_idle_time_sec: float = 0.0
    rolling_wheel_kwh: float = 0.0
    aerodynamic_wheel_kwh: float = 0.0
    grade_wheel_kwh: float = 0.0
    acceleration_wheel_kwh: float = 0.0
    propulsion_draw_kwh: float = 0.0
    regenerated_kwh: float = 0.0
    auxiliary_kwh: float = 0.0
    net_kwh: float = 0.0
    regen_power_capped_steps: int = 0
    missing_grade_steps: int = 0
    trace_fidelity: str = "observed_motion_trace"
    model_version: str = MODEL_VERSION
    profile_id: str = "generic_reference_ev_v1"
    profile_sha256: str = ""
    edge_breakdown: List[Dict[str, Any]] = field(default_factory=list)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def evaluate_motion_trace(
    steps: Iterable[MotionStep],
    config: Optional[EnergyModelConfig] = None,
    *,
    payload_mass_kg: Optional[float] = None,
    idle_durations_sec: Sequence[float] = (),
    trace_fidelity: str = "observed_motion_trace",
) -> EnergyResult:
    """Integrate wheel forces, propulsion, regen and auxiliary load.

    Speed and acceleration are SI units. Grade is an angle in radians. Missing
    grade is treated as flat and counted in the result's fidelity metadata.
    """

    config = config or EnergyModelConfig()
    payload = config.passenger_load_kg if payload_mass_kg is None else float(payload_mass_kg)
    if payload < 0:
        raise ValueError("payload_mass_kg_must_be_nonnegative")
    if not (0.0 < config.propulsion_efficiency <= 1.0):
        raise ValueError("propulsion_efficiency_must_be_in_(0,1]")
    if not (0.0 <= config.regenerative_efficiency <= 1.0):
        raise ValueError("regenerative_efficiency_must_be_in_[0,1]")
    if config.regenerative_power_cap_w < 0 or config.auxiliary_power_w < 0:
        raise ValueError("power_limits_must_be_nonnegative")
    mass = config.curb_mass_kg + payload
    effective_inertial_mass = mass + config.rotating_mass_kg
    result = EnergyResult(
        trace_fidelity=trace_fidelity,
        model_version=config.model_version,
        profile_id=config.profile_id,
        profile_sha256=config.fingerprint(),
    )
    for step in steps:
        dt = float(step.duration_sec)
        v0 = float(step.speed_start_mps)
        v1 = float(step.speed_end_mps)
        if dt <= 0 or v0 < 0 or v1 < 0:
            raise ValueError("motion_step_requires_positive_duration_and_nonnegative_speed")
        average_speed = (v0 + v1) / 2.0
        distance = average_speed * dt
        acceleration = (
            float(step.acceleration_mps2)
            if step.acceleration_mps2 is not None
            else (v1 - v0) / dt
        )
        slope_missing = step.slope_rad is None
        slope = 0.0 if slope_missing else float(step.slope_rad)
        rolling_force = (
            config.rolling_resistance_coefficient
            * mass
            * config.gravity_mps2
            * math.cos(slope)
        )
        aerodynamic_force = (
            0.5
            * config.air_density_kg_m3
            * config.drag_coefficient
            * config.frontal_area_m2
            * average_speed
            * average_speed
        )
        grade_force = mass * config.gravity_mps2 * math.sin(slope)
        acceleration_force = effective_inertial_mass * acceleration
        total_force = rolling_force + aerodynamic_force + grade_force + acceleration_force
        wheel_power_w = total_force * average_speed
        wheel_work_kwh = wheel_power_w * dt / 3_600_000.0

        result.distance_m += distance
        result.drive_time_sec += dt
        result.rolling_wheel_kwh += rolling_force * distance / 3_600_000.0
        result.aerodynamic_wheel_kwh += aerodynamic_force * distance / 3_600_000.0
        result.grade_wheel_kwh += grade_force * distance / 3_600_000.0
        result.acceleration_wheel_kwh += acceleration_force * distance / 3_600_000.0
        if wheel_power_w >= 0:
            result.propulsion_draw_kwh += wheel_work_kwh / config.propulsion_efficiency
        else:
            requested_regen_w = -wheel_power_w * config.regenerative_efficiency
            accepted_regen_w = min(requested_regen_w, config.regenerative_power_cap_w)
            if requested_regen_w > config.regenerative_power_cap_w:
                result.regen_power_capped_steps += 1
            result.regenerated_kwh += accepted_regen_w * dt / 3_600_000.0
        result.auxiliary_kwh += config.auxiliary_power_w * dt / 3_600_000.0
        if slope_missing:
            result.missing_grade_steps += 1

    for idle in idle_durations_sec:
        idle_s = float(idle)
        if idle_s < 0:
            raise ValueError("idle_duration_must_be_nonnegative")
        result.signal_idle_time_sec += idle_s
        result.auxiliary_kwh += config.auxiliary_power_w * idle_s / 3_600_000.0
    result.net_kwh = result.propulsion_draw_kwh - result.regenerated_kwh + result.auxiliary_kwh
    return result


def evaluate_route_edges(
    edges: Sequence[Any],
    congestion_speed_multipliers: Dict[int, float],
    config: Optional[EnergyModelConfig] = None,
    *,
    payload_mass_kg: Optional[float] = None,
    initial_speed_mps: float = 0.0,
    terminal_stop: bool = True,
    spatial_step_m: float = 5.0,
) -> EnergyResult:
    """Reconstruct a deterministic speed profile from route edge conditions.

    Congestion changes the speed cap through the experiment's frozen speed
    multiplier. Signal delays create explicit stop, idle and restart events.
    This adapter does not infer queue counts or add a congestion energy fee.
    """

    config = config or EnergyModelConfig()
    if initial_speed_mps < 0 or spatial_step_m <= 0:
        raise ValueError("invalid_route_motion_parameters")
    if not edges:
        return evaluate_motion_trace(
            [], config, payload_mass_kg=payload_mass_kg,
            trace_fidelity="estimated_from_route_edge_conditions",
        )

    # Each interval carries the edge whose grade and state apply to it.
    interval_distances: List[float] = []
    interval_edges: List[Any] = []
    point_caps: List[float] = [float("inf")]
    stop_points = set()
    point_index = 0
    signal_delays: List[float] = []

    for edge in edges:
        length = float(_value(edge, "length_m", 0.0))
        base_speed = float(_value(edge, "base_speed_mps", 0.0))
        level = int(_value(edge, "congestion_level", 0))
        delay = float(_value(edge, "signal_delay_sec", 0.0))
        if length < 0 or base_speed <= 0 or delay < 0:
            raise ValueError("invalid_edge_motion_fields:" + str(_value(edge, "edge_id", "unknown")))
        multiplier = congestion_speed_multipliers.get(level)
        if multiplier is None or multiplier <= 0:
            raise ValueError("congestion_level_missing_or_invalid:" + str(level))
        cap = base_speed * float(multiplier)
        # Bound integration size while retaining exact edge length.
        pieces = max(1, int(math.ceil(length / spatial_step_m)))
        dx = length / pieces if pieces else 0.0
        if point_caps[-1] == float("inf"):
            point_caps[-1] = cap
        else:
            point_caps[-1] = min(point_caps[-1], cap)
        for piece in range(pieces):
            interval_distances.append(dx)
            interval_edges.append(edge)
            interval_caps = cap
            point_caps.append(interval_caps)
        point_caps[-1] = min(point_caps[-1], cap)
        if delay > 0:
            stop_points.add(len(point_caps) - 1)
            signal_delays.append(delay)
        else:
            signal_delays.append(0.0)
    if terminal_stop:
        stop_points.add(len(point_caps) - 1)

    speeds = list(point_caps)
    for stop_index in stop_points:
        speeds[stop_index] = 0.0
    speeds[0] = min(speeds[0], float(initial_speed_mps))
    max_a = config.max_acceleration_mps2
    max_d = config.max_deceleration_mps2
    if max_a <= 0 or max_d <= 0:
        raise ValueError("acceleration_and_deceleration_limits_must_be_positive")
    for index, dx in enumerate(interval_distances):
        speeds[index + 1] = min(
            speeds[index + 1],
            math.sqrt(max(0.0, speeds[index] ** 2 + 2.0 * max_a * dx)),
        )
    for index in range(len(interval_distances) - 1, -1, -1):
        if index + 1 in stop_points:
            speeds[index + 1] = 0.0
        dx = interval_distances[index]
        speeds[index] = min(
            speeds[index],
            math.sqrt(max(0.0, speeds[index + 1] ** 2 + 2.0 * max_d * dx)),
        )
    if 0 in stop_points:
        speeds[0] = 0.0

    motion_steps: List[MotionStep] = []
    for index, (dx, edge) in enumerate(zip(interval_distances, interval_edges)):
        v0, v1 = speeds[index], speeds[index + 1]
        if v0 + v1 <= 1e-12:
            raise ValueError("route_motion_profile_stalled")
        dt = 2.0 * dx / (v0 + v1)
        grade = _value(edge, "slope_rad", None)
        motion_steps.append(MotionStep(dt, v0, v1, slope_rad=grade))

    # Add a signal's dwell after each edge. Iterate in route order so idle time
    # remains separate from movement while sharing the same auxiliary load.
    result = EnergyResult(
        trace_fidelity="estimated_from_route_edge_conditions",
        model_version=config.model_version,
        profile_id=config.profile_id,
        profile_sha256=config.fingerprint(),
    )
    interval_cursor = 0
    for edge, delay in zip(edges, signal_delays):
        pieces = max(1, int(math.ceil(float(_value(edge, "length_m", 0.0)) / spatial_step_m)))
        piece_result = evaluate_motion_trace(
            motion_steps[interval_cursor:interval_cursor + pieces],
            config,
            payload_mass_kg=payload_mass_kg,
            trace_fidelity="estimated_from_route_edge_conditions",
        )
        _accumulate(result, piece_result)
        edge_result = EnergyResult(
            trace_fidelity="estimated_from_route_edge_conditions",
            model_version=config.model_version,
            profile_id=config.profile_id,
            profile_sha256=config.fingerprint(),
        )
        _accumulate(edge_result, piece_result)
        interval_cursor += pieces
        if delay > 0:
            idle_result = evaluate_motion_trace(
                [], config, payload_mass_kg=payload_mass_kg,
                idle_durations_sec=(delay,),
                trace_fidelity="estimated_from_route_edge_conditions",
            )
            _accumulate(result, idle_result)
            _accumulate(edge_result, idle_result)
        edge_result.net_kwh = (
            edge_result.propulsion_draw_kwh
            - edge_result.regenerated_kwh
            + edge_result.auxiliary_kwh
        )
        result.edge_breakdown.append({
            "edge_id": str(_value(edge, "edge_id", "")),
            "distance_m": edge_result.distance_m,
            "energy_kwh": edge_result.net_kwh,
            "drive_time_sec": edge_result.drive_time_sec,
            "signal_idle_time_sec": edge_result.signal_idle_time_sec,
            "propulsion_draw_kwh": edge_result.propulsion_draw_kwh,
            "regenerated_kwh": edge_result.regenerated_kwh,
            "auxiliary_kwh": edge_result.auxiliary_kwh,
            "rolling_wheel_kwh": edge_result.rolling_wheel_kwh,
            "aerodynamic_wheel_kwh": edge_result.aerodynamic_wheel_kwh,
            "grade_wheel_kwh": edge_result.grade_wheel_kwh,
            "acceleration_wheel_kwh": edge_result.acceleration_wheel_kwh,
            "missing_grade_steps": edge_result.missing_grade_steps,
            "trace_fidelity": edge_result.trace_fidelity,
        })
    result.net_kwh = result.propulsion_draw_kwh - result.regenerated_kwh + result.auxiliary_kwh
    return result


def _accumulate(target: EnergyResult, source: EnergyResult) -> None:
    for name in (
        "distance_m", "drive_time_sec", "signal_idle_time_sec",
        "rolling_wheel_kwh", "aerodynamic_wheel_kwh", "grade_wheel_kwh",
        "acceleration_wheel_kwh", "propulsion_draw_kwh", "regenerated_kwh",
        "auxiliary_kwh",
    ):
        setattr(target, name, getattr(target, name) + getattr(source, name))
    target.regen_power_capped_steps += source.regen_power_capped_steps
    target.missing_grade_steps += source.missing_grade_steps


def model_metadata(config: Optional[EnergyModelConfig] = None) -> Dict[str, Any]:
    config = config or EnergyModelConfig()
    return {
        "model_version": config.model_version,
        "profile_id": config.profile_id,
        "profile": asdict(config),
        "profile_sha256": config.fingerprint(),
        "equations": {
            "wheel_force": "(m + rotating_mass)*acceleration + 0.5*rho*Cd*A*v^2 + Crr*m*g*cos(slope) + m*g*sin(slope)",
            "positive_wheel_power": "wheel_power / propulsion_efficiency",
            "negative_wheel_power": "min(-wheel_power * regenerative_efficiency, regenerative_power_cap)",
            "net_battery_energy": "propulsion_draw - regenerated_energy + auxiliary_energy",
        },
    }
