"""Outside-in tests for `goodall delete`: a person deletes what one work block captured.

Each test runs goodall commands the way a person would, in this process,
with a captured terminal, the fake runner and a clock the test sets, on a
log directory in tmp_path and a real git record repo. Between the commands
the test writes what Claude Code would have written: synthetic session
files in a synthetic Claude config folder inside tmp_path. Every record is
synthetic.
"""

import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import agent_session_copies, cli, hook, records
from goodall.fake_runner import FakeRunner, PretendProgram
from goodall.terminal import Terminal

PROJECT_FOLDER_NAME = "-Users-ada-work"
DELETED_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
KEPT_SESSION = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"

MORNING_START = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
MORNING_END = MORNING_START + timedelta(hours=1)
AFTERNOON_START = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
AFTERNOON_END = AFTERNOON_START + timedelta(hours=1)

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


def git(working_directory: Path, *arguments: str) -> str:
    """Run git for the test's own setup and checks, free of any GIT_ variable the test run inherited."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments], cwd=working_directory, env=environment, capture_output=True, check=True, text=True
    )
    return completed.stdout


def make_eval_account(tmp_path: Path, account_mode: str | None = None) -> EvalAccount:
    """Local settings, a record repo holding the eval config, a log directory, and an empty Claude projects folder.

    `account_mode` adds that setting to local settings; None leaves it out,
    which means dedicated account mode.
    """
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
    if account_mode is not None:
        with (config_directory / "local-settings.toml").open("a") as settings_file:
            settings_file.write(f'account_mode = "{account_mode}"\n')
    claude_config_directory = tmp_path / "claude-config"
    eval_account = EvalAccount(config_directory, record_repo, log_directory, claude_config_directory)
    eval_account.projects_folder.mkdir(parents=True)
    return eval_account


def record_line(claude_session: str, moment: datetime) -> bytes:
    """One transcript record as the Claude family writes it: one JSON object on a line, with a `timestamp`."""
    record = {
        "type": "user",
        "sessionId": claude_session,
        "timestamp": moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {"content": "synthetic"},
    }
    return json.dumps(record).encode("utf-8") + b"\n"


def write_session_file(path: Path, contents: bytes, last_written_at: datetime) -> None:
    """Write a file Claude Code would have written, last changed at `last_written_at` (goodall reads that time)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, (last_written_at.timestamp(), last_written_at.timestamp()))


def write_claude_session(eval_account: EvalAccount, claude_session: str, moments: list[datetime]) -> Path:
    """Write one Claude session's transcript, a tool result and file-edit backups; return the transcript."""
    contents = b""
    for moment in moments:
        contents += record_line(claude_session, moment)
    transcript = eval_account.projects_folder / PROJECT_FOLDER_NAME / f"{claude_session}.jsonl"
    write_session_file(transcript, contents, moments[-1])
    tool_result = eval_account.projects_folder / PROJECT_FOLDER_NAME / claude_session / "tool-results" / "a.txt"
    write_session_file(tool_result, b"synthetic tool output\n", moments[-1])
    backup = eval_account.claude_config_directory / "file-history" / claude_session / "notes.md@v1"
    write_session_file(backup, b"synthetic backup\n", moments[-1])
    history = eval_account.claude_config_directory / "history.jsonl"
    previous_history = b""
    if history.exists():
        previous_history = history.read_bytes()
    write_session_file(history, previous_history + b'{"display":"synthetic prompt"}\n', moments[-1])
    return transcript


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
    runner.answer_background(
        [sys.executable, "-I", "-m", "goodall.telemetry_receiver"], lambda: PretendProgram(ready_line='{"port": 4318}')
    )
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: now)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


def run_hook(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch, claude_session: str, now: datetime) -> None:
    """Run `goodall hook Stop` at the moment `now`, as Claude Code would for `claude_session`: a heartbeat."""
    use_eval_account(eval_account, monkeypatch)
    standard_input = io.StringIO(json.dumps({"session_id": claude_session}))
    terminal = Terminal(standard_output=io.StringIO(), standard_error=io.StringIO(), standard_input=standard_input)
    assert hook.run_hook("Stop", terminal, lambda: now) == 0


def work_block_ids(eval_account: EvalAccount) -> list[str]:
    """The ids of the work blocks in the record repo, oldest first."""
    ids = []
    for record_file in sorted((eval_account.record_repo / "work-blocks").glob("*.json")):
        ids.append(record_file.stem)
    return ids


