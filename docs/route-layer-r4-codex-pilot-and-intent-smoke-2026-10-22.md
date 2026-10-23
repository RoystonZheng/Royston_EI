# R4 Codex pilot and local intent smoke — 2026-10-22

## Scope and status

This note records bounded Codex CLI route checks and a separate local Qwen intent smoke. They are not the formal 100×100 R4 comparison. No multi-seed R4 route batch was started; the separate single-task 100×100 checkpoint below is not a batch result.

## Local Qwen intent smoke

The pinned local snapshot `mlx-community/Qwen3-4B-Instruct-2507-4bit` at revision `50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b` was downloaded into a temporary cache and its 2,263,022,417-byte weight file matched SHA-256 `2a73c6c248601ab904e035548abd8e6abb65ea27dcb5f342fb0a8910eb44173f`.

On frozen `grid-100x100-seed-0`, task `task-0000-0009`:

- With `RoadFeatureInputProtocol v1`, Qwen selected `feature:sign_08`, pickup `node_25_66`, and dropoff `node_42_43`. The deterministic candidate-whitelist validator accepted both endpoints; an evaluation performed only after generation matched both separate ground-truth endpoints. Local inference latency was 3,164 ms.
- With `graph_only` input on the same task, Qwen selected pickup `node_68_41` and dropoff `node_42_43`. The candidate-whitelist validator accepted the form, but the pickup did not match the separate ground truth. Local inference latency was 2,518 ms.
- An earlier prompt version produced the query schema version instead of the required output schema version and chose the opposite-side pickup candidate. The development attempt is retained at `outputs/route_layer/r4_qwen_intent_local_smoke_v1/seed_0/task-0000-0009/development_attempts/attempt_01_schema_invalid.json`. The prompt now distinguishes query and result schema versions and explains feature-side versus opposite-side candidates.

These are two local outputs for one task, not a multi-seed accuracy estimate and not evidence of route-planning performance. No API was called and no paid model tokens were used.

## Codex CLI pilot incident

The separate 8×8 pilot at `outputs/route_layer/r4_codex_ephemeral_8x8_pilot_v1/` received one legal eastward action: `node_1_2` → `node_2_2`. The completed model turn reported 9,698 input and 369 output tokens; the JSONL audit found no tool event.

The following CLI invocation exited before a model turn with `thread/resume failed: no rollout found for thread id …`. Its JSONL had no turn or usage event, so its model token usage is unreported/absent; it is not a zero-token provider invoice. The discrepancy came from a continuation being run in persistent-session mode with an ephemeral thread identifier. The failed action was not retried. The continuation state builder now always selects `ephemeral_per_request` and clears inherited session IDs. Existing resume tests verify that an old persistent ID is discarded.

The run remains stopped after this error. No more route requests were sent. The earlier completed two-step 8×8 smoke at `outputs/route_layer/r4_codex_ephemeral_fixed_smoke_v1/` is a separate smoke record and is not merged into the failed continuation.

## Verification

- `test_r4_codex_pilot_resume.py`: 11 passed.
- `test_r4_intent.py`: 5 passed.
- `test_r4_compact_route.py`: 7 passed.
- `py_compile` passed for the changed R4 intent and pilot modules.
- No multi-seed 100×100 route batch or Codex account invoice was produced. The one-task checkpoint below has a separate usage-derived rate-card estimate.

## Single-task 100×100 Codex route checkpoint

After the earlier 8×8 checks, one Codex CLI request was sent for frozen `grid-100x100-seed-0`, order `task-0000-0009`, using the locally validated Qwen intent endpoints. The route prompt contained the 2,990-node / 10,853-directed-edge package, no blocked edges, and no evaluator ground-truth fields. The independent reachability preflight passed both route legs; the unweighted shortest-path lower bound was 109 moves total, within the schema's 512-move limit. The preflight prompt was 16,428 bytes. The request used GPT-5.6 Luna at low reasoning effort, one model turn, zero retries, and no tool events.

The model returned `status=failed`, `failure_reason_code=unable_to_construct_route`, with an empty move sequence. The request itself succeeded, but it produced no route to validate; this is not an API failure and no road-rule violation occurred. The local reachability preflight had found the task chain reachable, so the model's inability is not evidence that the map is disconnected. The corrected replay labels this outcome as a model route-construction failure rather than an illegal route. It records zero proposed moves and marks route-driving energy as not evaluated; the null energy value must not be read as zero consumption. No API/compute invoice was reported by Codex CLI.

CLI usage: 15,567 input tokens (0 cached), 110 output tokens, 7,147 ms; no tool calls. The local rate-card calculation is 0.081135 GPT-5.6 Luna Standard credits, with a $0.0032454 API-rate equivalent. These are usage/rate-card estimates, not verified account charges or invoices.

This remains a one-order checkpoint on one seed, not a route-performance result or the 10-seed experiment. There was exactly one route request and no retry. Result and replay: `outputs/route_layer/r4_codex_formal_checkpoint_v1/`; runner: `scripts/run_r4_codex_formal_checkpoint.py`.

## Evidence files

- Local intent outputs: `outputs/route_layer/r4_qwen_intent_local_smoke_v1/seed_0/task-0000-0009/`
- Failed 8×8 continuation and JSONL: `outputs/route_layer/r4_codex_ephemeral_8x8_pilot_v1/`
- Earlier completed 8×8 smoke: `outputs/route_layer/r4_codex_ephemeral_fixed_smoke_v1/`
- Runner: `scripts/run_r4_qwen_intent_local_smoke.py`
- Continuation-session fix: `scripts/run_r4_codex_pilot_continuation.py`
