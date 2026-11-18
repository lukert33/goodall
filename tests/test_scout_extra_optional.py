"""Proves that goodall records an eval without the optional `scout` extra installed.

AGENTS.md invariant 8: inspect-ai and inspect-scout are an optional extra,
imported only by export code, so capture never depends on them. The tests
start a fresh interpreter that hides the extra's packages, whether or not
this environment has them, and check that every goodall module except the
one that builds the Scout database loads, and that the prompt gate runs.

Add a module to EXPORT_ONLY_MODULES only when it is export code that needs
the extra; everything else must import without it.
"""

import os
import subprocess
import sys
from pathlib import Path

# The modules allowed to import inspect-ai or inspect-scout.
EXPORT_ONLY_MODULES = ["goodall.scout_database"]

# Runs first inside the fresh interpreter: any import of the extra's packages
# fails as it would if they were not installed. inspect_swe comes with
# inspect-scout and holds its Claude Code conversion.
HIDE_THE_SCOUT_EXTRA = """
import importlib.abc
import sys

HIDDEN = ("inspect_ai", "inspect_scout", "inspect_swe")


class HideTheScoutExtra(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in HIDDEN:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, HideTheScoutExtra())
"""

# Imports every goodall module except the export-only ones and prints any
# that fails; then checks the hiding works, by importing an export-only one.
IMPORT_EVERY_MODULE = (
    HIDE_THE_SCOUT_EXTRA
    + """
import importlib
import pkgutil

import goodall

export_only = sys.argv[1:]
for module_info in pkgutil.iter_modules(goodall.__path__, prefix="goodall."):
    # goodall.__main__ runs goodall when imported; it holds one call to
    # goodall.entry_point, which is imported here like everything else.
    if module_info.name in export_only or module_info.name == "goodall.__main__":
        continue
    try:
        importlib.import_module(module_info.name)
    except ImportError as import_error:
        print(f"{module_info.name}: {import_error}")

for module_name in export_only:
    try:
        importlib.import_module(module_name)
    except ImportError:
        continue
    print(f"{module_name} imported although the scout extra was hidden")
"""
)

RUN_THE_PROMPT_GATE = (
    HIDE_THE_SCOUT_EXTRA
    + """
from goodall.entry_point import main

sys.argv = ["goodall", "hook", "UserPromptSubmit"]
raise SystemExit(main())
"""
)


def test_every_module_except_the_scout_database_imports_without_the_scout_extra() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", IMPORT_EVERY_MODULE, *EXPORT_ONLY_MODULES],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""


def test_the_prompt_gate_runs_without_the_scout_extra(tmp_path: Path) -> None:
    # No local settings, so the gate refuses the prompt with Claude Code's
    # refusal code. What matters is that it got that far without the extra.
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(tmp_path / "no-settings-here")
    completed = subprocess.run(
        [sys.executable, "-c", RUN_THE_PROMPT_GATE],
        input='{"session_id": "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"}',
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=environment,
    )
    assert completed.returncode == 2, completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr
    assert "Traceback" not in completed.stderr
