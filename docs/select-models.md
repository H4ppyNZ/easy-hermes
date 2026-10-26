# select-models.py — Central free-model selector

`select-models.py` is the multi-provider selector for easy-hermes. It discovers
configured providers, fetches their free-tier model catalogs, probes candidates
with a tiny chat completion, and writes the working default + fallback chain to
Hermes `config.yaml`.

## What it does

1. Reads the provider registry from `providers.py`.
2. Skips providers whose required env vars are missing (or fails key validation).
3. Fetches each provider's model catalog (poller `fetch_models`).
4. Sorts candidates (by context window), then probes the top-N with a real chat
   completion (backoff + per-provider quirks such as OpenRouter headers).
5. Merges passing models into `model.default` + `fallback_providers`.
6. Caches per-model pass/fail in `~/.hermes/cache/free_model_probe_cache.json`.

## Usage

```powershell
python select-models.py                # apply (writes config.yaml)
python select-models.py --dry-run      # preview only
python select-models.py --dry-run --json
python select-models.py --dry-run --refresh   # ignore cache, re-probe
python select-models.py --dry-run --nightly  # probe top-12
python select-models.py --dry-run --top 20    # probe top-20
python select-models.py --dry-run --full     # probe every candidate
python select-models.py --providers gemini,novita --dry-run
python select-models.py --prefer-local --dry-run
```

## Flags

| Flag | Meaning |
|------|---------|
| `--dry-run` | Preview only; writes nothing |
| `--refresh` | Ignore the probe cache |
| `--nightly` | Probe top-12 (out-of-hours budget) |
| `--top N` | Override probe budget |
| `--full` | Probe every candidate |
| `--providers` | Comma-separated provider slugs |
| `--prefer-local` | Include local Ollama |
| `--apply` | Write config (default when not dry-run) |
| `--json` | Emit JSON summary |

## Environment / API keys

Keys live in Windows user env (`HKEY_CURRENT_USER\Environment`), not shell env:

- `NVIDIA_API_KEY` / `NGC_API_KEY` (NVIDIA NIM, required)
- `GOOGLE_API_KEY` / `GEMINI_API_KEY` (Gemini, required; new `AQ.` keys work)
- `NOVITA_API_KEY` (Novita, required)
- `OLLAMA_API_KEY` / `OLLAMA_CLOUD_API_KEY` (Ollama Cloud, optional)
- `OPENROUTER_API_KEY` (OpenRouter, optional but enables probing)

## Key validation

Before probing, `providers.validate_key()` makes one authenticated smoke request
(`/models`). Bad/missing keys fail fast with a clear message instead of 404/403
noise. Non-auth HTTP errors and network errors are treated as inconclusive, so a
flaky endpoint does not block a run.

## Cache

`~/.hermes/cache/free_model_probe_cache.json` stores per-model pass/fail + note.
Cached results are reused unless `--refresh`. After code changes (e.g. new
headers), run with `--refresh` once.

## Provider quirks

- **OpenRouter**: requires `HTTP-Referer` + `X-Title` headers on chat
  completions; configured in `providers.py`.
- **NVIDIA NIM**: models are account-gated; 404 usually means no agreement on
  build.nvidia.com, not a bad key.
- **Gemini**: new keys start with `AQ.` (native REST `?key=`); top-listed models
  may be deprecated — use `--top 12`.
- **Novita**: `NOT_ENOUGH_BALANCE` means exhausted/expired credit (free
  Sandbox credit is 90-day, one-time and may not cover LLM API).
- **Ollama Cloud**: listing only (unverified).

## Ranking matrix (configurable)

`select-models.py` scores each passing model with a weighted sum of normalized
factors. Configure it in `~/.hermes/config.yaml` under `model_ranking`:

```yaml
model_ranking:
  weights:
    release_date: 0.40
    context_window: 0.30
    provider_priority: 0.30
  provider_priority:
    openrouter: 1.0
    gemini: 0.8
    nvidia: 0.7
    novita: 0.6
    ollama-cloud: 0.75
    ollama-local: 0.9
  missing_release_date: 0.0
```

- `weights` are normalized to sum 1.
- `provider_priority` values are clamped to 0..1; missing providers default to 1.0.
- `missing_release_date` is the normalized score used when a model has no release
  date (0.0 = penalize, 1.0 = treat as newest).
- Release dates come from the provider catalog (`created` for OpenRouter) and are
  normalized per run (min/max). Other providers may have no release date.

If `model_ranking` is absent, built-in defaults are used and written on first apply.

## Tests

```powershell
python -m pytest -q   # no network
```
