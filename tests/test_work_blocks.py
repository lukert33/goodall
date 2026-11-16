"""Outside-in tests for work blocks: `goodall start`, `goodall stop` and `goodall status`.

Each test runs goodall the way a contributor would, through `cli.main`, with
a captured terminal, the fake runner and a clock the test controls. It then
checks only what that person, or the researcher afterwards, could observe:
the exit code, what was printed, the files in the log directory, and the
records and commits in the record repo.

The eval account is built inside tmp_path: a home folder, local settings, a
record repo that is a real git repository with an eval config committed in
it, and a log directory. git runs for real through the fake runner; every
other program (only `claude --version` here) is scripted.
"""

import io
import json
import os
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, records, work_blocks
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "Building a SaaS product to first recurring revenue with AI."
workstreams = ["engineering", "research", "go-to-market"]
intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""

# A Monday morning in November, when London is on GMT, so local time and
# UTC agree and the expected text is easy to read.
MONDAY_MORNING = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
OTHER_CLAUDE_SESSION = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"


def git(working_directory: Path, *arguments: str) -> str:
    """Run git for the test's own setup or inspection, and return what it printed."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments], cwd=working_directory, env=environment, capture_output=True, text=True, check=True
    )
    return completed.stdout


@dataclass
class HandSetClock:
    """A clock the test sets and moves forward by hand; goodall asks it for the time."""

    now: datetime = MONDAY_MORNING

    def __call__(self) -> datetime:
        return self.now

    def advance(self, by: timedelta) -> None:
        self.now = self.now + by


@dataclass
class EvalAccount:
    """A pretend eval account: its folders, and the clock and programs goodall will see."""

    home_folder: Path
    config_directory: Path
    record_repo: Path
    log_directory: Path
    clock: HandSetClock = field(default_factory=HandSetClock)
    claude_code_version_output: str | None = "2.3.4 (Claude Code)\n"

    def work_block_files(self) -> list[Path]:
        return sorted((self.record_repo / "work-blocks").glob("*.json"))

    def work_block_records(self) -> list[dict[str, Any]]:
        records = []
        for record_file in self.work_block_files():
            records.append(json.loads(record_file.read_text()))
        return records

    def commit_subjects(self) -> list[str]:
        return git(self.record_repo, "log", "--format=%s").splitlines()

    def files_in_log_directory(self) -> list[Path]:
        return sorted(path for path in self.log_directory.rglob("*") if path.is_file())


@pytest.fixture
def eval_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[EvalAccount]:
    """An eval account whose local settings, eval config and record repo are all in order, in London."""
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "evals" / "goodall-pilot-records"
    log_directory = tmp_path / "EvalLogs" / "goodall-pilot"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_folder / ".config"))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)

    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        'record_repo = "~/evals/goodall-pilot-records"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
    yield EvalAccount(home_folder, config_directory, record_repo, log_directory)
    monkeypatch.undo()
    time.tzset()


@dataclass
class GoodallRun:
    """What one run of goodall did, as the person who ran it would see it."""

    exit_code: int
    standard_output: str
    standard_error: str
    runner: FakeRunner

    @property
    def everything_printed(self) -> str:
        """Both streams, with each run of spaces and line breaks made one space, for phrase checks."""
        return " ".join((self.standard_output + "\n" + self.standard_error).split())


def run_goodall(eval_account: EvalAccount, arguments: list[str]) -> GoodallRun:
    """Run goodall in this eval account, with a captured terminal, the fake runner and the test's clock."""
    runner = FakeRunner()
    if eval_account.claude_code_version_output is None:
        runner.answer_not_found(["claude", "--version"])
    else:
        runner.answer(["claude", "--version"], standard_output=eval_account.claude_code_version_output)
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=eval_account.clock)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue(), runner)


# ---------------------------------------------------------------------------
# goodall start, and goodall status while a work block is active
# ---------------------------------------------------------------------------


def test_start_begins_a_work_block_on_the_main_workstream_and_status_then_reports_it(
    eval_account: EvalAccount,
) -> None:
    start = run_goodall(eval_account, ["start"])

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert start.standard_error == ""
    assert "Started a work block on research" in start.everything_printed
    assert "Mon 16 Nov 2026, 09:30 GMT" in start.everything_printed

    eval_account.clock.advance(timedelta(minutes=20))
    status = run_goodall(eval_account, ["status"])

    assert status.exit_code == exit_codes.SUCCESS, status.standard_error
    assert "A work block is active on research" in status.everything_printed
    assert "since Mon 16 Nov 2026, 09:30 GMT" in status.everything_printed


