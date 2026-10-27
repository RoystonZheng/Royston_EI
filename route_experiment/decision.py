"""Deterministic time-versus-money route selection.

Hard feasibility is checked before this module is called.  The selector only
compares candidates from the same decision set and never lets a weighted score
compensate for an invalid route.
"""

from dataclasses import dataclass
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union


DECISION_MODEL_VERSION = "route_time_money_segment_v3_billable_net_energy"


@dataclass(frozen=True)
class RouteDecisionProfile:
    profile_id: str
    time_weight: float
    money_weight: float
    description: str


DECISION_PROFILES: Dict[str, RouteDecisionProfile] = {
    "time_priority_0_6_0_4": RouteDecisionProfile(
        profile_id="time_priority_0_6_0_4",
        time_weight=0.6,
        money_weight=0.4,
        description="time-priority: time 0.6, money 0.4",
    ),
    "money_priority_0_4_0_6": RouteDecisionProfile(
        profile_id="money_priority_0_4_0_6",
        time_weight=0.4,
        money_weight=0.6,
        description="money-priority: time 0.4, money 0.6",
    ),
}


def _finite_number(candidate: Mapping[str, Any], key: str) -> Optional[float]:
    value = candidate.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _finite_nonnegative(candidate: Mapping[str, Any], key: str) -> Optional[float]:
    number = _finite_number(candidate, key)
    return number if number is not None and number >= 0.0 else None


def _minmax(values: List[float]) -> List[float]:
    low = min(values)
    high = max(values)
    if high == low:
        return [0.0 for _ in values]
    span = high - low
    return [(value - low) / span for value in values]


