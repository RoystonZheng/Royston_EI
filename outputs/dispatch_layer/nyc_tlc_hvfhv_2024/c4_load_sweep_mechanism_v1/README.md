# C4 load-sweep mechanism preflight

This package runs D0 and D1 against the same 790-order, five-minute C4 trace for three synthetic fleet sizes. The training cost matrix includes direct observed same-zone ride medians. Empty pickup is zero only for same-zone vehicle placement; cross-zone empty pickup is unavailable. To keep this 790-order run bounded, the package stores per-round validation/candidate summaries rather than full candidate snapshots. This is a mechanism/sensitivity preflight, not an NYC performance estimate.
