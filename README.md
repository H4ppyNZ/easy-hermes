# easy-hermes

**A one-command setup for Hermes Agent on Windows, plus an automatic free-model
picker that keeps your default model working without you thinking about it.**

Hermes Agent is a terminal-native AI agent (an LLM wired to files, shell, web and
memory). Out of the box you have to choose a provider, paste a key, and keep
re-choosing models as free tiers rotate and die. This repo removes that work:

- installs Hermes and authenticates it in one interactive run,
- probes the **free tier** of eight providers, and writes the best *verified*
  model as your default with a fallback chain behind it,
- re-runs that check every night and restarts the gateway so the new default
  actually takes effect,
- reports the outcome of each nightly run to you.

Free tier only. The selector never enables a paid model, never spends, and
refuses to leave your config in a state where Hermes would silently fall back to
a paid one.

---

## Contents

1. [Requirements](#requirements)
2. [Quick start](#quick-start)
3. [Execution policy](#execution-policy)
4. [Updating](#updating)
5. [What you get](#what-you-get)
6. [How it works](#how-it-works)
7. [Providers](#providers)
8. [Configuration](#configuration)
9. [Selector reference](#selector-reference)
10. [Scheduled tasks and nightly maintenance](#scheduled-tasks-and-nightly-maintenance)
11. [Repository layout](#repository-layout)
12. [Development](#development)
13. [Troubleshooting](#troubleshooting)
14. [Safety and rollback](#safety-and-rollback)
15. [Status and limitations](#status-and-limitations)

---

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10/11 | PowerShell 5.1 is what `iex` uses; all `.ps1` here is ASCII-only and parses under 5.1 |
| Internet access | first install, model probes, and nightly updates all need it |
| An API key (optional) | OpenRouter, Ollama Cloud, Nous and local Ollama work keyless. NVIDIA, Gemini, Novita and Cloudflare need keys |
| Python | not needed separately — the selector runs on the Python inside the Hermes venv |

Hermes itself is installed for you unless you pass `-SkipInstall`. For
Linux/macOS/WSL2 use the official installer instead:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

---

## Quick start

Run this from PowerShell — the default path. It lifts execution policy for this
window only, runs the installer in `-Yolo` mode (install + auth + free-model
selection + scheduled tasks), then puts the policy back:

```powershell
# 1. lift execution policy for this window only
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process

# 2. download and run easy-hermes in yolo mode
#    (full unattended install: Hermes + auth + free-model selection + scheduled tasks)
iex "& { $(irm 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1') } -Yolo"

# 3. put it back
Set-ExecutionPolicy -ExecutionPolicy Restricted -Scope Process
```

If your policy is already `RemoteSigned` or looser, step 1 is unnecessary — run
step 2 on its own.

`-Yolo` is the unattended mode: the only input it can ask for is the Nous Portal
sign-in (a browser login, or paste an API key) if you are not already
authenticated. Everything else is decided for you. The default mode — no
`-Yolo` — is the opposite: it prompts for provider, model, whether to run the
selector, connector and ACP setup, and is the better choice when you want to
choose rather than accept.

### Other modes

| Command (after `iex "& { $(irm '<url>') } ..."`) | What it does |
|---|---|
| `-Yolo` | Full unattended install + auth + selector + scheduled tasks |
| `-Yolo -Model <model-id>` | Same, but pin a model instead of letting the selector pick |
| `-SkipInstall` | Hermes already on `PATH`; skip the install step (guided run) |
| `-SelectorOnly` | Just run the model selector (implies `-SkipInstall`) |
| `-SchedulerOnly` | Just register the nightly tasks and exit |
| `-NoUpdate` | Run without auto-updating, and remember the opt-out |

### Passing parameters

`iwr | iex` executes the downloaded script but **cannot pass parameters** to it —
`-Yolo` would go to `iex`, not the script. Two shapes both work; pick whichever
reads better:

```powershell
# iex with the arguments outside the string
iex "& { $(irm '<url>') } -Yolo"

# scriptblock cast: parameters bind inside
& ([scriptblock]::Create((iwr -useb '<url>').Content)) -Yolo
```

### About the `?v=N` in older one-liners

GitHub raw responses are cached by full URL, so a cached `?v=5` keeps serving old
bytes after a fix lands. The URL above carries no version query, so it always
resolves to current `main`. If you are following an older `?v=N` one-liner and a
run looks like stale code, bump the number (it must match `VERSION` and
`$ScriptVersion`) or bypass the cache with
`?nocache=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`.

---

## Execution policy

`Restricted` is the Windows default and blocks `.ps1` files on disk; the
`irm | iex` form runs from memory, so it is unaffected. If you download the
script instead, or want it to stick for your user account:

```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope CurrentUser   # persistent
Set-ExecutionPolicy -ExecutionPolicy Undefined  -Scope CurrentUser   # revert
```

---

## Updating

Two install shapes, updated differently:

- **One-liner** (`irm | iex` / the scriptblock form) — nothing is stored locally, so
  every run already uses the latest `main`. `$PSScriptRoot` is empty in this mode, so the
  installer syncs the file set from `MANIFEST.json` into `%LOCALAPPDATA%\hermes\easy-hermes`
  and runs the selector from there.
- **Clone** — the checkout next to `hermes-setup.ps1` is authoritative. `-Yolo` (or
  `-Update`) fast-forwards it with `git fetch` + `merge --ff-only`; a dirty tree or a
  missing upstream is skipped with a warning, never overwritten. The pulled commits are
  printed.

Auto-update is **on for `-Yolo`, off otherwise**. Turn it off permanently with
`-NoUpdate` (writes a marker file) or `EASY_HERMES_NO_UPDATE=1`; delete
`%LOCALAPPDATA%\hermes\easy-hermes\NOUPDATE` or pass `-Update` to re-enable for one run.

`EASY_HERMES_RAW_BASE` overrides the download root, so tests can point a run at a
local mirror instead of GitHub.

---

## What you get

After a `-Yolo` run:

1. **Hermes installed and authenticated**, with `hermes` on `PATH`.
2. **A default model chosen by live measurement**, not by a static list. The
   selector asks every reachable free model a real question, keeps the ones that
   answer cleanly, ranks them, and promotes only after repeated clean passes.
3. **A fallback chain.** If the default is rate-limited or dies, Hermes moves to
   the next verified model automatically.
4. **Nightly maintenance** (3 AM by default): update Hermes → re-run the
   selector → restart the gateway → run your own extra tasks.
5. **A morning report** at 7:20 AM from a Hermes cron job that reads the Windows
   Event Log and tells you whether last night's run succeeded, which model is
   now default, and what the chain looks like.
6. **A primer** printed at the end of the first run explaining `hermes`,
   `hermes chat`, `/model`, ACP and MCP.

Useful commands once installed:

```
hermes                 # interactive chat
hermes chat -q "..."   # one-shot answer
hermes model           # change provider/model
hermes doctor          # health check
```

---

## How it works

Two layers, deliberately separated.

```
pollers/poll_<provider>.py     CATALOG ONLY — list what each provider offers.
   │                           No probing, no config writes, never raises.
   ▼
providers.py                   REGISTRY — which providers exist, what env var
   │                           proves they are configured, whether to probe.
   ▼
selector/                      VALIDATION + PROMOTION
   ├─ probing.py               ask each candidate a real question
   ├─ probe_cache.py           remember the answer (TTL by failure class)
   ├─ ranking.py               normalise + score candidates
   ├─ history.py               promotion history + the promotion gate
   ├─ configio.py              atomic, locked merge into config.yaml
   └─ runner.py                fan out per provider, orchestrate, print
   │
   ▼
config.yaml                    model.default + fallback_providers
```

`select-models.py` is a thin facade over `selector/` — it keeps the original CLI
and re-exports every public name, so scheduled tasks and imports are unchanged.

### What counts as a "clean pass"

A model is promoted on evidence, not on being listed. A clean pass means:

- it answered on the **first** reasoning effort tried,
- with **no** 429/5xx backoff retry,
- and **no** token-budget retry.

A model that only succeeds after a retry is recorded but excluded from the
fallback chain. This is why a working default does not silently become a broken
one after a provider gets flaky.

### The promotion gate

`model.default` is sticky by design. A challenger has to earn the slot:

| Knob | Default | Meaning |
|---|---|---|
| `PROMOTION_MARGIN` | 2 | consecutive clean passes required to be eligible |
| `HYSTERESIS_DAYS` | 7 | days newer a challenger must be to displace a healthy incumbent |
| `HISTORY_PRUNE_DAYS` | 30 | drop promotion history for models unseen this long |
| emergency promotion | on | if the incumbent **failed probing**, promote the best available even below the margin |

An incumbent that simply isn't in the free-model list this run is treated as
*out of scope* and is never displaced. Only a measured failure triggers an
emergency promotion. Disable it with `--no-emergency`.

On a virgin config there is no incumbent to protect, so `hermes-setup.ps1` and the
nightly task pass `--promotion-margin 1` for that first run only; every later run
uses the default of 2. Leaving `model.default` unset is treated as a failure,
because the runtime then silently substitutes the provider's **paid** model.

### Probe cache

Results live at `<hermes-home>/cache/free_model_probe_cache.json`, with a TTL
per failure class so a permanent 404 ages out slowly and a transient 429 is
retried quickly:

| Class | TTL |
|---|---|
| permanent (404/410/gone) | 30 days |
| transient (429/5xx/timeout/empty) | 1 day |
| standard (success/other) | 7 days |

**A cached entry masks a code change.** After editing headers or validation,
re-run with `--refresh` once.

### Ranking

Candidates are scored on a configurable weight matrix in `config.yaml`
(`model_ranking`), default:

```yaml
weights:
  release_date: 0.40        # newer is better
  context_window: 0.30      # bigger is better
  provider_priority: 0.30   # per-provider trust
  reasoning: 0.0            # 1.0 if the probe produced reasoning tokens
  response_time: 0.0        # lower is better
  boost: 0.0
```

Every dimension is min-max normalised across the current candidate pool.
Boosted models get a sort bonus **and** must re-prove they work right now — a
cached pass never keeps a dead boosted model in the chain.

---

## Providers

Eight providers are registered. "Probed" means the selector sends a real chat
completion; "listed" means it only reads the catalog and marks entries
unverified (unverified models never enter the fallback chain).

| Provider | Slug | Env (any-of) | Default | Probed |
|---|---|---|---|---|
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` (optional) | yes | yes |
| NVIDIA NIM | `nvidia` | `NVIDIA_API_KEY`, `NGC_API_KEY` | yes | yes |
| Google AI Studio (Gemini) | `gemini` | `GOOGLE_API_KEY`, `GEMINI_API_KEY` | yes | yes |
| Ollama Cloud | `ollama-cloud` | `OLLAMA_API_KEY`, `OLLAMA_CLOUD_API_KEY` (optional) | yes | yes |
| Novita AI | `novita` | `NOVITA_API_KEY`, `NOVITA_KEY`, `NOVITA_AI_API_KEY` | yes | yes |
| Nous Portal | `nous` | `NOUS_API_KEY`, else Hermes auth store | yes | yes |
| Local Ollama | `ollama-local` | none (daemon) | **no** — opt in with `--prefer-local` | yes |
| Cloudflare Workers AI | `cloudflare` | `CLOUDFLARE_API_TOKEN` + account id | **no** — opt in | no (account-scoped) |

A provider is "configured" when at least one of its `env_any` keys is set, or
when it needs no key at all. Missing keys are reported at the end of a run
rather than failing it.

Provider quirks worth knowing:

- **Nous** reads its access token from Hermes' own credential pool
  (`<hermes-home>/auth.json`). An **expired** token is detected and reported with
  the recovery command; Nous probing is skipped rather than reported as stale
  cache. A rate-limit cooldown does *not* skip probing, because the cache still
  holds real pass/fail data. Fix expiry with `hermes auth refresh nous`.
- **Gemini** keys now start with `AQ.` and are sent as an `x-goog-api-key`
  header, never in a URL.
- **OpenRouter** chat completions require `HTTP-Referer` + `X-Title` headers, or
  a valid key returns 401. Probing is capped at 3 models per run (override with
  `--openrouter-top N`, `0` = no cap) to protect the free daily quota.
- **NVIDIA NIM** models are account-gated: a 404 means "not enabled for your
  account", not "bad key". Agreements are accepted on build.nvidia.com.
- **Novita** free Sandbox credit is one-time and may not cover LLM calls;
  `NOT_ENOUGH_BALANCE` means exhausted or expired credit.
- **Ollama Cloud** has no documented free-tier field; the poller treats listed
  models as free subject to quotas.
- **Cloudflare Workers AI** is account-scoped with no fixed probe URL, so it is
  listed as best-effort and disabled by default.

### Catalog pollers

The pollers are small, standalone scripts that **list** (never probe) provider
model catalogs. They never rewrite `config.yaml` and never raise. They live in
`pollers/`, so run them from the repo root (or the install dir):

```bash
python -m pollers.poll_openrouter --json      # out/openrouter.json
python -m pollers.poll_nous --json            # out/nous.json
python -m pollers.poll_novita  --json          # out/novita.json
python -m pollers.poll_gemini   --json         # out/gemini.json
python -m pollers.poll_nvidia   --json         # out/nvidia.json
python -m pollers.poll_ollama_cloud --json     # out/ollama-cloud.json
python -m pollers.poll_cloudflare --json       # out/cloudflare.json
```

> Do **not** invoke them as `python pollers/poll_openrouter.py` or from inside
> `pollers/` — they import repo-root modules (`safe_url`, `atomic_io`) and only
> add their own directory to `sys.path`, so both forms fail with
> `ModuleNotFoundError: No module named 'safe_url'`.

Each poller takes `--out DIR` / `--output DIR` and refuses a path containing
`..`. `--json` writes the normalized list; without it a table is printed.

---

## Configuration

### API keys

Keys live in the **Windows user environment** (`HKEY_CURRENT_USER\Environment`),
not in a shell session and not in `config.yaml`. Set them once with the helper,
which prompts securely and never echoes a value:

```powershell
.\Set-EnvKeys.ps1
```

Open a new terminal afterwards. To verify:

```powershell
[Environment]::GetEnvironmentVariable('NVIDIA_API_KEY','User')
```

`.env.example` lists every accepted name and alias. Where to get each key:

- **Nous Portal** (recommended; 300+ models, OAuth or API key):
  https://portal.nousresearch.com — create an inference API key
  (`sk-nous-...`). Env `NOUS_API_KEY`, or let Hermes' own OAuth pool supply it.
- **OpenRouter** (optional; raises rate limits; the free listing works keyless):
  https://openrouter.ai/settings/keys. Env `OPENROUTER_API_KEY`.
- **NVIDIA NIM** (required to probe): https://org.ngc.nvidia.com/setup/api-key —
  select at least "AI Foundation Models and Endpoints" / "NGC Catalog"; legacy
  keys are not supported. Env `NVIDIA_API_KEY` or `NGC_API_KEY`.
- **Google AI Studio / Gemini** (required): https://aistudio.google.com/apikey.
  New keys are auth keys and start with `AQ.`. Env `GOOGLE_API_KEY` or
  `GEMINI_API_KEY`.
- **Ollama Cloud** (optional; public tags work without a key):
  https://ollama.com/settings/keys. Env `OLLAMA_API_KEY` or `OLLAMA_CLOUD_API_KEY`.
- **Novita AI** (required): https://novita.ai/settings. Env `NOVITA_API_KEY`
  (aliases `NOVITA_KEY`, `NOVITA_AI_API_KEY`).
- **Cloudflare Workers AI** (optional, opt-in): dashboard → AI → Workers AI →
  Use REST API. Token scoped to Workers AI read/run, plus the account id. Env
  `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`.

### Files the selector owns

| Path | What |
|---|---|
| `<hermes-home>/config.yaml` | `model.default`, `fallback_providers`, `model_ranking` |
| `<hermes-home>/config.yaml.bak` | backup written before every config change |
| `<hermes-home>/cache/free_model_probe_cache.json` | probe results |
| `<hermes-home>/cache/promotion_history.json` | per-model pass streaks the gate reads |
| `<hermes-home>/cache/banned_models.json` | models excluded from candidates |
| `<hermes-home>/cache/boosted_models.json` | models sorted to the top |
| `<hermes-home>/cache/reasoning_caps.json` | advertised reasoning efforts per model |
| `<hermes-home>/cache/ollama_model_cache.json` | local Ollama probe results |

`<hermes-home>` defaults to `%LOCALAPPDATA%\hermes`. Override with
`--hermes-home` or `HERMES_HOME`.

### Banning and boosting

```bash
python select-models.py --ban anthropic/claude-3.5-haiku   # never consider
python select-models.py --unban anthropic/claude-3.5-haiku
python select-models.py --list-banned
python select-models.py --boost openai/gpt-4o-mini         # sort to top
python select-models.py --unboost openai/gpt-4o-mini
python select-models.py --list-boosted
```

Banned models are filtered before ranking *and* before probing. Boosted models
are re-probed live every run.

---

## Selector reference

Run from the repo root (or from the install dir under
`%LOCALAPPDATA%\hermes\easy-hermes`). Always preview first:

```bash
python select-models.py --dry-run
```

| Flag | Effect |
|---|---|
| `--dry-run` | probe and print the would-be config; write nothing |
| `--apply` | write config (the default unless `--dry-run` or a listing/inspection flag is used) |
| `--refresh` | ignore the probe cache |
| `--full` | probe every candidate instead of the top-N budget |
| `--top N` | override the probe budget (default 3) |
| `--providers a,b` | restrict to these slugs |
| `--prefer-local` | include `ollama-local` |
| `--capability CAP` | keep only models whose capabilities contain `CAP` |
| `--reasoning` | probe with `reasoning_effort=high`, report reasoning chars |
| `--model ID` | retest one model id across the selected providers (repeatable) |
| `--promotion-margin N` | override consecutive clean passes required |
| `--hysteresis-days N` | override the displacement window |
| `--no-emergency` | disable emergency promotion when the incumbent is dead |
| `--no-promote` | refresh the chain only; leave `model.default` alone |
| `--set-default ID` | force the default, bypassing the gate |
| `--ban / --unban / --list-banned` | manage the ban list |
| `--boost / --unboost / --list-boosted` | manage the boost list |
| `--set-weight K=V` | set a ranking weight (repeatable) |
| `--set-priority PROV=V` | set a provider priority (repeatable) |
| `--set-missing-release S` | score used when a model has no release date |
| `--show-ranking` | print the current ranking matrix |
| `--show-history` | print the promotion history table |
| `--show-response-times` | print recorded probe latency (ms) |
| `--list-only` | list candidates unverified; probe nothing |
| `--poll-cooldown SEC` | seconds between probes within a provider (default 1, 2 under `--full`) |
| `--openrouter-top N` | cap OpenRouter probes (default 3, `0` = no cap) |
| `--hermes-home PATH` | target Hermes home |
| `--today YYYY-MM-DD` | treat the run as happening on this date (testing/backfill) |
| `--json` | emit a machine-readable summary |

`--set-weight` / `--set-priority` / `--set-missing-release` are **ranking-matrix
edits only**: they write `model_ranking` and exit, and do not touch
`fallback_providers` or `model.default`. Run a normal pass afterwards to see the
effect on the chain.

Local Ollama has its own entry point (`hermes-ollama-selector.py`), kept separate
so a machine with no GPU is never asked about local models:

```bash
python hermes-ollama-selector.py --dry-run        # preview
python hermes-ollama-selector.py --apply          # merge into fallbacks
python hermes-ollama-selector.py --apply --set-default
python hermes-ollama-selector.py --apply --replace
python hermes-ollama-selector.py --base-url http://host:11434
```

`--no-apply` probes and caches without writing config (it is the default when
`--apply` is absent). Every model a local Ollama serves is free and private, so
there is no promotion gate — a model that answers the probe cleanly is admitted.
The probe still matters, because a pulled model can be broken (bad quant, missing
template, OOM on load).

---

## Scheduled tasks and nightly maintenance

`hermes-setup.ps1` registers exactly two things, both under the `\Hermes\` task
folder:

| Task | Script | When |
|---|---|---|
| `Nightly Restart` | `Set-NightlyTask.ps1` → `NightlyRestartTask.ps1` | 3:00 AM daily |
| `NightlyRestart monitor` | `Set-HermesCronMonitor.ps1` (Hermes cron job) | 7:20 AM daily |

There is deliberately **one** owner of the model chain. A second selector task
would probe the free tier twice a night and feed the promotion gate duplicate
observations, so no standalone selector task is registered.

### The four nightly steps

`NightlyRestartTask.ps1` runs, in order:

1. `hermes update --no-gateway-restart --yes`
2. `select-models.py --hermes-home <home> --full`
3. `hermes gateway restart`
4. your extra tasks

**Step 3 runs after step 2 on purpose.** The gateway must restart *after*
`config.yaml` is rewritten, or the new default does not take effect until the
following night. Everything is logged to the Windows Event Log under source
`NightlyRestartTask`.

### Extra tasks (step 4)

Host-specific extras come from a **gitignored** `extra-tasks.ps1` sitting next
to `NightlyRestartTask.ps1`, so a push never carries another machine's paths.
Changing that file needs no task re-registration.

```powershell
# extra-tasks.ps1
@(
    @{ Name = 'bedrock updater'; Command = { & 'D:\tools\bedrockupdater.ps1' -Restart } }
    @{ Name = 'log cleanup';    Path = 'D:\tools\cleanup.ps1'; Arguments = @('-KeepDays', 7) }
)
```

The file is dot-sourced, so an entry's scriptblock can close over variables the
file defines. It accepts an array of entries, a name → entry map, or a single
entry. A missing literal script path is skipped with a log line; a task that
throws is logged and the run continues. Override the path with
`-ExtraTasksConfig`, or pass one-off scripts with `-ExtraTasks`. Schema and more
examples: [`docs/nightly-restart.md`](docs/nightly-restart.md).

### The monitor

The 7:20 AM Hermes cron job reads the day's Event Log entries, treats "no
events" as a failure condition worth investigating, whitelists known benign
noise, checks the pinned-vs-installed cua-driver version, and reports the current
model chain. Its prompt lives at `cron/nightly-monitor-prompt.txt`.

### Registering manually

```powershell
.\Set-NightlyTask.ps1                       # 3:00 AM
.\Set-NightlyTask.ps1 -RunAt '3:30AM'
.\Set-NightlyTask.ps1 -HermesHome 'C:\path\to\hermes'
.\Set-HermesCronMonitor.ps1                # 7:20 AM, local delivery
.\Set-HermesCronMonitor.ps1 -Deliver 'discord:1234567890'
```

> **Interactive principal.** Both tasks are registered to run only when the
> owning user is logged on. On a machine that is normally logged off at night,
> re-register with `-LogonType S4U` and a stored credential, or the 3 AM run
> simply never fires. `Get-ScheduledTask` is authoritative — the Task Scheduler
> GUI can show stale entries.

---

## Repository layout

```
hermes-setup.ps1              installer + selector/scheduler entry point
select-models.py              CLI facade over selector/
selector/                     the selector implementation
  paths.py                    runtime path state, constants, cache I/O
  lists.py                    banned / boosted lists
  probe_cache.py              probe cache with per-class TTL
  history.py                  promotion history + the gate
  ranking.py                  release dates, capabilities, ranking matrix
  probing.py                  provider fetch + chat probes
  configio.py                 config.yaml load + locked merge write
  runner.py                   per-provider pipeline + main()
providers.py                  provider registry (single source of truth)
pollers/                      one catalog poller per provider + shared poll_base
hermes-ollama-selector.py     local Ollama probe + merge (opt-in)
release_dates.py              release-date resolution (OpenRouter/HF/hardcoded)
safe_url.py                   fail-closed origin validation for paginated URLs
atomic_io.py                  atomic writes + cross-process file locks
chain_delta.py                chain diffs for weight tuning
Set-EnvKeys.ps1               secure provider-key prompts → user env
Set-NightlyTask.ps1           registers the nightly task
NightlyRestartTask.ps1        the four-step nightly job
Set-HermesCronMonitor.ps1     registers the morning Hermes cron monitor
cron/                         the monitor's prompt
tools/                        gen_manifest.py, check_repo_health.py
tests/                        91 pure tests, no network
docs/                         design and operations notes
MANIFEST.json / VERSION       the file set the installer syncs
```

Further reading:

- [`docs/installer-safety.md`](docs/installer-safety.md) — blast radius per step
- [`docs/select-models.md`](docs/select-models.md) — selector internals and flags
- [`docs/nightly-restart.md`](docs/nightly-restart.md) — extra-tasks schema
- [`docs/ollama-selector.md`](docs/ollama-selector.md) — local Ollama
- [`docs/capability-tags.md`](docs/capability-tags.md) — capability-aware selection

---

## Development

```bash
python -m pytest -q                       # 91 tests, no network
python tools/check_repo_health.py         # pre-push gate
python tools/gen_manifest.py              # regenerate MANIFEST.json + VERSION
```

`check_repo_health.py` asserts every `.ps1` is pure ASCII, that `MANIFEST.json`
covers every provider registry module plus the spec-loaded
`hermes-ollama-selector.py`, that any `?v=N` in the README one-liners matches
`VERSION` (the query is optional; a stale one is not), and that `$ScriptVersion`
matches `VERSION`.

For any `.ps1` change, also verify it parses under **Windows PowerShell 5.1**,
not just `pwsh`:

```powershell
$err = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$err)
$err.Count
```

Conventions that are not obvious and will bite you:

- **`.ps1` files are CRLF and ASCII-only.** PowerShell 5.1 decodes BOM-less
  UTF-8 as CP1252, so a box-drawing character silently corrupts the script.
- **`param()` must be the first statement** — even `$ErrorActionPreference`
  before it breaks parameter binding.
- **Generated artifacts are not committed** (`__pycache__/`, `out/`, `reports/`).
- **Tests must patch `selector.paths.*`, never the facade attribute.** Patching
  the facade silently no-ops for spec-loaded modules, and a test then writes to
  the real `config.yaml`.
- **A new provider** is a poller (`pollers/poll_template.py` is the starter),
  one line in `providers.py`, tests in `tests/`, and an entry in
  `.env.example`. No selector rewrite.
- **After deleting a module**, grep the whole tree — comments and docs included —
  and regenerate `MANIFEST.json` in the same commit.

---

## Troubleshooting

**"No models passed probing -- config untouched."**
Nothing answered cleanly. Usually a key problem, not a selector problem. Run
`python select-models.py --dry-run --full --refresh` and read the per-provider
lines: `key validation failed`, `401`, `403`, `404` (account-gated) and
`429 daily quota` each mean something different.

**"Optional provider keys not set"** at the end of a run.
Expected on a fresh machine. Run `Set-EnvKeys.ps1`, open a new terminal, then
re-run with `--refresh`.

**The default model never changes.**
By design — it needs 2 consecutive clean passes and a challenger 7 days newer.
`python select-models.py --show-history` shows the streak per model;
`--set-default ID` forces it.

**A model that used to pass now fails every run.**
Check the probe note, not the model: `[cache]` prefixes mean the selector is
replaying a cached result, not testing. `--refresh` forces a real probe.

**Nous probes all fail but other providers are fine.**
The pooled Nous token is expired. The selector prints an explicit line and
skips Nous rather than reporting stale cache as live. Fix with
`hermes auth refresh nous` or `hermes setup --portal`.

**The nightly task ran but nothing changed.**
Interactive-principal tasks only fire when someone is logged on, and the Task
Scheduler GUI can show stale entries. `Get-ScheduledTask -TaskPath '\Hermes\'`
is authoritative.

**A fix I pushed is not live for a tester.**
The recommended one-liner uses a bare raw URL, so it always serves current
`main` — but any `?v=N` variant is cached per full URL and will keep serving old
bytes. Bump the `?v=N` (it must match `VERSION`) or verify with a cache-busting
query: `?nocache=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`.

**`python` resolves to the Windows Store stub.**
The selector needs a real interpreter with PyYAML. `hermes-setup.ps1` derives the
venv Python from `HERMES_HOME` and the `hermes` on `PATH`; if none works it
lists what it tried. `hermes doctor` is the first check.

---

## Safety and rollback

What a re-run can do, worst case per step:

| Step | Runs when | Effect |
|---|---|---|
| auto-update | `-Yolo` / `-Update` | clone: `git fetch` + `merge --ff-only`; one-liner: re-syncs `MANIFEST.json` files. Dirty trees and failed fetches skip with a warning — your local work is never rewritten |
| install | default | runs the official installer, which **upgrades an existing Hermes checkout** (`git reset --hard` after stashing) |
| auth | always | no-op when already authenticated |
| selector | `-Yolo`, `-SelectorOnly`, or yes | **rewrites `fallback_providers`**; may move `model.default` |
| verify | always | `hermes doctor`, read-only |

Safe re-run order:

```bash
cp "$LOCALAPPDATA/hermes/config.yaml" "$LOCALAPPDATA/hermes/config.yaml.bak"
python select-models.py --dry-run          # prove nothing is written
python select-models.py                    # apply
```

Config writes are atomic (temp file + `os.replace`) and lock-protected across
processes, so a concurrent selector run cannot interleave a half-written
`config.yaml`. A backup of the previous config is written as `config.yaml.bak`
before every change.

Auto-update opt-out is sticky:

```powershell
# disable permanently for this machine
iex "& { $(irm 'https://raw.githubusercontent.com/H4ppyNZ/easy-hermes/main/hermes-setup.ps1') } -NoUpdate"
```

That writes `%LOCALAPPDATA%\hermes\easy-hermes\NOUPDATE`. `EASY_HERMES_NO_UPDATE=1`
has the same effect and cannot be overridden; `-Update` overrides the marker for
one run.

Rollback for the whole repo is a revert, since scheduled tasks point at repo
files:

```bash
git revert HEAD && git push origin main
```

---

## Status and limitations

Phase 3 is complete: `select-models.py` is the sole selector, wired into
`hermes-setup.ps1` and the nightly task; `poll_nous.py` ships; the old
`hermes-model-selector.py` was deleted (see git history). `model.default` and
`fallback_providers` are owned by `select-models.py`.

- **Windows only.** The `.ps1` layer is not portable; the selector itself is.
- **Free tiers rotate.** A model that passes tonight can be gone next month. That
  is the reason the selector measures instead of trusting a list.
- **Free-tier data is sometimes account-scoped** (Cloudflare, NVIDIA). Those
  providers are opt-in and marked unverified rather than pretended to work.
- **Release dates are approximate** when a provider exposes none; the year is
  inferred from the model id and flagged as such in ranking.
- **Interactive-principal tasks need a logged-on user.** See above.
- **Open items:** capability ranking weight; performance/cost/observability/ux/
  accessibility/compliance review axes (not yet covered). Tracked in `TODO.md`.
- **No license file is currently included** in this repository.
