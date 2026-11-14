"""selector.configio — extracted from select-models.py

Split out of the original monolithic select-models.py.  Function bodies are
moved verbatim; only module-level path state is routed through
``selector.paths`` so it stays mutable at runtime.
"""
from __future__ import annotations

from selector import paths
from selector.paths import DEFAULT_PROVIDER_PRIORITY, DEFAULT_RANKING_WEIGHTS
from selector.history import _model_id
import sys


def load_config():
    import yaml
    if not paths.CONFIG_PATH.exists():
        return {}, yaml
    try:
        with open(paths.CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}), yaml
    except Exception:
        return {}, yaml


def write_config(passing, yaml_mod, chosen=None, current_default=None, promote=True, probed_all=None):
    if not passing and not chosen and not current_default:
        return
    import atomic_io
    lock_path = str(paths.CONFIG_PATH) + ".lock"
    with atomic_io.file_lock(lock_path):
        try:
            if paths.CONFIG_PATH.exists():
                bak = paths.CONFIG_PATH.with_suffix(".yaml.bak")
                atomic_io.atomic_write_text(bak, paths.CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        cfg, _ = load_config()
        if not isinstance(cfg, dict):
            cfg = {}
        if "model_ranking" not in cfg:
            cfg["model_ranking"] = {
                "weights": dict(DEFAULT_RANKING_WEIGHTS),
                "provider_priority": dict(DEFAULT_PROVIDER_PRIORITY),
                "missing_release_date": 0.0,
            }
        # effective default: promoted chosen, or kept incumbent, or first passing
        effective_default_id = None
        default_model = None
        if promote and chosen is not None:
            default_model = chosen
            effective_default_id = _model_id(chosen)
        elif promote and current_default:
            effective_default_id = current_default
        elif promote and passing:
            default_model = passing[0]
            effective_default_id = _model_id(passing[0])

        if default_model is not None:
            best = default_model
            best_provider = best.get("_provider", "custom")
            hermes_provider = best_provider if best_provider in ("nous", "openrouter", "custom", "ollama-cloud", "nvidia", "gemini", "novita") else "custom"
            mcfg = cfg.setdefault("model", {})
            if not isinstance(mcfg, dict):
                mcfg = {"default": mcfg} if isinstance(mcfg, str) else {}
                cfg["model"] = mcfg
            mcfg["default"] = best["id"]
            mcfg["provider"] = hermes_provider
            if hermes_provider == "custom":
                # Preserve the originating provider's identity (Hermes stores these
                # as `custom`, which loses it in logs/config/monitor output). Consumers
                # (e.g. the NightlyRestart monitor) read `provider_display`.
                mcfg["provider_display"] = best_provider
                if best.get("_base_url"):
                    mcfg["base_url"] = best["_base_url"]
            else:
                mcfg.pop("base_url", None)
                mcfg.pop("provider_display", None)

        # fallback chain: all passing except the effective default
        fb = []
        for m in (passing or []):
            if effective_default_id and _model_id(m) == effective_default_id:
                continue
            prov = m.get("_provider", "custom")
            hp = prov if prov in ("nous", "openrouter", "custom", "ollama-cloud", "nvidia", "gemini", "novita") else "custom"
            entry = {"provider": hp, "model": m["id"]}
            if hp == "custom":
                entry["provider_display"] = prov
                if m.get("_base_url"):
                    entry["base_url"] = m["_base_url"]
            fb.append(entry)

        existing = cfg.get("fallback_providers")
        if not isinstance(existing, list):
            existing = []
        # Keep only fallback entries that are still passing; a model that stops
        # passing (e.g. is no longer free / fails its probe) drops out of the
        # chain instead of lingering forever. Models from providers not probed
        # this run (e.g. --providers nous) are preserved, not pruned.
        passing_ids = {_model_id(m) for m in (passing or [])}
        probed_ids = {str(m.get("model")) for m in (probed_all or []) if isinstance(m, dict)}
        existing = [e for e in existing if isinstance(e, dict) and (
            str(e.get("model")) not in probed_ids or str(e.get("model")) in passing_ids)]
        # Migrate legacy custom entries to carry provider_display (identity is
        # otherwise lost because Hermes stores them as provider: custom).
        try:
            import providers as _provmod
            _slugs = list(getattr(_provmod, "PROVIDERS", {}).keys())
        except Exception:
            _slugs = []
        for e in existing:
            if not isinstance(e, dict) or e.get("provider") != "custom":
                continue
            if "provider_display" in e and e.get("provider_display"):
                continue
            derived = None
            model = str(e.get("model") or "")
            if model.startswith("@cf/"):
                derived = "cloudflare"
            bu = e.get("base_url")
            if derived is None and isinstance(bu, str):
                low = bu.lower()
                for slug in _slugs:
                    if slug and slug in low:
                        derived = slug
                        break
                if derived is None:
                    # fall back to first hostname label
                    try:
                        from urllib.parse import urlparse
                        host = urlparse(bu).hostname or ""
                        if host:
                            derived = host.split(".")[0]
                            if derived == "api":
                                parts = host.split(".")
                                if len(parts) > 1:
                                    derived = parts[1]
                    except Exception:
                        derived = None
            if derived:
                e["provider_display"] = derived
        existing_keys = {(str(e.get("provider")), str(e.get("model"))) for e in existing if isinstance(e, dict)}
        new_fb = [e for e in fb if (str(e.get("provider")), str(e.get("model"))) not in existing_keys]
        cfg["fallback_providers"] = list(existing) + new_fb

        try:
            import atomic_io
            import io
            buf = io.StringIO()
            yaml_mod.safe_dump(cfg, buf, default_flow_style=False, sort_keys=False, allow_unicode=True)
            atomic_io.atomic_write_text(paths.CONFIG_PATH, buf.getvalue(), encoding="utf-8")
            print(f"Updated {paths.CONFIG_PATH}")
            if effective_default_id:
                print(f"  model.default -> {effective_default_id}")
        except Exception as e:
            print(f"ERROR writing config: {e}", file=sys.stderr)