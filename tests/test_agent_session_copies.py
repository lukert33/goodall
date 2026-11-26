"""Outside-in tests for the baseline: agent sessions copied into the log directory when a work block ends.

Each test runs goodall commands the way a person would, in this process,
with a captured terminal, the fake runner and a clock the test sets. Between
the commands the test writes what a source application would have written:
synthetic session files in a synthetic Claude config folder inside tmp_path.
The tests then check what a researcher would find in the log directory.

Every record here is synthetic. The layout and the key names (`timestamp`,
`entrypoint`, `sessionId`) are the ones seen in the development account on
2026-11-26, keys and counts only (docs/source-applications.md).
"""

import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import cli, hook
from goodall.fake_runner import FakeRunner, PretendProgram
from goodall.terminal import Terminal

PROJECT_FOLDER_NAME = "-Users-ada-work"
OTHER_PROJECT_FOLDER_NAME = "-Users-ada-elsewhere"

CODE_TAB_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
TERMINAL_SESSION = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"
EARLIER_SESSION = "c3d4e5f6-0718-4a2b-9c3d-4e5f60718293"

# What each source application writes in a record's `entrypoint` field.
CODE_TAB_ENTRYPOINT = "claude-desktop"
TERMINAL_ENTRYPOINT = "cli"

BLOCK_START = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
BLOCK_END = BLOCK_START + timedelta(hours=1)


@dataclass(frozen=True)
class EvalAccount:
    """The folders these tests touch, all inside tmp_path."""

    config_directory: Path
    record_repo: Path
    log_directory: Path
    claude_config_directory: Path

    @property
    def projects_folder(self) -> Path:
        return self.claude_config_directory / "projects"

    @property
    def copied_transcripts(self) -> Path:
        return self.log_directory / "transcripts"


EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "A test eval."
workstreams = ["research"]
intervention_categories = ["infrastructure"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""


def git(working_directory: Path, *arguments: str) -> None:
    """Run git for the test's own setup, free of any GIT_ variable the test run inherited."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    subprocess.run(["git", *arguments], cwd=working_directory, env=environment, capture_output=True, check=True)


def make_eval_account(tmp_path: Path) -> EvalAccount:
    """Local settings, a record repo holding the eval config, a log directory, and an empty Claude projects folder."""
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
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
    eval_account = EvalAccount(config_directory, record_repo, log_directory, claude_config_directory)
    eval_account.projects_folder.mkdir(parents=True)
    return eval_account


def record_line(claude_session: str, moment: datetime, entrypoint: str, text: str = "synthetic") -> bytes:
    """One transcript record as the Claude family writes it: one JSON object on a line, with a `timestamp`."""
    record = {
        "type": "user",
        "sessionId": claude_session,
        "entrypoint": entrypoint,
        "timestamp": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z",
        "message": {"content": text},
    }
    return json.dumps(record).encode("utf-8") + b"\n"


def write_session_file(path: Path, contents: bytes, last_written_at: datetime) -> None:
    """Write a file a source application would have written, last changed at `last_written_at`.

    The modification time is set by hand because goodall reads it: a file
    last changed before a work block began cannot hold a record from inside
    it. A test that left the real clock's time there would depend on when
    the tests are run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, (last_written_at.timestamp(), last_written_at.timestamp()))


def write_claude_session(
    eval_account: EvalAccount,
    claude_session: str,
    moments: list[datetime],
    entrypoint: str,
    project_folder_name: str = PROJECT_FOLDER_NAME,
) -> bytes:
    """Write one Claude session's transcript with a record at each of `moments`; return its exact bytes."""
    contents = b""
    for moment in moments:
        contents += record_line(claude_session, moment, entrypoint)
    # A line that is not ASCII and holds a byte that is not valid UTF-8, so
    # a copy that decoded and re-encoded anything would not match.
    contents += b'{"type":"assistant","message":{"content":"Caf\xc3\xa9 \xe2\x9c\x93 \xff"}}\n'
    transcript = eval_account.projects_folder / project_folder_name / f"{claude_session}.jsonl"
    write_session_file(transcript, contents, moments[-1])
    return contents


