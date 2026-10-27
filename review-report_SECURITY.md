# Security Review — easy-hermes (updated)

**Repository:** `C:/Users/mattw/Documents/repos/easy-hermes`
**Reviewer:** Security (trust boundaries, injection, authz, secret handling, unsafe deserialization)
**Scope:** `poll_*.py`, `safe_url.py`, `poll_base.py`, `atomic_io.py`, `providers.py`, `select-models.py`, `release_dates.py`, `hermes-setup.ps1`, `NightlyRestartTask.ps1`, `Set-NightlyTask.ps1`, `Set-ModelSelectorTask.ps1`, `Run-ModelSelectorTask.ps1`
**Verification:** current tree at commit `81014e4`; existing test suite passes (`69 passed`).

---

## Summary

The refactor closed the headline SSRF/credential-exfiltration path for **absolute** cross-origin pagination URLs, removed the duplicated inline `safe_url` shims, and made `poll_base.safe_origin_check()` fail closed. `_gemini_probe` no longer puts the key in the query string. The env base-URL guards now reject empty netlocs, so the `file://` bypass is gone.

What remains: `providers.validate_key` still leaks the Gemini key into the query string, the env guards still permit `http://` and userinfo, poller `--base-url` overrides bypass the guards entirely, installer/scheduled-task trust roots are unchanged, and poller output writes are still non-atomic and path-traversal prone.

**Verdict: request changes.**

---

## Prior findings status

| ID | Finding | Status |
|---|---|---|
| B1 | `safe_url.same_origin()` fail-open | **PARTIALLY FIXED** (cross-origin absolute + scheme-relative now denied; relative/unparseable still allowed) |
| M1 | `poll_base.safe_origin_check()` fail-open | **FIXED** |
| M2 | `_gemini_probe` key in URL + unvalidated model id | **PARTIALLY FIXED** (header form + quoting done; `providers.validate_key` still query string; no charset allowlist) |
| M3 | env base-URL allowlist empty-netloc bypass | **PARTIALLY FIXED** (`file://` closed; `http://` and userinfo still allowed) |
| M4 | installer unpinned remote execution | **STILL OPEN** |
| M5 | scheduled-task wrapper is user-writable target | **STILL OPEN** |
| M6 | `NightlyRestartTask.ps1` executes caller-supplied paths | **PARTIALLY FIXED** (existence check only) |
| m1 | non-atomic poller output / `--out` traversal | **STILL OPEN** |
| m2 | non-atomic `config.yaml` backup | **STILL OPEN** |
| m3 | `importlib` exec of sibling `.py` | **STILL OPEN** |
| m4 | `providers.validate_key` reports var name only | **NO ISSUE** (verified correct) |
| n1 | `write_output` ambiguity on failure | **STILL OPEN** |
| n2 | Unicode/homoglyph in origin check | **STILL OPEN** |
| n3 | OpenRouter `startswith("http")` accepts cleartext | **STILL OPEN** |
| n4 | `-ExecutionPolicy Bypass` on task actions | **STILL OPEN** |
| n5 | `__import__("json")` smell | **STILL OPEN** |

---

## Findings

### MAJOR

#### M2 (partial). `providers.validate_key` still sends the Gemini key in the query string
**File:** `providers.py:154-166`
`url = base + "/models?key=" + urllib.parse.quote(key, safe="")` places the secret in the URL, where it can be logged by proxies/intermediaries. `_gemini_probe` already uses the `x-goog-api-key` header; this path was not updated.
**Suggested fix:** send the header form matching `poll_gemini.py:521` and `_gemini_probe`, and drop `key=` from the URL.

#### M3 (partial). Env base-URL guards still allow `http://` and userinfo
**File:** `poll_nvidia.py:41-47`, `poll_gemini.py:40-46`
`_scheme in ("", "http", "https")` permits a cleartext override (Bearer/x-goog-api-key sent in plaintext) and there is no userinfo check, so `https://user:pw@integrate.api.nvidia.com` is accepted.
**Suggested fix:** require `scheme == "https"`, reject empty scheme, and require `username is None and password is None`.

#### NEW-M1. Poller `--base-url` overrides bypass the env allowlists and can redirect credentials
**File:** `poll_nvidia.py:431-436` (and `base_url` handling in `poll_gemini.py:496-514`, `poll_openrouter.py:154-158`)
The env-var allowlist protects only the environment path. `--base-url`/`--endpoint`/`--base` accept an arbitrary URL and the poller attaches the provider's `Authorization`/`x-goog-api-key` to it. A wrapper or scheduled invocation that passes a hostile `--base-url` exfiltrates the key. (select-models probes use trusted `probe_base_url` from `providers.py`, so the exposure is mainly the CLI pollers.)
**Suggested fix:** apply the same host/scheme/userinfo allowlist to any caller-supplied base URL, or reject overrides whose host is not the provider's official host.

#### M4. Installer downloads and executes a remote script with no integrity check
**File:** `hermes-setup.ps1:198-203` (installer), `hermes-setup.ps1:142-156` (selector)
`Invoke-WebRequest ... install.ps1` followed by `& $installer` with no hash verification; the selector is accepted based on a substring match (`Select-String 'def choose_default'`). `$SelectorUrl` still points at moving `main`.
**Suggested fix:** pin SHA-256 hashes, verify with `Get-FileHash` before execution, and fail closed on mismatch.

#### M5. Scheduled-task execution target is a user-writable, predictable file
**File:** `Set-ModelSelectorTask.ps1:57-71`, `Run-ModelSelectorTask.ps1:3`
The wrapper is written next to the repo and registered with `-ExecutionPolicy Bypass`; any process running as the user can replace it and gain the user's credentials/token at the next run. `Get-SelectorScript` also uses a fixed `$env:TEMP\hermes-model-selector.py` path.
**Suggested fix:** store the wrapper outside the user-writable tree (or ACL it), drop `-ExecutionPolicy Bypass`, and use a randomized temp filename.

