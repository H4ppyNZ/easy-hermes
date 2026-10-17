# tools/ -- Operational helper scripts

These scripts are **not** part of the installer or the daily scheduled tasks. They are
one-off / on-demand tools for fixing specific problems on a Windows Hermes host.

## Configure-FreeModelFallbacks.ps1

Configures free Nous Portal models as fallback providers. **Superseded by
`hermes-model-selector.py`** which probes models live and applies a promotion gate.
This script is kept for reference and for environments where the selector cannot run
(e.g., no Python venv). It uses a hardcoded list of free models sorted by daily usage.

To use the selector instead (recommended):
```powershell
python hermes-model-selector.py --dry-run   # preview
python hermes-model-selector.py             # apply
```