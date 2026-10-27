# NYC completed method task checkpoint

This checkpoint contains individual method-result JSON files whose progress events were terminal (`complete`), whose recorded SHA-256 matched the source, and whose source files were closed at capture. Active progress logs and execution traces are not included.

The set combines completed items from the running multi-month replicate, a separately labelled legacy seed 4 replicate, and the running official EV v2 branch. Legacy outputs remain separate; the official branch includes a D7 replay derived from D6, which is not an independent policy sample. These are task outputs only: they do not complete the batch or update its paired audit, and must not be used as a cross-month ranking or effect claim.

`COMPLETION_INDEX.json` records each event time, source path, output classification, byte count, and source/archive SHA-256.
