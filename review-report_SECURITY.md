# Security Review — easy-hermes

**Repository:** `C:/Users/mattw/Documents/repos/easy-hermes`
**Reviewer:** Security (trust boundaries, injection, authz, secret handling, unsafe deserialization)
**Scope:** `poll_*.py`, `safe_url.py`, `poll_base.py`, `atomic_io.py`, `providers.py`, `select-models.py`, `hermes-model-selector.py`, `hermes-ollama-selector.py`, `hermes-setup.ps1`, `NightlyRestartTask.ps1`, `Set-NightlyTask.ps1`, `Set-ModelSelectorTask.ps1`, `Run-ModelSelectorTask.ps1`
**Out of scope (other reviewers):** performance, style, naming, correctness of control flow

---

## Summary

The three hardenings named in the brief are **present in the tree**, and two of them are genuinely effective:

- **Pagination origin validation** — `safe_url.py` exists and all five pollers call `safe_url.safe_next(...)` on every server-supplied pagination URL (`poll_openrouter.py:185`, `poll_nvidia.py:490`, `poll_gemini.py:579`, `poll_novita.py:373`, `poll_ollama_cloud.py:315`). I verified empirically that an absolute cross-origin `next` URL is dropped to `None` while a same-origin URL passes. The primary SSRF/credential-exfiltration path from the previous report is **closed**.
- **Allowlisted env base URLs** — `poll_nvidia.py:41-55` and `poll_gemini.py:40-56` now gate `NVIDIA_BASE_URL` / `GEMINI_BASE_URL` on `netloc == "<official host>"`. I confirmed at runtime that `https://evil.example.com`, `https://integrate.api.nvidia.com.evil.com`, and `https://user:pw@integrate.api.nvidia.com` are all ignored. **Closed.**
- **Atomic writes** — `atomic_io.py` implements `mkstemp` → `fsync` → `os.replace` (`atomic_io.py:63-78`) plus an `msvcrt`/`fcntl` file lock, and `config.yaml` / cache writes in the selectors go through it. **Present.**

What remains is a set of **fail-open fallbacks** and **trust-boundary crossings that are still open**: the origin guard degrades to "allow" instead of "deny" whenever a URL cannot be parsed, the env-base-URL allowlist accepts host-less values that produce `file://`-scheme URLs, the Gemini API key is still placed in a request URL (query string) rather than a header in two places, and `select-models.py` interpolates an unvalidated, remote-supplied model id directly into a URL path. The installer still downloads and executes a remote script with no integrity check, and the scheduled-task wrapper is a user-writable file that is the task's execution target.

**Verdict: request changes** — no single finding is a trivially weaponisable remote exploit on its own, but the origin guard's fail-open semantics mean the headline mitigation is not a control, it is a hint.

---

## Findings

### BLOCKER

#### B1. `safe_url.same_origin()` fails open on any input it cannot parse — the origin guard is not a control
**File:** `safe_url.py:21-29` (consumed at `poll_base.py:65-71`, `poll_openrouter.py:185`, `poll_nvidia.py:490`, `poll_gemini.py:579`, `poll_novita.py:373`, `poll_ollama_cloud.py:315`)

`same_origin()` returns `True` for anything `origin_of()` cannot classify:

```python
def same_origin(url, base):
    u = origin_of(url)
    if u is None:
        return True  # relative / no origin -> resolve against base
```

`origin_of()` returns `None` for scheme-relative (`//evil.example.com/x`), scheme-less (`evil.example.com/x`), whitespace, `javascript:...`, and non-string inputs. Every one of those is then treated as "safe, same origin". Verified:

```
safe_next('//evil.example.com/collect',  BASE) -> '//evil.example.com/collect'
safe_next('evil.example.com/collect',      BASE) -> 'evil.example.com/collect'
safe_next('not a url at all',              BASE) -> 'not a url at all'
safe_next('javascript:alert(1)',           BASE) -> 'javascript:alert(1)'
safe_next(12345,                           BASE) -> 12345
```

