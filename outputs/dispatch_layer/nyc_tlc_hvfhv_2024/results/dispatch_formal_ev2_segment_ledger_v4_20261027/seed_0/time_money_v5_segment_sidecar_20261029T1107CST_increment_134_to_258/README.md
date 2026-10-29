# NYC EV2 v5 segment economics increment

Offline increment from the repository's 134-result baseline to 258 audited results: 124 new run rows and 7,392 segment rows.

The sidecar completed with zero model calls, zero SUMO reruns, and no source-result modifications. It remains blocked for route-choice ranking: the electricity tariff and complete shared candidate pool are not frozen, with method/API/compute costs and fare reconciliation incomplete. Monetary fields remain null where inputs are absent; weighted rankings were not emitted.

Local workspace paths were rewritten to repository-relative paths. The manifest and SHA256SUMS preserve source and normalized hashes.
