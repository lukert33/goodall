"""Proves that the modules a Claude Code hook will import load only the standard library.

ADR 0005 and AGENTS.md invariant 7: the prompt gate runs before every prompt,
so the code it imports must be cheap to load and must not break when a
third-party package is missing. The test starts a fresh interpreter, because
the test process itself has pytest and much else already loaded.

Add a module to HOOK_PATH_MODULES as soon as the hook path imports it.
"""

import subprocess
import sys

HOOK_PATH_MODULES = [
    "goodall.local_settings",
]

# Runs inside the fresh interpreter: import the modules, then print every
# newly loaded module that is neither the standard library nor goodall
# itself. Modules loaded before the imports are left out, because the
# virtual environment's start-up loads a helper (`_virtualenv`) that the
# hook path has no say over.
LIST_THIRD_PARTY_MODULES = """
import importlib
import sys

already_loaded = set(sys.modules)
for module_name in sys.argv[1:]:
    importlib.import_module(module_name)

for loaded_name in sorted(set(sys.modules) - already_loaded):
    top_level_name = loaded_name.split(".")[0]
    if top_level_name in sys.stdlib_module_names or top_level_name == "goodall":
        continue
    print(loaded_name)
"""


def test_hook_path_modules_import_only_the_standard_library() -> None:
    # sys.executable is the project's virtual environment, which is how the
    # fresh interpreter finds the installed goodall package.
    completed = subprocess.run(
        [sys.executable, "-c", LIST_THIRD_PARTY_MODULES, *HOOK_PATH_MODULES],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
