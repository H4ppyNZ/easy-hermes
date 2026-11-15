"""Regression tests for the installer's file distribution.

The selector is a package (select-models.py + selector/ + pollers/), so the
one-liner path can no longer get away with downloading a single .py file.
These tests prove MANIFEST.json is complete and that the one-liner path works
with $PSScriptRoot empty -- the two failure modes that broke the refactor.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_PS1 = os.path.join(REPO, "hermes-setup.ps1")
MANIFEST = os.path.join(REPO, "MANIFEST.json")


def _manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _all_files():
    m = _manifest()
    return [f for group in ("python", "powershell", "data") for f in m[group]]


def _copy_manifest_files(dest):
    for rel in _all_files():
        src = os.path.join(REPO, rel.replace("/", os.sep))
        dst = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy(src, dst)


def _python():
    """The same venv python the installer resolves."""
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes",
                        "hermes-agent", "venv", "Scripts", "python.exe")
    return cand if os.path.exists(cand) else sys.executable


def test_manifest_covers_every_local_import_of_the_entry_point():
    """Walking imports from select-models.py must stay inside the manifest."""
    import ast

    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    listed = set(_all_files())

    def imports_of(path):
        out = set()
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                out.add(node.module)
                if node.names:
                    out.add(f"{node.module}.{node.names[0].name}")
        return out

    seen, todo = set(), ["select-models.py"]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        assert rel in listed, f"{rel} is imported by the selector but missing from MANIFEST.json"
        for mod in imports_of(os.path.join(REPO, rel.replace("/", os.sep))):
            if mod.split(".")[0] in stdlib:
                continue
            for cand in (mod.replace(".", os.sep) + ".py",
                         os.path.join(*mod.split("."), "__init__.py")):
                if os.path.exists(os.path.join(REPO, cand)):
                    todo.append(cand.replace(os.sep, "/"))
                    break


def test_one_liner_file_set_runs_the_selector(tmp_path):
    """The files a one-liner user ends up with must be enough to run it.

    This is the regression test for 'ModuleNotFoundError: No module named
    selector' -- the refactor split the selector into a package but the
    installer still fetched one file.
    """
    dest = tmp_path / "install"
    dest.mkdir()
    _copy_manifest_files(str(dest))

    r = subprocess.run(
        [_python(), "select-models.py", "--providers", "ollama-local",
         "--list-only", "--dry-run"],
        cwd=str(dest), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    combined = r.stdout + r.stderr
    assert "ModuleNotFoundError" not in combined, combined
    assert "Traceback" not in combined, combined


def test_installer_resolves_a_base_dir_when_psscriptroot_is_empty():
    """Under the one-liner $PSScriptRoot is empty; the installer must not throw.

    Extracts Resolve-ScriptBaseDir from hermes-setup.ps1 and runs it the way the
    scriptblock one-liner does (no $PSScriptRoot, no sibling files).
    """
    src = open(SETUP_PS1, encoding="utf-8").read()
    start = src.index("function Resolve-ScriptBaseDir {")
    end = src.index("\n}\n", start) + len("\n}\n")
    fn = src[start:end]
    assert "Join-Path $PSScriptRoot" in fn

    home = tempfile.mkdtemp(prefix="eh-home-")
    probe = os.path.join(home, "probe.ps1")
    with open(probe, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "$PSScriptRoot = ''\n"
            "$HermesHome = '" + home.replace("'", "''") + "'\n"
            "$InstallDir = Join-Path $HermesHome 'easy-hermes'\n"
            + fn +
            "\nWrite-Output ('BASE=' + (Resolve-ScriptBaseDir))\n"
        )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", probe],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "BASE=" + os.path.join(home, "easy-hermes") in r.stdout, r.stdout + r.stderr


def test_no_unguarded_psscriptroot_join_path():
    """Every Join-Path $PSScriptRoot must be behind a $PSScriptRoot guard.

    An unguarded one throws ParameterBindingValidationException under the
    scriptblock one-liner and killed the yolo scheduler registration.
    """
    lines = open(SETUP_PS1, encoding="utf-8").read().splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # comments quoting the pattern are not code
        if "Join-Path $PSScriptRoot" not in line:
            continue
        window = "\n".join(lines[max(0, i - 4):i + 1])
        assert "$PSScriptRoot" in window and ("if ($PSScriptRoot)" in window
                                              or "if ($PSScriptRoot -" in window), \
            f"unguarded Join-Path $PSScriptRoot at line {i + 1}: {line}"


def test_no_update_flag_persists_marker():
    """-NoUpdate must be a real switch, and the opt-out must be sticky."""
    src = open(SETUP_PS1, encoding="utf-8").read()
    assert "[switch]$NoUpdate" in src
    assert "[switch]$Update" in src
    assert "NOUPDATE" in src
    assert "EASY_HERMES_NO_UPDATE" in src
    # the marker is honoured before any network call
    assert src.index("Test-UpdateDeclined") < src.index("Sync-InstallFiles -Dir")
