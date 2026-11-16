# Fix two easy-hermes defects found in the 2026-11-16 review.
#
# 1. NightlyRestartTask.ps1 carried U+2500 box-drawing characters in two
#    comments (381 non-ASCII bytes). The repo's own rule is "keep .ps1 source
#    ASCII-only": iex / Get-Content -Encoding default decodes a BOM-less file
#    as CP1252, and a mangled multi-byte char can terminate a string literal.
#    Already fixed by hand in the working tree; keep it that way.
# 2. README.md advertised ?v=4 one-liners while $ScriptVersion / VERSION /
#    MANIFEST.json all say 5. Bump to ?v=5 so testers stop hitting a cached
#    older installer. Also correct the stale "69 tests" count -> 81.

import hashlib
import os
import re
import sys

REPO = r"C:\Users\mattw\Documents\repos\easy-hermes"


def read_bytes(rel):
    with open(os.path.join(REPO, rel), "rb") as fh:
        return fh.read()


def write_bytes(rel, data):
    with open(os.path.join(REPO, rel), "wb") as fh:
        fh.write(data)


def crlf_of(data):
    return b"\r\n" if data.count(b"\r\n") else b"\n"


def check_ps1_ascii():
    offenders = []
    for root, _dirs, files in os.walk(REPO):
        if ".git" in root or "__pycache__" in root:
            continue
        for name in files:
            if not name.endswith(".ps1"):
                continue
            path = os.path.join(root, name)
            raw = open(path, "rb").read()
            non_ascii = [b for b in raw if b > 127]
            if non_ascii:
                offenders.append((os.path.relpath(path, REPO), len(non_ascii)))
    return offenders


def check_manifest_covers_registry():
    """providers.py names pollers as strings; they must all be in MANIFEST.json."""
    import ast
    import json

    src = open(os.path.join(REPO, "providers.py"), encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "module"
                        and isinstance(val, ast.Constant) and isinstance(val.value, str)):
                    mods.add(val.value)
    manifest = json.load(open(os.path.join(REPO, "MANIFEST.json"), encoding="utf-8"))
    listed = {f for group in ("python", "powershell", "data") for f in manifest[group]}
    missing = sorted(m for m in mods if m.replace(".", "/") + ".py" not in listed)
    extra_paths = ["hermes-ollama-selector.py"]  # loaded via spec_from_file_location
    missing += [p for p in extra_paths if p not in listed]
    return missing


def main():
    failures = []

    offenders = check_ps1_ascii()
    if offenders:
        failures.append(f"non-ASCII bytes in .ps1: {offenders}")
    else:
        print("[ok] every .ps1 is pure ASCII")

    missing = check_manifest_covers_registry()
    if missing:
        failures.append(f"registry modules / computed-path files missing from MANIFEST.json: {missing}")
    else:
        print("[ok] MANIFEST.json covers every providers.py module + hermes-ollama-selector.py")

    readme = read_bytes("README.md").decode("utf-8")
    if "?v=4" in readme:
        failures.append("README.md still advertises ?v=4 one-liners")
    elif "?v=5" not in readme:
        failures.append("README.md has no ?v=5 one-liner")
    else:
        print("[ok] README.md one-liners use ?v=5")

    version = read_bytes("VERSION").decode().strip()
    script = read_bytes("hermes-setup.ps1").decode("utf-8")
    m = re.search(r"\$ScriptVersion\s*=\s*'(\d+)'", script)
    if not m or m.group(1) != version:
        failures.append(f"$ScriptVersion ({m.group(1) if m else '?'}) != VERSION ({version})")
    else:
        print(f"[ok] $ScriptVersion matches VERSION ({version})")

    import subprocess
    for pattern, label in ((r"\b69 tests\b", "stale '69 tests'"),
                           (r"\b69, pure", "stale '69,' test count")):
        for rel in ("README.md", "AGENTS.md", "docs/select-models.md", "docs/selector-intention.md"):
            text = read_bytes(rel).decode("utf-8")
            if re.search(pattern, text):
                failures.append(f"{rel}: {label}")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
