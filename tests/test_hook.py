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
    return hook_input("UserPromptSubmit", prompt="Summarize the failing tests.", prompt_id="prompt-1")


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


def test_a_prompt_is_refused_when_goodall_is_not_set_up(tmp_path: Path) -> None:
    empty_config_directory = tmp_path / "goodall-config"
    empty_config_directory.mkdir()

    completed = run_hook(empty_config_directory, ["UserPromptSubmit"], prompt_input())

    assert completed.returncode == 2
    assert completed.stderr == (
        "Goodall is not set up in this macOS user account, so this prompt was not sent. "
        "Run goodall onboard in a terminal to set it up, then send your prompt again.\n"
    )


def test_a_prompt_is_refused_when_local_settings_are_broken_without_quoting_them(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    # A secret pasted by mistake, unquoted, so the file is not valid TOML.
    settings_file = eval_account.config_directory / "local-settings.toml"
    settings_file.write_text(settings_file.read_text() + "contributor_token = sk-ant-api03-pastedbymistake\n")

    completed = run_hook(eval_account.config_directory, ["UserPromptSubmit"], prompt_input())

    assert completed.returncode == 2
    assert completed.stderr == (
        "Goodall cannot read its local settings, so this prompt was not sent. "
        "Run goodall doctor in a terminal to see what to fix, then send your prompt again.\n"
    )
    assert "pastedbymistake" not in completed.stderr


def test_a_prompt_is_refused_when_the_log_directory_cannot_be_reached_without_naming_it(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    start_work_block(eval_account.log_directory)
    # As when the external drive holding the log directory is unplugged.
    eval_account.log_directory.rename(tmp_path / "unplugged")

    completed = run_hook(eval_account.config_directory, ["UserPromptSubmit"], prompt_input())

    assert completed.returncode == 2
    assert completed.stderr == (
        "Goodall cannot reach its log directory, so nothing can be recorded and this prompt was not sent. "
        "If the log directory is on an external drive, connect it; then run goodall status in a terminal "
        "and send your prompt again.\n"
    )
    assert str(eval_account.log_directory) not in completed.stderr


# What Claude Code shows the person, through the hook's JSON `systemMessage`,
# when a prompt went through although goodall could not read its input.
UNREADABLE_INPUT_NOTICE = (
    "Goodall could not read the details Claude Code sent with this prompt, so it sent the prompt and kept your "
    "work block recording without linking this Claude session to it; please tell your eval's researcher, who may "
    "need to update goodall."
)

# Input goodall cannot read, and the category each is noted under. The
# prompt text in each must never reach the log directory.
UNREADABLE_INPUTS = [
    ("", "empty"),
    ('{"prompt": "Private words", "session_id": "0f6c1d2e', "not_json"),
    (json.dumps({"prompt": "Private words"}), "no_session_id"),
    (json.dumps({"prompt": "Private words", "session_id": "Private-words"}), "session_id_not_uuid"),
]


def noted_hook_problems(log_directory: Path) -> list[dict[str, object]]:
    """The lines goodall noted about hooks it could not handle fully, not yet reported."""
    problems_file = log_directory / "hook-problems" / "hook-problems.jsonl"
    if not problems_file.exists():
        return []
    noted = []
    for line in problems_file.read_text().splitlines():
        noted.append(json.loads(line))
    return noted


def test_a_prompt_whose_input_cannot_be_read_goes_through_during_a_work_block_with_a_warning(
    tmp_path: Path,
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)

    for unreadable_input, _ in UNREADABLE_INPUTS:
        completed = run_hook(eval_account.config_directory, ["UserPromptSubmit"], unreadable_input)

        # Exit 0 lets the prompt through. Standard output holds one JSON
        # object and nothing else, because Claude Code would add any other
        # text to Claude's context; its systemMessage is shown to the person.
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        assert completed.stdout == json.dumps({"systemMessage": UNREADABLE_INPUT_NOTICE}) + "\n"

    # The work block still shows activity, with no Claude session named.
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [None] * len(UNREADABLE_INPUTS)
    noted = noted_hook_problems(eval_account.log_directory)
    expected_problems = []
    for _, problem in UNREADABLE_INPUTS:
        expected_problems.append(problem)
    assert [line["problem"] for line in noted] == expected_problems
    for line in noted:
        assert sorted(line) == ["at", "event", "problem", "work_block"]
        assert line["event"] == "UserPromptSubmit"
        assert line["work_block"] == work_block.id
    problems_file = eval_account.log_directory / "hook-problems" / "hook-problems.jsonl"
    assert "Private" not in problems_file.read_text()
    assert problems_file.stat().st_mode & 0o777 == 0o600


def test_a_prompt_whose_input_cannot_be_read_is_still_refused_when_no_work_block_is_active(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)

    for unreadable_input, _ in UNREADABLE_INPUTS:
        completed = run_hook(eval_account.config_directory, ["UserPromptSubmit"], unreadable_input)

        # Unreadable input must never open a gate that would otherwise be closed.
        assert completed.returncode == 2, unreadable_input
        assert completed.stdout == ""
        assert completed.stderr.startswith("Goodall is not recording, so this prompt was not sent.")
    assert noted_hook_problems(eval_account.log_directory) == []


def test_a_prompt_whose_input_cannot_be_read_is_still_refused_when_goodall_is_not_set_up(tmp_path: Path) -> None:
    not_set_up = tmp_path / "not-set-up"
    not_set_up.mkdir()

    completed = run_hook(not_set_up, ["UserPromptSubmit"], "{cut short")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Goodall is not set up" in completed.stderr


# Every hook event other than the prompt gate, each with the fields Claude
# Code adds for it.
RECORDING_EVENTS = {
    "SessionStart": hook_input("SessionStart", source="startup"),
    "PostToolUse": hook_input(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": "uv run pytest"},
        tool_response={"stdout": "3 passed", "stderr": ""},
        tool_use_id="toolu_01",
        duration_ms=812,
    ),
    "Stop": hook_input("Stop", stop_hook_active=False, last_assistant_message="All tests pass now."),
    "SessionEnd": hook_input("SessionEnd", reason="other"),
}


def test_every_other_event_leaves_a_heartbeat_silently(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)

    for event_name, event_input in RECORDING_EVENTS.items():
        completed = run_hook(eval_account.config_directory, [event_name], event_input)

        assert completed.returncode == 0, event_name
        assert completed.stdout == ""
        assert completed.stderr == ""
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [CLAUDE_SESSION] * len(RECORDING_EVENTS)


def test_other_events_exit_0_silently_whatever_is_wrong(tmp_path: Path) -> None:
    # Exit 2 from a Stop hook would keep Claude from finishing its reply, so
    # nothing goodall finds wrong may reach Claude Code from these events.
    not_set_up = tmp_path / "not-set-up"
    not_set_up.mkdir()
    no_work_block = make_eval_account(tmp_path)

    for config_directory in [not_set_up, no_work_block.config_directory]:
        for event_name in RECORDING_EVENTS:
            for event_input in [RECORDING_EVENTS[event_name], "", "{cut short"]:
                completed = run_hook(config_directory, [event_name], event_input)

                assert completed.returncode == 0, (config_directory, event_name, event_input)
                assert completed.stdout == ""
                assert completed.stderr == ""


def test_an_event_with_unreadable_input_still_leaves_a_heartbeat(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)

    completed = run_hook(eval_account.config_directory, ["PostToolUse"], "{cut short")

    assert completed.returncode == 0
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [None]


# ---------------------------------------------------------------------------
# A hook command that does not name its event
# ---------------------------------------------------------------------------


def test_a_bare_hook_command_on_user_prompt_submit_is_still_a_working_prompt_gate(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)

    # The event comes from hook_event_name in what Claude Code sends.
    refused = run_hook(eval_account.config_directory, [], prompt_input())
    work_block = start_work_block(eval_account.log_directory)
    let_through = run_hook(eval_account.config_directory, [], prompt_input())

    assert refused.returncode == 2
    assert refused.stderr.startswith("Goodall is not recording, so this prompt was not sent.")
    assert let_through.returncode == 0, let_through.stderr
    assert let_through.stdout == ""
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [CLAUDE_SESSION]
    assert noted_hook_problems(eval_account.log_directory) == []


def test_a_bare_hook_command_on_any_other_event_leaves_its_heartbeat(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)

    for event_input in RECORDING_EVENTS.values():
        completed = run_hook(eval_account.config_directory, [], event_input)

        assert completed.returncode == 0
        assert completed.stdout == completed.stderr == ""
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [CLAUDE_SESSION] * len(RECORDING_EVENTS)


def test_the_event_on_the_command_line_wins_over_the_one_in_the_input(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)

    # No work block: as the prompt gate this would be refused, but a Stop
    # hook must never refuse anything.
    completed = run_hook(eval_account.config_directory, ["Stop"], prompt_input())

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""


def test_a_bare_hook_command_whose_input_names_no_event_exits_1_silently_and_is_noted(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)
    no_event_name = json.dumps({"session_id": CLAUDE_SESSION, "prompt": "Private words"})

    for event_input, problem in [
        ("{cut short", "no_event_name"),
        (no_event_name, "no_event_name"),
        (hook_input("Notification", message="Private words"), "event_not_known"),
    ]:
        completed = run_hook(eval_account.config_directory, [], event_input)

        # 1, not 2: which event fired is unknown, and 2 would refuse a prompt
        # or keep Claude from stopping. Nothing printed can reach Claude.
        assert completed.returncode == 1, event_input
        assert completed.stdout == completed.stderr == ""
        [noted] = noted_hook_problems(eval_account.log_directory)[-1:]
        assert noted["problem"] == problem
        assert noted["event"] is None
        assert noted["work_block"] == work_block.id
    problems_file = eval_account.log_directory / "hook-problems" / "hook-problems.jsonl"
    assert "Private" not in problems_file.read_text()


def test_a_bare_hook_command_with_no_settings_exits_1_and_writes_nothing(tmp_path: Path) -> None:
    not_set_up = tmp_path / "not-set-up"
    not_set_up.mkdir()

    completed = run_hook(not_set_up, [], "{cut short")

    assert completed.returncode == 1
    assert completed.stdout == completed.stderr == ""


def test_an_event_goodall_does_not_handle_named_on_the_command_line_leaves_a_heartbeat_and_is_noted(
    tmp_path: Path,
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block = start_work_block(eval_account.log_directory)

    completed = run_hook(eval_account.config_directory, ["PreToolUse"], hook_input("PreToolUse", tool_name="Bash"))

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
    assert heartbeat_sessions(eval_account.log_directory, work_block) == [CLAUDE_SESSION]
    [noted] = noted_hook_problems(eval_account.log_directory)
    assert noted["problem"] == "event_not_known"
