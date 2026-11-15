#!/usr/bin/env python3
"""Regenerate MANIFEST.json -- the file set hermes-setup.ps1 syncs for the
one-liner install path and uses as the update set for clone installs.

Why this exists: the selector is a package (select-models.py + selector/ +
pollers/), so "download one .py file" no longer works. The installer needs an
explicit, machine-checkable list of what must land on disk, and tests assert
the list actually covers the selector's transitive local imports.

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


def resolve(mod: str) -> str | None:
    """Map a module name to a repo-relative file path, or None if external."""
    parts = mod.split(".")
    if parts[0] in STDLIB:
        return None
    # pkg.sub -> pkg/sub.py ; pkg -> pkg.py or pkg/__init__.py
    for cand in (os.path.join(*parts) + ".py", os.path.join(*parts, "__init__.py")):
        if os.path.exists(os.path.join(REPO, cand)):
            return cand.replace(os.sep, "/")
    return None


def closure(entry: str) -> list[str]:
    seen: set[str] = set()
    todo = [entry]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for mod in local_imports(os.path.join(REPO, rel)):
            r = resolve(mod)
            if r and r not in seen:
                todo.append(r)
    return sorted(seen)


def main() -> int:
    py = closure(ENTRY)
    missing = [f for f in py + POWERSHELL + DATA
               if not os.path.exists(os.path.join(REPO, f.replace("/", os.sep)))]
    if missing:
        print("manifest references missing files:", missing, file=sys.stderr)
        return 1
    manifest = {
        "version": 4,
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