The inline fallback duplicated in each poller (`poll_openrouter.py:20-26`, `poll_nvidia.py:11-15`, `poll_gemini.py:13-17`, `poll_novita.py:14-18`, `poll_ollama_cloud.py:14-18`) is strictly worse — a bare `safe_next` that returns `nxt` unconditionally — so any import failure silently disables origin validation entirely.

**Why this is a blocker rather than a nit:** the entire purpose of this module is to be the boundary that stops a server-supplied URL from carrying credentials to a third party. A guard that answers "allow" when it cannot decide is not a guard. Today the *absolute-URL* path (the one that would actually leak a Bearer token) is correctly rejected, which is why this is not a live exploit — but the fail-open semantics mean any future `_next_url` that yields a raw scheme-relative or non-URL string, or any `urlsplit` exception, silently reopens the original SSRF/credential-exfiltration hole with no test failing.

**Suggested fix:** invert the default. Deny on uncertainty, and make the inline fallbacks fail closed.
```python
def same_origin(url, base):
    u = origin_of(url)
    if u is None:
        return False          # unparseable -> deny
    b = origin_of(base)
    if b is None:
        return False
    if u[0] not in ("http", "https") or b[0] not in ("http", "https"):
        return False
    return u == b
```
Additionally: require the scheme to be `https` (or explicitly opt into `http` for localhost-only paths such as Ollama), restrict the netloc to a per-poller allowlist rather than to "whatever the start URL happened to be", and delete the five duplicated inline `safe_url` shims in favour of a single `sys.path`-guaranteed import (see `poll_template.py:13`, which already does this correctly). Add a regression test asserting `safe_next("//evil/x", base) is None` and `safe_next("file:///x", base) is None`.

---

### MAJOR

#### M1. `poll_base.safe_origin_check()` swallows all exceptions and returns the unvalidated URL
**File:** `poll_base.py:65-71` (also the generic paginator at `poll_base.py:74-102`)

```python
def safe_origin_check(nxt, base):
    try:
        import safe_url
        return safe_url.safe_next(nxt, base)
    except Exception:
        return nxt          # <- fail-open
```

Any `ImportError`, `AttributeError`, or unexpected exception from `safe_url` returns the **raw, server-supplied** URL to the caller, which then fetches it with the provider's `Authorization` header attached. `paginate()` (`poll_base.py:90`) is currently unused, so this is latent rather than live — but it is the same fail-open pattern as B1 in the shared module that future pollers are told to use (`poll_template.py:15` imports `poll_base` as the blessed path).

**Suggested fix:** `except Exception: return None`. Log the failure. Never return the unvalidated input from a validation helper.

#### M2. `select-models.py` places the Gemini API key in a request URL, and interpolates an unvalidated remote model id into that URL
**File:** `select-models.py:941-945` (`_gemini_probe`), also `providers.py:157` (`validate_key`)

```python
url = f"{base_url.rstrip('/')}/{mid}:generateContent?key={api_key}"
```

Two distinct problems:

1. **Secret in a URL.** The API key travels in the query string. Query strings are recorded in web-server and proxy access logs, in corporate TLS-intercept appliances, in `bash`/shell history if the URL is ever echoed, and in process arguments. The hardened `poll_gemini.py` correctly sends `x-goog-api-key` as a header (`poll_gemini.py:521-523`); `select-models.py` and `providers.validate_key` were not updated to match.
2. **Unvalidated, remotely-sourced model id in a URL path.** `model_id` comes from poller JSON. It is concatenated into the path with no percent-encoding and no character allowlist, and the same string is prefixed with `"models/"` without checking it does not already start with `models/` or contain `/`, `?`, or `#`. A malicious or typosquatted upstream listing can therefore redirect the probe request — and the credentials it carries — to an arbitrary path or host-relative target on `generativelanguage.googleapis.com`, and can overwrite the `key` query parameter with an attacker-chosen value.