def record_a_morning_work_block(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch) -> str:
    """Start, work in one Claude session, leave telemetry, and stop; return the work block's id."""
    run_goodall(eval_account, monkeypatch, ["start"], MORNING_START)
    write_claude_session(eval_account, DELETED_SESSION, [MORNING_START + timedelta(minutes=5)])
    run_hook(eval_account, monkeypatch, DELETED_SESSION, MORNING_START + timedelta(minutes=6))
    [telemetry_directory] = list((eval_account.log_directory / "telemetry").iterdir())
    (telemetry_directory / "logs.jsonl").write_bytes(b'{"synthetic": "telemetry"}\n')
    stop = run_goodall(eval_account, monkeypatch, ["stop"], MORNING_END)
    assert stop.exit_code == 0, stop.standard_error
    [work_block_id] = work_block_ids(eval_account)
    return work_block_id


def files_under(folder: Path) -> list[str]:
    """Every file under `folder`, by its path relative to `folder`."""
    found: list[str] = []
    if not folder.exists():
        return found
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found.append(str(path.relative_to(folder)))
    return found


# ---------------------------------------------------------------------------


def test_deleting_a_work_block_removes_what_it_captured_and_leaves_a_record_saying_only_that_it_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    assert files_under(eval_account.copied_transcripts) != []
    commits_before = git(eval_account.record_repo, "rev-list", "--count", "HEAD")

    deleted_at = AFTERNOON_START
    delete = run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], deleted_at)

    assert delete.exit_code == 0, delete.everything_printed
    assert files_under(eval_account.copied_transcripts) == []
    assert files_under(eval_account.log_directory / "claude-config") == [
        # The prompt history is shared by every Claude session, so it is kept and the output says so.
        f"{only_child(eval_account.log_directory / 'claude-config')}/history.jsonl"
    ]
    assert files_under(eval_account.log_directory / "telemetry") == []
    assert files_under(eval_account.log_directory / "work-blocks") == []
    record_file = eval_account.record_repo / "work-blocks" / f"{work_block_id}.json"
    assert json.loads(record_file.read_text()) == {
        "schema_version": 1,
        "record_type": "deleted_work_block",
        "id": work_block_id,
        "contributor": "ada-lovelace",
        "deleted_at": "2026-11-16T14:00:00Z",
        "other_work_blocks_lost_transcripts": [],
    }
    # Committed, and git history left alone: the old record is still in it.
    assert git(eval_account.record_repo, "status", "--porcelain") == ""
    assert int(git(eval_account.record_repo, "rev-list", "--count", "HEAD")) == int(commits_before) + 1
    assert "research" in git(eval_account.record_repo, "show", f"HEAD~1:work-blocks/{work_block_id}.json")
    assert "Deleted what work block" in delete.everything_printed
    assert "history.jsonl" in delete.everything_printed
    assert "still keep their own files" in delete.everything_printed


def only_child(folder: Path) -> str:
    [child] = list(folder.iterdir())
    return child.name


def test_without_the_confirming_flag_nothing_is_deleted_and_the_command_to_run_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    log_files_before = files_under(eval_account.log_directory)
    record_before = (eval_account.record_repo / "work-blocks" / f"{work_block_id}.json").read_text()

    preview = run_goodall(eval_account, monkeypatch, ["delete", work_block_id], AFTERNOON_START)

    assert preview.exit_code == 2
    assert files_under(eval_account.log_directory) == log_files_before
    assert (eval_account.record_repo / "work-blocks" / f"{work_block_id}.json").read_text() == record_before
    assert "Nothing has been deleted" in preview.everything_printed
    assert "Agent sessions 1 (3 files)" in preview.everything_printed
    assert f"goodall delete {work_block_id} --delete-captured-data" in preview.standard_output
    # Counts and kinds only: nothing read from a transcript is ever printed.
    assert "synthetic" not in preview.everything_printed


def test_without_the_flag_json_describes_what_would_be_deleted_in_a_stable_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)

    preview = run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--json"], AFTERNOON_START)

    assert preview.exit_code == 2
    document = json.loads(preview.standard_output)
    assert sorted(document) == [
        "agent_sessions",
        "already_deleted",
        "confirm_command",
        "deleted",
        "failures",
        "kinds",
        "left_in_place",
        "other_work_blocks_lost_transcripts",
        "record",
        "warnings",
        "work_block",
    ]
    assert document["deleted"] is False
    assert document["agent_sessions"] == [{"app_family": "Claude", "id": DELETED_SESSION}]
    assert {"kind": "agent_session_copies", "files": 3} in document["kinds"]
    assert document["confirm_command"] == f"goodall delete {work_block_id} --delete-captured-data"


