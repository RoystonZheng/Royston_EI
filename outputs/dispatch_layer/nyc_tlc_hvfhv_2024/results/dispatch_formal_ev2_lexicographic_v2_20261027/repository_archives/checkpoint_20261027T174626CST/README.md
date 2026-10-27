# NYC seed0 EV v2 audit snapshot — 20261027T174626CST

This checkpoint indexes the latest stable seed0 evidence available at capture. The paired audit has 17/90 complete paired cells and 119/630 method-result files for one seed. The newly completed F2 cell contributes seven method files, each hash-matched to the pickup coverage audit.

The pickup coverage audit reports 10,500/10,500 candidate rows with a missing pickup-leg EV v2 component and 385 selected gap rows. Its seven-method F2 cell has two selected gap rows per method. Fixed-assignment energy recalculation is post-hoc: it does not repair the online decisions. The separate energy sidecar completed with expected gaps; 119 outputs were integrity-verified with zero errors, while 511 expected outputs remain absent.

These are partial simulation and audit results. They do not support complete-matrix, cross-month, or effect claims. `SOURCE_FILES.tsv` lists every source path, size and SHA-256 included or indexed by this snapshot.