**Suggested fix:** send the key as the `x-goog-api-key` header (matching `poll_gemini.py:522`) and percent-encode the model id:
```python
mid = urllib.parse.quote(model_id, safe="")
url = f"{base_url.rstrip('/')}/models/{mid}:generateContent"
req = urllib.request.Request(url, data=..., headers={"x-goog-api-key": api_key, ...})
```
Validate `model_id` against `^[A-Za-z0-9._-]+$` before use, and drop `key=` from `providers.py:157` in favour of the header form. Do not log or print `url` on the error path (`poll_openrouter.py:170` prints URLs — keep it that way only for keyless endpoints).

#### M3. Env base-URL allowlists accept host-less and non-HTTP values, yielding `file://` URLs
**File:** `poll_nvidia.py:41-55`, `poll_gemini.py:40-56`

The guard is `if _host in ("", "integrate.api.nvidia.com")`. `_host` is `urlsplit(value).netloc.lower()`, so the allowlist is bypassed by any value with an empty authority. Verified at runtime:

```
NVIDIA_BASE_URL='file:///etc/passwd'  -> 'file:///etc/passwd/models'
NVIDIA_BASE_URL='not a url'           -> 'not a url/models'
NVIDIA_BASE_URL='/local/path'         -> '/local/path/models'
NVIDIA_BASE_URL='   '                 -> '   /models'
```

`urllib.request.urlopen` will happily fetch `file://` URLs. `file:///C:/Users/<user>/AppData/Local/hermes/auth.json` is therefore reachable as a poller endpoint, and the poller's response is parsed and fed into the candidate pool that becomes `model.default` / `fallback_providers` in `config.yaml` (`select-models.py:1041-1103`). The scheme is never checked, only the netloc, and the empty-netloc branch is explicitly permitted.

**Suggested fix:** require an exact match, reject empty authority, and require `https`:
```python
_ALLOWED = {"integrate.api.nvidia.com"}
p = urllib.parse.urlsplit(_env_base)
if p.scheme == "https" and p.netloc.lower() in _ALLOWED and p.username is None and p.password is None:
    ...
```
Apply the same shape to `poll_gemini.py:47` and to any future base-URL override.

#### M4. Installer downloads and executes a remote script with no integrity verification
**File:** `hermes-setup.ps1:198-203`

```powershell
$installer = "$env:TEMP\hermes-install.ps1"
Invoke-WebRequest -Uri "https://hermes-agent.nousresearch.com/install.ps1" -OutFile $installer -UseBasicParsing
& $installer @installArgs
```

The comment on line 197 claims the installer "handles its own prerequisites, verification and receipts" — that is the *Hermes* installer's behaviour, not a property of this script, and nothing here pins or checks the bytes before execution. A compromised CDN, a BGP/DNS hijack, or a TLS-intercept proxy yields arbitrary code execution in the user's context. `$ScriptVersion = '3'` (line 53) is not a content hash.

The selector fetch (`hermes-setup.ps1:142-156`) has the same shape with a weaker check: `Select-String -Pattern 'def choose_default'` (line 152) proves only that the string occurs somewhere in the file — an attacker who controls the response controls that too.

**Suggested fix:** pin an expected SHA-256 for each artefact, verify with `Get-FileHash -Algorithm SHA256` before execution, and fail closed on mismatch. If pinning the upstream installer is not possible, download it, display the hash, and require an explicit `-AcceptInstallerHash <hex>` confirmation. For the selector, pin to a commit SHA rather than the moving `main` branch (`$SelectorUrl`, line 122 — the comment on line 120 already warns about this and then does not do it).

#### M5. The scheduled task's execution target is a user-writable, predictable file
**File:** `Set-ModelSelectorTask.ps1:57-71`, `Run-ModelSelectorTask.ps1:3`, `hermes-setup.ps1:142`

`Set-ModelSelectorTask.ps1` generates `Run-ModelSelectorTask.ps1` next to itself with hardcoded absolute paths baked in (verified in the committed copy: it points at `C:\Users\mattw\...\hermes-model-selector.py`), and registers it as the task action. Because it runs with `-ExecutionPolicy Bypass` (`Set-ModelSelectorTask.ps1:71`) and is a plain user-writable file, **any process running as the user — including the pollers themselves, or anything that can write into the repo checkout — can replace the file that runs at 04:00 with arbitrary code, and it will execute with the user's credentials and their Hermes auth token.**