def test_a_transcript_shared_with_another_work_block_is_deleted_whole_and_that_work_block_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    morning_id = record_a_morning_work_block(eval_account, monkeypatch)
    # In the afternoon the person resumes the same Claude session, and works in another.
    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START)
    write_claude_session(
        eval_account, DELETED_SESSION, [MORNING_START + timedelta(minutes=5), AFTERNOON_START + timedelta(minutes=5)]
    )
    write_claude_session(eval_account, KEPT_SESSION, [AFTERNOON_START + timedelta(minutes=10)])
    run_goodall(eval_account, monkeypatch, ["stop"], AFTERNOON_END)
    afternoon_id = work_block_ids(eval_account)[1]

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", morning_id, "--delete-captured-data", "--json"], AFTERNOON_END
    )

    assert delete.exit_code == 0, delete.everything_printed
    document = json.loads(delete.standard_output)
    assert document["other_work_blocks_lost_transcripts"] == [afternoon_id]
    assert sorted(files_under(eval_account.copied_transcripts)) == [
        f"{PROJECT_FOLDER_NAME}/{KEPT_SESSION}.jsonl",
        f"{PROJECT_FOLDER_NAME}/{KEPT_SESSION}/tool-results/a.txt",
    ]
    tombstone = json.loads((eval_account.record_repo / "work-blocks" / f"{morning_id}.json").read_text())
    assert tombstone["other_work_blocks_lost_transcripts"] == [afternoon_id]


def test_a_deleted_transcript_is_not_copied_again_when_a_later_work_block_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    morning_id = record_a_morning_work_block(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["delete", morning_id, "--delete-captured-data"], AFTERNOON_START)

    # The person resumes the deleted Claude session in a later work block.
    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START)
    write_claude_session(
        eval_account, DELETED_SESSION, [MORNING_START + timedelta(minutes=5), AFTERNOON_START + timedelta(minutes=5)]
    )
    stop = run_goodall(eval_account, monkeypatch, ["stop"], AFTERNOON_END)

    assert stop.exit_code == 0, stop.everything_printed
    assert files_under(eval_account.copied_transcripts) == []


def test_the_active_work_block_cannot_be_deleted_and_the_next_step_is_to_stop_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    run_goodall(eval_account, monkeypatch, ["start"], MORNING_START)
    [marker_id] = [path.name for path in (eval_account.log_directory / "telemetry").iterdir()]

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", marker_id, "--delete-captured-data"], MORNING_START + timedelta(minutes=5)
    )

    assert delete.exit_code == 1
    assert "goodall stop" in delete.everything_printed
    assert (eval_account.log_directory / "work-blocks" / "active-work-block.json").exists()


def test_deleting_a_work_block_again_says_it_is_already_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START)

    again = run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_END)

    assert again.exit_code == 0
    assert "already deleted" in again.everything_printed


def test_an_id_that_is_not_a_work_block_id_is_a_usage_error_and_is_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)

    delete = run_goodall(eval_account, monkeypatch, ["delete", "ada@example.org"], MORNING_START)

    assert delete.exit_code == 2
    assert "ada@example.org" not in delete.everything_printed


def test_a_work_block_id_with_no_work_block_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_account = make_eval_account(tmp_path)

    delete = run_goodall(eval_account, monkeypatch, ["delete", "20261116T093000Z-ada-lovelace-7f3a9c2e"], MORNING_START)

    assert delete.exit_code == 1
    assert "No work block" in delete.everything_printed


def test_a_work_block_whose_copy_is_still_waiting_has_its_agent_sessions_kept_from_ever_being_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    # As if the copy at goodall stop had failed: the note is back and nothing is copied.
    shutil.rmtree(eval_account.copied_transcripts)
    agent_session_copies.note_work_block_to_copy(eval_account.log_directory, work_block_id, MORNING_START, MORNING_END)

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START
    )
    status = run_goodall(eval_account, monkeypatch, ["status"], AFTERNOON_START + timedelta(minutes=1))

    assert delete.exit_code == 0, delete.everything_printed
    assert agent_session_copies.work_blocks_to_copy(eval_account.log_directory) == []
    assert agent_session_copies.read_deleted_agent_sessions(eval_account.log_directory) == {("Claude", DELETED_SESSION)}
    assert "Copied" not in status.everything_printed
    assert files_under(eval_account.copied_transcripts) == []


