"""Outside-in tests for `goodall hook`, the command Claude Code runs for its hooks.

Each test runs the installed `goodall` command in a fresh process, exactly as
Claude Code does: the hook event's name on the command line, the event's
details as JSON on standard input, and GOODALL_CONFIG_DIR pointing at local
settings inside tmp_path. It then checks only what Claude Code and the
researcher could observe: the exit code, what was printed, and the heartbeats
in the log directory.

The hook input is synthetic, with the field names Claude Code 2.1.x was seen
to send in the hook spike (goodall-fs9.1, `bd memories hook`).
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from goodall import records, work_blocks

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"

# The installed `goodall` command, beside the interpreter running the tests,
# so the tests also prove the console script reaches the hook.
GOODALL_COMMAND = str(Path(sys.executable).parent / "goodall")


@dataclass(frozen=True)
class EvalAccount:
    """The parts of an eval account the hook touches, all inside tmp_path."""

    config_directory: Path
    log_directory: Path


def make_eval_account(tmp_path: Path) -> EvalAccount:
    """Write local settings naming a record repo and a log directory that both exist."""
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{record_repo}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    return EvalAccount(config_directory=config_directory, log_directory=log_directory)


def start_work_block(log_directory: Path) -> records.WorkBlockRecord:
    """Put a running work block's marker in the log directory, started a minute ago.

    The hook reads the real clock, so the work block starts just before now
    rather than at a fixed moment, or it would already be overdue.
    """
    started_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    machine = records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None)
    running = records.build_work_block_record("ada-lovelace", "research", started_at, machine)
    assert work_blocks.create_marker(log_directory, running)
    return running


def hook_input(event_name: str, **extra_fields: object) -> str:
    """The JSON Claude Code sends on standard input, with the fields every event shares."""
    fields: dict[str, object] = {
        "session_id": CLAUDE_SESSION,
        "transcript_path": f"/Users/ada/.claude/projects/-Users-ada-work/{CLAUDE_SESSION}.jsonl",
        "cwd": "/Users/ada/work",
        "hook_event_name": event_name,
        "permission_mode": "default",
    }
    fields.update(extra_fields)
    return json.dumps(fields)


def prompt_input() -> str:
    return hook_input("UserPromptSubmit", prompt="Summarise the failing tests.", prompt_id="prompt-1")


def run_hook(config_directory: Path, arguments: list[str], standard_input: str) -> subprocess.CompletedProcess[str]:
    """Run `goodall hook ...` as Claude Code would, and return what happened."""
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(config_directory)
    return subprocess.run(
        [GOODALL_COMMAND, "hook", *arguments],
        input=standard_input,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=environment,
    )


def heartbeat_sessions(log_directory: Path, work_block: records.WorkBlockRecord) -> list[str | None]:
    """The Claude session recorded by each heartbeat of `work_block`, in order."""
    heartbeat_file = work_blocks.WorkBlockFiles(log_directory).heartbeat_file(work_block.id)
    if not heartbeat_file.exists():
        return []
    sessions = []
    for line in heartbeat_file.read_text().splitlines():
        sessions.append(json.loads(line)["claude_session"])
    return sessions


def test_a_prompt_during_a_work_block_goes_through_and_links_the_claude_session(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)

    completed = run_hook(eval_account.config_directory, ["UserPromptSubmit"], prompt_input())

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [CLAUDE_SESSION]


def test_a_prompt_with_no_work_block_is_refused_with_the_command_to_run(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)

    completed = run_hook(eval_account.config_directory, ["UserPromptSubmit"], prompt_input())

    # Claude Code refuses a prompt when its hook exits 2, and shows the
    # person what the hook wrote to standard error.
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (
        "Goodall is not recording, so this prompt was not sent. "
        "Open a terminal, run goodall start, then send your prompt again.\n"
    )
