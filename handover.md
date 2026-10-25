# handover.md — easy-hermes free-model selector (Phase 2)

## Status
Phase 2 is complete and pushed to `main`. All 5 provider pollers are merged into
`main` plus `providers.py` (registry) and `select-models.py` (central selector).
53 tests pass (`python -m pytest -q`).

## What was built
- `poll_openrouter.py`, `poll_nvidia.py`, `poll_gemini.py`, `poll_ollama_cloud.py`,
  `poll_novita.py` — per-provider catalog pollers (normalized schema, mocked tests).
- `providers.py` — provider registry (env detection, probe kind, default-enabled).
- `select-models.py` — central selector: fetch -> probe top-3 (top-12 nightly) ->
  7-day cache -> write `model.default` + `fallback_providers` to config.yaml.
- `selector-intention.md` — goal/design doc.
- `process.md` — merge/test process notes.

## Keys (already set in Windows user env, NOT in shell env)
NVIDIA_API_KEY, GOOGLE_API_KEY, NOVITA_API_KEY, OLLAMA_API_KEY are set in
`HKEY_CURRENT_USER\Environment`. OPENROUTER_API_KEY is optional (not set).

## Known issue: `python` resolves to wrong binary
In PowerShell, `python` currently points to `~/.local/bin/python`, which is a
bash-only symlink to python3.11 and triggers the Windows Store installer prompt.
Fix (PowerShell, persistent):
```powershell
$dir = "$env:USERPROFILE\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none"
# prepend to user PATH
[Environment]::SetEnvironmentVariable("Path", "$dir;" + [Environment]::GetEnvironmentVariable("Path","User"), "User")
# also remove ~/.local/bin from PATH if present, or just call python by full path:
& "$dir\python.exe" --version
```
Also disable the Windows Store app-execution alias for python.exe
(Settings -> Apps -> Advanced app settings -> App execution aliases).

Until fixed, invoke by full path:
```powershell
& "$env:USERPROFILE\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe" select-models.py --dry-run
```

## Next steps after restart
1. Fix python (above).
2. Test the selector with keys:
   ```powershell
   python select-models.py --dry-run        # preview
   python select-models.py --dry-run --json # JSON summary
   python select-models.py                 # apply to config
   ```
3. Verify `C:\Users\mattw\AppData\Local\hermes\config.yaml` got model.default +
   fallback_providers, and `cache/free_model_probe_cache.json` was created.
4. Phase 3 (when ready): wire select-models.py into hermes-setup.ps1 + nightly task;
   add poll_nous.py; add user-tunable ordering weights.

## Useful commands
- Tests: `python -m pytest -q` (53 tests, no network)
- Poller smoke: `python poll_openrouter.py --json` (no key needed)
- Per-provider: see selector-intention.md / process.md
EOF
echo written