def test_start_while_a_work_block_is_active_changes_nothing_says_which_and_exits_0(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    files_before = {path: path.read_bytes() for path in eval_account.files_in_log_directory()}
    eval_account.clock.advance(timedelta(minutes=45))

    again = run_goodall(eval_account, ["start", "--workstream", "engineering"])

    assert again.exit_code == exit_codes.SUCCESS, again.standard_error
    assert "A work block is already active on research since Mon 16 Nov 2026, 09:30 GMT" in again.everything_printed
    assert "goodall stop" in again.everything_printed
    assert {path: path.read_bytes() for path in eval_account.files_in_log_directory()} == files_before


def test_start_on_a_workstream_the_eval_does_not_have_lists_the_valid_ones_and_starts_nothing(
    eval_account: EvalAccount,
) -> None:
    run = run_goodall(eval_account, ["start", "--workstream", "Research"])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_output == ""
    assert run.standard_error.startswith("FAIL  ")
    assert "engineering, research, go-to-market" in run.everything_printed
    assert "goodall start --workstream engineering" in run.standard_error
    assert eval_account.files_in_log_directory() == []


def marker_file(eval_account: EvalAccount) -> Path:
    return eval_account.log_directory / "work-blocks" / "active-work-block.json"


def test_the_marker_holds_the_running_work_block_and_only_this_account_can_read_it(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])

    marker = json.loads(marker_file(eval_account).read_text())
    assert marker["record_type"] == "work_block"
    assert marker["contributor"] == "ada-lovelace"
    assert marker["workstream"] == "research"
    assert marker["started_at"] == "2026-11-16T09:30:00Z"
    assert marker["ended_at"] is None
    assert marker["end_state"] is None
    assert marker["machine"]["claude_code_version"] == "2.3.4"
    assert marker["machine"]["goodall_version"] == "0.0.1"
    assert "billing" not in json.dumps(marker)
    assert stat.S_IMODE(marker_file(eval_account).stat().st_mode) == 0o600
    assert stat.S_IMODE(marker_file(eval_account).parent.stat().st_mode) == 0o700


def test_racing_starts_make_exactly_one_work_block(eval_account: EvalAccount) -> None:
    # Eight `goodall start` commands at once, as two terminals or agents
    # might: all succeed, exactly one starts a work block, and the others
    # report it as the active one.
    runs: list[GoodallRun] = []
    barrier = threading.Barrier(8)

    def start_when_all_are_ready() -> None:
        barrier.wait()
        runs.append(run_goodall(eval_account, ["start"]))

    threads = [threading.Thread(target=start_when_all_are_ready) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [run.exit_code for run in runs] == [exit_codes.SUCCESS] * 8
    started = [run for run in runs if "Started a work block" in run.everything_printed]
    assert len(started) == 1
    assert len(list(marker_file(eval_account).parent.glob("*.json"))) == 1


def test_creating_the_marker_is_won_by_exactly_one_of_two_racers(tmp_path: Path) -> None:
    # The step that decides the race, without the rest of goodall start
    # around it: both racers have already seen no marker.
    machine = records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None)
    first = records.build_work_block_record("ada-lovelace", "research", MONDAY_MORNING, machine)
    second = records.build_work_block_record("ada-lovelace", "engineering", MONDAY_MORNING, machine)

    assert work_blocks.create_marker(tmp_path, first) is True
    assert work_blocks.create_marker(tmp_path, second) is False
    running = work_blocks.read_marker(tmp_path)
    assert isinstance(running, work_blocks.RunningWorkBlock)
    assert running.record == first
    # The losing racer leaves no temporary file behind.
    assert sorted(path.name for path in (tmp_path / "work-blocks").iterdir() if path.is_file()) == [
        "active-work-block.json"
    ]


# ---------------------------------------------------------------------------
# goodall stop
# ---------------------------------------------------------------------------


