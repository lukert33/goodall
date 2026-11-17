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

and, as a listing of a real projects folder's file names showed (no file
was opened), `custom-title.json` and `tool-results/` inside `<session id>/`.
"""

import fcntl
import io
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


# A PNG's first bytes: binary, not text, and not valid UTF-8.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"


def write_whole_claude_session_folder(eval_account: EvalAccount) -> dict[str, bytes]:
    """Fill `<session id>/` with every kind of file Claude Code 2.1.x was seen to keep there, and more.

    The names follow what a Claude projects folder holds (listed by name
    only): `custom-title.json` directly inside, `subagents/`, and
    `tool-results/` with text, images and a `pdf-<id>/` folder of pages.
    A folder nested a dozen levels deep and an unknown kind of file stand
    for whatever a later Claude Code version adds. Returns the files as the
    copy should hold them.
    """
    session_folder = eval_account.project_folder / CLAUDE_SESSION
    deep_folder = Path("future-feature") / "/".join(f"level-{number}" for number in range(12))
    session_files = {
        Path("custom-title.json"): b'{"title":"Fix the tests"}',
        Path("subagents") / "agent-a1b2c3.jsonl": b'{"type":"user","isSidechain":true}\n',
        Path("subagents") / "agent-a1b2c3.meta.json": b'{"agentType":"Explore"}',
        Path("subagents") / "nested" / "agent-d4e5f6.jsonl": b'{"type":"user"}\n',
        Path("tool-results") / "toolu_01ABC.txt": b"3 passed\n",
        Path("tool-results") / "toolu_01DEF.png": PNG_BYTES,
        Path("tool-results") / "mcp-0f6c1d2e-page.html": b"<p>Caf\xc3\xa9</p>",
        Path("tool-results") / "pdf-5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3" / "page-1.jpg": b"\xff\xd8\xff\xe0",
        deep_folder / "something.unknown": b"\x00\x01\x02",
        Path(".hidden-file"): b"hidden\n",
    }
    expected = {}
    for relative_path, contents in session_files.items():
        (session_folder / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (session_folder / relative_path).write_bytes(contents)
        expected[f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/{relative_path}"] = contents
    return expected


def test_a_hook_copies_the_whole_claude_session_folder_byte_for_byte_in_claude_codes_layout(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    expected_session_files = write_whole_claude_session_folder(eval_account)

    completed = run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Read"))

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
        **expected_session_files,
    }
    for copied_path in eval_account.copied_transcripts.rglob("*"):
        expected_mode = 0o700 if copied_path.is_dir() else 0o600
        assert stat.S_IMODE(copied_path.stat().st_mode) == expected_mode, copied_path
    # Nothing failed, so nothing was noted for goodall status.
    assert not (eval_account.log_directory / "transcript-sync" / "failed-copies.jsonl").exists()


def test_a_session_file_rewritten_in_place_is_copied_again_and_its_earlier_copy_kept_aside(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    meta_file = eval_account.project_folder / CLAUDE_SESSION / "subagents" / "agent-a1b2c3.meta.json"
    meta_file.parent.mkdir(parents=True)
    meta_file.write_bytes(b'{"agentType":"Explore","status":"running"}')
    run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Agent"))
    # Rewritten in place, not appended to: the new contents do not start with the old.
    meta_file.write_bytes(b'{"agentType":"Explore","status":"done"}')

    completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))

    assert completed.returncode == 0
    meta_name = f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/subagents/agent-a1b2c3.meta.json"
    assert files_and_bytes(eval_account.copied_transcripts)[meta_name] == b'{"agentType":"Explore","status":"done"}'
    assert superseded_copies(eval_account) == {meta_name: b'{"agentType":"Explore","status":"running"}'}


def test_a_session_file_that_disappears_from_claude_codes_folder_keeps_its_copy(tmp_path: Path) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    tool_result = eval_account.project_folder / CLAUDE_SESSION / "tool-results" / "toolu_01ABC.txt"
    tool_result.parent.mkdir(parents=True)
    tool_result.write_bytes(b"3 passed\n")
    run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Bash"))
    tool_result.unlink()
    tool_result.parent.rmdir()

    completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))

    assert completed.returncode == 0
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/tool-results/toolu_01ABC.txt": b"3 passed\n",
    }
    assert not (eval_account.log_directory / "transcript-sync" / "failed-copies.jsonl").exists()


def test_links_special_files_and_unreadable_files_in_a_session_folder_are_skipped_and_noted_and_the_rest_copied(
    tmp_path: Path, locked_folders: list[Path]
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    session_folder = eval_account.project_folder / CLAUDE_SESSION
    tool_results = session_folder / "tool-results"
    tool_results.mkdir(parents=True)
    (tool_results / "toolu_01ABC.txt").write_bytes(b"3 passed\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "private.txt").write_bytes(b"private\n")
    # A link to a file outside the projects folder, and a link to a folder.
    (tool_results / "toolu_01LNK.txt").symlink_to(elsewhere / "private.txt")
    (session_folder / "linked-folder").symlink_to(elsewhere)
    # A named pipe: opening it to read would wait for a writer forever.
    os.mkfifo(tool_results / "toolu_01FIF.txt")
    unreadable_file = tool_results / "toolu_01UNR.txt"
    unreadable_file.write_bytes(b"secret\n")
    unreadable_file.chmod(0)
    unreadable_folder = session_folder / "unreadable-folder"
    unreadable_folder.mkdir()
    (unreadable_folder / "inside.txt").write_bytes(b"inside\n")
    unreadable_folder.chmod(0)
    locked_folders.append(unreadable_folder)

    try:
        completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))
    finally:
        unreadable_file.chmod(0o600)

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/tool-results/toolu_01ABC.txt": b"3 passed\n",
    }
    failed_copies = eval_account.log_directory / "transcript-sync" / "failed-copies.jsonl"
    problem = json.loads(failed_copies.read_text())["problem"]
    for skipped_name in ["toolu_01LNK.txt", "linked-folder", "toolu_01FIF.txt", "toolu_01UNR.txt", "unreadable-folder"]:
        assert skipped_name in problem
    assert "private" not in problem
    assert "secret" not in problem


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
    # Each refusal is noted for goodall status, without the path's contents.
    failed_copies = eval_account.log_directory / "transcript-sync" / "failed-copies.jsonl"
    problems = [json.loads(line)["problem"] for line in failed_copies.read_text().splitlines()]
    assert len(problems) == len(refused_paths)
    for problem in problems:
        assert problem.startswith("TranscriptPathRefused: ")
        assert "not a transcript" not in problem


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


# Passed to run_goodall to say the terminal's CLAUDE_CONFIG_DIR is the one the hooks use.
SAME_AS_HOOKS = Path("same-as-hooks")


def run_goodall(
    eval_account: EvalAccount,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    terminal_claude_config_directory: Path | None = SAME_AS_HOOKS,
) -> GoodallRun:
    """Run a goodall command in this process, with a captured terminal, the fake runner and a fixed clock.

    The terminal's CLAUDE_CONFIG_DIR is the hooks' unless
    `terminal_claude_config_directory` names another folder, or is None
    for a terminal where it is not set at all.
    """
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(eval_account.config_directory))
    if terminal_claude_config_directory is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    elif terminal_claude_config_directory == SAME_AS_HOOKS:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(eval_account.claude_config_directory))
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(terminal_claude_config_directory))
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


def test_a_hook_with_the_log_directory_unplugged_stays_silent_and_status_copies_once_it_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    unplugged = tmp_path / "unplugged"
    eval_account.log_directory.rename(unplugged)

    completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
    # No stray folder was made where the log directory should be.
    assert not eval_account.log_directory.exists()

    unplugged.rename(eval_account.log_directory)
    status = run_goodall(eval_account, monkeypatch, ["status"])

    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
    }
    assert "Brought the copy of 1 Claude session's transcript up to date" in status.everything_printed


@pytest.fixture
def locked_folders() -> Iterator[list[Path]]:
    """Folders a test makes read-only; their permissions are given back afterwards so pytest can delete them."""
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere, so a folder cannot be made read-only to it")
    folders: list[Path] = []
    yield folders
    for folder in folders:
        folder.chmod(0o700)


def test_a_copy_that_fails_in_a_hook_is_silent_but_kept_for_status_to_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked_folders: list[Path]
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    eval_account.copied_transcripts.mkdir()
    eval_account.copied_transcripts.chmod(0o500)
    locked_folders.append(eval_account.copied_transcripts)

    completed = run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
    failed_copies = eval_account.log_directory / "transcript-sync" / "failed-copies.jsonl"
    failure = json.loads(failed_copies.read_text())
    assert failure["event"] == "Stop"
    assert failure["claude_session"] == CLAUDE_SESSION
    assert "Permission denied" in failure["problem"]

    # While the folder is still read-only, status warns, and keeps the hook's record.
    status = run_goodall(eval_account, monkeypatch, ["status", "--json"])
    warning_kinds = [warning["kind"] for warning in json.loads(status.standard_output)["warnings"]]
    assert warning_kinds == ["transcripts_not_copied"]
    assert failed_copies.exists()

    # Once it can be written, status copies, says the hooks had failed, and says it only once.
    eval_account.copied_transcripts.chmod(0o700)
    fixed_status = run_goodall(eval_account, monkeypatch, ["status"])
    later_status = run_goodall(eval_account, monkeypatch, ["status"])

    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
    }
    assert "could not copy a transcript 1 time when a Claude Code hook fired" in fixed_status.everything_printed
    assert "transcript" not in later_status.everything_printed
    assert not failed_copies.exists()


def synthetic_transcript_bytes(size: int) -> bytes:
    """About `size` bytes of transcript-like JSON lines, each one different, so a mix-up would show."""
    lines = []
    total = 0
    line_number = 0
    while total < size:
        line = b'{"type":"assistant","n":%d,"message":{"content":"%s"}}\n' % (line_number, b"x" * 900)
        lines.append(line)
        total += len(line)
        line_number += 1
    return b"".join(lines)


def test_a_hook_killed_mid_copy_leaves_the_old_copy_or_the_new_one_and_status_clears_its_leftovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    old_transcript = synthetic_transcript_bytes(1024 * 1024)
    eval_account.transcript.write_bytes(old_transcript)
    run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))
    new_transcript = old_transcript + synthetic_transcript_bytes(63 * 1024 * 1024)
    eval_account.transcript.write_bytes(new_transcript)
    copy = eval_account.copied_transcripts / PROJECT_FOLDER_NAME / f"{CLAUDE_SESSION}.jsonl"
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(eval_account.config_directory)
    environment["CLAUDE_CONFIG_DIR"] = str(eval_account.claude_config_directory)

    # SIGKILL, which nothing can catch, the moment the hook's temporary file
    # appears: the copy is then under way and not yet renamed into place.
    for _ in range(5):
        hook_process = subprocess.Popen(
            [GOODALL_COMMAND, "hook", "Stop"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(hook_input(eval_account, "Stop").encode())
        hook_process.stdin.close()
        leftovers_before = len(list(copy.parent.glob(".*.goodall-tmp")))
        while hook_process.poll() is None and len(list(copy.parent.glob(".*.goodall-tmp"))) == leftovers_before:
            pass
        hook_process.kill()
        hook_process.wait(timeout=30)

        assert copy.read_bytes() in (old_transcript, new_transcript)
        if copy.read_bytes() == new_transcript:
            # Start again from the old copy, so the next kill also lands mid-copy.
            copy.write_bytes(old_transcript)
    # At least one kill landed mid-copy, or this test proved nothing.
    assert len(list(copy.parent.glob(".*.goodall-tmp"))) > 0

    # Leftover temporary files from the killed hooks, made to look an hour old,
    # and one just made, as by a hook copying at this very moment.
    an_hour_ago = (MONDAY_MORNING - timedelta(hours=1)).timestamp()
    for leftover in copy.parent.glob(".*.goodall-tmp"):
        os.utime(leftover, (an_hour_ago, an_hour_ago))
    in_progress = copy.parent / f".{copy.name}.0000.goodall-tmp"
    in_progress.write_bytes(b"")
    os.utime(in_progress, (MONDAY_MORNING.timestamp(), MONDAY_MORNING.timestamp()))

    run_goodall(eval_account, monkeypatch, ["status"])

    assert copy.read_bytes() == new_transcript
    assert sorted(copy.parent.glob(".*.goodall-tmp")) == [in_progress]


def test_two_copies_of_one_claude_session_never_run_at_once(tmp_path: Path) -> None:
    # Claude Code runs the PostToolUse hooks of parallel tool calls at the
    # same moment. Two copies racing could each find the other's copy "not
    # the start" of their own and keep it aside for nothing, so one waits.
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    lock_folder = eval_account.log_directory / "transcript-sync" / "locks"
    lock_folder.mkdir(parents=True)
    environment = dict(os.environ)
    environment["GOODALL_CONFIG_DIR"] = str(eval_account.config_directory)
    environment["CLAUDE_CONFIG_DIR"] = str(eval_account.claude_config_directory)

    with (lock_folder / f"{CLAUDE_SESSION}.lock").open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        hook_process = subprocess.Popen([GOODALL_COMMAND, "hook", "Stop"], stdin=subprocess.PIPE, env=environment)
        assert hook_process.stdin is not None
        hook_process.stdin.write(hook_input(eval_account, "Stop").encode())
        hook_process.stdin.close()
        time.sleep(0.5)
        assert hook_process.poll() is None
        assert not eval_account.copied_transcripts.exists()
    # Closing the file released the lock.

    assert hook_process.wait(timeout=30) == 0
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES,
    }


# ---------------------------------------------------------------------------
# Out of the box: the sweep finds the folder Claude Code used, whatever the terminal says
# ---------------------------------------------------------------------------

OTHER_SESSION = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"


def test_status_from_a_terminal_without_claude_config_dir_sweeps_the_folder_the_hooks_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude Code runs with CLAUDE_CONFIG_DIR set, so its hooks do too; the
    # person's terminal has never heard of it.
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    run_hook(eval_account, "PostToolUse", hook_input(eval_account, "PostToolUse", tool_name="Bash"))
    # Then Claude Code is killed mid-turn: no more hooks, but more was written.
    with eval_account.transcript.open("ab") as transcript:
        transcript.write(MORE_LINES)
    # And another Claude session in the same folder was killed before any hook copied it.
    (eval_account.project_folder / f"{OTHER_SESSION}.jsonl").write_bytes(MORE_LINES)

    status = run_goodall(eval_account, monkeypatch, ["status"], terminal_claude_config_directory=None)

    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES + MORE_LINES,
        f"{PROJECT_FOLDER_NAME}/{OTHER_SESSION}.jsonl": MORE_LINES,
    }
    assert "Brought the copies of 2 Claude sessions' transcripts up to date" in status.everything_printed


def test_status_from_a_terminal_with_a_different_claude_config_dir_sweeps_both_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    run_hook(eval_account, "Stop", hook_input(eval_account, "Stop"))
    with eval_account.transcript.open("ab") as transcript:
        transcript.write(MORE_LINES)
    terminal_claude_config_directory = tmp_path / "terminal-claude-config"
    terminal_project_folder = terminal_claude_config_directory / "projects" / "-Users-ada-elsewhere"
    terminal_project_folder.mkdir(parents=True)
    (terminal_project_folder / f"{OTHER_SESSION}.jsonl").write_bytes(FIRST_LINES)

    status = run_goodall(
        eval_account, monkeypatch, ["status"], terminal_claude_config_directory=terminal_claude_config_directory
    )

    assert "Brought the copies of 2 Claude sessions' transcripts up to date" in status.everything_printed
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl": FIRST_LINES + MORE_LINES,
        f"-Users-ada-elsewhere/{OTHER_SESSION}.jsonl": FIRST_LINES,
    }


def test_start_sweeps_the_default_claude_folder_even_when_no_hook_ever_ran_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude Code with no CLAUDE_CONFIG_DIR writes to ~/.claude/projects;
    # killed before its first tool call, it fired no copying hook at all.
    eval_account = make_eval_account(tmp_path)
    default_project_folder = Path.home() / ".claude" / "projects" / PROJECT_FOLDER_NAME
    default_project_folder.mkdir(parents=True)
    (default_project_folder / f"{OTHER_SESSION}.jsonl").write_bytes(FIRST_LINES)

    start = run_goodall(eval_account, monkeypatch, ["start"])

    assert start.exit_code == 0, start.standard_error
    assert files_and_bytes(eval_account.copied_transcripts) == {
        f"{PROJECT_FOLDER_NAME}/{OTHER_SESSION}.jsonl": FIRST_LINES,
    }


def test_a_hook_remembers_the_claude_projects_folder_from_its_own_environment_never_from_its_input(
    tmp_path: Path,
) -> None:
    eval_account = make_eval_account(tmp_path)
    eval_account.transcript.write_bytes(FIRST_LINES)
    elsewhere_project_folder = tmp_path / "elsewhere" / "projects" / PROJECT_FOLDER_NAME
    elsewhere_project_folder.mkdir(parents=True)
    elsewhere_transcript = elsewhere_project_folder / f"{CLAUDE_SESSION}.jsonl"
    elsewhere_transcript.write_bytes(FIRST_LINES)

    run_hook(eval_account, "Stop", hook_input(eval_account, "Stop", transcript_path=str(elsewhere_transcript)))

    remembered_file = eval_account.log_directory / "transcript-sync" / "claude-projects-folders.json"
    remembered = json.loads(remembered_file.read_text())["claude_projects_folders"]
    assert remembered == [str((eval_account.claude_config_directory / "projects").resolve())]
    assert stat.S_IMODE(remembered_file.stat().st_mode) == 0o600
