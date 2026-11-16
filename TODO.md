# TODO

## 1. Handle Nous auth expiry / cooldown (OPEN)

Symptom (seen 2026-11-16): `hermes auth list` shows nous rate-limited (429) with a
cooldown timer, and Hermes itself reports "no API key or login was found for it. Add one
with 'hermes auth add nous' or pick a connected provider in /model." The selector's
`--full` run still listed 3 candidates from the metadata cache but every probe came back
`[cache] ok/fail`, so no model is re-verified while the token is benched.

Current state:
- `selector/runner.py` reads `<HERMES_HOME>/auth.json` -> credential_pool.nous[*].access_token
  into NOUS_API_KEY when that env var is unset. It does NOT check expires_at /
  last_error_reset_at / failure_reason, so an expired or rate-limited token is used as-is.
- `providers.validate_key()` treats HTTP 401/403 as "key rejected"; a 429 is deliberately
  not a key problem, so a benched token passes validation and the provider is skipped only
  later by probe results.
- `hermes auth refresh nous` exists (device_code singleton supports refresh; rotates tokens
  and clears the local exhaustion block). Nothing in easy-hermes calls it.
- `NightlyRestartTask.ps1` never touches auth. Step 2 can therefore run for weeks against a
  dead token without anyone noticing.

Fix shape (not yet implemented):
1. In `selector/runner.py`, after loading the nous credential, skip probing and print an
   explicit "nous: token expired/cooldown until <ts>" line when `expires_at` has passed or
   `last_error_reset_at` is in the future. Do not silently fall back to stale cache entries.
2. Have step 2 of `NightlyRestartTask.ps1` call `hermes auth refresh nous` before the
   selector and treat a non-zero exit as a logged warning (not fatal), since other providers
   may still pass.
3. The cron monitor prompt (`cron/nightly-monitor-prompt.txt`) should report a repeated
   "key OK (NOUS_API_KEY)" + all-`[cache]` output as an auth-staleness signal, not as
   individual model failures.
4. Add a test for the new expiry/cooldown branch (patch paths.HERMES_HOME at the
   `selector.paths` level, per the note in tests/test_select_models_config.py).

## 2. check/build/test process to "tidy up" easy-hermes (PARTLY DONE 2026-11-16)

Applied in this pass (uncommitted, on `main`):
- `tools/check_repo_health.py` added: asserts every .ps1 is pure ASCII, MANIFEST.json covers
  every `providers.py` registry module plus the spec-loaded `hermes-ollama-selector.py`,
  README one-liners carry the current `?v=N`, and `$ScriptVersion` == VERSION. Run it with
  `python tools/check_repo_health.py` before every push.
- `NightlyRestartTask.ps1`: replaced two U+2500 box-drawing comment rules with ASCII `--`
  (the repo's own rule is ASCII-only .ps1 source; 381 non-ASCII bytes were present).
- `README.md`: one-liners bumped `?v=4` -> `?v=5`; test count 69 -> 81.
- `AGENTS.md`, `docs/select-models.md`, `docs/selector-intention.md`: test count 69 -> 87.
- Nous auth expiry guard added: `selector/runner.py` `_nous_cred_state()` skips probing when the
  pooled access token is expired, prints the reason plus the recovery command, and no longer
  reports a stale cache as live results. Rate-limit cooldowns deliberately do NOT skip
  probing (the cache still holds real pass/fail data). Tests: `tests/test_nous_auth_state.py`
  (6 tests; suite now 87).

Still open under this item:
- Resolved 2026-11-16: `Set-ModelSelectorTask.ps1` deleted, with its `MANIFEST.json`
  entry, the `AGENTS.md`/`README.md`/`docs/ollama-selector.md` rows and the generated
  `Run-ModelSelectorTask.ps1` `.gitignore` entry; `tools/Configure-FreeModelFallbacks.ps1`
  deleted; the stale `ReviewNightlyTask.ps1` comment in `hermes-setup.ps1` reworded.
- Resolved 2026-11-16: the three `work-*` clones under Documents/repos (gemini, novita,
  ollama-cloud) deleted, the five merged/superseded local branches deleted, and the six
  merged origin feature branches pruned. Only `main` remains locally and on origin.
- "Omega" is undefined in this repo. If it means a named review/tidy pass (capability,
  performance/cost, observability/ux, accessibility, compliance axes from README), say which
  axes are in scope before starting.