def score_route_candidates(
    candidates: Iterable[Mapping[str, Any]],
    profile: Union[str, RouteDecisionProfile],
    *,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    """Score feasible candidates with per-scenario min-max normalized costs.

    Each candidate must provide ``candidate_id``, ``feasible``,
    ``travel_time_sec``, and a known ``money_cost`` (known operating cost minus
    known fare revenue, when fare data is available). Money values may be
    negative when revenue exceeds known costs. A caller must verify that the
    amount uses an explicit price basis; unknown amounts must never be
    represented as zero.
    """

    selected_profile = DECISION_PROFILES[profile] if isinstance(profile, str) else profile
    eligible: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    for item in candidates:
        candidate_id = str(item.get("candidate_id", ""))
        if not item.get("feasible", False):
            excluded.append({"candidate_id": candidate_id, "reason": "hard_constraint_failed"})
            continue
        travel_time = _finite_nonnegative(item, "travel_time_sec")
        money_cost = _finite_number(item, "money_cost")
        if travel_time is None:
            excluded.append({"candidate_id": candidate_id, "reason": "travel_time_missing_or_invalid"})
            continue
        if money_cost is None:
            excluded.append({"candidate_id": candidate_id, "reason": "money_cost_missing_or_invalid"})
            continue
        if not candidate_id:
            excluded.append({"candidate_id": "", "reason": "candidate_id_missing"})
            continue
        eligible.append({
            **dict(item),
            "candidate_id": candidate_id,
            "travel_time_sec": travel_time,
            "money_cost": money_cost,
        })

    result: Dict[str, Any] = {
        "model_version": DECISION_MODEL_VERSION,
        "profile": {
            "profile_id": selected_profile.profile_id,
            "time_weight": selected_profile.time_weight,
            "money_weight": selected_profile.money_weight,
        },
        "normalization": "min_max_over_same_feasible_candidate_set_per_scenario",
        "candidate_count": len(eligible),
        "excluded_candidates": excluded,
        "currency": currency,
        "candidates": [],
        "selected_candidate_id": None,
        "status": "no_feasible_candidates",
    }
    if not eligible:
        return result

    normalized_times = _minmax([item["travel_time_sec"] for item in eligible])
    normalized_money = _minmax([item["money_cost"] for item in eligible])
    ranked: List[Dict[str, Any]] = []
    for item, time_norm, money_norm in zip(eligible, normalized_times, normalized_money):
        score = (
            selected_profile.time_weight * time_norm
            + selected_profile.money_weight * money_norm
        )
        ranked.append({
            "candidate_id": item["candidate_id"],
            "travel_time_sec": item["travel_time_sec"],
            "money_cost": item["money_cost"],
            "normalized_time": time_norm,
            "normalized_money": money_norm,
            "score": score,
        })

    ranked.sort(key=lambda item: (
        round(item["score"], 12),
        item["travel_time_sec"],
        item["money_cost"],
        item["candidate_id"],
    ))
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    result["candidates"] = ranked
    result["selected_candidate_id"] = ranked[0]["candidate_id"]
    result["status"] = "selected" if len(ranked) > 1 else "single_feasible_candidate"
    return result


def calculate_segment_economics(
    *,
    distance_m: float,
    energy_kwh: float,
    currency: str,
    electricity_price_per_kwh: Optional[float],
    segment_type: str,
    fare_per_km: Optional[float] = None,
    observed_fare_revenue_amount: Optional[float] = None,
    api_cost_amount: Optional[float] = None,
    compute_cost_amount: Optional[float] = None,
    price_basis: Optional[str] = None,
    fare_basis: Optional[str] = None,
) -> Dict[str, Any]:
    """Account known per-segment energy/API costs and fare revenue.

    ``segment_type`` must be ``deadhead`` or ``passenger``. Fare revenue is
    zero only for a deadhead segment; passenger revenue is ``None`` when no
    fare schedule or observed fare amount is available. In that case the
    known operating-cost subtotal is still reported, but ``net_economic_cost``
    stays ``None`` so an unknown fare is never treated as zero revenue.
    """

    if segment_type not in {"deadhead", "passenger"}:
        raise ValueError("segment_type_must_be_deadhead_or_passenger")

    def checked(value: Optional[float], name: str) -> Optional[float]:
        if value is None:
            return None
        number = float(value)
        if not math.isfinite(number) or number < 0.0:
            raise ValueError(name + "_must_be_finite_and_nonnegative")
        return number

    def checked_net_energy(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("energy_kwh_must_be_finite")
        return number

    distance = checked(distance_m, "distance_m")
    energy = checked_net_energy(energy_kwh)
    billable_energy = max(0.0, energy) if energy is not None else None
    tariff = checked(electricity_price_per_kwh, "electricity_price_per_kwh")
    api_cost = checked(api_cost_amount, "api_cost_amount")
    compute_cost = checked(compute_cost_amount, "compute_cost_amount")
    fare_rate = checked(fare_per_km, "fare_per_km")
    observed_fare = checked(observed_fare_revenue_amount, "observed_fare_revenue_amount")

    electricity_cost = (
        billable_energy * tariff
        if energy is not None and tariff is not None
        else None
    )
    known_costs = [value for value in (electricity_cost, api_cost, compute_cost) if value is not None]
    known_operating_cost = sum(known_costs) if known_costs else None
    if segment_type == "deadhead":
        fare_revenue: Optional[float] = 0.0
        applied_fare_basis = "known_zero_for_deadhead"
    elif observed_fare is not None:
        fare_revenue = observed_fare
        applied_fare_basis = fare_basis or "observed_or_frozen_segment_fare"
    elif fare_rate is not None:
        if distance is None:
            fare_revenue = None
            applied_fare_basis = None
        else:
            fare_revenue = distance / 1000.0 * fare_rate
            applied_fare_basis = fare_basis or "passenger_distance_km_times_configured_rate"
    else:
        fare_revenue = None
        applied_fare_basis = None

    net_economic_cost = (
        known_operating_cost - fare_revenue
        if (
            electricity_cost is not None
            and known_operating_cost is not None
            and fare_revenue is not None
        )
        else None
    )
    all_operating_cost_components_known = all(
        value is not None for value in (electricity_cost, api_cost, compute_cost)
    )
    if energy is None:
        status = "segment_energy_unpriced"
    elif fare_revenue is None:
        status = "operating_cost_only_fare_unpriced"
    elif electricity_cost is None:
        status = "net_cost_not_priced_electricity_price_missing"
    elif net_economic_cost is None:
        status = "net_cost_not_priced"
    elif all_operating_cost_components_known:
        status = "priced_net_cost_partial_business_model"
    else:
        status = "partial_cost_and_revenue_priced"

    return {
        "currency": currency,
        "segment_type": segment_type,
        "distance_m": distance,
        "energy_kwh": energy,
        "billable_energy_kwh": billable_energy,
        "electricity_price_per_kwh": tariff,
        "electricity_cost_amount": electricity_cost,
        "api_cost_amount": api_cost,
        "compute_cost_amount": compute_cost,
        "known_operating_cost_amount": known_operating_cost,
        "fare_per_km": fare_rate,
        "fare_revenue_amount": fare_revenue,
        "fare_basis": applied_fare_basis,
        "net_economic_cost": net_economic_cost,
        "price_basis": price_basis,
        "accounting_status": status,
        "full_profitability_established": False,
        "excluded_costs": [
            "vehicle depreciation",
            "maintenance",
            "insurance",
            "taxes and tolls unless explicitly priced",
            "charging service fees unless included in the electricity tariff",
        ],
    }


def aggregate_route_segment_economics(
    segments: Iterable[Mapping[str, Any]],
    *,
    currency: str,
    electricity_price_per_kwh: Optional[float],
    fare_per_km: Optional[float] = None,
    price_basis: Optional[str] = None,
    fare_basis: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate itemized deadhead/passenger segments without filling gaps.

    Each input segment supplies ``distance_m``, ``energy_kwh`` and
    ``segment_type`` and may override the shared electricity/fare rates or
    provide an observed fare and measured API/compute expenses. The route's
    net economic cost is available only when every segment has priced energy
    and known revenue (deadhead revenue is known zero). Per-segment details
    remain in the output for audit and later recomputation.
    """

    segment_inputs = list(segments)
    details: List[Dict[str, Any]] = []
    segment_costs: List[float] = []
    segment_revenues: List[float] = []
    total_distance_m = 0.0
    total_energy_kwh = 0.0
    total_billable_energy_kwh = 0.0
    total_distance_complete = True
    total_energy_complete = True
    net_complete = bool(segment_inputs)
    for index, segment in enumerate(segment_inputs):
        segment_id = str(segment.get("segment_id", index))
        segment_type = str(segment.get("segment_type", ""))
        segment_fare_rate = segment.get("fare_per_km", fare_per_km)
        raw_links = segment.get("road_links", []) or []
        road_links: List[Dict[str, Any]] = []
        if raw_links:
            for link_index, raw_link in enumerate(raw_links):
                link_distance = _finite_nonnegative(
                    {"distance_m": raw_link.get("distance_m", raw_link.get("length_m"))},
                    "distance_m",
                )
                link_energy = _finite_number(
                    {"energy_kwh": raw_link.get("energy_kwh")}, "energy_kwh"
                )
                link_tariff = raw_link.get(
                    "electricity_price_per_kwh",
                    segment.get("electricity_price_per_kwh", electricity_price_per_kwh),
                )
                # Electricity is billed at the parent route-leg boundary after
                # summing signed link net energy; per-link clipping changes the
                # bill when regeneration occurs on only part of a route.
                link_electricity_cost = None
                link_fare = (
                    link_distance / 1000.0 * float(segment_fare_rate)
                    if (
                        segment_type == "passenger"
                        and segment_fare_rate is not None
                        and link_distance is not None
                    )
                    else (0.0 if segment_type == "deadhead" else None)
                )
                road_links.append({
                    **dict(raw_link),
                    "segment_index": link_index,
                    "distance_m": link_distance,
                    "energy_kwh": link_energy,
                    "electricity_price_per_kwh": link_tariff,
                    "electricity_cost_amount": link_electricity_cost,
                    "electricity_cost_basis": "priced_at_parent_route_leg_after_net_energy_sum",
                    "fare_revenue_amount": link_fare,
                    "fare_basis": (
                        segment.get("fare_basis", fare_basis)
                        if segment_fare_rate is not None
                        else (
                            "known_zero_for_deadhead"
                            if segment_type == "deadhead"
                            else None
                        )
                    ),
                })
        segment_distance = segment.get("distance_m")
        if segment_distance is None and road_links:
            link_distances = [link["distance_m"] for link in road_links]
            segment_distance = (
                sum(link_distances)
                if all(value is not None for value in link_distances)
                else None
            )
        segment_energy = segment.get("energy_kwh")
        if segment_energy is None and road_links:
            link_energies = [link["energy_kwh"] for link in road_links]
            segment_energy = (
                sum(link_energies)
                if all(value is not None for value in link_energies)
                else None
            )
        details_row = calculate_segment_economics(
            distance_m=segment_distance,
            energy_kwh=segment_energy,
            currency=currency,
            electricity_price_per_kwh=segment.get(
                "electricity_price_per_kwh", electricity_price_per_kwh
            ),
            segment_type=segment_type,
            fare_per_km=segment_fare_rate,
            observed_fare_revenue_amount=segment.get("fare_revenue_amount"),
            api_cost_amount=segment.get("api_cost_amount"),
            compute_cost_amount=segment.get("compute_cost_amount"),
            price_basis=segment.get("price_basis", price_basis),
            fare_basis=segment.get("fare_basis", fare_basis),
        )
        details_row["segment_id"] = segment_id
        details_row["road_links"] = road_links
        if road_links:
            link_distances = [link["distance_m"] for link in road_links]
            link_energies = [link["energy_kwh"] for link in road_links]
            if (
                details_row["distance_m"] is None
                or details_row["energy_kwh"] is None
                or not all(value is not None for value in link_distances + link_energies)
            ):
                details_row["road_link_breakdown_reconciles"] = False
            else:
                distance_delta = abs(
                    float(details_row["distance_m"]) - sum(link_distances)
                )
                energy_delta = abs(
                    float(details_row["energy_kwh"]) - sum(link_energies)
                )
                details_row["road_link_breakdown_reconciles"] = (
                    distance_delta <= 1e-6 and energy_delta <= 1e-9
                )
        else:
            details_row["road_link_breakdown_reconciles"] = None
        details.append(details_row)
        if details_row["distance_m"] is None:
            total_distance_complete = False
        else:
            total_distance_m += float(details_row["distance_m"])
        if details_row["energy_kwh"] is None:
            total_energy_complete = False
        else:
            total_energy_kwh += float(details_row["energy_kwh"])
            total_billable_energy_kwh += float(details_row["billable_energy_kwh"] or 0.0)
        if details_row["known_operating_cost_amount"] is None:
            net_complete = False
        else:
            segment_costs.append(float(details_row["known_operating_cost_amount"]))
        if details_row["net_economic_cost"] is None:
            net_complete = False
        if details_row["fare_revenue_amount"] is None:
            net_complete = False
        else:
            segment_revenues.append(float(details_row["fare_revenue_amount"]))

    route_known_operating_cost = (
        sum(segment_costs) if details and len(segment_costs) == len(details) else None
    )
    route_fare_revenue = (
        sum(segment_revenues) if details and len(segment_revenues) == len(details) else None
    )
    route_net_cost = (
        route_known_operating_cost - route_fare_revenue
        if (
            net_complete
            and route_known_operating_cost is not None
            and route_fare_revenue is not None
        )
        else None
    )
    segment_statuses = {row["accounting_status"] for row in details}
    if "operating_cost_only_fare_unpriced" in segment_statuses:
        accounting_status = "operating_cost_only_fare_unpriced"
    elif "net_cost_not_priced_electricity_price_missing" in segment_statuses:
        accounting_status = "net_cost_not_priced_electricity_price_missing"
    elif route_net_cost is not None:
        accounting_status = "priced_net_cost_partial_business_model"
    else:
        accounting_status = "net_cost_incomplete_segment_inputs"
    return {
        "currency": currency,
        "segment_count": len(details),
        "distance_m": total_distance_m if total_distance_complete else None,
        "energy_kwh": total_energy_kwh if total_energy_complete else None,
        "billable_energy_kwh": total_billable_energy_kwh if total_energy_complete else None,
        "known_operating_cost_amount": route_known_operating_cost,
        "fare_revenue_amount": route_fare_revenue,
        "net_economic_cost": route_net_cost,
        "accounting_status": accounting_status,
        "full_profitability_established": False,
        "segments": details,
    }
