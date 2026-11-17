"""Outside-in tests for transcript sync: Claude Code's transcripts copied into the log directory.

Each hook test runs the installed `goodall` command in a fresh process, as
Claude Code does: the event's name on the command line, the event's details
as JSON on standard input, GOODALL_CONFIG_DIR pointing at local settings and
CLAUDE_CONFIG_DIR pointing at a synthetic Claude Code folder, all inside
tmp_path. The tests then check what a researcher would find in the log
directory: the same files, byte for byte, in Claude Code's own layout.

Every transcript here is synthetic. The layout is the one Claude Code 2.1.x
was seen to use in the hook spike (goodall-fs9.1, `bd memories transcript`):

    <projects folder>/<project folder>/<session id>.jsonl
    <projects folder>/<project folder>/<session id>/subagents/agent-<id>.jsonl
                                                   (with .meta.json and .prefix.json beside it)
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
PROJECT_FOLDER_NAME = "-Users-ada-work"

# The installed `goodall` command, beside the interpreter running the tests.
GOODALL_COMMAND = str(Path(sys.executable).parent / "goodall")

# Two transcript lines as Claude Code writes them. The second holds text
# that is not ASCII and a byte that is not valid UTF-8, so a copy that
# decoded and re-encoded anything would not match.
FIRST_LINES = (
    b'{"type":"user","sessionId":"0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5","message":{"content":"Fix the tests"}}\n'
    b'{"type":"assistant","message":{"content":[{"type":"text","text":"Caf\xc3\xa9 \xe2\x9c\x93 \xff"}]}}\n'
)
MORE_LINES = b'{"type":"user","message":{"content":[{"type":"tool_result","content":"3 passed"}]}}\n'


@dataclass(frozen=True)
class EvalAccount:
    """The folders transcript sync touches, all inside tmp_path."""

    config_directory: Path
    log_directory: Path
    claude_config_directory: Path

    @property
    def project_folder(self) -> Path:
        return self.claude_config_directory / "projects" / PROJECT_FOLDER_NAME

    @property
    def transcript(self) -> Path:
        return self.project_folder / f"{CLAUDE_SESSION}.jsonl"

    @property
    def copied_transcripts(self) -> Path:
        return self.log_directory / "transcripts"


def make_eval_account(tmp_path: Path) -> EvalAccount:
    """Local settings naming a record repo and a log directory, and an empty Claude projects folder."""
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
    claude_config_directory = tmp_path / "claude-config"
    eval_account = EvalAccount(config_directory, log_directory, claude_config_directory)
    eval_account.project_folder.mkdir(parents=True)
    return eval_account


def hook_input(eval_account: EvalAccount, event_name: str, **extra_fields: object) -> str:
    """The JSON Claude Code sends on standard input, naming the synthetic transcript."""
    fields: dict[str, object] = {
        "session_id": CLAUDE_SESSION,
        "transcript_path": str(eval_account.transcript),
        "cwd": "/Users/ada/work",
        "hook_event_name": event_name,
        "permission_mode": "default",
    }
    fields.update(extra_fields)
    return json.dumps(fields)


def run_hook(eval_account: EvalAccount, event_name: str, standard_input: str) -> subprocess.CompletedProcess[str]:
    """Run `goodall hook <event>` as Claude Code would, and return what happened."""
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(eval_account.config_directory)
    environment["CLAUDE_CONFIG_DIR"] = str(eval_account.claude_config_directory)
    return subprocess.run(
        [GOODALL_COMMAND, "hook", event_name],
        input=standard_input,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=environment,
    )


def files_and_bytes(folder: Path) -> dict[str, bytes]:
    """Every file under `folder`, by its path relative to `folder`, with its exact contents."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(folder))] = path.read_bytes()
    return found


def test_a_stop_hook_copies_the_transcript_byte_for_byte_in_claude_codes_layout(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)

    completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop", stop_hook_active=False))

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
    }


def test_a_transcript_that_grew_between_two_hooks_is_copied_again_in_full(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Bash"))
    with eval_account.transcript.open("ab") as transcript:
        transcript.write(MORE_LINES)

    completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))

    assert completed.returncode == 0
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES + MORE_LINES,
    }
