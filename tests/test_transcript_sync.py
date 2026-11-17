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

import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import cli
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

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


def write_subagent_files(eval_account: EvalAccount) -> dict[str, bytes]:
    """Write one subagent's three files beside the transcript, and return them as the copy should hold them."""
    subagents_folder = eval_account.project_folder / CLAUDE_SESSION / "subagents"
    subagents_folder.mkdir(parents=True)
    subagent_files = {
        "agent-a1b2c3.jsonl": b'{"type":"user","isSidechain":true,"message":{"content":"Find the bug"}}\n',
        "agent-a1b2c3.meta.json": b'{"agentType":"Explore"}',
        "agent-a1b2c3.prefix.json": b'{"prefix":[]}\n',
    }
    expected = {}
    for file_name, contents in subagent_files.items():
        (subagents_folder / file_name).write_bytes(contents)
        expected[f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/subagents/{file_name}"] = contents
    return expected


def test_a_hook_copies_the_claude_sessions_subagent_files_in_the_same_layout(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    expected_subagent_files = write_subagent_files(eval_account)

    completed = run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Agent"))

    assert completed.returncode == 0
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
        **expected_subagent_files,
    }


def superseded_copies(eval_account: EvalAccount) -> dict[str, bytes]:
    """Every earlier copy kept aside, by its path below its `superseded-transcripts/<moment>/` folder."""
    found = {}
    for moment_folder in sorted((eval_account.log_directory / "superseded-transcripts").iterdir()):
        for relative_path, contents in files_and_bytes(moment_folder).items():
            found[relative_path] = contents
    return found


def test_a_transcript_that_shrank_is_copied_and_the_longer_copy_is_kept_aside(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES + MORE_LINES)
    run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))
    # Something rewrote the transcript shorter: the copy must not lose the end.
    eval_account.transcript.write_bytes(FIRST_LINES)

    completed = run_hook(eval_account, "SessionEnd", hook_input(eval_account, "SessionEnd", reason="other"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    transcript_name = f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl"
    assert files_and_bytes(eval_account.copied_transcripts) == {transcript_name: FIRST_LINES}
    assert superseded_copies(eval_account) == {transcript_name: FIRST_LINES + MORE_LINES}


def test_a_transcript_that_was_rewritten_is_copied_and_the_earlier_copy_is_kept_aside(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))
    rewritten = FIRST_LINES.replace(b"Fix the tests", b"Fix the build") + MORE_LINES
    eval_account.transcript.write_bytes(rewritten)

    run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))

    transcript_name = f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl"
    assert files_and_bytes(eval_account.copied_transcripts) == {transcript_name: rewritten}
    assert superseded_copies(eval_account) == {transcript_name: FIRST_LINES}


def test_a_transcript_path_that_leads_outside_the_claude_projects_folder_is_refused_quietly(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    private_file = elsewhere / f"{CLAUDE_SESSION}.jsonl"
    private_file.write_bytes(b"not a transcript\n")
    other_project_folder = eval_account.project_folder.parent / "-Users-ada-other"
    other_project_folder.mkdir()
    (other_project_folder / f"{CLAUDE_SESSION}.jsonl").symlink_to(private_file)
    (eval_account.project_folder.parent / "-Users-ada-linked").symlink_to(elsewhere)
    nested_folder = eval_account.project_folder / "nested"
    nested_folder.mkdir()
    (nested_folder / f"{CLAUDE_SESSION}.jsonl").write_bytes(FIRST_LINES)
    eval_account.transcript.write_bytes(FIRST_LINES)
    refused_paths = [
        # Outside the projects folder altogether, directly or by `..`.
        private_file,
        eval_account.project_folder / ".." / ".." / ".." / "elsewhere" / f"{CLAUDE_SESSION}.jsonl",
        # A transcript that is a symbolic link out, or in a project folder that is one.
        other_project_folder / f"{CLAUDE_SESSION}.jsonl",
        eval_account.project_folder.parent / "-Users-ada-linked" / f"{CLAUDE_SESSION}.jsonl",
        # Not where Claude Code keeps a transcript: too deep, or named for another Claude session.
        nested_folder / f"{CLAUDE_SESSION}.jsonl",
        eval_account.project_folder / "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3.jsonl",
        # The projects folder itself.
        eval_account.project_folder.parent,
    ]

    for refused_path in refused_paths:
        completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop", transcript_path=str(refused_path)))

        assert completed.returncode == 0, refused_path
        assert completed.stdout == ""
        assert completed.stderr == ""
    assert not eval_account.copied_transcripts.exists()


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
    # Growth is the normal case, so nothing is kept aside for it.
    assert not (eval_account.log_directory / "superseded-transcripts").exists()


# ---------------------------------------------------------------------------
# The sweep: goodall start, stop and status copy whatever the hooks missed
# ---------------------------------------------------------------------------

MONDAY_MORNING = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)


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


def run_goodall(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch, arguments: list[str]) -> GoodallRun:
    """Run a goodall command in this process, with a captured terminal, the fake runner and a fixed clock."""
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(eval_account.config_directory))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(eval_account.claude_config_directory))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    runner = FakeRunner()
    runner.answer(["claude", "--version"], standard_output="2.1.274 (Claude Code)\n")
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: MONDAY_MORNING)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


def test_goodall_start_copies_a_transcript_left_behind_by_a_killed_claude_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Bash"))
    # Claude Code was killed (SIGKILL fires no hook) after writing more.
    with eval_account.transcript.open("ab") as transcript:
        transcript.write(MORE_LINES)
    expected_subagent_files = write_subagent_files(eval_account)

    start = run_goodall(eval_account, monkeypatch, ["start"])

    assert start.exit_code == 0, start.standard_error
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES + MORE_LINES,
        **expected_subagent_files,
    }
    assert "Brought the copy of 1 Claude session's transcript up to date in the log directory." in (
        start.everything_printed
    )


def test_goodall_status_copies_every_claude_session_in_every_project_folder_and_says_so_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    other_project_folder = eval_account.project_folder.parent / "-Users-ada-other"
    other_project_folder.mkdir()
    other_session = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"
    (other_project_folder / f"{other_session}.jsonl").write_bytes(MORE_LINES)
    # Not a Claude session's transcript, so not copied.
    (other_project_folder / "notes.jsonl").write_bytes(b"{}\n")

    first_status = run_goodall(eval_account, monkeypatch, ["status"])
    second_status = run_goodall(eval_account, monkeypatch, ["status"])

    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
        f"-Users-ada-other/{other_session}.jsonl": MORE_LINES,
    }
    assert "Brought the copies of 2 Claude sessions' transcripts up to date in the log directory." in (
        first_status.everything_printed
    )
    assert "transcript" not in second_status.everything_printed