Compounding this, `hermes-setup.ps1:142` writes the selector to the predictable, user-writable path `Join-Path $env:TEMP 'hermes-model-selector.py'` — a classic temp-path-target symlink/hardlink pre-creation primitive.

**Suggested fix:** register the task against a script stored outside the user-writable repo tree (or set an ACL denying write to `Users`), remove `-ExecutionPolicy Bypass` from the registered action, and have `Get-SelectorScript` use `New-TemporaryFile` / `Join-Path $env:TEMP ((New-Guid).ToString() + '.py')` instead of a fixed name.

#### M6. `NightlyRestartTask.ps1` executes caller-supplied script paths via scriptblock
**File:** `NightlyRestartTask.ps1:199-215`

```powershell
$otherTasks += @{ Name = (Split-Path $t -Leaf); Command = { & $t } }
...
$output = & $task.Command 2>&1 | Out-String
```

`-ExtraTasks` (line 55) is an arbitrary path list, and each entry is invoked with `&` — full language execution, no validation that the path is inside an expected directory. Whoever can influence the task invocation (a shared config, a wrapper, a helpfully-edited script) controls code that runs nightly as the gateway user. The `Write-Log` at line 212 then writes the output into the Application event log, so any secrets in that output are persisted to a world-readable-by-admins log.

**Suggested fix:** resolve each entry with `Resolve-Path` and assert it is under an allowlisted root before invoking; refuse absolute paths outside the Hermes home; and scrub known secret shapes before writing to the event log.

---

### MINOR

#### m1. Poller output files are written non-atomically and via attacker-influenced paths
**File:** `poll_nvidia.py:600-611`, `poll_gemini.py:642-663`, `poll_novita.py:525-531`, `poll_ollama_cloud.py:487-493`, `poll_openrouter.py:266-280`

All five pollers use `open(path, "w")` rather than `atomic_io.atomic_write_json`, despite `atomic_io` existing and being used by the selectors. `--out` / `--output` values are used verbatim (`poll_nvidia.py:553-563`, `poll_openrouter.py:233-244`), so `--out ../../..` escapes the output directory. `poll_gemini.py:656-660` additionally writes a second copy (`poll_gemini.json`) next to the target without any path check.

**Suggested fix:** route all poller output through `atomic_io.atomic_write_json`, and resolve `--out` against the repo's `out/` directory, rejecting any resolved path that escapes it.

#### m2. Backup of `config.yaml` is written non-atomically
**File:** `select-models.py:1045-1049`

`bak.write_text(CONFIG_PATH.read_text(...))` truncates and rewrites the backup in place. A crash mid-write leaves a corrupt backup — the one artefact kept specifically to recover from a bad `config.yaml` write. The main write on line 1110 is correctly atomic.

**Suggested fix:** `atomic_io.atomic_write_text(bak, CONFIG_PATH.read_text(...))`.

#### m3. `importlib` executes a sibling `.py` as code, selected by filename
**File:** `select-models.py:452-464`

`spec_from_file_location("hermes_model_selector", p)` + `exec_module(mod)` runs whatever is at `<repo>/hermes-model-selector.py`. This is a deliberate compatibility shim, and the path is repo-relative and not attacker-controlled in normal operation — but it means a file dropped into the repo tree is executed, with no signature or hash check, on the first `--apply`-less run that needs a release date.

**Suggested fix:** acceptable as-is if the repo tree is trusted; document the assumption in the module docstring. If the tree is not trusted, import the module normally (`import hermes_model_selector`) rather than via `exec_module`, which at least makes the dependency explicit and testable.

#### m4. `providers.validate_key` reports the API-key variable *name*, not the key
**File:** `providers.py:166`, surfaced at `select-models.py:1130` / `1414`