def test_a_work_block_whose_record_is_still_waiting_gets_its_deleted_record_in_the_record_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    # As if the record repo had not taken the record yet: it waits in the log directory.
    record_file = eval_account.record_repo / "work-blocks" / f"{work_block_id}.json"
    waiting_file = eval_account.log_directory / "work-blocks" / "unrecorded" / f"{work_block_id}.json"
    waiting_file.write_text(record_file.read_text())
    git(eval_account.record_repo, "rm", "--quiet", f"work-blocks/{work_block_id}.json")
    git(eval_account.record_repo, "commit", "--quiet", "-m", "Take the record out again")

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START
    )

    assert delete.exit_code == 0, delete.everything_printed
    assert not waiting_file.exists()
    assert json.loads(record_file.read_text())["record_type"] == "deleted_work_block"
    assert git(eval_account.record_repo, "status", "--porcelain") == ""


def test_fixes_go_with_the_record_and_journal_entries_and_activitywatch_copies_stay_and_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    fix = run_goodall(eval_account, monkeypatch, ["fixblock", work_block_id, "--workstream", "research"], MORNING_END)
    assert fix.exit_code == 0, fix.everything_printed
    entry = records.build_journal_entry("ada-lovelace", MORNING_END, "Chose Postgres.", work_block=work_block_id)
    entry_file = eval_account.record_repo / records.relative_path_for(entry)
    entry_file.parent.mkdir()
    entry_file.write_text(records.render_record(entry))
    git(eval_account.record_repo, "add", ".")
    git(eval_account.record_repo, "commit", "--quiet", "-m", "Add a journal entry")
    activitywatch_copy = eval_account.log_directory / "activitywatch" / "20261116T103000.000000Z" / "buckets.json"
    activitywatch_copy.parent.mkdir(parents=True)
    activitywatch_copy.write_text("{}\n")

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data", "--json"], AFTERNOON_START
    )

    assert delete.exit_code == 0, delete.everything_printed
    document = json.loads(delete.standard_output)
    assert {"kind": "work_block_fixes", "files": 1} in document["kinds"]
    assert list((eval_account.record_repo / "work-block-fixes").glob("*.json")) == []
    assert entry_file.exists()
    assert activitywatch_copy.exists()
    left_kinds = [left["kind"] for left in document["left_in_place"]]
    assert "entries_left" in left_kinds
    assert "activitywatch_copies_left" in left_kinds
    assert git(eval_account.record_repo, "status", "--porcelain") == ""


def test_a_copy_goodall_cannot_read_stops_the_deletion_before_anything_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    copied_transcript = eval_account.copied_transcripts / PROJECT_FOLDER_NAME / f"{DELETED_SESSION}.jsonl"
    log_files_before = files_under(eval_account.log_directory)
    copied_transcript.chmod(0)
    try:
        delete = run_goodall(
            eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START
        )
    finally:
        copied_transcript.chmod(0o600)

    assert delete.exit_code == 1
    assert "Nothing was deleted" in delete.everything_printed
    assert files_under(eval_account.log_directory) == log_files_before


# ---------------------------------------------------------------------------
# Deleting, pausing and the account mode together (ADR 0021)


def pause_file(eval_account: EvalAccount) -> Path:
    return eval_account.log_directory / "work-blocks" / "pause.json"


def record_a_morning_work_block_and_pause(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch) -> str:
    """Start, work in one Claude session, and pause; return the paused work block's id."""
    run_goodall(eval_account, monkeypatch, ["start"], MORNING_START)
    write_claude_session(eval_account, DELETED_SESSION, [MORNING_START + timedelta(minutes=5)])
    run_hook(eval_account, monkeypatch, DELETED_SESSION, MORNING_START + timedelta(minutes=6))
    pause = run_goodall(eval_account, monkeypatch, ["pause"], MORNING_END)
    assert pause.exit_code == 0, pause.standard_error
    [work_block_id] = work_block_ids(eval_account)
    return work_block_id


