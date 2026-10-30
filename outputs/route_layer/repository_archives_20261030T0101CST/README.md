# R4 deterministic safety-gate checkpoint (2026-10-30 CST)

This archive contains a zero-model-call directed-edge recovery test and a fresh SUMO replay of a previously archived 113-edge route on the frozen `grid-100x100-seed-0` map. The vehicle completed, route edge matching passed, and one-way violations were 0. These results validate a safety sub-gate; they are not new LLM route samples and do not prove that the formal 129-task R4 matrix passed.

The current acceptance audit also records that the formal route chain does not yet call `score_route_candidates`, so there is no same-task candidate pool with complete 4:6 and 6:4 rankings, explanations, and selected-route EV2/SUMO binding. It made no model calls and remains blocked before formal resume.

The formal R4 batch remains paused under the user's direct instruction: “在途结束后停发（推荐）”. This offline gate does not authorize any model request or runner restart.

Generated XML and frozen-map JSON files are gzip-compressed losslessly. `archive_manifest.json` maps original paths and SHA-256 values to compressed archive paths; `SHA256SUMS` validates every archived file. The first failed test-harness invocation log was excluded because the same offline test subsequently passed and the failed invocation was a path-resolution error.
