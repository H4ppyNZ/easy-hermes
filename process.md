# Process: Review & merge new free-model pollers

$py = "$env:USERPROFILE\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"

Branches to process (one per provider, isolated, not yet merged to `main`):

| # | Provider | Branch | Commit message | Tests |
|---|----------|--------|----------------|-------|
| 1 | OpenRouter | `feature/poll-free-models-openrouter` | `feat(openrouter): draft poller for free models` | 10 passed (verified public API) |
| 2 | NVIDIA NIM | `feature/poll-free-models-nvidia` | `feat(nvidia): draft poller for free models` | 9 passed (best-effort) |
| 3 | Google AI Studio (Gemini) | `feature/poll-free-models-gemini` | `feat(gemini): draft poller for free models` | 8 passed (best-effort) |
| 4 | Ollama Cloud | `feature/poll-free-models-ollama-cloud` | `feat(ollama-cloud): draft poller for free models` | 12 passed (best-effort) |
| 5 | NovitaAI | `feature/poll-free-models-novita` | `feat(novita): draft poller for free models` | 14 passed (best-effort) |

Total: 53 tests passing across all branches.

## Pre-flight
```bash
cd C:\Users\mattw\Documents\repos\easy-hermes
git stash                                    # preserve local hermes-model-selector.py edit
git checkout -b integrate/poll-free-models main
```

## Review each branch (before merging)
```bash
git diff main..origin/feature/poll-free-models-openrouter -- poll_openrouter.py
# repeat per provider; check: no secrets, env vars documented, --json works, tests pass
```

## Merge sequentially (README conflicts expected)
Merge in this order (OpenRouter first — verified, cleanest README section):

```bash
for b in openrouter nvidia gemini ollama-cloud novita; do
  git merge --no-ff origin/feature/poll-free-models-$b || { echo "CONFLICT on $b"; break; }
  python -m pytest -q || { echo "TESTS FAILED after $b"; break; }
done
```

When README conflicts occur, resolve by **combining** each branch's "Pollers"/provider section into one README (don't pick one side). `.env.example` conflicts: concatenate the env var blocks. `poll_*.py` and `tests/` files are disjoint, so they merge cleanly.

## Validate the integration
```bash
python -m pytest -q                          # all suites
python poll_openrouter.py --json | head        # live smoke (no key needed)
python poll_nvidia.py                        # expect exit 2 without NVIDIA_API_KEY
python poll_gemini.py                        # expect exit 2 without GOOGLE_API_KEY
```

## Land it
```bash
git checkout main
git merge --no-ff integrate/poll-free-models
git push origin main
git stash pop                                 # restore local edit
```

## Keep branches for review
Leave `feature/poll-free-models-*` branches un-deleted until validated in production;
delete later with `git push origin --delete feature/poll-free-models-<slug>`.

## Caveats
- Branches were never auto-merged to `main` (per original "do not auto-merge" constraint).
- `poll_nvidia.py`'s test sits at repo root (`test_poll_nvidia.py`); consider moving to `tests/` during merge for consistency.
- Reconcile `.gitignore` (OpenRouter added entries) once, not per branch.

---

## Testing each poller's functionality

Use the scratch clones (no need to touch the main working tree), or `git stash && git checkout feature/poll-free-models-<slug>`.

### OpenRouter (no key needed)
```bash
cd C:\Users\mattw\AppData\Local\hermes\cache\scratch\work-openrouter
python poll_openrouter.py --json > out.json
# expect: table printed + JSON array of free models (id/name/context). Exit 0.
python -m pytest -q   # 10 passed
```

### NVIDIA NIM (key required)
```bash
cd ...\scratch\work-nvidia
set NVIDIA_API_KEY=<key> #Linx
$env:NVIDIA_API_KEY = "<key>" #powershell temporary
[System.Environment]::SetEnvironmentVariable("NVIDIA_API_KEY", "<key>", "User") #powershell permanent
python poll_nvidia.py --json
# without key: python poll_nvidia.py  -> exit 2, clear "missing creds" message
python -m pytest -q   # 9 passed
```

### Gemini (key required)
```bash
cd ...\scratch\verify-gemini
set GOOGLE_API_KEY=<key>
$env:GOOGLE_API_KEY = "<key>" #powershell temporary
[System.Environment]::SetEnvironmentVariable("GOOGLE_API_KEY", "<key>", "User") #powershell permanent
python poll_gemini.py --json
# without key: exit 2
python -m pytest -q   # 8 passed
```

### Ollama Cloud (key optional for public endpoint)
```bash
cd ...\scratch\verify-ollama-cloud
python poll_ollama_cloud.py --json          # tries public endpoint first
set OLLAMA_API_KEY=<key>
[System.Environment]::SetEnvironmentVariable("OLLAMA_API_KEY", "<key>", "User") #powershell permanent
python poll_ollama_cloud.py --json          # authed
python -m pytest -q   # 12 passed
```

### NovitaAI (key required)
```bash
cd ...\scratch\verify-novita
set NOVITA_API_KEY=<key>
[System.Environment]::SetEnvironmentVariable("NOVITA_API_KEY", "<key>", "User") #powershell permanent
python poll_novita.py --json
python -m pytest -q   # 14 passed
```

### What "passing" looks like
- **Exit 0** + non-empty table/JSON = endpoint reachable, filter working.
- **Exit 2** + readable error = creds handling works (expected without keys).
- **pytest green** = filter + normalization logic correct (no network needed).

### Quick all-in-one (from main repo, isolated)
```bash
git stash
for b in openrouter nvidia gemini ollama-cloud novita; do
  git checkout feature/poll-free-models-$b
  python -m pytest -q
done
git checkout main
git stash pop
```
