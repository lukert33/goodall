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
    # Where the `goodall` command starts, and `goodall hook` itself.
    "goodall.entry_point",
    "goodall.hook",
    "goodall.exit_codes",
    # The prompt gate checks the log directory can be written to through it.
    "goodall.folders",
    "goodall.local_settings",
    # Not imported by `goodall hook` today; kept standard-library-only so
    # later hook work (transcript sync, goodall-fs9.6) can read the eval.
    "goodall.eval_config",
    # Imported by goodall.local_settings for its Failure record; the hook
    # writes its refusals through it.
    "goodall.terminal",
    # Not imported by `goodall hook`, which starts no program (see
    # test_the_hook_command_loads_no_third_party_module_and_starts_no_program);
    # kept standard-library-only all the same.
    "goodall.runner",
    # The prompt gate asks it whether a work block is active, and the hook
    # leaves heartbeats through it; it reads the marker with goodall.records.
    "goodall.work_blocks",
    "goodall.records",
    # PostToolUse, Stop and SessionEnd copy the Claude session's transcript
    # into the log directory through it.
    "goodall.transcripts",
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


# Runs inside the fresh interpreter: with a work block running in the log
# directory named on the command line, ask whether it is active and leave a
# heartbeat, as the hook will on every prompt, then print every module loaded
# along the way that is neither the standard library nor goodall, and
# `subprocess` if it was loaded at all: the hook's work-block calls must
# never start a program.
HEARTBEAT_AND_LIST_MODULES = """
import sys
from datetime import UTC, datetime

already_loaded = set(sys.modules)
from goodall import work_blocks

log_directory = __import__("pathlib").Path(sys.argv[1])
now = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
if work_blocks.active_work_block(log_directory, now) is None:
    raise SystemExit("expected an active work block")
if work_blocks.record_heartbeat(log_directory, now, "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5") is None:
    raise SystemExit("expected the heartbeat to be recorded")

for loaded_name in sorted(set(sys.modules) - already_loaded):
    top_level_name = loaded_name.split(".")[0]
    if top_level_name == "subprocess":
        print(loaded_name)
    if top_level_name in sys.stdlib_module_names or top_level_name == "goodall":
        continue
    print(loaded_name)
"""


def test_checking_the_work_block_and_leaving_a_heartbeat_loads_only_the_standard_library(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from goodall import records, work_blocks

    machine = records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None)
    running = records.build_work_block_record(
        "ada-lovelace", "research", datetime(2026, 11, 16, 9, 30, tzinfo=UTC), machine
    )
    assert work_blocks.create_marker(tmp_path, running)

    completed = subprocess.run(
        [sys.executable, "-c", HEARTBEAT_AND_LIST_MODULES, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert work_blocks.read_claude_sessions(tmp_path, running.id) == ("0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5",)


# Runs inside the fresh interpreter as the `goodall` command does, with the
# event name given on the command line and Claude Code's input on standard
# input. Afterwards it prints every module that is neither the standard
# library nor goodall, and any of NOT_ON_THE_HOOK_PATH that was loaded at
# all. Only modules loaded after start-up count, as above.
RUN_HOOK_AND_LIST_MODULES = """
import sys

NOT_ON_THE_HOOK_PATH = ["subprocess", "argparse", "goodall.cli", "goodall.runner"]

already_loaded = set(sys.modules)
sys.argv = ["goodall", "hook", sys.argv[1]]
from goodall.entry_point import main

exit_code = main()
if exit_code != 0:
    raise SystemExit(f"goodall hook exited {exit_code}")

for loaded_name in sorted(set(sys.modules) - already_loaded):
    top_level_name = loaded_name.split(".")[0]
    if loaded_name in NOT_ON_THE_HOOK_PATH:
        print(loaded_name)
    elif top_level_name not in sys.stdlib_module_names and top_level_name != "goodall":
        print(loaded_name)
"""


def test_the_hook_command_loads_no_third_party_module_and_starts_no_program(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from goodall import records, work_blocks

    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{tmp_path}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    started_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    machine = records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None)
    running = records.build_work_block_record("ada-lovelace", "research", started_at, machine)
    assert work_blocks.create_marker(log_directory, running)
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(config_directory)
    hook_input = '{"session_id": "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5", "transcript_path": "/tmp/t.jsonl"}'

    # The prompt gate, and an event that only leaves a heartbeat.
    for event_name in ["UserPromptSubmit", "PostToolUse"]:
        completed = subprocess.run(
            [sys.executable, "-c", RUN_HOOK_AND_LIST_MODULES, event_name],
            input=hook_input,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=environment,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == "", event_name
    # Both heartbeats were recorded, so the hook really ran its whole path.
    heartbeat_file = work_blocks.WorkBlockFiles(log_directory).heartbeat_file(running.id)
    assert len(heartbeat_file.read_text().splitlines()) == 2
