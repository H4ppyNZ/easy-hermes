# Process: free-model selection tooling

## Current status (Phase 2 merged)

- `providers.py` + 5 pollers + `select-models.py` are on `main`.
- `hermes-model-selector.py` and `hermes-ollama-selector.py` remain separate, as intended.
- `select-models.py` has a configurable weighted ranking matrix, CLI weight flags, and `--reasoning`.
- Tests: `python -m pytest -q` (no network).

## Daily commands

```powershell
# syntax check
python -m py_compile select-models.py providers.py

# dry-run preview (no writes)
python select-models.py --dry-run

# re-probe everything (ignore probe cache)
python select-models.py --dry-run --refresh

# narrow to one provider
python select-models.py --providers gemini --dry-run

# retest one model across providers
python select-models.py --model deepseek-v4-pro:0813 --dry-run --refresh

# night-level budget (top-12)
python select-models.py --dry-run --nightly

# full sweep (every candidate) - use sparingly
python select-models.py --dry-run --full

# weight management
python select-models.py --show-ranking
python select-models.py --set-weight release_date 0.5
python select-models.py --set-priority openrouter 2.0
python select-models.py --set-missing-release 0.5

# reasoning probe
python select-models.py --dry-run --reasoning
```

## Keys

Set in Windows user env (`HKEY_CURRENT_USER\Environment`):

- `NVIDIA_API_KEY` (or `NGC_API_KEY`)
- `GOOGLE_API_KEY` / `GEMINI_API_KEY`
- `NOVITA_API_KEY`
- `OLLAMA_API_KEY` / `OLLAMA_CLOUD_API_KEY`
- `OPENROUTER_API_KEY` (optional)

## Selector ownership

- `select-models.py` is the central multi-provider selector.
- `hermes-model-selector.py` is the existing Nous-specific selector (primary-slot authority).
- `hermes-ollama-selector.py` is local Ollama (fallbacks/optional default).
- Merge owner for `model.default`: see `selector-intention.md` "Review" section; not merged yet.

## Scheduler notes

- Current task defaults: nightly restart 3 AM; model selector 4 AM (task-registration params).
- Schedule times are exposed as parameters; edit the .ps1 only as a last resort.
