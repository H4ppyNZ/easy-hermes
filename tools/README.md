# tools/ -- Operational helper scripts

These scripts are **not** part of the installer or the daily scheduled tasks. They are
one-off / on-demand tools for fixing specific problems on a Windows Hermes host.

## gen_manifest.py / check_repo_health.py

`gen_manifest.py` regenerates `MANIFEST.json` (the installer file set);
`check_repo_health.py` is the pre-push gate (ASCII-only .ps1, manifest coverage,
`?v=N` consistency, stale test counts).