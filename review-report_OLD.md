# Multi-Reviewer Report — easy-hermes

**Repository:** `C:/Users/mattw/Documents/repos/easy-hermes`
**Domain:** code
**Team size:** 5
**Reviewers:** Security, Correctness, Concurrency, Maintainability, Architecture
**Overall verdict:** request changes

## Per-reviewer findings

### Security Reviewer (security, privacy) — verdict: request changes

- blocker — `poll_openrouter.py:187` — `fetch_all_models` follows server-supplied pagination URLs and re-sends the Bearer API key to those hosts (SSRF/credential exfiltration). Fix: validate origin / strip credentials cross-origin.
- blocker — `poll_nvidia.py:437` — `_next_url` targets fetched with `Authorization` header. Fix: pin to NVIDIA origin.
- blocker — `poll_gemini.py:489` — pagination fetches leak `x-goog-api-key`. Fix: pin to Gemini origin.
- blocker — `poll_ollama_cloud.py:312` — unchecked next URL fetched with Bearer token. Fix: validate origin.
- blocker — `poll_novita.py:361` — unchecked next URL fetched with Bearer token. Fix: validate origin.
- high — `poll_gemini.py:367` — API key embedded in request URL via `_with_key`; leaks via logs/visited. Fix: use header only.
- high — `hermes-model-selector.py:344` — unsanitized model IDs interpolated into release-date lookup URLs. Fix: percent-encode + validate.
- medium — `poll_nvidia.py:28` — env base URL override exfiltrates key. Fix: allowlist hosts.
- medium — `poll_gemini.py:33` — env base URL override exfiltrates key. Fix: allowlist hosts.
- medium — `hermes-setup.ps1:199` — downloads executed without integrity checks. Fix: verify hashes.
- medium — `hermes-setup.ps1:142` — predictable temp file path. Fix: random temp file.
- low — `hermes-setup.ps1:199` — remote installer run without verification. Fix: confirm checksum.
- low — `NightlyRestartTask.ps1:194` — hardcoded external script path. Fix: protect path / verify.
- low — `poll_nvidia.py:602` — output path traversal. Fix: validate path.

### Correctness Reviewer (correctness, testing) — verdict: request changes

- major — `select-models.py:945` — `write_config` called as `write_config(all_passing, False, yaml)` but signature is `write_config(passing, yaml_mod)`; `--apply` raises `TypeError`.
- major — `providers.py:35` — NVIDIA provider entry omits `"slug": "nvidia"`.
- major — `hermes-model-selector.py:393` — `get_free_models_from_cache` direct-indexes cache; missing/malformed cache raises instead of yielding empty.
- major — `hermes-model-selector.py:121` — `load_auth` assumes `LOCALAPPDATA` set; crashes with `KeyError` if unset.
- major — `poll_gemini.py:246` — `_is_generative` returns `True` for an empty `supportedGenerationMethods` list.

### Concurrency Reviewer (concurrency, resilience) — verdict: request changes

- blocker — `hermes-model-selector.py:784` — `update_config()` RMW on `config.yaml` without cross-process lock.
- blocker — `select-models.py:645` — `write_config()` truncates `config.yaml` directly; non-atomic backup.
- blocker — `hermes-ollama-selector.py:394` — fallback path writes `config.yaml` with `open(..., "w")`.
- major — `hermes-model-selector.py:195` — promotion history load/mutate/save without lock.
- major — `hermes-model-selector.py:184` — `save_fallback_cache()` non-atomic JSON write.
- major — `select-models.py:44` — `save_cache()` uses `Path.write_text()` non-atomically.
- major — `Set-NightlyTask.ps1:1` — two scheduled tasks can both run the selector; `IgnoreNew` only dedupes within one task.
- minor — `hermes-ollama-selector.py:306` — Ollama cache JSON non-atomic.
- minor — `poll_gemini.py:632` — poll output JSON files written non-atomically.
- minor — `hermes-model-selector.py:317` — module-global `_OR_MAP` mutated without sync.

### Maintainability Reviewer (maintainability, documentation) — verdict: request changes