def files_and_bytes(folder: Path) -> dict[str, bytes]:
    """Every file under `folder`, by its path relative to `folder`, with its exact contents."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(folder))] = path.read_bytes()
    return found


@dataclass(frozen=True)
class GoodallRun:
    """What one run of a goodall command did, as the person who ran it would see it."""

    exit_code: int
    standard_output: str
    standard_error: str

    @property
    def everything_printed(self) -> str:
        """Both streams, with each run of spaces and line breaks made one space, for phrase checks."""
        return " ".join((self.standard_output + "\n" + self.standard_error).split())


def use_eval_account(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point goodall, and the Claude family, at the test's folders, as the eval account's environment would."""
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(eval_account.config_directory))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(eval_account.claude_config_directory))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)


def run_goodall(
    eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch, arguments: list[str], now: datetime
) -> GoodallRun:
    """Run a goodall command in this process at the moment `now`, with a captured terminal and the fake runner."""
    use_eval_account(eval_account, monkeypatch)
    runner = FakeRunner()
    runner.answer(["claude", "--version"], standard_output="2.1.274 (Claude Code)\n")
    # goodall start's telemetry receiver, which these tests do not look at.
    runner.answer_background(
        [sys.executable, "-I", "-m", "goodall.telemetry_receiver"], lambda: PretendProgram(ready_line='{"port": 4318}')
    )
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: now)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


def run_hook(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch, event_name: str, now: datetime) -> int:
    """Run `goodall hook <event>` in this process at the moment `now`, as Claude Code would for the Code tab session."""
    use_eval_account(eval_account, monkeypatch)
    transcript = eval_account.projects_folder / PROJECT_FOLDER_NAME / f"{CODE_TAB_SESSION}.jsonl"
    standard_input = io.StringIO(json.dumps({"session_id": CODE_TAB_SESSION, "transcript_path": str(transcript)}))
    terminal = Terminal(standard_output=io.StringIO(), standard_error=io.StringIO(), standard_input=standard_input)
    return hook.run_hook(event_name, terminal, lambda: now)


def test_a_work_block_with_the_code_tab_and_a_terminal_ends_with_both_agent_sessions_copied_and_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    # An agent session that ended the evening before: no record of it is inside the work block.
    write_claude_session(
        eval_account, EARLIER_SESSION, [BLOCK_START - timedelta(hours=14)], TERMINAL_ENTRYPOINT, PROJECT_FOLDER_NAME
    )

    start = run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    # The person works in the desktop app's Code tab, and in Claude Code in
    # a terminal opened in another folder: every agent session in the eval
    # account counts, whatever folder it ran in.
    code_tab_bytes = write_claude_session(
        eval_account,
        CODE_TAB_SESSION,
        [BLOCK_START + timedelta(minutes=5), BLOCK_START + timedelta(minutes=20)],
        CODE_TAB_ENTRYPOINT,
    )
    terminal_bytes = write_claude_session(
        eval_account,
        TERMINAL_SESSION,
        [BLOCK_START + timedelta(minutes=30)],
        TERMINAL_ENTRYPOINT,
        OTHER_PROJECT_FOLDER_NAME,
    )
    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)

    assert (start.exit_code, stop.exit_code) == (0, 0), start.standard_error + stop.standard_error
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CODE_TAB_SESSION}.jsonl": code_tab_bytes,
        f"{OTHER_PROJECT_FOLDER_NAME}/{TERMINAL_SESSION}.jsonl": terminal_bytes,
    }
    assert "Agent sessions copied 2" in stop.everything_printed


def test_hooks_copy_nothing_while_the_work_block_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    write_claude_session(eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT)

    for event_name in ["PostToolUse", "Stop", "SessionEnd"]:
        assert run_hook(eval_account, monkeypatch, event_name, BLOCK_START + timedelta(minutes=6)) == 0

    assert not eval_account.copied_transcripts.exists()


