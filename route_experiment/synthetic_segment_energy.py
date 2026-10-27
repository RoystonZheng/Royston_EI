"""EV v2 energy estimates for synthetic legs that lack road-edge traces.

These estimates are deliberately separate from observed-edge evaluation.  A
caller must provide an explicit synthetic-segment reason and retain the
returned trace-fidelity label in the experiment ledger.
"""

from __future__ import annotations

import math
from typing import Optional

from .energy_model import EnergyModelConfig, EnergyResult, MotionStep, evaluate_motion_trace


def evaluate_synthetic_start_stop_segment(
    distance_m: float,
    duration_sec: float,
    config: Optional[EnergyModelConfig] = None,
    *,
    payload_mass_kg: Optional[float] = None,
    trace_fidelity: str = "synthetic_centroid_distance_time_start_stop_profile",
) -> EnergyResult:
    """Estimate signed EV v2 energy from a synthetic segment's distance/time.

    The profile starts and ends at rest, accelerates and decelerates at the
    configured limits, and cruises between those phases.  Cruise speed is
    solved so the trace matches the supplied distance and duration exactly.
    The grade is unknown and therefore passed as ``None`` to the shared EV v2
    integrator, which records each missing-grade step while using flat-road
    force for the estimate.
    """

    config = config or EnergyModelConfig()
    distance = float(distance_m)
    duration = float(duration_sec)
    if not math.isfinite(distance) or distance < 0:
        raise ValueError("synthetic_segment_distance_must_be_finite_and_nonnegative")
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("synthetic_segment_duration_sec_must_be_finite_and_nonnegative")

    if distance == 0.0:
        steps = (
            [MotionStep(duration, 0.0, 0.0, slope_rad=None, acceleration_mps2=0.0)]
            if duration > 0.0 else []
        )
        return evaluate_motion_trace(
            steps,
            config,
            payload_mass_kg=payload_mass_kg,
            trace_fidelity=trace_fidelity,
        )
    if duration <= 0.0:
        raise ValueError("positive_synthetic_segment_distance_requires_positive_duration")

    acceleration = float(config.max_acceleration_mps2)
    deceleration = float(config.max_deceleration_mps2)
    if acceleration <= 0.0 or deceleration <= 0.0:
        raise ValueError("synthetic_segment_requires_positive_acceleration_limits")

    time_per_speed = (1.0 / acceleration) + (1.0 / deceleration)
    minimum_duration = math.sqrt(2.0 * distance * time_per_speed)
    if duration + 1e-9 < minimum_duration:
        raise ValueError("synthetic_segment_distance_time_infeasible_from_rest")

    discriminant = max(0.0, duration * duration - 2.0 * time_per_speed * distance)
    peak_speed = (2.0 * distance) / (duration + math.sqrt(discriminant))
    acceleration_time = peak_speed / acceleration
    deceleration_time = peak_speed / deceleration
    cruise_time = duration - acceleration_time - deceleration_time
    if cruise_time < -1e-8:
        raise ValueError("synthetic_segment_start_stop_profile_has_negative_cruise_time")
    cruise_time = max(0.0, cruise_time)

    steps = [
        MotionStep(
            acceleration_time,
            0.0,
            peak_speed,
            slope_rad=None,
            acceleration_mps2=acceleration,
        )
    ]
    if cruise_time > 1e-9:
        steps.append(
            MotionStep(
                cruise_time,
                peak_speed,
                peak_speed,
                slope_rad=None,
                acceleration_mps2=0.0,
            )
        )
    steps.append(
        MotionStep(
            deceleration_time,
            peak_speed,
            0.0,
            slope_rad=None,
            acceleration_mps2=-deceleration,
        )
    )
    result = evaluate_motion_trace(
        steps,
        config,
        payload_mass_kg=payload_mass_kg,
        trace_fidelity=trace_fidelity,
    )
    if abs(result.distance_m - distance) > max(1e-6, distance * 1e-9):
        raise RuntimeError("synthetic_segment_energy_distance_reconciliation_failed")
    if abs(result.drive_time_sec - duration) > max(1e-6, duration * 1e-9):
        raise RuntimeError("synthetic_segment_energy_duration_reconciliation_failed")
    return result
