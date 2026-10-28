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
| `--top N` | Override probe budget |
| `--full` | Probe every candidate |
| `--reasoning` | Probe with reasoning_effort=high and report reasoning chars |
| `--providers` | Comma-separated provider slugs |
| `--model` | Retest a specific model id on selected providers |
| `--prefer-local` | Include local Ollama |
|| `--capability CAP` | Keep only models whose capabilities include CAP |
|| `--hermes-home PATH` | Override Hermes home directory |
|| `--today YYYY-MM-DD` | Treat run as happening on given date (testing) |
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
    reasoning: 0.0
    response_time: 0.0
    boost: 0.0
  provider_priority:
    openrouter: 1.0
    gemini: 1.0
    nvidia: 1.0
    novita: 1.0
    ollama-cloud: 1.0
    ollama-local: 1.0
    nous: 1.0
  missing_release_date: 0.0
```

- `weights` are normalized to sum 1.
- `provider_priority` values are clamped to 0..1; missing providers default to 1.0 (neutral).
- `missing_release_date` is the normalized score used when a model has no release
  date (0.0 = penalize, 1.0 = treat as newest).
- `reasoning` is 1.0 for models that emit reasoning content when probed with `--reasoning`.
- Release dates come from the provider catalog (`created` for OpenRouter) and are
  normalized per run (min/max). Other providers may have no release date.

If `model_ranking` is absent, built-in defaults are used and written on first apply.

## Provider descriptors in config.yaml

When `select-models.py` writes `model.default` and `fallback_providers` to
`config.yaml`, each entry needs a `provider` field that Hermes can resolve at
runtime. Three categories exist:

| Category | Provider value | base_url written? | Example |
|----------|---------------|-------------------|---------|
| Built-in Hermes provider | `nous`, `openrouter`, `ollama-cloud`, `nvidia`, `novita` | No (uses provider's own endpoint) | Ollama Cloud, NVIDIA, Novita models |
| Named custom provider (via `providers:`) | `gemini` | No (uses `providers:` entry) | Gemini models |
| Custom OpenAI-compatible endpoint | `custom` | Yes (`base_url`) | Any unregistered provider |

### Adding a new provider as a named descriptor

If a provider should get a real descriptor (not `custom`), three changes are needed:

1. **select-models.py** — add the provider slug to the allowed tuple in two places:
   - Line ~1097: `hermes_provider = best_provider if best_provider in ("nous", "openrouter", "custom", "<slug>") else "custom"`
   - Line ~1115: `hp = prov if prov in ("nous", "openrouter", "custom", "<slug>") else "custom"`

   The `base_url` guard (`if hp == "custom" and ...`) automatically skips base_url for non-custom providers.

2. **Hermes runtime** — the slug must resolve to a real provider. Either:
   - Add it as a built-in in `hermes_cli/providers.py` (like `ollama-cloud`), **or**
   - Add a `providers:` entry in `config.yaml` with `name`, `base_url`, `api_key`/`key_env` (Hermes resolves these at runtime via `runtime_provider_custom.py`)

3. **Migration** — existing configs with `provider: custom` + `base_url: <endpoint>` for that provider should be converted to `provider: <slug>` (no base_url). A one-pass Python migration:
   ```python
   if entry.get("provider") == "custom" and entry.get("base_url", "").rstrip("/") == "<endpoint>":
       entry["provider"] = "<slug>"
       entry.pop("base_url", None)
   ```

### Why not just always use `custom`?

`custom` works functionally, but it loses provider identity in logs, config, and the
NightlyRestart monitor's status message. A named descriptor makes it clear which
backend is being used and avoids ambiguity when multiple custom endpoints share
similar base URLs.

## Tests

```powershell
python -m pytest -q   # 69 tests, no network
```