def test_deleting_the_paused_work_block_removes_the_pause_so_the_next_work_block_continues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block_and_pause(eval_account, monkeypatch)
    assert json.loads(pause_file(eval_account).read_text())["work_block"] == work_block_id

    preview = run_goodall(eval_account, monkeypatch, ["delete", work_block_id], AFTERNOON_START)

    # The preview changes nothing, the pause included.
    assert preview.exit_code == 2
    assert pause_file(eval_account).exists()

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START
    )

    assert delete.exit_code == 0, delete.everything_printed
    # The pause named the deleted work block, and when it was paused: it is
    # part of what goodall kept about that work block, so it goes too.
    assert not pause_file(eval_account).exists()
    assert files_under(eval_account.log_directory / "work-blocks") == []
    status = run_goodall(eval_account, monkeypatch, ["status", "--json"], AFTERNOON_START + timedelta(minutes=1))
    document = json.loads(status.standard_output)
    assert document["paused"] is False
    assert document["paused_work_block"] is None

    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START + timedelta(minutes=5))
    run_goodall(eval_account, monkeypatch, ["stop"], AFTERNOON_END)
    [_, afternoon_id] = work_block_ids(eval_account)
    afternoon_record = json.loads((eval_account.record_repo / "work-blocks" / f"{afternoon_id}.json").read_text())
    assert "continues_work_block" not in afternoon_record


def test_deleting_a_work_block_leaves_a_pause_that_names_another_work_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    morning_id = record_a_morning_work_block(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START)
    run_goodall(eval_account, monkeypatch, ["pause"], AFTERNOON_END)
    [_, afternoon_id] = work_block_ids(eval_account)
    pause_before = pause_file(eval_account).read_text()

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", morning_id, "--delete-captured-data"], AFTERNOON_END + timedelta(hours=1)
    )

    assert delete.exit_code == 0, delete.everything_printed
    assert pause_file(eval_account).read_text() == pause_before
    assert json.loads(pause_before)["work_block"] == afternoon_id


@pytest.mark.parametrize("account_mode", ["dedicated", "mixed"])
def test_deleting_a_work_block_another_continues_leaves_that_record_untouched_and_its_pointer_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_mode: str
) -> None:
    eval_account = make_eval_account(tmp_path, account_mode=account_mode)
    paused_id = record_a_morning_work_block_and_pause(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START)
    run_goodall(eval_account, monkeypatch, ["stop"], AFTERNOON_END)
    [_, continuing_id] = work_block_ids(eval_account)
    paused_file = eval_account.record_repo / "work-blocks" / f"{paused_id}.json"
    continuing_file = eval_account.record_repo / "work-blocks" / f"{continuing_id}.json"
    continuing_before = continuing_file.read_bytes()
    # Both fields are on the records before the deletion.
    assert json.loads(paused_file.read_text())["account_mode"] == account_mode
    assert json.loads(continuing_before)["continues_work_block"] == paused_id
    assert json.loads(continuing_before)["account_mode"] == account_mode

    delete = run_goodall(
        eval_account, monkeypatch, ["delete", paused_id, "--delete-captured-data"], AFTERNOON_END + timedelta(hours=1)
    )

    assert delete.exit_code == 0, delete.everything_printed
    # The continuing work block's record is not rewritten: its pointer still
    # names a file, which now says only that the work block was deleted.
    assert continuing_file.read_bytes() == continuing_before
    assert git(eval_account.record_repo, "status", "--porcelain") == ""
    deleted = json.loads(paused_file.read_text())
    assert deleted["id"] == json.loads(continuing_before)["continues_work_block"]
    # Neither the account mode nor a pointer to another work block survives.
    assert deleted == {
        "schema_version": 1,
        "record_type": "deleted_work_block",
        "id": paused_id,
        "contributor": "ada-lovelace",
        "deleted_at": "2026-11-16T16:00:00Z",
        "other_work_blocks_lost_transcripts": [],
    }
    # The record repo still reads: status and a later start are not upset by
    # a pointer to a deleted work block.
    status = run_goodall(eval_account, monkeypatch, ["status", "--json"], AFTERNOON_END + timedelta(hours=2))
    assert json.loads(status.standard_output)["failures"] == []


def test_deleting_a_work_block_that_continues_another_leaves_a_record_without_the_pointer_or_the_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path, account_mode="mixed")
    paused_id = record_a_morning_work_block_and_pause(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START)
    run_goodall(eval_account, monkeypatch, ["stop"], AFTERNOON_END)
    [_, continuing_id] = work_block_ids(eval_account)
    paused_file = eval_account.record_repo / "work-blocks" / f"{paused_id}.json"
    paused_before = paused_file.read_bytes()

    delete = run_goodall(
        eval_account,
        monkeypatch,
        ["delete", continuing_id, "--delete-captured-data"],
        AFTERNOON_END + timedelta(hours=1),
    )

    assert delete.exit_code == 0, delete.everything_printed
    deleted_text = (eval_account.record_repo / "work-blocks" / f"{continuing_id}.json").read_text()
    assert sorted(json.loads(deleted_text)) == [
        "contributor",
        "deleted_at",
        "id",
        "other_work_blocks_lost_transcripts",
        "record_type",
        "schema_version",
    ]
    assert "account_mode" not in deleted_text
    assert "continues_work_block" not in deleted_text
    assert paused_id not in deleted_text
    assert paused_file.read_bytes() == paused_before


