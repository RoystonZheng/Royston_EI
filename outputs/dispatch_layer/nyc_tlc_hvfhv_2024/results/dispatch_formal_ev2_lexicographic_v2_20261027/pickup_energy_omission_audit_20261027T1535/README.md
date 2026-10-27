# NYC EVv2 pickup energy omission audit

This sidecar scans immutable result JSONs currently present under seed_0/evaluation for the formal EVv2 run. For each saved candidate table it counts nonzero synthetic pickup distance with an empty road-edge sequence and zero pickup energy components. It also records result hashes, cell, method, saved round identifiers, and the number of round-0000 selected pairs whose saved candidate shows the omission.

The saved compact tables are not persisted for every round, so candidate counts are a lower bound over persisted candidate tables, not a full run-wide count. Service metrics in completed results remain separately auditable; reported energy is incomplete for positive-distance pickup legs. D1/D2/D6/D7/D8 use candidate EV energy in the last optimizer objective tier; their selection evidence is not corrected by this scan.

This audit makes no model calls and does not edit source results. Use the fixed-assignment EVv2 recalculation sidecar for corrected post-hoc energy totals; that sidecar cannot retroactively correct decisions.