def test_a_work_block_nobody_stopped_has_its_agent_sessions_copied_by_the_next_goodall_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    last_activity = BLOCK_START + timedelta(minutes=40)
    code_tab_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5), last_activity], CODE_TAB_ENTRYPOINT
    )
    assert run_hook(eval_account, monkeypatch, "Stop", last_activity) == 0
    # The person went home without running goodall stop. That evening an
    # agent session ran with no work block active: it has no record inside
    # the one goodall closes at its last heartbeat.
    write_claude_session(eval_account, TERMINAL_SESSION, [BLOCK_START + timedelta(hours=10)], TERMINAL_ENTRYPOINT)

    next_morning = run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START + timedelta(hours=24))

    assert next_morning.exit_code == 0, next_morning.standard_error
    assert "was left running" in next_morning.everything_printed
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CODE_TAB_SESSION}.jsonl": code_tab_bytes,
    }
    assert "Copied 1 agent session of a work block that had ended into the log directory." in (
        next_morning.everything_printed
    )


def test_an_agent_session_used_in_two_work_blocks_is_copied_again_whole_when_the_second_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    first_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT
    )
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)
    copied_transcript = eval_account.copied_transcripts / PROJECT_FOLDER_NAME / f"{CODE_TAB_SESSION}.jsonl"
    assert copied_transcript.read_bytes() == first_bytes

    afternoon = BLOCK_START + timedelta(hours=5)
    run_goodall(eval_account, monkeypatch, ["start"], afternoon)
    more_bytes = record_line(CODE_TAB_SESSION, afternoon + timedelta(minutes=10), CODE_TAB_ENTRYPOINT)
    write_session_file(
        eval_account.projects_folder / PROJECT_FOLDER_NAME / f"{CODE_TAB_SESSION}.jsonl",
        first_bytes + more_bytes,
        afternoon + timedelta(minutes=10),
    )
    run_goodall(eval_account, monkeypatch, ["stop"], afternoon + timedelta(hours=1))

    assert copied_transcript.read_bytes() == first_bytes + more_bytes
    # The earlier copy was the start of the new one, so nothing was kept aside.
    assert not (eval_account.log_directory / "superseded-transcripts").exists()


def test_a_session_file_rewritten_since_it_was_copied_is_copied_again_and_the_earlier_copy_kept_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    first_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT
    )
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)

    afternoon = BLOCK_START + timedelta(hours=5)
    run_goodall(eval_account, monkeypatch, ["start"], afternoon)
    # Something rewrote the transcript: it no longer starts with what was copied.
    rewritten_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [afternoon + timedelta(minutes=10)], CODE_TAB_ENTRYPOINT
    )
    stop = run_goodall(eval_account, monkeypatch, ["stop"], afternoon + timedelta(hours=1))

    relative_path = f"{PROJECT_FOLDER_NAME}/{CODE_TAB_SESSION}.jsonl"
    assert files_and_bytes(eval_account.copied_transcripts) == {relative_path: rewritten_bytes}
    kept = files_and_bytes(eval_account.log_directory / "superseded-transcripts")
    assert list(kept.values()) == [first_bytes]
    [kept_path] = kept
    assert kept_path.endswith(relative_path)
    assert stop.exit_code == 0


def test_a_copy_that_fails_is_a_warning_at_stop_and_goodall_status_copies_once_the_log_directory_is_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    code_tab_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT
    )
    # The folder the copies go in cannot be written to.
    eval_account.copied_transcripts.mkdir()
    eval_account.copied_transcripts.chmod(0o500)
    try:
        stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)
        status_while_broken = run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(minutes=1))
    finally:
        eval_account.copied_transcripts.chmod(0o700)

    # The work block is stopped and nothing is lost, so stop succeeded (AGENTS.md, "Command-line conventions").
    assert stop.exit_code == 0
    assert "Stopped your work block on research" in stop.everything_printed
    for run in [stop, status_while_broken]:
        assert "WARN" in run.standard_error
        assert "Goodall could not copy everything of the agent sessions of a work block" in run.everything_printed
        assert f"Claude agent session {CODE_TAB_SESSION}: Permission denied." in run.everything_printed
        assert "goodall tries again each time you run goodall start, stop or status" in run.everything_printed
    assert files_and_bytes(eval_account.copied_transcripts) == {}

    first_status = run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(minutes=2))
    second_status = run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(minutes=3))

    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CODE_TAB_SESSION}.jsonl": code_tab_bytes,
    }
    assert "Copied 1 agent session of a work block that had ended into the log directory." in (
        first_status.everything_printed
    )
    assert "agent session" not in second_status.everything_printed


