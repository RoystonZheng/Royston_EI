# R4 stop re-lock checkpoint

The direct user instruction remains to stop model requests after any in-flight request completes. At this check there was no in-flight request, runner, aggregator, lock holder, or open file handle. A second automatic resume state change cleared worker_02’s halt flag and increased the request counter from 12,084 to 12,089; no route-complete result was added. The parent and all eight workers were re-locked. The counter does not confirm billing. This sanitized snapshot excludes prompts, traces, and raw task state.