#### M6 (partial). `NightlyRestartTask.ps1` still executes caller-supplied script paths
**File:** `NightlyRestartTask.ps1:199-224`
The new `Test-Path` check only confirms existence; each `-ExtraTasks` entry is still invoked with `&` (full language execution) and its output (which may contain secrets) is written to the Application event log.
**Suggested fix:** resolve each path and assert it lies under an allowlisted root before invocation; scrub known secret shapes before `Write-EventLog`.

### MINOR

#### B1 (residual). `safe_url` still allows relative/unparseable strings
**File:** `safe_url.py:21-34`
Cross-origin absolute and scheme-relative URLs are now blocked, but any string without a netloc (including host-like `evil.example.com/x`) is treated as a safe relative path. In practice these resolve against the base host, so no cross-origin fetch occurs, but the guard still answers "allow" on uncertainty.
**Suggested fix:** keep the relative allowance for genuine relative paths, but reject strings that look like an authority (contain a dot-host or `:` scheme) unless they parse as a relative path under the base.

#### m1. Poller output writes are non-atomic and `--out` is not contained
**File:** `poll_nvidia.py:593-611`, `poll_gemini.py:643-663`, `poll_novita.py:521-529`, `poll_ollama_cloud.py:483-491`, `poll_openrouter.py:263-277`
All five use `open(path, "w")`; `--out`/`--output` is used verbatim, so `--out ../../..` escapes the output directory. `poll_gemini.write_output` also writes a second `poll_gemini.json` next to the target with no path check.
**Suggested fix:** route through `atomic_io.atomic_write_json` and resolve `--out` against the repo `out/` directory, rejecting escapes.

#### m2. `config.yaml` backup is written non-atomically
**File:** `select-models.py:1088-1090`
`bak.write_text(...)` truncates in place; a crash mid-write corrupts the recovery backup.
**Suggested fix:** `atomic_io.atomic_write_text(bak, CONFIG_PATH.read_text(...))`.

#### m3. `importlib` executes sibling `.py` files as code
**File:** `select-models.py:844-847`, `1029-1032`, `1062-1065`
`spec_from_file_location(...) + exec_module(...)` runs `hermes-ollama-selector.py` from the repo tree with no integrity check.
**Suggested fix:** import normally (`import hermes_ollama_selector`) or document the trusted-tree assumption.

#### NEW-m2. `_gemini_probe` does not validate the model id charset
**File:** `select-models.py:967-973`
Quoting is applied, so URL injection is largely closed, but there is no allowlist (e.g. `^[A-Za-z0-9._/-]+$`) and the `models/` prefix logic is purely string-based.
**Suggested fix:** validate against an allowlist before constructing the URL.

#### NEW-m3. `release_dates.py` interpolates a remote model id into URL paths
**File:** `release_dates.py:38`, `release_dates.py:54`
`urllib.parse.quote(model_id, safe="/")` preserves `/`, allowing path traversal against the OpenRouter/HuggingFace endpoints (no credentials involved, but untrusted data drives outbound requests).
**Suggested fix:** quote with `safe=""` and validate the model id.

### NIT

- **n1.** `poll_gemini.py:652` `write_output` returns the path even when the write raised and was swallowed. (File: `poll_gemini.py:643-664`)
- **n2.** `safe_url.origin_of()` performs no Unicode normalization; trailing-dot/homoglyph hosts won't match. (File: `safe_url.py:11-18`)
- **n3.** `poll_openrouter.py:132` `candidate.startswith("http")` accepts cleartext pagination targets. (File: `poll_openrouter.py:132`)
- **n4.** `Set-NightlyTask.ps1:26` and `Set-ModelSelectorTask.ps1:71` register actions with `-ExecutionPolicy Bypass`. (Files above)
- **n5.** `select-models.py:933` uses `__import__("json")` where `json` is already imported. (File: `select-models.py:933`)

---

## Verified present (no action needed)

| Claim | Status | Evidence |
|---|---|---|
| Pagination origin validation | Present, effective for absolute URLs | `safe_url.py`; all 5 pollers call `safe_url.safe_next` |
| `poll_base.safe_origin_check` fail-closed | Fixed | `poll_base.py:65-71` returns `None` on exception |
| Inline `safe_url` shims removed | Fixed | pollers import `safe_url` directly |
| Allowlisted env base URLs (empty netloc) | Fixed | `poll_nvidia.py:41-47`, `poll_gemini.py:40-46` require truthy host |
| Atomic writes (selectors) | Present | `atomic_io.py:63-78`; used in `select-models.py`, `hermes-ollama-selector.py` |
| No unsafe deserialization | Confirmed | `yaml.safe_load`/`safe_dump` only; no pickle/marshal/eval/exec/subprocess |
| Secrets echoed | No leaks | only variable names printed (`providers.py:166`) |
| `providers.validate_key` var-name reporting | Correct | `providers.py:166` |

---

## Overall verdict

**Request changes.**

No single finding is a trivially weaponisable remote exploit on its own; each requires an upstream compromise, a local writer, a network position, or a malicious invocation. But the env/CLI base-URL boundary is still bypassable in three ways (http, userinfo, `--base-url`), the Gemini key still travels in a query string in `providers.validate_key`, and the installer/scheduled-task trust roots are unchanged. The documented hardening is not yet the load-bearing control it is presented as.

Suggested order of work: **M2 (providers) → M3 (https+userinfo) → NEW-M1 (--base-url allowlist) → M4 (installer pinning) → M5 (task trust root) → M6 → minor/nit items.**
