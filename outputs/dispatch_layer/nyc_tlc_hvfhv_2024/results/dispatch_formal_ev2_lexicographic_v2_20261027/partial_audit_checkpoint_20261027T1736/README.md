# NYC EV v2 partial paired-audit checkpoint — 2026-10-27 17:36 CST

The official paired audit covers one seed: 17 fully valid paired cells out of 90 expected and 119 present method-result files out of 630. The checkpoint is an intermediate audit snapshot, not a complete matrix or a cross-month effect conclusion.

A later pickup-energy coverage audit found missing pickup-leg EV v2 components in all 10,500 candidate rows represented by those 119 files, including 385 selected candidate rows. The seven-method F2 cell is validator-valid under the official paired audit, yet each method has two selected rows with this pickup-energy gap. Post-hoc fixed-assignment energy sidecars do not repair decisions made with incomplete online candidate features. See the adjacent `pickup_energy_coverage_audit_20261027T0938Z.json` and `pickup_energy_omission_audit_20261027T1736/` evidence.
