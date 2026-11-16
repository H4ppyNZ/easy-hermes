#!/usr/bin/env python3
"""Regenerate MANIFEST.json -- the file set hermes-setup.ps1 syncs for the
one-liner install path and uses as the update set for clone installs.

Why this exists: the selector is a package (select-models.py + selector/ +
pollers/), so "download one .py file" no longer works. The installer needs an
explicit, machine-checkable list of what must land on disk, and tests assert
the list actually covers the selector's transitive local imports.

Three things are not reachable by a plain import walk from the entry point, and
each one silently produced a selector that could only poll local Ollama:

  * provider pollers -- providers.py holds their module names as data and
    selector/probing.py imports them with importlib
  * the pollers' own imports -- safe_url.py, and poll_base.py (imported bare,
    because the pollers put their own directory on sys.path)
  * files loaded by computed path -- pollers/poll_ollama_local.py execs
    <repo>/hermes-ollama-selector.py via spec_from_file_location

Anything still unplaceable is reported and fails the run rather than dropped.

Run from the repo root:  python tools/gen_manifest.py
"""
from __future__ import annotations

import ast
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}

# Files the installer / scheduler need that are NOT reachable by walking
# imports from select-models.py.
POWERSHELL = [
    "hermes-setup.ps1",
    "Set-NightlyTask.ps1",
    "NightlyRestartTask.ps1",
    "Set-HermesCronMonitor.ps1",
    "Set-ModelSelectorTask.ps1",
    "Set-EnvKeys.ps1",
]
DATA = ["cron/nightly-monitor-prompt.txt"]
# Files loaded at import time by computed path rather than by an import
# statement, so no AST walk can see them. pollers/poll_ollama_local.py does
# spec_from_file_location(<repo>/hermes-ollama-selector.py) at module level.
EXTRA = ["hermes-ollama-selector.py"]
# Modules provided by the environment rather than shipped from this repo:
# third-party packages available to the Hermes venv, plus the Hermes install's
# own utils.py -- hermes-ollama-selector.py imports it for comment-preserving
# YAML writes and falls back to yaml.dump when it is absent.
EXTERNAL = {"requests", "yaml", "utils"}
PROVIDERS = "providers.py"
ENTRY = "select-models.py"


def local_imports(path: str) -> set[str]:
    """Repo-local modules imported by a file, as repo-relative paths."""
    out: set[str] = set()
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module)
            # 'from pkg import name' may mean pkg/name.py
            if node.module:
                out.add(f"{node.module}.{_name_of(node)}")
    return out


def _name_of(node: ast.ImportFrom) -> str:
    return node.names[0].name if node.names else ""


def resolve(mod: str, base: str = "") -> str | None:
    """Map a module name to a repo-relative path, or None if it is not a file.

    `base` is the importing file's directory relative to the repo root. The
    pollers do `sys.path.insert(0, os.path.dirname(__file__))` and then
    `import poll_base`, so a bare module name has to be looked for next to the
    file that imported it before it is looked for at the repo root.
    """
    parts = mod.split(".")
    if parts[0] in STDLIB:
        return None
    roots = [os.path.join(REPO, base)] if base else []
    roots.append(REPO)
    for root in roots:
        for cand in (os.path.join(*parts) + ".py",
                     os.path.join(*parts, "__init__.py")):
            path = os.path.join(root, cand)
            if os.path.exists(path):
                return os.path.relpath(path, REPO).replace(os.sep, "/")
    return None


def resolve_prefix(mod: str, base: str = "") -> str | None:
    """Longest resolvable prefix -- 'pkg.mod.fn' resolves to pkg/mod.py.

    `from poll_base import http_get_json` yields both 'poll_base' and
    'poll_base.http_get_json'; the second is a name inside the module, not a
    file, and must not be reported as missing.
    """
    parts = mod.split(".")
    while parts:
        found = resolve(".".join(parts), base)
        if found:
            return found
        parts.pop()
    return None


def closure(entry: str) -> tuple[list[str], list[str]]:
    """Transitive repo-local imports of `entry`, plus anything unresolvable."""
    seen: set[str] = set()
    todo = [entry]
    unresolved: set[str] = set()
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        base = os.path.dirname(rel)
        for mod in local_imports(os.path.join(REPO, rel)):
            if mod.split(".")[0] in STDLIB or mod.split(".")[0] in EXTERNAL:
                continue
            found = resolve_prefix(mod, base)
            if found is None:
                unresolved.add(mod)
            elif found not in seen:
                todo.append(found)
    return sorted(seen), sorted(unresolved)


def registry_modules() -> tuple[list[str], list[str]]:
    """Poller files named by the provider registry, as repo-relative paths.

    providers.py holds module names as data ("module": "pollers.poll_nvidia")
    and selector/probing.py imports them with importlib. An AST walk from the
    entry point therefore never sees them, which is how the first cut of this
    manifest shipped a selector that could only poll local Ollama. Returns
    (files, unresolved) so a dangling entry can be reported as an error rather
    than quietly dropped.
    """
    tree = ast.parse(open(os.path.join(REPO, PROVIDERS), encoding="utf-8").read())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, val in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "module"
                    and isinstance(val, ast.Constant) and isinstance(val.value, str)):
                mods.add(val.value)
    files, unresolved = [], []
    for mod in sorted(mods):
        found = resolve_prefix(mod)
        if found is None:
            unresolved.append(mod)
        elif found not in files:
            files.append(found)
    return files, sorted(unresolved)


def main() -> int:
    py, unresolved_imports = closure(ENTRY)
    registered, unresolved_registry = registry_modules()

    py = set(py) | set(EXTRA)
    # A registry entry names only the poller file. Its own dependencies have to
    # be walked too -- adding the poller without them just moves the failure
    # from "no module named pollers.poll_x" to "no module named safe_url".
    for rel in registered:
        more, more_unresolved = closure(rel)
        py |= set(more)
        unresolved_imports += more_unresolved
    py = sorted(py)

    missing = [f for f in py + POWERSHELL + DATA
               if not os.path.exists(os.path.join(REPO, f.replace("/", os.sep)))]
    if missing:
        print("manifest references missing files:", missing, file=sys.stderr)
        return 1
    if unresolved_registry:
        print("provider registry references modules that do not exist:",
              unresolved_registry, file=sys.stderr)
        return 1
    if unresolved_imports:
        print("imports that resolve to no file in the repo "
              "(add them to EXTRA if they are loaded dynamically):",
              sorted(set(unresolved_imports)), file=sys.stderr)
        return 1
    manifest = {
        "version": 5,
        "entry": ENTRY,
        "note": ("Files hermes-setup.ps1 syncs into the install dir for the "
                 "one-liner path, and uses as the update set for clone installs. "
                 "Regenerate with: python tools/gen_manifest.py"),
        "python": py,
        "powershell": POWERSHELL,
        "data": DATA,
    }
    path = os.path.join(REPO, "MANIFEST.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    with open(os.path.join(REPO, "VERSION"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{manifest['version']}\n")
    print(f"MANIFEST.json: {len(py)} python + {len(POWERSHELL)} powershell + {len(DATA)} data")
    for f in py:
        print("  py ", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