def test_a_copy_that_fails_appears_in_the_warnings_of_goodall_status_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    write_claude_session(eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT)
    eval_account.copied_transcripts.mkdir()
    eval_account.copied_transcripts.chmod(0o500)
    try:
        run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)
        status = run_goodall(eval_account, monkeypatch, ["status", "--json"], BLOCK_END + timedelta(minutes=1))
    finally:
        eval_account.copied_transcripts.chmod(0o700)

    warning_kinds = [warning["kind"] for warning in json.loads(status.standard_output)["warnings"]]
    assert warning_kinds == ["agent_sessions_not_copied"]


def test_goodall_stop_in_a_terminal_without_claude_config_dir_finds_the_folder_the_hooks_ran_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    code_tab_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT
    )
    # The hook inherits Claude Code's environment, and remembers its config folder.
    assert run_hook(eval_account, monkeypatch, "PostToolUse", BLOCK_START + timedelta(minutes=6)) == 0

    use_eval_account(eval_account, monkeypatch)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    runner = FakeRunner()
    standard_output = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=io.StringIO())
    exit_code = cli.main(["stop"], terminal=terminal, runner=runner, clock=lambda: BLOCK_END)

    assert exit_code == 0
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CODE_TAB_SESSION}.jsonl": code_tab_bytes,
    }


def files_an_importer_would_take_for_claude_sessions(log_directory: Path) -> list[str]:
    """The files Inspect Scout's claude_code importer would read as Claude sessions if pointed at `log_directory`.

    Its rule (`discover_session_files` in inspect_scout, read 2026-11-18):
    a folder holding `*.jsonl` files is a project folder, and every such file
    in it is a Claude session; otherwise each folder inside it is tried as a
    project folder, one level down and no deeper.
    """
    found = []
    candidate_folders = [log_directory]
    if not any(log_directory.glob("*.jsonl")):
        candidate_folders = sorted(path for path in log_directory.iterdir() if path.is_dir())
    for folder in candidate_folders:
        for session_file in sorted(folder.glob("*.jsonl")):
            found.append(str(session_file.relative_to(log_directory)))
    return found


def test_nothing_in_the_log_directory_beside_transcripts_looks_like_a_project_folder_to_an_importer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    first_bytes = write_claude_session(
        eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT
    )
    history = eval_account.claude_config_directory / "history.jsonl"
    history.write_bytes(b'{"display":"synthetic prompt"}\n')
    assert run_hook(eval_account, monkeypatch, "Stop", BLOCK_START + timedelta(minutes=6)) == 0
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)
    # A second work block in which both files were rewritten, so earlier copies are kept aside too.
    afternoon = BLOCK_START + timedelta(hours=5)
    run_goodall(eval_account, monkeypatch, ["start"], afternoon)
    write_claude_session(eval_account, CODE_TAB_SESSION, [afternoon + timedelta(minutes=10)], CODE_TAB_ENTRYPOINT)
    history.write_bytes(b'{"display":"rewritten"}\n')
    run_goodall(eval_account, monkeypatch, ["stop"], afternoon + timedelta(hours=1))

    assert first_bytes in files_and_bytes(eval_account.log_directory / "superseded-transcripts").values()
    assert files_and_bytes(eval_account.log_directory / "superseded-claude-config") != {}
    assert files_an_importer_would_take_for_claude_sessions(eval_account.log_directory) == []
    assert files_an_importer_would_take_for_claude_sessions(eval_account.copied_transcripts) == [
        f"{PROJECT_FOLDER_NAME}/{CODE_TAB_SESSION}.jsonl"
    ]


def test_everything_goodall_keeps_about_copying_is_readable_by_this_macos_user_account_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START)
    write_claude_session(eval_account, CODE_TAB_SESSION, [BLOCK_START + timedelta(minutes=5)], CODE_TAB_ENTRYPOINT)
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END)

    for top_folder_name in ["transcripts", "transcript-sync"]:
        top_folder = eval_account.log_directory / top_folder_name
        for path in [top_folder, *top_folder.rglob("*")]:
            expected_permissions = 0o700 if path.is_dir() else 0o600
            assert stat.S_IMODE(path.stat().st_mode) == expected_permissions, path
