# select-intention.md — Goal & Design for centralized free-model selection

## Goal

A person with **no Hermes/AI/coding experience** installs `easy-hermes` and, with
nothing more than the API keys they already have (or none at all for local
Ollama/OpenRouter), ends up on a **working, recent free model by default**,
with a sensible fallback chain. Adding a new provider later must not require
rewriting the selector — just adding one poller that follows the contract below.

## Non-goals (for now)

- No auto-execution of paid models or spending. Free-tier only.
- No voice/UX polish of model answers (functionality first).
- No merging provider-specific quirks into one giant script.

## Core idea: two layers

1. **Pollers** (`poll_<provider>.py`) — *catalog only*.
   - One file per provider, runnable standalone.
   - List what the provider advertises as free (or free-tier usable).
   - Normalize to a **uniform schema**. No probing, no config writes.
   - Output JSON to `out/<provider>.json`.

2. **Selector** (one central process, e.g. `hermes-model-selector.py` generalized
   or a new `select-models.py`) — *validation + promotion*.
   - Discovers which providers are configured (env vars / `config.yaml`).
   - Calls each relevant poller (import or subprocess).
   - **Probes** candidates (tiny chat completion) with backoff + concurrency cap.
   - Applies the promotion gate (clean pass, no 429 retry consumed).
   - Writes `model.default` + `fallback_providers` to `config.yaml`.
   - Caches results.

Pollers stay dumb and uniform; the selector stays smart and central. New providers
are added by dropping in a poller + a one-line registration — no selector rewrite.

## Uniform poller contract

### CLI
- `python poll_<slug>.py` (no args) → human table on stdout, exit 0.
- `python poll_<slug>.py --json` → JSON to stdout **and** `out/<slug>.json`.
- `--out DIR` overrides output dir. `--chat-only` (optional) filters non-chat.
- Missing creds → stderr message + exit 2. Never crash on one failure.

### Module API (importable by the selector)
- `fetch_models(**kw) -> list[dict]` normalized.
- `normalize_model(raw) -> dict`.
- `is_free_model(raw) -> bool` / `filter_models(raws) -> list`.
- `poll(**kw) -> list[dict]` (never raises; returns `[]` on failure).

### Normalized record schema
```json
{
  "id": "provider/model:free",
  "display_name": "Human name",
  "context_window": 131072,
  "rate_limits": { "requests_per_minute": 40, "source": "documented" },
  "notes": "free filter rule + caveats"
}
```
`rate_limits`/`context_window` may be `null` when unknown; never omit keys.

### JSON envelope
```json
{ "source": "<endpoint>", "count": N, "models": [ <record>, ... ] }
```

### Auth
- Env vars only. Document in `.env.example` + README. No hardcoded secrets.

### Filter rule
- Explain the free-only rule in a code comment (e.g. pricing==0, `:free` suffix,
  documented free quota, or "all listed are free-tier" with caveat).

## Provider registry (to be added)

A single source of truth, e.g. `providers.py` or a `providers.yaml`:

```yaml
openrouter:
  module: poll_openrouter
  env: [OPENROUTER_API_KEY]   # optional
  probe: true
nvidia:
  module: poll_nvidia
  env: [NVIDIA_API_KEY, NGC_API_KEY]   # required
  probe: true
gemini:
  module: poll_gemini
  env: [GOOGLE_API_KEY, GEMINI_API_KEY] # required
  probe: true
ollama-cloud:
  module: poll_ollama_cloud
  env: [OLLAMA_API_KEY, OLLAMA_CLOUD_API_KEY]  # optional
  probe: false   # listing only; cloud probing TBD
novita:
  module: poll_novita
  env: [NOVITA_API_KEY, NOVITA_KEY, NOVITA_AI_API_KEY]  # required
  probe: true
ollama-local:
  module: hermes-ollama-selector  # special: no poller, probes local daemon
  env: []
  probe: true
  default_enabled: false  # most users lack local hardware; opt-in via --prefer-local
```

The selector iterates this registry, skips providers with missing required env,
and runs the rest.

## Open questions (decide before coding)

1. **Probe in selector vs poller?** Recommendation: probe in selector (keeps pollers
   fast/listing-only). Pollers stay importable and testable without network probes.
2. **Concurrency cap for probing:** 4 parallel? NIM is ~40 RPM; Nous/OpenRouter
   similar. Need a shared rate limiter or per-provider limits.
3. **Fallback chain ordering:** keep release-date sort for now. Agreed this should
   grow: expose a user-tunable weighting (e.g. `model_ranking` in config.yaml with
   ordered keys/weights like context_window, release_date, provider_priority) and a
   `--order` flag. Local Ollama is NOT preferred by default (most users lack
   hardware); opt-in via `--prefer-local`.
4. **Caching TTL:** current stance is "one pass = one sample, no TTL". Keep, but
   allow `--no-cache`/`--refresh`.
5. **Nous Portal integration:** `hermes-model-selector.py` reads a local cache
   (`endpoint_model_metadata.json`), not a live poller. Should we add
   `poll_nous.py` for consistency, or keep the cache-based path? (Recommend: add
   poller later; keep cache path for now.)
6. **Privacy:** user is privacy-driven and moving to local Ollama. Should the
   default prefer local models when present? (Recommend: yes, opt-in via
   `--prefer-local`.)

## Migration plan

- Phase 1 (done): per-provider pollers exist, normalized schema, tests pass.
- Phase 2: introduce provider registry + a thin selector wrapper that calls pollers
  then probes. Keep `hermes-model-selector.py` and `hermes-ollama-selector.py`
  working unchanged.
- Phase 3: installer (`hermes-setup.ps1`) calls the new selector (fast path: probe top-N
  only, to keep first-run interactive). Scheduled task runs the full probe out of
  hours (daily 4 AM) where per-minute free quotas are not a constraint.
- Phase 4: add `poll_nous.py`, retire cache-only path.

## Additional suggestions (prior to Phase 2)

- **Provider detection:** treat a provider as "configured" if all its required env
  vars are present. Add an override `HERMES_PROVIDERS=gemini,novita` (or
  `providers:` in config.yaml) so users can disable one without unsetting keys.
- **Probe budget:** start at top-12 per provider sorted by context window; expect to
  remove the cap once caching proves out. Full probe only in the nightly task.
  Keeps first-run under ~2 minutes.
- **Merge, don't replace:** on each run, merge passing models into the existing
  chain and mark failed ones DEAD (reuse Nous selector's IN SCOPE / DEAD /
  OUT OF SCOPE logic). Never wipe a working chain because one endpoint blipped.
- **Caching (DECIDED):** `cache/free_model_probe_cache.json` stores per-model
  pass/fail + timestamps + a "last_probed" stamp. Skip re-probing models probed
  within the TTL (default 7 days) unless `--refresh`. This avoids retesting
  stale/invalidated models and saves quota. Also debugging "why did my default
  change?".
- **Backwards compat:** keep `hermes-model-selector.py` and
  `hermes-ollama-selector.py` working; new selector wraps them rather than
  replacing, so existing scheduled tasks keep functioning.
- **Nous poller:** add `poll_nous.py` in Phase 4 so Nous matches the contract;
  until then the selector keeps reading `endpoint_model_metadata.json`.

## Success criteria

- `python select-models.py` (or `hermes-model-selector.py --all`) on a fresh
  install with only `OPENROUTER_API_KEY` set produces a working default model +
  fallback chain without manual intervention.
- Adding provider "foo" requires only `poll_foo.py` + one registry line + tests.
- All pollers pass `python -m pytest -q` with no network.
EOF
echo "written"