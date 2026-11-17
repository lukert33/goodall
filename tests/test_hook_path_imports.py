"""Proves that the modules a Claude Code hook will import load only the standard library.

ADR 0005 and AGENTS.md invariant 7: the prompt gate runs before every prompt,
so the code it imports must be cheap to load and must not break when a
third-party package is missing. The test starts a fresh interpreter, because
the test process itself has pytest and much else already loaded.

Add a module to HOOK_PATH_MODULES as soon as the hook path imports it.
"""

import os
import subprocess
import sys
from pathlib import Path

HOOK_PATH_MODULES = [
    "goodall.local_settings",
    "goodall.eval_config",
    # Imported by goodall.local_settings for its Failure record.
    "goodall.terminal",
    # The prompt gate will run git through it.
    "goodall.runner",
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


# Runs inside the fresh interpreter: read real local settings and the eval
# config they lead to, then print every module loaded along the way that is
# neither the standard library nor goodall. Reading, not just importing, is
# checked, because a module can import more inside a function the first time
# it runs.
READ_SETTINGS_AND_LIST_THIRD_PARTY_MODULES = """
import sys

already_loaded = set(sys.modules)
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.local_settings import LocalSettings, read_local_settings

reading = read_local_settings()
if not isinstance(reading, LocalSettings):
    raise SystemExit(f"expected valid local settings, got {reading!r}")
eval_reading = read_eval_for_this_account(reading)
if not isinstance(eval_reading, EvalForThisAccount):
    raise SystemExit(f"expected a valid eval config, got {eval_reading!r}")

for loaded_name in sorted(set(sys.modules) - already_loaded):
    top_level_name = loaded_name.split(".")[0]
    if top_level_name in sys.stdlib_module_names or top_level_name == "goodall":
        continue
    print(loaded_name)
"""


def test_reading_local_settings_and_eval_config_loads_only_the_standard_library(tmp_path: Path) -> None:
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    (tmp_path / "records").mkdir()
    (tmp_path / "records" / "eval.toml").write_text(
        "schema_version = 1\n"
        'name = "goodall-pilot"\n'
        'description = "A test eval."\n'
        'workstreams = ["research"]\n'
        'intervention_categories = ["infrastructure"]\n'
        '[[contributors]]\nid = "ada-lovelace"\ndisplay_name = "Ada Lovelace"\n'
        '[[scope]]\nrepository = "github.com/your-org/goodall-pilot-app"\npaths = ["**"]\n'
    )
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{tmp_path / "records"}"\n'
        f'log_directory = "{tmp_path / "logs"}"\n'
        'secrets_manager = "1password"\n'
    )
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(config_directory)

    completed = subprocess.run(
        [sys.executable, "-c", READ_SETTINGS_AND_LIST_THIRD_PARTY_MODULES],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""


# Runs inside the fresh interpreter: read a real env file and check its
# references, then print every module loaded along the way that is neither
# the standard library nor goodall. The code that launches programs will
# read the env files through these two modules, so they stay standard
# library only like the hook path (ADR 0005).
READ_ENV_FILE_AND_LIST_THIRD_PARTY_MODULES = """
import sys
from pathlib import Path

already_loaded = set(sys.modules)
from goodall.env_files import EnvFileReading, read_env_file
from goodall.secrets_manager import is_well_formed_secret_reference

reading = read_env_file(Path(sys.argv[1]))
if not isinstance(reading, EnvFileReading) or reading.problems:
    raise SystemExit(f"expected a readable env file, got {reading!r}")
for entry in reading.entries:
    if not is_well_formed_secret_reference("1password", entry.value):
        raise SystemExit(f"expected a well-formed reference for {entry.name}")

for loaded_name in sorted(set(sys.modules) - already_loaded):
    top_level_name = loaded_name.split(".")[0]
    if top_level_name in sys.stdlib_module_names or top_level_name == "goodall":
        continue
    print(loaded_name)
"""


def test_reading_an_env_file_and_checking_its_references_loads_only_the_standard_library(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('ANTHROPIC_API_KEY_ENGINEERING="op://Eval/anthropic-engineering/credential"\n')

    completed = subprocess.run(
        [sys.executable, "-c", READ_ENV_FILE_AND_LIST_THIRD_PARTY_MODULES, str(env_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
