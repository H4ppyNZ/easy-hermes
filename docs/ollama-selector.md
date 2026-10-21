# Ollama models in the Hermes selector

## What this is

`hermes-ollama-selector.py` is the Ollama counterpart of `hermes-model-selector.py`
(which handles the Nous Portal free tier). It probes the models served by a local
Ollama daemon and folds the ones that actually answer into Hermes' config:

- as entries in `fallback_providers` (provider `custom`, with `base_url`), and
- optionally as `model.default` (`--set-default`).

Every model a local Ollama serves is free and private, so there is no promotion
gate: a model that answers the probe cleanly is admitted. The probe still matters
because a pulled model can be broken (bad quant, missing template, OOM on load).

## Run it

```powershell
python hermes-ollama-selector.py --dry-run          # preview, write nothing
python hermes-ollama-selector.py                   # probe + merge into fallbacks
python hermes-ollama-selector.py --set-default     # also promote best model to default
python hermes-ollama-selector.py --replace         # replace the chain instead of merging
python hermes-ollama-selector.py --no-apply        # probe + cache only
python hermes-ollama-selector.py --base-url http://host:11434
python hermes-ollama-selector.py --hermes-home C:\Users\me\AppData\Local\hermes
```

Probe results are cached at `<hermes-home>/cache/ollama_model_cache.json`.

## How it fits (and where it does not yet)

- Today: a standalone script, run on demand. It merges Ollama models into the
  existing `fallback_providers` chain (Nous entries are preserved; dedup by
  provider+model+base_url).
- It does not touch `model.default` unless `--set-default` is given, so it does
  not fight `hermes-model-selector.py` for the primary slot.
- Later: merge into `hermes-setup.ps1` step 5 ("model selector"). Natural
  integration points:
  - After the Nous selector runs, invoke this script with `--apply-fallbacks`
    (default) so local models are appended to the same chain.
  - Expose a `-Local` / `-Ollama` switch in `hermes-setup.ps1` that also passes
    `--set-default` for users who want a fully-local primary.
  - Register a scheduled task (mirror `Set-ModelSelectorTask.ps1`) if you want
    the local chain refreshed nightly; Ollama models change rarely, so on-demand
    is usually enough.

## Caveats

- The probe uses the OpenAI-compatible endpoint (`/v1/chat/completions`), so the
  Ollama daemon must be running (`ollama serve`).
- Probing all installed models can take a few minutes on first run; subsequent
  runs re-probe (no TTL cache by design, matching the Nous selector's stance
  that a single pass is one sample).
- Fallback entries carry `base_url`, which Hermes uses to route to the local
  daemon; `api_key` is left unset (Ollama needs none).
