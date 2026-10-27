# NYC audit and offline recalculation archives

Created 2026-10-27T15:48+08:00. Bundles preserve audit-listed stable files with per-member SHA-256 manifests. Running batch output was not copied as a directory.

- seed0 EV v2 checkpoint: latest saved audit at 07:39:31 UTC; 12/90 complete paired cells, not a final matrix.
- seed4 legacy checkpoint: isolated snapshot at 07:21:19 UTC; 59/90 cells; not combined with seed0 or seed3. One trace no longer matched the checkpoint audit and is listed in that bundle’s EXCLUDED_UNVERIFIED.tsv.
- EV v2 recalculation: all 91 then-existing result files were recalculated offline; 539/630 expected files are not yet available. It does not retroactively change assignments.
- The seed3 replicate snapshot (25/90 cells) and earlier seed0/seed4 snapshots remain in repository_archives_20261027T1524CST.

Every included source file hash was checked against its audit/index before packaging and rechecked from the compressed archive. Credential-pattern scan found no matches. Every included source file and archive is below 100 MiB.