- major — `select-models.py:945` — `write_config()` called with 3 args but defined with 2; `--apply` crashes.
- major — `select-models.py:388` — `fetch_provider` uses `__import__` + `TypeError` probing for fetch callable; implicit contract.
- major — `hermes-model-selector.py:813` — hand-rolled `sys.argv` parsing; use `argparse`.
- major — `select-models.py:27` — module-level mutable globals `RANKING`/`REASONING_MODE`.
- major — `poll_nvidia.py:1` — ~300+ lines duplicated with `poll_gemini.py`.
- minor — `poll_novita.py:41` — near-duplicate of `poll_ollama_cloud.py`.
- minor — `poll_novita.py:454` — many alias functions obscure canonical API.
- minor — `providers.py:194` — redundant second loop in `get_api_key`.
- minor — `hermes-model-selector.py:813` — env-derived paths at import time.
- minor — `hermes-model-selector.py:374` — hardcoded release dates in source.
- minor — `poll_nvidia.py:60` — over-engineered `_HeaderMap` adapter.
- nit — `select-models.py:426` — `sort_candidates` duplicates `rank_models`.
- nit — `tests/` — no tests cover selector apply/config paths.

### Architecture Reviewer (architecture, compatibility) — verdict: request changes

- major — `hermes-model-selector.py` + `select-models.py` — two selectors both write `model.default` and `fallback_providers`; last writer wins, can bypass Nous promotion gate.
- major — `select-models.py:597,945` — `write_config` called with extra positional argument; apply path fails.
- major — config writers differ (ruamel comment-preserving vs `yaml.safe_dump`); formatting/comment loss.
- major — `select-models.py:836-842` — selector calls private `_is_chat_like` from `poll_nvidia`.
- major — `select-models.py:369-385` — selector special-cases Ollama and imports `hermes_ollama_selector` directly.
- major — `poll_nvidia.py` — `poll` aliased to a function that can raise, violating never-raises contract.
- major — `NightlyRestartTask.ps1` — hardcoded user-specific paths and host-specific extra task.
- minor — `providers.py` — NVIDIA entry lacks `slug`.
- minor — `select-models.py` — global mutable state for ranking/reasoning mode.
- minor — `test_poll_nvidia.py` — inconsistent test placement (root vs `tests/`).

## Cross-cutting findings (raised by 2+ reviewers)

- **`select-models.py:945` `write_config` arity mismatch** — Correctness, Maintainability, Architecture (high confidence). Breaks `--apply`.
- **NVIDIA provider missing `slug`** — Correctness, Architecture (and registry inconsistency). Fix: add `"slug": "nvidia"`.
- **Module-level mutable globals (`RANKING`, `REASONING_MODE`)** — Maintainability, Architecture. Hidden coupling.
- **Non-atomic config/cache writes + concurrent runs** — Concurrency; also touched by Architecture (two selectors). Lost updates/corruption risk.
- **Two competing selectors owning `model.default`** — Architecture (and implicit in Concurrency). Last-writer-wins.
- **Hardcoded user/host-specific paths in `NightlyRestartTask.ps1`** — Security (low), Architecture (major), Concurrency (major).
- **Duplicated poller scaffolding** — Maintainability (major/minor).

## Coverage

| Axis | Covered by |
|---|---|
| correctness | Correctness |
| testing | Correctness |
| security | Security |
| privacy | Security |
| maintainability | Maintainability |
| documentation | Maintainability |
| architecture | Architecture |
| compatibility | Architecture |
| concurrency | Concurrency |
| resilience | Concurrency |
| performance | — |
| cost | — |
| observability | — |
| ux | — |
| accessibility | — |
| compliance | — |

Uncovered axes: performance, cost, observability, ux, accessibility, compliance. Offer re-run with `--must` additions if desired.

## Top 3 blockers

1. **Unchecked pagination URLs leak API keys (SSRF/credential exfiltration)** across all pollers (`poll_openrouter.py`, `poll_nvidia.py`, `poll_gemini.py`, `poll_ollama_cloud.py`, `poll_novita.py`).
2. **Concurrent read-modify-write on `config.yaml`/caches without locking or atomic writes** (`hermes-model-selector.py`, `select-models.py`, `hermes-ollama-selector.py`).
3. **`--apply` path is broken** (`select-models.py:945`, `write_config` arity mismatch) — selector cannot write config at all.

## One-paragraph summary

The multi-reviewer panel found the easy-hermes toolchain functional but not production-ready: five security blockers around server-controlled pagination URLs exfiltrating API keys, three concurrency blockers around non-atomic concurrent config/cache writes, a broken `--apply` path due to a `write_config` arity mismatch, two competing selectors with incompatible YAML writers, and significant duplication/coupling in the pollers and selectors. Verdict: request changes; address the pagination SSRF and atomic-write blockers first, then the apply-path bug and selector ownership.
