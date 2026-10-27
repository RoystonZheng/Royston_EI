# EV energy model v2

**Status:** implemented as the shared route/dispatch model on 2026-10-26. The profile is a generic reference vehicle assumption; it is not calibrated to a Tesla, Kia, or a measured fleet.

## Reference profile

| Parameter | Value |
|---|---:|
| Curb mass | 1,900 kg |
| Passenger load | 75 kg per occupied route |
| Rotating mass | 50 kg |
| Rolling resistance coefficient | 0.009 |
| Drag coefficient | 0.26 |
| Frontal area | 2.35 m² |
| Air density | 1.225 kg/m³ |
| Propulsion efficiency | 0.90 |
| Regenerative efficiency | 0.65 |
| Auxiliary power | 800 W |
| Comfortable acceleration limit | 1.2 m/s² |
| Comfortable deceleration limit | 2.0 m/s² |
| Regenerative power cap | 50 kW |

The profile is versioned as `generic_reference_ev_v1`; its canonical JSON fingerprint is written with each result. Parameters are explicit so later calibration can replace the profile without changing the equations.

## Per-step calculation

For vehicle mass `m` (curb mass plus payload), rotating mass `m_rot`, speed `v`, acceleration `a`, road slope `theta`, and gravity `g`, the model calculates:

```text
F_accel = (m + m_rot) * a
F_aero  = 0.5 * rho * Cd * A * v^2
F_roll  = Crr * m * g * cos(theta)
F_grade = m * g * sin(theta)
F_wheel = F_accel + F_aero + F_roll + F_grade
P_wheel = F_wheel * v
```

Positive wheel power draws `P_wheel / propulsion_efficiency` from the battery. Negative wheel power recovers `min(-P_wheel * regenerative_efficiency, 50 kW)` before adding auxiliary power. Auxiliary energy is charged for every moving, signal-waiting, and explicitly idle second. Net energy is propulsion draw minus recovered energy plus auxiliary energy. The output also keeps signed wheel-work components so acceleration, rolling, drag, and grade can be inspected separately.

## Congestion and stop/start behavior

- Grid and dispatch route adapters apply the saved congestion speed multiplier to each edge's speed cap. The profile reconstructs a bounded speed curve, including acceleration and braking at signals and the route endpoint.
- A positive signal delay creates a stop, auxiliary idle energy, and a restart. Congestion has no fixed kWh/km surcharge, and the adapter does not invent queue counts.
- When a time-step speed trace exists, callers can pass its observed speeds, duration, and optional slope directly to `evaluate_motion_trace`. Missing slope is treated as flat and counted in the result metadata.
- Existing grid results do not contain a per-step speed trace or grade. Their v2 recalculation is therefore tagged `estimated_from_route_edge_conditions`, with flat grade explicitly recorded.
- Invalid proposed routes are reported as hypothetical estimates separately from executed/legal prefixes. A partial route remains a prefix and is never labeled a completed trip.

## Price separation

Electricity price is applied after kWh is calculated. The route experiment's historical Tesla China station tariff is retained only as a reference charging-price profile; it is not a vehicle model or a source for energy consumption.

## References for model structure

SUMO's official electric-vehicle model likewise exposes mass, drag, rolling resistance, propulsion efficiency, recuperation efficiency, and battery power limits as explicit parameters. Its battery output can include time-step energy, speed, and acceleration when configured: [SUMO Electric](https://sumo.dlr.de/docs/Models/Electric.html). SUMO's MMPEVEM documentation describes energy based on speed, acceleration, slope, and time step, with regeneration constrained by motor limits: [SUMO MMPEVEM](https://sumo.dlr.de/userdoc/Models/MMPEVEM.html). These sources motivate the model inputs and structure; they do not validate the chosen generic parameter values.