def test_a_deleted_work_block_record_refuses_an_account_mode_or_a_work_block_it_continues() -> None:
    deleted = records.build_deleted_work_block(
        "20261116T093000Z-ada-lovelace-0a1b2c3d", "ada-lovelace", AFTERNOON_START, []
    )
    values = json.loads(records.render_record(deleted))
    values["account_mode"] = "mixed"
    values["continues_work_block"] = "20261116T080000Z-ada-lovelace-1a2b3c4d"

    parsed = records.parse_deleted_work_block(json.dumps(values))

    assert sorted(problem.field for problem in parsed.problems) == ["account_mode", "continues_work_block"]


def test_what_a_work_block_captured_is_found_the_same_way_in_both_account_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previews = {}
    files_left = {}
    for account_mode in ["dedicated", "mixed"]:
        account_folder = tmp_path / account_mode
        account_folder.mkdir()
        eval_account = make_eval_account(account_folder, account_mode=account_mode)
        work_block_id = record_a_morning_work_block(eval_account, monkeypatch)

        preview = run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--json"], AFTERNOON_START)
        delete = run_goodall(
            eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START
        )

        assert delete.exit_code == 0, delete.everything_printed
        document = json.loads(preview.standard_output)
        previews[account_mode] = (document["kinds"], document["agent_sessions"])
        files_left[account_mode] = (
            files_under(eval_account.copied_transcripts),
            files_under(eval_account.log_directory / "telemetry"),
            files_under(eval_account.log_directory / "work-blocks"),
        )

    assert previews["mixed"] == previews["dedicated"]
    assert {"kind": "agent_session_copies", "files": 3} in previews["mixed"][0]
    assert previews["mixed"][1] == [{"app_family": "Claude", "id": DELETED_SESSION}]
    assert files_left["mixed"] == files_left["dedicated"] == ([], [], [])


# ---------------------------------------------------------------------------
# The timeline, which is derived from every raw record


def record_an_afternoon_work_block(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch) -> GoodallRun:
    """Start, work in another Claude session, and stop; return what `goodall stop` printed."""
    run_goodall(eval_account, monkeypatch, ["start"], AFTERNOON_START)
    write_claude_session(eval_account, KEPT_SESSION, [AFTERNOON_START + timedelta(minutes=5)])
    run_hook(eval_account, monkeypatch, KEPT_SESSION, AFTERNOON_START + timedelta(minutes=6))
    stop = run_goodall(eval_account, monkeypatch, ["stop"], AFTERNOON_END)
    assert stop.exit_code == 0, stop.standard_error
    return stop


def test_after_a_deletion_a_later_goodall_stop_and_a_rebuild_report_no_timeline_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], MORNING_END)

    stop = record_an_afternoon_work_block(eval_account, monkeypatch)
    rebuild = run_goodall(eval_account, monkeypatch, ["timeline", "--rebuild", "--json"], AFTERNOON_END)

    assert "could not be read" not in stop.everything_printed
    assert "timeline" not in stop.standard_error
    assert rebuild.exit_code == 0, rebuild.everything_printed
    assert json.loads(rebuild.standard_output)["warnings"] == []


def test_goodall_fixblock_says_a_deleted_work_block_was_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    work_block_id = record_a_morning_work_block(eval_account, monkeypatch)
    run_goodall(eval_account, monkeypatch, ["delete", work_block_id, "--delete-captured-data"], AFTERNOON_START)
    commits_before = git(eval_account.record_repo, "rev-list", "--count", "HEAD")

    fixblock = run_goodall(
        eval_account, monkeypatch, ["fixblock", work_block_id, "--workstream", "research"], AFTERNOON_END
    )

    assert fixblock.exit_code == 1
    assert f"What work block {work_block_id} captured was deleted" in fixblock.everything_printed
    assert "No work block record with the id" not in fixblock.everything_printed
    assert git(eval_account.record_repo, "rev-list", "--count", "HEAD") == commits_before