def test_stop_ends_the_work_block_cleanly_and_records_it_with_its_claude_sessions(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    # The hook leaves heartbeats while the person works, with the Claude
    # session each came from.
    for minutes, claude_session in [(10, CLAUDE_SESSION), (50, OTHER_CLAUDE_SESSION), (70, CLAUDE_SESSION)]:
        work_blocks.record_heartbeat(
            eval_account.log_directory, MONDAY_MORNING + timedelta(minutes=minutes), claude_session
        )
    eval_account.clock.advance(timedelta(hours=2, minutes=5))

    stop = run_goodall(eval_account, ["stop"])

    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    assert stop.standard_error == ""
    assert "Stopped your work block on research" in stop.everything_printed
    assert "Mon 16 Nov 2026, 11:35 GMT" in stop.everything_printed
    [record] = eval_account.work_block_records()
    assert record["contributor"] == "ada-lovelace"
    assert record["workstream"] == "research"
    assert record["started_at"] == "2026-11-16T09:30:00Z"
    assert record["ended_at"] == "2026-11-16T11:35:00Z"
    assert record["end_state"] == "clean"
    assert record["claude_sessions"] == [CLAUDE_SESSION, OTHER_CLAUDE_SESSION]
    assert record["machine"]["claude_code_version"] == "2.3.4"
    [record_file] = eval_account.work_block_files()
    assert eval_account.commit_subjects()[0] == f"Add work block record {record_file.stem}"
    assert str(record_file) in stop.standard_output
    assert not marker_file(eval_account).exists()

    status = run_goodall(eval_account, ["status"])
    assert status.exit_code == exit_codes.ERROR
    assert "No work block is active" in status.everything_printed


def test_stop_with_no_work_block_active_says_so_and_exits_1(eval_account: EvalAccount) -> None:
    stop = run_goodall(eval_account, ["stop"])

    assert stop.exit_code == exit_codes.ERROR
    assert stop.standard_error.startswith("FAIL  No work block is active, so there was nothing to stop.")
    assert "goodall start" in stop.standard_error
    assert eval_account.work_block_files() == []


def test_stop_whose_record_cannot_be_written_keeps_it_says_so_and_the_next_command_writes_it(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    # git has no name or email to commit with, so the record repo cannot take the record.
    git(eval_account.record_repo, "config", "--unset", "user.name")
    eval_account.clock.advance(timedelta(hours=1))

    stop = run_goodall(eval_account, ["stop"])

    assert stop.exit_code == exit_codes.ERROR
    assert stop.standard_output == ""
    assert "Your work block on research has ended, so goodall is no longer recording" in stop.everything_printed
    assert "Nothing is lost" in stop.everything_printed
    assert "git config --global user.name" in stop.everything_printed
    assert eval_account.work_block_files() == []
    [waiting_file] = (eval_account.log_directory / "work-blocks" / "unrecorded").glob("*.json")
    assert str(waiting_file) in stop.everything_printed
    assert not marker_file(eval_account).exists()

    git(eval_account.record_repo, "config", "user.name", "Ada Lovelace")
    eval_account.clock.advance(timedelta(minutes=5))
    status = run_goodall(eval_account, ["status"])

    assert status.exit_code == exit_codes.ERROR
    assert "Recorded your work block on research" in status.everything_printed
    [record] = eval_account.work_block_records()
    assert record["end_state"] == "clean"
    assert record["ended_at"] == "2026-11-16T10:30:00Z"
    assert not waiting_file.exists()


# ---------------------------------------------------------------------------
# A work block nobody stopped: the inferred end
# ---------------------------------------------------------------------------


def test_a_forgotten_work_block_is_closed_at_its_last_heartbeat_by_the_next_start(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    # A day of work with Claude Code: a heartbeat every half hour, in two
    # Claude sessions, the last at 17:55.
    heartbeat_at = MONDAY_MORNING
    while heartbeat_at < datetime(2026, 11, 16, 17, 30, tzinfo=UTC):
        heartbeat_at = heartbeat_at + timedelta(minutes=30)
        work_blocks.record_heartbeat(eval_account.log_directory, heartbeat_at, CLAUDE_SESSION)
    last_heartbeat = datetime(2026, 11, 16, 17, 55, tzinfo=UTC)
    assert work_blocks.record_heartbeat(eval_account.log_directory, last_heartbeat, OTHER_CLAUDE_SESSION) is not None
    # Nobody ran goodall stop. The next morning:
    eval_account.clock.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    start = run_goodall(eval_account, ["start"])

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert "was left running" in start.everything_printed
    assert "closed it at its last heartbeat, Mon 16 Nov 2026, 17:55 GMT" in start.everything_printed
    assert "Started a work block on research at Tue 17 Nov 2026, 09:00 GMT" in start.everything_printed
    [record] = eval_account.work_block_records()
    assert record["started_at"] == "2026-11-16T09:30:00Z"
    assert record["ended_at"] == "2026-11-16T17:55:00Z"
    assert record["end_state"] == "inferred"
    assert record["claude_sessions"] == [CLAUDE_SESSION, OTHER_CLAUDE_SESSION]
    assert json.loads(marker_file(eval_account).read_text())["started_at"] == "2026-11-17T09:00:00Z"


def test_a_heartbeat_never_revives_a_forgotten_work_block(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    the_next_morning = datetime(2026, 11, 17, 8, 45, tzinfo=UTC)

    # A hook fires before anyone has run goodall start again.
    assert work_blocks.record_heartbeat(eval_account.log_directory, the_next_morning, CLAUDE_SESSION) is None

    eval_account.clock.now = the_next_morning
    status = run_goodall(eval_account, ["status"])
    assert status.exit_code == exit_codes.ERROR
    assert "No work block is active" in status.everything_printed
    [record] = eval_account.work_block_records()
    assert record["end_state"] == "inferred"
    assert record["ended_at"] == "2026-11-16T09:30:00Z"
    assert record["claude_sessions"] == []


def test_a_work_block_idle_for_less_than_the_idle_limit_is_still_active(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    eval_account.clock.advance(work_blocks.IDLE_LIMIT - timedelta(minutes=1))

    status = run_goodall(eval_account, ["status"])

    assert status.exit_code == exit_codes.SUCCESS
    assert eval_account.work_block_files() == []