Not a leak — `key_name` is the environment-variable name, and the value is never printed. Recorded only to confirm that the secret-handling here is correct; no change required. Worth keeping the `x-goog-api-key` fix in M2 from regressing this.

---

### NIT

- **n1.** `poll_gemini.py:652` `write_output` returns the path even when the write raised and was swallowed by the caller's `except Exception: pass` at line 661 — the caller cannot distinguish success from failure. (`poll_gemini.py:779-784`.)
- **n2.** `safe_url.origin_of()` lowercases the netloc but not the path, and performs no Unicode normalisation; `https://integrate.api.nvidia.com` vs. a homoglyph or a trailing-dot variant (`integrate.api.nvidia.com.`) will not match. With the per-poller netloc allowlist suggested in B1 this becomes moot.
- **n3.** `poll_openrouter.py:135` `if candidate.startswith("http")` accepts `http://` (cleartext) as a pagination target; a same-host downgrade to plaintext would still carry the Bearer token over the wire. Require `https` unless the base is a localhost address.
- **n4.** `Set-NightlyTask.ps1:26` and `Set-ModelSelectorTask.ps1:71` both register actions with `-ExecutionPolicy Bypass`. This is needed for an unsigned local file, but it means the registered task would still run a tampered script without complaint — see M5.
- **n5.** `select-models.py:907` uses `__import__("json")` inline where a module-level `import json` is already present at line 4. Harmless; noted only because `__import__` with a dynamic name is a smell that auditors will flag.

---

## Verified present (no action needed)

| Claim | Status | Evidence |
|---|---|---|
| Pagination origin validation | **Present, effective for absolute URLs** | `safe_url.py`; all 5 pollers call `safe_url.safe_next`; cross-origin absolute URL returns `None` (runtime-verified) |
| Allowlisted env base URLs | **Present, but bypassable** | `poll_nvidia.py:41-55`, `poll_gemini.py:40-56`; evil host correctly ignored — see M3 for the empty-netloc bypass |
| Atomic writes | **Present** | `atomic_io.py:63-78` (mkstemp+fsync+os.replace), `atomic_io.py:21-56` (file lock); used at `select-models.py:52,61,129,1110,1370`, `hermes-model-selector.py:191,213,793`, `hermes-ollama-selector.py:309,399` |
| No unsafe deserialization | **Confirmed** | `yaml.safe_load` / `yaml.safe_dump` only (`hermes-model-selector.py:795,810`, `select-models.py:1036,1109`); no `pickle`, `marshal`, `eval`, `exec`, or `subprocess` anywhere in the tree |
| `file://` / `ftp://` blocked by `urlopen` | **No — `file://` is fetched** | runtime-verified in M3 |
| Secrets echoed to stdout/stderr | **No leaks found** | keys are read and forwarded as headers; only variable *names* are printed (`providers.py:166`) |
| `poll_nous.py` cache read | **Safe** | reads `endpoint_model_metadata.json`, no credentials, no network |
| `providers.validate_key` URL construction | **Fixed in `poll_gemini.py`, not in `select-models.py`** | header form at `poll_gemini.py:522`; query-string form still at `select-models.py:945`, `providers.py:157` |

---

## Overall verdict

**Request changes.**

The repository is meaningfully safer than the version described in `review-report_OLD.md`: the SSRF/credential-exfiltration blockers are genuinely closed, the env-base-URL overrides are allowlisted, and unsafe deserialization is absent. But the origin guard — the single control that the whole pagination design rests on — fails **open** rather than closed (`safe_url.py:24`, `poll_base.py:71`, and five duplicated shims), the Gemini key still travels in a query string and an unvalidated remote model id is interpolated into a URL path, and the installer path executes unpinned remote code. None of these is a drive-by remote exploit on its own; each requires an upstream compromise, a local writer, or a network-position attacker. Together they mean the documented hardening is not yet the load-bearing control it is presented as.

Suggested order of work: **B1 → M1 → M2 → M3** (all four are small, localised edits that close the credential-handling boundary), then **M4/M5** (installer pinning and the scheduled-task trust root), then the minor/nit items.
