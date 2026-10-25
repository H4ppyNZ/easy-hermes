# NVIDIA NIM free-tier model polling

`poll_nvidia.py` polls `GET https://integrate.api.nvidia.com/v1/models`
(OpenAI-compatible) using `Authorization: Bearer <key>`.

## Auth

Environment variables only:

- `NVIDIA_API_KEY` (preferred)
- `NGC_API_KEY` (fallback)

NIM requires authentication even for listing, so a missing key exits with
code 2.

## Free-tier note

NIM has no explicit free SKU distinction in the listing. All models returned
by `/v1/models` are treated as free-tier usable, subject to rate limits
(~40 RPM). Free-tier eligibility is quota-based, not a listing flag, so this
poller is best-effort/draft.

## Usage

```powershell
$env:NVIDIA_API_KEY = "<your key>"
python poll_nvidia.py
python poll_nvidia.py --json
```

## Normalized output

Each model: `{id, display_name, context_window, rate_limits, notes}`.
`context_window` is null unless present in the listing. `rate_limits` comes
from response headers when available, otherwise the documented 40 RPM free
tier.

## Caveats

- Account + API key required (card may not be required).
- ~40 RPM free tier.
- Geographic/region restrictions may apply.
- Best-effort: quota-based eligibility is not a listing flag.
