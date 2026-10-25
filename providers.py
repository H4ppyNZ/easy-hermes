"""Provider registry for free-model selection.

Single source of truth for which providers exist, how to detect that they are
configured, and whether they should be probed. Pollers (poll_<slug>.py) are the
catalog layer; this registry drives the selector.

Detection rule: a provider is "configured" when at least one key in `env_any`
is set (or `env_any` is empty, meaning always available, e.g. public
endpoints / local daemons). `default_enabled` controls whether a provider is
included unless the user opts in (e.g. local Ollama, since most users lack
hardware).
"""

from __future__ import annotations

PROVIDERS = {
    "openrouter": {
        "slug": "openrouter",
        "display": "OpenRouter",
        "module": "poll_openrouter",
        "env_any": [],                       # public listing; key optional
        "env_optional": ["OPENROUTER_API_KEY"],
        "requires_key": False,
        "probe": True,
        "probe_kind": "openai",
        "probe_base_url": "https://openrouter.ai/api/v1",
        "default_enabled": True,
        "probe_top_first_run": 3,
        "probe_top_nightly": 12,
    },
    "nvidia": {
        "slug": "nvidia",
        "display": "NVIDIA NIM",
        "module": "poll_nvidia",
        "env_any": ["NVIDIA_API_KEY", "NGC_API_KEY"],
        "env_optional": [],
        "requires_key": True,
        "probe": True,
        "probe_kind": "openai",
        "probe_base_url": "https://integrate.api.nvidia.com/v1",
        "default_enabled": True,
        "probe_top_first_run": 3,
        "probe_top_nightly": 12,
    },
    "gemini": {
        "slug": "gemini",
        "display": "Google AI Studio (Gemini)",
        "module": "poll_gemini",
        "env_any": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
        "env_optional": [],
        "requires_key": True,
        "probe": True,
        "probe_kind": "gemini",
        "probe_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_enabled": True,
        "probe_top_first_run": 3,
        "probe_top_nightly": 12,
    },
    "ollama-cloud": {
        "slug": "ollama-cloud",
        "display": "Ollama Cloud",
        "module": "poll_ollama_cloud",
        "env_any": [],                       # public tags endpoint works keyless
        "env_optional": ["OLLAMA_API_KEY", "OLLAMA_CLOUD_API_KEY"],
        "requires_key": False,
        "probe": False,
        "probe_kind": "openai",
        "probe_base_url": "https://api.ollama.com/v1",
        "default_enabled": True,
        "probe_top_first_run": 3,
        "probe_top_nightly": 12,
    },
    "novita": {
        "slug": "novita",
        "display": "Novita AI",
        "module": "poll_novita",
        "env_any": ["NOVITA_API_KEY", "NOVITA_KEY", "NOVITA_AI_API_KEY"],
        "env_optional": [],
        "requires_key": True,
        "probe": True,
        "probe_kind": "openai",
        "probe_base_url": "https://api.novita.ai/v3/openai",
        "default_enabled": True,
        "probe_top_first_run": 3,
        "probe_top_nightly": 12,
    },
    "ollama-local": {
        "slug": "ollama-local",
        "display": "Local Ollama",
        "module": "hermes-ollama-selector",
        "env_any": [],                       # daemon, no key
        "env_optional": [],
        "requires_key": False,
        "probe": True,
        "probe_kind": "ollama-local",
        "probe_base_url": None,
        "default_enabled": False,            # opt-in: most users lack hardware
        "probe_top_first_run": 3,
        "probe_top_nightly": 12,
    },
    # Nous Portal is handled by hermes-model-selector.py (cache-based) for now.
    # A poll_nous.py will be added in Phase 4 to match this contract.
}

# Providers that are always attempted unless disabled by the user.
ALWAYS_TRY = [p for p, cfg in PROVIDERS.items() if cfg["default_enabled"]]


def configured_providers(user_list=None, prefer_local=False):
    """Return list of provider slugs that are configured & enabled."""
    import os
    out = []
    for slug, cfg in PROVIDERS.items():
        if user_list is not None and slug not in user_list:
            continue
        if not cfg["default_enabled"] and not prefer_local:
            # ollama-local is opt-in
            continue
        # env check
        env_any = cfg.get("env_any", [])
        if env_any and not any(os.environ.get(k) for k in env_any):
            continue
        out.append(slug)
    return out


def get_api_key(cfg):
    import os
    for k in cfg.get("env_any", []) + cfg.get("env_optional", []):
        v = os.environ.get(k)
        if v and str(v).strip():
            return str(v).strip(), k
    # fallback: any optional
    for k in cfg.get("env_optional", []):
        v = os.environ.get(k)
        if v and str(v).strip():
            return str(v).strip(), k
    return None, None
