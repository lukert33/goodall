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

import fcntl
import http.client
import io
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from goodall import (
    cli,
    exit_codes,
    records,
    telemetry_capture,
    telemetry_receipts,
    work_block_commands,
    work_blocks,
)
from goodall.fake_runner import (
    SIMULATED_START_TIME,
    FakeBackgroundPrograms,
    FakeRunner,
    InProcessTelemetryReceiver,
    PretendProgram,
    SimulatedProgram,
)
from goodall.runner import BackgroundProgram, RealRunner
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

# How every command that goodall runs to start its telemetry receiver begins.
RECEIVER_COMMAND_START = [sys.executable, "-I", "-m", "goodall.telemetry_receiver"]


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
    # The telemetry receivers goodall starts, simulated in this process
    # unless a test asks for real ones; shared by every command's runner.
    background: FakeBackgroundPrograms = field(default_factory=FakeBackgroundPrograms)
    # What the next simulated receiver does: None for a working one, or a
    # pretend program that exits at once, as a receiver whose port is taken does.
    failing_receiver: PretendProgram | None = None

    def simulated_receivers(self) -> list[SimulatedProgram]:
        """Every simulated receiver goodall started, oldest first."""
        return list(self.background.started.values())

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
    eval_account = EvalAccount(home_folder, config_directory, record_repo, log_directory)
    yield eval_account
    stop_every_receiver(eval_account)
    monkeypatch.undo()
    time.tzset()


def stop_every_receiver(eval_account: EvalAccount) -> None:
    """Stop every receiver a test left running, simulated or real, so none outlives its test."""
    eval_account.background.stop_all()
    for record_file in telemetry_capture.receiver_record_files(eval_account.log_directory):
        receiver_record = telemetry_capture.read_receiver_record(record_file)
        if receiver_record is None:
            continue
        program = BackgroundProgram.from_json(receiver_record.program)
        if program is not None and program.process_started_at != SIMULATED_START_TIME:
            RealRunner().stop_background(program, timeout_seconds=2)


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


def run_goodall(eval_account: EvalAccount, arguments: list[str], real_receiver: bool = False) -> GoodallRun:
    """Run goodall in this eval account, with a captured terminal, the fake runner and the test's clock.

    With `real_receiver`, the telemetry receiver `goodall start` runs is a
    real background process: this test interpreter running
    `python -m goodall.telemetry_receiver`.
    """
    real_programs = ["git"]
    if real_receiver:
        real_programs.append(sys.executable)
    runner = FakeRunner(real_programs=real_programs, background=eval_account.background)
    failing_receiver = eval_account.failing_receiver
    if failing_receiver is None:
        runner.answer_background(RECEIVER_COMMAND_START, InProcessTelemetryReceiver)
    else:
        runner.answer_background(RECEIVER_COMMAND_START, lambda: failing_receiver)
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


def test_stop_whose_record_cannot_be_written_exits_0_warns_and_the_next_command_writes_it(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    # git has no name or email to commit with, so the record repo cannot take the record.
    git(eval_account.record_repo, "config", "--unset", "user.name")
    eval_account.clock.advance(timedelta(hours=1))

    stop = run_goodall(eval_account, ["stop"])

    # The work block is ended and nothing is lost, so what was asked is done.
    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    assert stop.standard_output == ""
    assert stop.standard_error.startswith("WARN  Your work block on research is stopped.")
    assert "FAIL" not in stop.everything_printed
    assert "has not reached the record repo yet" in stop.everything_printed
    assert "Nothing is lost and nothing needs to be run again" in stop.everything_printed
    assert "the next time you run goodall start, goodall stop or goodall status" in stop.everything_printed
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


def test_stop_the_morning_after_exits_0_with_a_warning_when_the_closed_blocks_record_cannot_be_written(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    git(eval_account.record_repo, "config", "--unset", "user.name")
    eval_account.clock.now = datetime(2026, 11, 17, 8, 30, tzinfo=UTC)

    stop = run_goodall(eval_account, ["stop"])

    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    assert "was left running" in stop.everything_printed
    assert stop.standard_error.startswith("WARN  ")
    assert "FAIL" not in stop.everything_printed
    assert "nothing needs to be run again" in stop.everything_printed
    assert len(list((eval_account.log_directory / "work-blocks" / "unrecorded").glob("*.json"))) == 1


def test_start_exits_0_with_a_warning_when_a_closed_blocks_record_cannot_be_written(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    git(eval_account.record_repo, "config", "--unset", "user.name")
    eval_account.clock.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    start = run_goodall(eval_account, ["start"])

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert "Started a work block on research" in start.everything_printed
    assert start.standard_error.startswith("WARN  ")
    assert "FAIL" not in start.everything_printed


def test_status_exits_as_it_would_without_a_waiting_record_when_one_cannot_be_written(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    git(eval_account.record_repo, "config", "--unset", "user.name")
    eval_account.clock.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    status = run_goodall(eval_account, ["status"])

    # Exit 1 here is the usual "no work block is active"; the waiting record is only a warning.
    assert status.exit_code == exit_codes.ERROR
    assert "WARN  " in status.standard_error
    assert "FAIL" not in status.standard_error


def test_stop_the_morning_after_closes_the_forgotten_work_block_at_its_last_heartbeat_and_exits_0(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    eval_account.clock.now = datetime(2026, 11, 17, 8, 30, tzinfo=UTC)

    stop = run_goodall(eval_account, ["stop"])

    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    assert stop.standard_error == ""
    assert "was left running" in stop.everything_printed
    [record] = eval_account.work_block_records()
    assert record["end_state"] == "inferred"


# ---------------------------------------------------------------------------
# A marker goodall cannot read: the abnormal end
# ---------------------------------------------------------------------------


def damaged_markers(eval_account: EvalAccount) -> list[Path]:
    return sorted((eval_account.log_directory / "work-blocks" / "damaged-markers").glob("*"))


def test_a_damaged_marker_is_kept_aside_and_what_can_be_recovered_is_recorded_as_abnormal(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    work_blocks.record_heartbeat(eval_account.log_directory, MONDAY_MORNING + timedelta(minutes=40), CLAUDE_SESSION)
    # Something damaged the marker: its machine facts are gone.
    marker = json.loads(marker_file(eval_account).read_text())
    del marker["machine"]
    damaged_text = json.dumps(marker)
    marker_file(eval_account).write_text(damaged_text)
    eval_account.clock.advance(timedelta(hours=1))

    status = run_goodall(eval_account, ["status"])

    assert status.exit_code == exit_codes.ERROR
    assert "could not read the marker of your work block" in status.everything_printed
    [kept] = damaged_markers(eval_account)
    assert kept.read_text() == damaged_text
    assert str(kept) in status.everything_printed
    assert not marker_file(eval_account).exists()
    [record] = eval_account.work_block_records()
    assert record["id"] == marker["id"]
    assert record["workstream"] == "research"
    assert record["started_at"] == "2026-11-16T09:30:00Z"
    assert record["ended_at"] == "2026-11-16T10:10:00Z"
    assert record["end_state"] == "abnormal"
    assert record["claude_sessions"] == [CLAUDE_SESSION]

    start = run_goodall(eval_account, ["start"])
    assert start.exit_code == exit_codes.SUCCESS
    assert "Started a work block" in start.everything_printed


def test_a_marker_cut_short_is_kept_aside_and_recorded_with_an_unknown_workstream(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    marker_file(eval_account).write_text('{"schema_version": 1, "record_type": "work_bl')
    # The damage happened at 09:45; that is all the file can still say about when.
    damaged_at = (MONDAY_MORNING + timedelta(minutes=15)).timestamp()
    os.utime(marker_file(eval_account), (damaged_at, damaged_at))
    eval_account.clock.advance(timedelta(hours=1))

    start = run_goodall(eval_account, ["start"])

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert "could not read the marker of your work block" in start.everything_printed
    [kept] = damaged_markers(eval_account)
    assert kept.read_text() == '{"schema_version": 1, "record_type": "work_bl'
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    [record] = eval_account.work_block_records()
    assert record["workstream"] == "unknown"
    assert record["end_state"] == "abnormal"
    assert record["contributor"] == "ada-lovelace"
    assert record["started_at"] == "2026-11-16T09:45:00Z"
    assert "did not say which workstream" in start.everything_printed
    assert "recorded with the workstream unknown" in start.everything_printed
    assert "Started a work block" in start.everything_printed


def test_a_damaged_marker_naming_a_workstream_the_eval_does_not_have_is_recorded_as_unknown(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    marker = json.loads(marker_file(eval_account).read_text())
    marker["workstream"] = "Research!"
    marker_file(eval_account).write_text(json.dumps(marker))

    status = run_goodall(eval_account, ["status"])

    [record] = eval_account.work_block_records()
    assert record["id"] == marker["id"]
    assert record["workstream"] == "unknown"
    assert record["started_at"] == "2026-11-16T09:30:00Z"
    assert "Research!" not in status.everything_printed


def test_a_damaged_marker_that_gives_no_start_time_at_all_is_not_given_an_invented_one(
    eval_account: EvalAccount,
) -> None:
    eval_reading = work_block_commands.find_eval_for_work_blocks()
    assert not isinstance(eval_reading, list)
    nothing_left = work_blocks.DamagedMarker(values={"workstream": "research"}, modified_at=None)

    recovered = work_block_commands.recover_work_block(FakeRunner(), eval_reading, nothing_left)

    assert recovered is None


# ---------------------------------------------------------------------------
# A work block waiting for its workstream, and goodall fixblock
# ---------------------------------------------------------------------------


def record_work_block_with_unknown_workstream(eval_account: EvalAccount) -> str:
    """Start a work block, damage its marker so the workstream is lost, let goodall record it; return its id."""
    run_goodall(eval_account, ["start"])
    marker = json.loads(marker_file(eval_account).read_text())
    del marker["workstream"]
    marker_file(eval_account).write_text(json.dumps(marker))
    eval_account.clock.advance(timedelta(minutes=5))
    run_goodall(eval_account, ["status"])
    [record] = eval_account.work_block_records()
    assert record["workstream"] == "unknown"
    return str(record["id"])


def fix_files(eval_account: EvalAccount) -> list[Path]:
    return sorted((eval_account.record_repo / "work-block-fixes").glob("*.json"))


def test_status_lists_a_work_block_waiting_for_its_workstream_with_a_command_for_each_workstream(
    eval_account: EvalAccount,
) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)

    status = run_goodall(eval_account, ["status"])

    assert "recorded with the workstream unknown" in status.everything_printed
    assert "Mon 16 Nov 2026, 09:30 GMT" in status.everything_printed
    # Each command stands whole on its own line, so it can be copied and run.
    command_lines = status.standard_error.splitlines()
    for workstream in ["engineering", "research", "go-to-market"]:
        command = f"goodall fixblock {work_block_id} --workstream {workstream}"
        assert any(line.strip().endswith(command) for line in command_lines), command


def test_status_json_lists_work_blocks_waiting_for_their_workstream_under_a_stable_key(
    eval_account: EvalAccount,
) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)

    status = run_goodall(eval_account, ["status", "--json"])

    document = json.loads(status.standard_output)
    assert document["work_blocks_waiting_for_workstream"] == [
        {
            "id": work_block_id,
            "started_at": "2026-11-16T09:30:00Z",
            "ended_at": "2026-11-16T09:30:00Z",
            "commands": [
                f"goodall fixblock {work_block_id} --workstream engineering",
                f"goodall fixblock {work_block_id} --workstream research",
                f"goodall fixblock {work_block_id} --workstream go-to-market",
            ],
        }
    ]
    assert document["warnings"] == []


def test_fixblock_records_the_workstream_beside_the_untouched_record_and_status_stops_listing_it(
    eval_account: EvalAccount,
) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)
    [original_file] = eval_account.work_block_files()
    original_text = original_file.read_text()

    fixblock = run_goodall(eval_account, ["fixblock", work_block_id, "--workstream", "engineering"])

    assert fixblock.exit_code == exit_codes.SUCCESS, fixblock.standard_error
    assert "counts towards engineering" in fixblock.everything_printed
    assert "had been recorded as unknown" in fixblock.everything_printed
    assert original_file.read_text() == original_text
    [fix_file] = fix_files(eval_account)
    fix = json.loads(fix_file.read_text())
    assert fix["record_type"] == "work_block_fix"
    assert fix["work_block"] == work_block_id
    assert fix["workstream"] == "engineering"
    assert fix["contributor"] == "ada-lovelace"
    assert f"Add work block fix {fix['id']}" in eval_account.commit_subjects()
    assert git(eval_account.record_repo, "status", "--porcelain") == ""

    status = run_goodall(eval_account, ["status", "--json"])
    assert json.loads(status.standard_output)["work_blocks_waiting_for_workstream"] == []


def test_fixblock_may_fix_a_block_again_and_each_fix_is_kept(eval_account: EvalAccount) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)
    run_goodall(eval_account, ["fixblock", work_block_id, "--workstream", "engineering"])

    again = run_goodall(eval_account, ["fixblock", work_block_id, "--workstream", "research"])

    assert again.exit_code == exit_codes.SUCCESS, again.standard_error
    assert "had been recorded as engineering" in again.everything_printed
    assert len(fix_files(eval_account)) == 2


def test_fixblock_may_fix_a_block_whose_workstream_was_known_but_wrong(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    run_goodall(eval_account, ["stop"])
    [record] = eval_account.work_block_records()

    fixblock = run_goodall(eval_account, ["fixblock", record["id"], "--workstream", "go-to-market"])

    assert fixblock.exit_code == exit_codes.SUCCESS, fixblock.standard_error
    assert "had been recorded as research" in fixblock.everything_printed
    assert len(fix_files(eval_account)) == 1


def test_fixblock_with_nothing_given_shows_a_whole_command_to_copy_and_exits_2(eval_account: EvalAccount) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)

    fixblock = run_goodall(eval_account, ["fixblock"])

    assert fixblock.exit_code == exit_codes.USAGE_ERROR
    assert "No work block id was given" in fixblock.everything_printed
    assert "No --workstream was given" in fixblock.everything_printed
    assert f"goodall fixblock {work_block_id} --workstream engineering" in fixblock.standard_error
    assert fix_files(eval_account) == []


def test_fixblock_with_an_id_that_is_not_a_work_block_id_does_not_repeat_it_and_exits_2(
    eval_account: EvalAccount,
) -> None:
    fixblock = run_goodall(eval_account, ["fixblock", "my-Secret-thing", "--workstream", "research"])

    assert fixblock.exit_code == exit_codes.USAGE_ERROR
    assert "is not a work block id" in fixblock.everything_printed
    assert "my-Secret-thing" not in fixblock.everything_printed
    assert "goodall fixblock 20261116T093000Z-ada-lovelace-7f3a9c2e --workstream research" in fixblock.standard_error


def test_fixblock_refuses_unknown_as_the_workstream_and_exits_2(eval_account: EvalAccount) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)

    fixblock = run_goodall(eval_account, ["fixblock", work_block_id, "--workstream", "unknown"])

    assert fixblock.exit_code == exit_codes.USAGE_ERROR
    assert "placeholder" in fixblock.everything_printed
    assert f"goodall fixblock {work_block_id} --workstream research" in fixblock.standard_error
    assert fix_files(eval_account) == []


def test_fixblock_refuses_a_workstream_the_eval_does_not_have_without_repeating_it(
    eval_account: EvalAccount,
) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)

    fixblock = run_goodall(eval_account, ["fixblock", work_block_id, "--workstream", "Reserch"])

    assert fixblock.exit_code == exit_codes.USAGE_ERROR
    assert "Reserch" not in fixblock.everything_printed
    assert "engineering, research, go-to-market" in fixblock.everything_printed
    assert fix_files(eval_account) == []


def test_fixblock_for_a_work_block_the_record_repo_does_not_hold_says_what_to_check_and_exits_1(
    eval_account: EvalAccount,
) -> None:
    fixblock = run_goodall(
        eval_account, ["fixblock", "20261116T093000Z-ada-lovelace-7f3a9c2e", "--workstream", "research"]
    )

    assert fixblock.exit_code == exit_codes.ERROR
    assert "No work block record with the id 20261116T093000Z-ada-lovelace-7f3a9c2e" in fixblock.everything_printed
    assert "goodall status" in fixblock.everything_printed
    assert fix_files(eval_account) == []


def test_fixblock_for_a_work_block_whose_record_is_still_waiting_on_this_mac_says_to_write_it_first(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    git(eval_account.record_repo, "config", "--unset", "user.email")
    run_goodall(eval_account, ["stop"])
    [waiting_file] = work_blocks.unrecorded_files(eval_account.log_directory)

    fixblock = run_goodall(eval_account, ["fixblock", waiting_file.stem, "--workstream", "engineering"])

    assert fixblock.exit_code == exit_codes.ERROR
    assert "still waiting on this Mac" in fixblock.everything_printed
    assert "run goodall status" in fixblock.everything_printed
    assert fix_files(eval_account) == []


def test_fixblock_whose_fix_cannot_be_committed_exits_1_and_leaves_no_file(eval_account: EvalAccount) -> None:
    work_block_id = record_work_block_with_unknown_workstream(eval_account)
    git(eval_account.record_repo, "config", "--unset", "user.email")

    fixblock = run_goodall(eval_account, ["fixblock", work_block_id, "--workstream", "research"])

    assert fixblock.exit_code == exit_codes.ERROR
    assert fix_files(eval_account) == []


# ---------------------------------------------------------------------------
# Prompts whose hook input goodall could not read
# ---------------------------------------------------------------------------


def send_prompt_goodall_cannot_read(eval_account: EvalAccount) -> int:
    """Run the prompt gate in this account with input it cannot read, as Claude Code would; return the exit code."""
    terminal = Terminal(
        standard_output=io.StringIO(), standard_error=io.StringIO(), standard_input=io.StringIO("{cut short")
    )
    return cli.main(["hook", "UserPromptSubmit"], terminal=terminal, runner=FakeRunner(), clock=eval_account.clock)


def test_status_warns_once_about_prompts_whose_hook_input_could_not_be_read(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    eval_account.clock.advance(timedelta(minutes=10))
    assert send_prompt_goodall_cannot_read(eval_account) == exit_codes.SUCCESS
    eval_account.clock.advance(timedelta(minutes=5))
    assert send_prompt_goodall_cannot_read(eval_account) == exit_codes.SUCCESS

    status = run_goodall(eval_account, ["status"])

    assert status.exit_code == exit_codes.SUCCESS
    assert "could not read what Claude Code sent with a prompt 2 times since Mon 16 Nov 2026, 09:40 GMT" in (
        status.everything_printed
    )
    assert "Those prompts were sent and your work block kept recording" in status.everything_printed
    assert "not linked to the work block" in status.everything_printed
    assert "goodall may need updating" in status.everything_printed
    assert "Tell the researcher who runs the eval" in status.everything_printed

    # Said once, then filed away, kept.
    again = run_goodall(eval_account, ["status", "--json"])
    assert json.loads(again.standard_output)["warnings"] == []
    [filed] = (eval_account.log_directory / "hook-problems" / "reported").glob("*.ndjson")
    assert len(filed.read_text().splitlines()) == 2
    # Never a `.jsonl`, which an importer pointed at the log directory would take for a Claude session.
    assert not list((eval_account.log_directory / "hook-problems").rglob("*.jsonl"))


def test_status_still_warns_about_hook_problems_noted_under_the_old_file_name(eval_account: EvalAccount) -> None:
    # Goodall once noted hook problems in `hook-problems.jsonl`, a name an
    # importer takes for a Claude session. A log directory may still hold one.
    problems_folder = eval_account.log_directory / "hook-problems"
    problems_folder.mkdir(parents=True)
    old_problems_file = problems_folder / "hook-problems.jsonl"
    old_problems_file.write_text(
        '{"at": "2026-11-16T09:20:00Z", "event": null, "work_block": null, "problem": "no_event_name"}\n'
    )
    (problems_folder / "hook-problems.ndjson").write_text(
        '{"at": "2026-11-16T09:25:00Z", "event": null, "work_block": null, "problem": "no_event_name"}\n'
    )

    status = run_goodall(eval_account, ["status", "--json"])

    [warning] = json.loads(status.standard_output)["warnings"]
    text = " ".join(f"{warning['what_happened']} {warning['why']} {warning['what_to_do_next']}".split())
    assert "ran 2 times since Mon 16 Nov 2026, 09:20 GMT without being told which Claude Code event" in text
    assert [path.name for path in problems_folder.iterdir() if path.is_file()] == []
    assert len(list((problems_folder / "reported").glob("hook-problems-*.ndjson"))) == 2
    assert not list(problems_folder.rglob("*.jsonl"))


def test_status_warns_about_a_hook_that_ran_without_being_told_its_event(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    terminal = Terminal(
        standard_output=io.StringIO(), standard_error=io.StringIO(), standard_input=io.StringIO("{cut short")
    )
    exit_code = cli.main(["hook"], terminal=terminal, runner=FakeRunner(), clock=eval_account.clock)
    assert exit_code == exit_codes.ERROR

    status = run_goodall(eval_account, ["status", "--json"])

    [warning] = json.loads(status.standard_output)["warnings"]
    assert warning["kind"] == "hook_event_unknown"
    text = " ".join(f"{warning['what_happened']} {warning['why']} {warning['what_to_do_next']}".split())
    assert "ran 1 time since Mon 16 Nov 2026, 09:30 GMT without being told which Claude Code event" in text
    assert "docs/claude-code-hooks.md" in text


def test_status_json_includes_the_warning_about_unreadable_hook_input(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    send_prompt_goodall_cannot_read(eval_account)

    status = run_goodall(eval_account, ["status", "--json"])

    [warning] = json.loads(status.standard_output)["warnings"]
    assert warning["kind"] == "hook_input_unreadable"


# ---------------------------------------------------------------------------
# An unreachable log directory, and an end that was interrupted
# ---------------------------------------------------------------------------


def test_start_with_the_log_directory_unreachable_says_not_recording_and_starts_nothing(
    eval_account: EvalAccount,
) -> None:
    # An external drive holding the log directory has been unplugged.
    unplugged = eval_account.log_directory.with_name("unplugged")
    eval_account.log_directory.rename(unplugged)

    start = run_goodall(eval_account, ["start"])

    assert start.exit_code == exit_codes.ERROR
    assert start.standard_error.startswith("FAIL  Not recording:")
    assert not eval_account.log_directory.exists()
    assert sorted(path for path in unplugged.rglob("*")) == []


def test_a_work_block_whose_log_directory_is_unplugged_is_not_active_until_it_is_back(
    eval_account: EvalAccount,
) -> None:
    run_goodall(eval_account, ["start"])
    unplugged = eval_account.log_directory.with_name("unplugged")
    eval_account.log_directory.rename(unplugged)

    status = run_goodall(eval_account, ["status", "--json"])
    stop = run_goodall(eval_account, ["stop"])

    assert status.exit_code == exit_codes.ERROR
    assert json.loads(status.standard_output)["work_block_active"] is False
    assert work_blocks.active_work_block(eval_account.log_directory, eval_account.clock.now) is None
    assert stop.exit_code == exit_codes.ERROR
    assert stop.standard_error.startswith("FAIL  Not recording:")

    unplugged.rename(eval_account.log_directory)
    eval_account.clock.advance(timedelta(minutes=30))
    assert run_goodall(eval_account, ["stop"]).exit_code == exit_codes.SUCCESS
    [record] = eval_account.work_block_records()
    assert record["end_state"] == "clean"


def test_an_end_interrupted_after_its_record_was_saved_is_finished_by_the_next_command(
    eval_account: EvalAccount,
) -> None:
    # goodall stop saved the ended record in the log directory, then the Mac
    # lost power before the marker was removed.
    run_goodall(eval_account, ["start"])
    running = work_blocks.read_marker(eval_account.log_directory)
    assert isinstance(running, work_blocks.RunningWorkBlock)
    ended_at = MONDAY_MORNING + timedelta(hours=1)
    ended = records.build_work_block_record(
        "ada-lovelace",
        "research",
        MONDAY_MORNING,
        running.record.machine,
        ended_at=ended_at,
        end_state="clean",
        suffix=running.id[-8:],
    )
    assert work_blocks.save_unrecorded(eval_account.log_directory, ended)
    assert work_blocks.active_work_block(eval_account.log_directory, ended_at) is None
    eval_account.clock.advance(timedelta(hours=2))

    status = run_goodall(eval_account, ["status"])

    assert status.exit_code == exit_codes.ERROR
    assert not marker_file(eval_account).exists()
    [record] = eval_account.work_block_records()
    assert record["ended_at"] == "2026-11-16T10:30:00Z"
    assert record["end_state"] == "clean"


# ---------------------------------------------------------------------------
# goodall status --json, for agents
# ---------------------------------------------------------------------------


def test_status_json_while_a_work_block_is_active_describes_it(eval_account: EvalAccount) -> None:
    start = run_goodall(eval_account, ["start", "--workstream", "engineering"])
    work_blocks.record_heartbeat(eval_account.log_directory, MONDAY_MORNING + timedelta(minutes=25), CLAUDE_SESSION)
    eval_account.clock.advance(timedelta(minutes=30))

    status = run_goodall(eval_account, ["status", "--json"])

    assert status.exit_code == exit_codes.SUCCESS
    assert status.standard_error == ""
    document = json.loads(status.standard_output)
    running = work_blocks.read_marker(eval_account.log_directory)
    assert isinstance(running, work_blocks.RunningWorkBlock)
    assert document == {
        "work_block_active": True,
        "eval": {"name": "goodall-pilot"},
        "contributor": {"id": "ada-lovelace", "display_name": "Ada Lovelace", "main_workstream": "research"},
        "work_block": {
            "id": running.id,
            "workstream": "engineering",
            "started_at": "2026-11-16T09:30:00Z",
            "last_heartbeat_at": "2026-11-16T09:55:00Z",
            "claude_sessions": [CLAUDE_SESSION],
            "telemetry_receiver": {"running": True, "port": receiver_port_shown(start)},
        },
        "ended_work_blocks": [],
        "work_blocks_waiting_for_workstream": [],
        "warnings": [],
        "failures": [],
    }


def test_status_json_reports_a_work_block_it_closed_and_recorded(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    eval_account.clock.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    status = run_goodall(eval_account, ["status", "--json"])

    assert status.exit_code == exit_codes.ERROR
    document = json.loads(status.standard_output)
    assert document["work_block_active"] is False
    assert document["work_block"] is None
    [ended] = document["ended_work_blocks"]
    assert ended["workstream"] == "research"
    assert ended["end_state"] == "inferred"
    assert ended["ended_at"] == "2026-11-16T09:30:00Z"
    assert ended["recorded"] is True


# ---------------------------------------------------------------------------
# Journal entries and interventions name the work block they fall in
# ---------------------------------------------------------------------------


def test_a_journal_entry_written_during_a_work_block_names_it_and_one_after_does_not(
    eval_account: EvalAccount,
) -> None:
    # goodall journal reads the real clock, so the work block starts now.
    eval_account.clock.now = datetime.now(UTC)
    run_goodall(eval_account, ["start"])
    running = work_blocks.read_marker(eval_account.log_directory)
    assert isinstance(running, work_blocks.RunningWorkBlock)

    during = run_goodall(eval_account, ["journal", "Chose Postgres; I decided."])
    run_goodall(eval_account, ["stop"])
    after = run_goodall(eval_account, ["journal", "Wrote up the day."])

    assert during.exit_code == after.exit_code == exit_codes.SUCCESS
    entries_by_text = {}
    for entry_file in (eval_account.record_repo / "journal").glob("*.md"):
        entry_text = entry_file.read_text()
        entries_by_text[entry_text.rsplit("+++", 1)[1].strip()] = entry_text
    assert f'work_block = "{running.id}"' in entries_by_text["Chose Postgres; I decided."]
    assert "work_block" not in entries_by_text["Wrote up the day."]


def test_an_intervention_that_happened_before_the_active_work_block_began_names_no_work_block(
    eval_account: EvalAccount,
) -> None:
    eval_account.clock.now = datetime.now(UTC)
    run_goodall(eval_account, ["start"])
    an_hour_ago = (eval_account.clock.now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    intervene = run_goodall(
        eval_account,
        ["intervene", "--category", "infrastructure", "--decision", "escalate", "--occurred-at", an_hour_ago, "VPN."],
    )

    assert intervene.exit_code == exit_codes.SUCCESS, intervene.standard_error
    [intervention_file] = (eval_account.record_repo / "interventions").glob("*.md")
    assert "work_block" not in intervention_file.read_text()


# ---------------------------------------------------------------------------
# Telemetry during a work block
# ---------------------------------------------------------------------------

# A compact OTLP logs export, in the shape Claude Code sends; synthetic.
CLAUDE_CODE_LOGS_BODY = (
    b'{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"claude-code"}}]},'
    b'"scopeLogs":[{"logRecords":[{"timeUnixNano":"1763285400000000000","body":{"stringValue":'
    b'"claude_code.user_prompt"},"attributes":[{"key":"session.id","value":{"stringValue":'
    b'"0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"}}]}]}]}]}'
)

RECEIVER_ADDRESS_PATTERN = re.compile(r"http://127\.0\.0\.1:(\d+)")


def post_like_claude_code(port: int, path: str, body: bytes) -> int:
    """Send one OTLP http/json export as Claude Code's exporter does, and return the HTTP status."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def receiver_port_shown(run: GoodallRun) -> int:
    """The port goodall said its telemetry receiver listens on."""
    address_match = RECEIVER_ADDRESS_PATTERN.search(run.everything_printed)
    assert address_match is not None, run.everything_printed
    return int(address_match.group(1))


def telemetry_directory(eval_account: EvalAccount, work_block_id: str) -> Path:
    return eval_account.log_directory / "telemetry" / work_block_id


def running_work_block_id(eval_account: EvalAccount) -> str:
    running = work_blocks.read_marker(eval_account.log_directory)
    assert isinstance(running, work_blocks.RunningWorkBlock)
    return running.id


def test_a_telemetry_request_sent_during_a_work_block_is_kept_as_one_verbatim_line_with_its_receipt(
    eval_account: EvalAccount,
) -> None:
    start = run_goodall(eval_account, ["start"], real_receiver=True)
    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    work_block_id = running_work_block_id(eval_account)
    port = receiver_port_shown(start)

    assert post_like_claude_code(port, "/v1/logs", CLAUDE_CODE_LOGS_BODY) == 200

    eval_account.clock.advance(timedelta(minutes=30))
    stop = run_goodall(eval_account, ["stop"], real_receiver=True)
    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    directory = telemetry_directory(eval_account, work_block_id)
    assert (directory / "logs.jsonl").read_bytes() == CLAUDE_CODE_LOGS_BODY + b"\n"
    receipt_check = telemetry_receipts.check_receipts(directory)
    assert receipt_check.receipt_count == 1
    assert receipt_check.problems() == []
    [record] = eval_account.work_block_records()
    assert record["telemetry"] == "captured"
    assert "Telemetry captured, 1 request" in stop.everything_printed
    # The receiver is gone, and so is goodall's record of it.
    assert telemetry_capture.receiver_record_files(eval_account.log_directory) == []


def send_prompt(eval_account: EvalAccount) -> GoodallRun:
    """Run the prompt gate as Claude Code does before a prompt, with readable input."""
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    hook_input = json.dumps({"session_id": CLAUDE_SESSION, "hook_event_name": "UserPromptSubmit", "prompt": "Go on."})
    terminal = Terminal(
        standard_output=standard_output, standard_error=standard_error, standard_input=io.StringIO(hook_input)
    )
    exit_code = cli.main(["hook", "UserPromptSubmit"], terminal=terminal, runner=FakeRunner(), clock=eval_account.clock)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue(), FakeRunner())


def receiver_process_id(eval_account: EvalAccount, work_block_id: str) -> int:
    receiver_record = telemetry_capture.receiver_record_for(eval_account.log_directory, work_block_id)
    assert receiver_record is not None
    process_id = receiver_record.program["process_id"]
    assert isinstance(process_id, int)
    return process_id


def test_killing_the_receiver_during_a_work_block_loses_no_acknowledged_line_and_is_noticed(
    eval_account: EvalAccount,
) -> None:
    start = run_goodall(eval_account, ["start"], real_receiver=True)
    work_block_id = running_work_block_id(eval_account)
    port = receiver_port_shown(start)
    bodies = []
    for number in range(3):
        body = CLAUDE_CODE_LOGS_BODY.replace(b"1763285400000000000", f"176328540000000000{number}".encode())
        assert post_like_claude_code(port, "/v1/logs", body) == 200
        bodies.append(body)

    os.kill(receiver_process_id(eval_account, work_block_id), signal.SIGKILL)
    eval_account.clock.advance(timedelta(minutes=5))

    # The next prompt still goes through, and the person is told once.
    first_prompt = send_prompt(eval_account)
    second_prompt = send_prompt(eval_account)
    assert first_prompt.exit_code == second_prompt.exit_code == exit_codes.SUCCESS
    notice = json.loads(first_prompt.standard_output)["systemMessage"]
    assert "telemetry receiver has stopped" in notice
    assert "goodall start" in notice
    assert second_prompt.standard_output == ""

    status = run_goodall(eval_account, ["status", "--json"], real_receiver=True)
    document = json.loads(status.standard_output)
    assert status.exit_code == exit_codes.SUCCESS
    assert document["work_block"]["telemetry_receiver"] == {"running": False, "port": port}
    assert [warning["kind"] for warning in document["warnings"]] == ["telemetry_receiver_not_running"]

    stop = run_goodall(eval_account, ["stop"], real_receiver=True)
    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    directory = telemetry_directory(eval_account, work_block_id)
    assert (directory / "logs.jsonl").read_bytes() == b"".join(body + b"\n" for body in bodies)
    receipt_check = telemetry_receipts.check_receipts(directory)
    assert receipt_check.receipt_count == 3
    assert receipt_check.problems() == []
    [record] = eval_account.work_block_records()
    assert record["telemetry"] == "incomplete"
    assert record["claude_sessions"] == [CLAUDE_SESSION]


def write_synthetic_transcript(written_at: datetime) -> Path:
    """A one-record Claude Code transcript in this test's Claude config folder, written at `written_at`."""
    project_folder = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-Users-ada-work"
    project_folder.mkdir(parents=True)
    transcript = project_folder / f"{CLAUDE_SESSION}.jsonl"
    timestamp = written_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    transcript.write_text(json.dumps({"type": "user", "timestamp": timestamp, "message": "synthetic"}) + "\n")
    # Goodall reads the modification time, so it must not be the real clock's.
    os.utime(transcript, (written_at.timestamp(), written_at.timestamp()))
    return transcript


def test_a_work_block_whose_receiver_cannot_start_still_captures_transcripts_and_records_telemetry_missing(
    eval_account: EvalAccount,
) -> None:
    # Another program already listens on the port asked for.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other_program:
        other_program.bind(("127.0.0.1", 0))
        other_program.listen()
        taken_port = other_program.getsockname()[1]

        start = run_goodall(eval_account, ["start", "--telemetry-port", str(taken_port)], real_receiver=True)

    assert start.exit_code == exit_codes.SUCCESS
    assert "Started a work block on research" in start.everything_printed
    assert start.standard_error.startswith("WARN")
    assert "telemetry receiver did not start" in start.everything_printed
    assert f"another program is using port {taken_port}" in start.everything_printed
    assert str(eval_account.log_directory) not in start.everything_printed
    # The agent session is still copied when the work block ends.
    transcript = write_synthetic_transcript(eval_account.clock())
    standard_input = io.StringIO(json.dumps({"session_id": CLAUDE_SESSION, "transcript_path": str(transcript)}))
    terminal = Terminal(standard_output=io.StringIO(), standard_error=io.StringIO(), standard_input=standard_input)
    assert cli.main(["hook", "PostToolUse"], terminal=terminal, clock=eval_account.clock) == exit_codes.SUCCESS
    assert send_prompt(eval_account).exit_code == exit_codes.SUCCESS

    eval_account.clock.advance(timedelta(minutes=30))
    stop = run_goodall(eval_account, ["stop"], real_receiver=True)

    assert stop.exit_code == exit_codes.SUCCESS
    [record] = eval_account.work_block_records()
    assert record["telemetry"] == "missing"
    copied = eval_account.log_directory / "transcripts" / "-Users-ada-work" / transcript.name
    assert copied.read_bytes() == transcript.read_bytes()


def test_status_shows_where_the_receiver_listens_while_a_work_block_is_active(eval_account: EvalAccount) -> None:
    start = run_goodall(eval_account, ["start"])
    port = receiver_port_shown(start)

    status = run_goodall(eval_account, ["status"])
    status_json = run_goodall(eval_account, ["status", "--json"])

    assert status.standard_error == ""
    assert f"Telemetry receiver  listening on http://127.0.0.1:{port}" in status.standard_output
    document = json.loads(status_json.standard_output)
    assert document["work_block"]["telemetry_receiver"] == {"running": True, "port": port}
    assert document["warnings"] == []


def test_start_shows_the_receivers_port_prominently_and_uses_the_port_asked_for(eval_account: EvalAccount) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    start = run_goodall(eval_account, ["start", "--telemetry-port", str(free_port)])

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert f"Telemetry receiver  http://127.0.0.1:{free_port} (port {free_port})" in start.standard_output
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT is http://127.0.0.1:{free_port}" in start.everything_printed
    assert post_like_claude_code(free_port, "/v1/metrics", b'{"resourceMetrics":[]}') == 200
    directory = telemetry_directory(eval_account, running_work_block_id(eval_account))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("port", ["0", "70000"])
def test_start_with_a_telemetry_port_that_is_not_a_port_starts_nothing_and_exits_2(
    eval_account: EvalAccount, port: str
) -> None:
    start = run_goodall(eval_account, ["start", "--telemetry-port", port])

    assert start.exit_code == exit_codes.USAGE_ERROR
    assert start.standard_error.startswith("FAIL  The --telemetry-port given is not a port.")
    assert eval_account.files_in_log_directory() == []


def test_the_receiver_of_a_forgotten_work_block_is_stopped_by_the_next_start(eval_account: EvalAccount) -> None:
    run_goodall(eval_account, ["start"])
    [forgotten_receiver] = eval_account.simulated_receivers()
    eval_account.clock.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)

    start = run_goodall(eval_account, ["start"])

    assert start.exit_code == exit_codes.SUCCESS
    assert not forgotten_receiver.is_running()
    [forgotten, new] = eval_account.simulated_receivers()
    assert new.is_running()
    [record] = eval_account.work_block_records()
    assert record["end_state"] == "inferred"
    assert record["telemetry"] == "missing"


def test_a_receiver_left_by_an_interrupted_end_is_stopped_by_the_next_command(eval_account: EvalAccount) -> None:
    # goodall stop was killed after the marker went but before the receiver
    # was stopped: the receiver's record remains, for a work block no longer active.
    run_goodall(eval_account, ["start"])
    work_block_id = running_work_block_id(eval_account)
    work_blocks.WorkBlockFiles(eval_account.log_directory).marker.unlink()

    run_goodall(eval_account, ["status"])

    [receiver] = eval_account.simulated_receivers()
    assert not receiver.is_running()
    assert telemetry_capture.receiver_record_for(eval_account.log_directory, work_block_id) is None


def test_restart_is_not_refused_while_the_prompt_gate_briefly_holds_the_receipt_log_lock(
    eval_account: EvalAccount,
) -> None:
    # In the gap before the new receiver listens, the old receiver's record is
    # still on disk, so the prompt gate may try the receipt log's lock. Here a
    # probe holds it for a moment, as the gate's check does, while goodall
    # start restarts the receiver.
    first = run_goodall(eval_account, ["start"])
    port = receiver_port_shown(first)
    work_block_id = running_work_block_id(eval_account)
    [crashed] = eval_account.simulated_receivers()
    assert isinstance(crashed, InProcessTelemetryReceiver)
    crashed.crash()
    receipt_log = telemetry_capture.TelemetryFiles(eval_account.log_directory, work_block_id).receipt_log
    probe_descriptor = os.open(receipt_log, os.O_RDONLY)
    fcntl.flock(probe_descriptor, fcntl.LOCK_EX)
    release_probe = threading.Timer(0.3, os.close, args=[probe_descriptor])
    release_probe.start()
    try:
        again = run_goodall(eval_account, ["start"])
    finally:
        release_probe.join()

    assert again.exit_code == exit_codes.SUCCESS, again.standard_error
    assert "telemetry receiver was not running, so goodall started it again" in again.everything_printed
    assert post_like_claude_code(port, "/v1/logs", CLAUDE_CODE_LOGS_BODY) == 200


def test_start_while_a_work_block_is_active_restarts_a_stopped_receiver_on_the_same_port(
    eval_account: EvalAccount,
) -> None:
    first = run_goodall(eval_account, ["start"])
    port = receiver_port_shown(first)
    [crashed] = eval_account.simulated_receivers()
    assert isinstance(crashed, InProcessTelemetryReceiver)
    crashed.crash()

    again = run_goodall(eval_account, ["start"])

    assert again.exit_code == exit_codes.SUCCESS, again.standard_error
    assert "A work block is already active" in again.everything_printed
    assert "telemetry receiver was not running, so goodall started it again" in again.everything_printed
    assert receiver_port_shown(again) == port
    assert post_like_claude_code(port, "/v1/logs", CLAUDE_CODE_LOGS_BODY) == 200
    run_goodall(eval_account, ["stop"])
    [record] = eval_account.work_block_records()
    assert record["telemetry"] == "incomplete"


# ---------------------------------------------------------------------------
# The record repo stays on this Mac: no command sends anything anywhere
# ---------------------------------------------------------------------------

# git's commands that talk to another repository or change the list of
# remotes. Goodall commits to the record repo and never runs any of them
# (ADR 0023).
GIT_COMMANDS_THAT_REACH_A_REMOTE = ["push", "fetch", "pull", "remote", "ls-remote", "clone"]


def git_command_name(arguments: list[str]) -> str | None:
    """The git command in `arguments` (`commit` in `git -c x=y commit -m ...`), or None when it is not git."""
    if Path(arguments[0]).name != "git":
        return None
    position = 1
    while position < len(arguments) and arguments[position].startswith("-"):
        # `-c name=value` and `-C folder` take the next argument as theirs.
        if arguments[position] in ["-c", "-C"]:
            position += 1
        position += 1
    if position >= len(arguments):
        return None
    return arguments[position]


def programs_that_reach_a_remote(run: GoodallRun) -> list[list[str]]:
    """Every command this run of goodall started that could send a record off this Mac."""
    reaching = []
    for command in run.runner.calls:
        if Path(command.arguments[0]).name == "gh":
            reaching.append(command.arguments)
        elif git_command_name(command.arguments) in GIT_COMMANDS_THAT_REACH_A_REMOTE:
            reaching.append(command.arguments)
    return reaching


def test_a_record_repo_with_a_remote_set_by_hand_is_committed_to_and_never_pushed_by_any_command(
    eval_account: EvalAccount,
) -> None:
    remote = eval_account.home_folder.parent / "remote.git"
    git(eval_account.home_folder.parent, "clone", "--quiet", "--bare", str(eval_account.record_repo), str(remote))
    git(eval_account.record_repo, "remote", "add", "origin", str(remote))
    git(eval_account.record_repo, "fetch", "--quiet", "origin")
    git(eval_account.record_repo, "branch", "--quiet", "--set-upstream-to=origin/main")
    commits_before = len(eval_account.commit_subjects())

    runs = [run_goodall(eval_account, ["start"])]
    runs.append(run_goodall(eval_account, ["journal", "Chose Postgres."]))
    runs.append(
        run_goodall(eval_account, ["intervene", "--category", "infrastructure", "--decision", "modify", "Stopped it."])
    )
    runs.append(run_goodall(eval_account, ["status"]))
    eval_account.clock.advance(timedelta(minutes=40))
    runs.append(run_goodall(eval_account, ["stop"]))
    [record] = eval_account.work_block_records()
    runs.append(run_goodall(eval_account, ["fixblock", record["id"], "--workstream", "engineering"]))

    for run in runs:
        assert run.exit_code == exit_codes.SUCCESS, run.standard_error
        assert programs_that_reach_a_remote(run) == []
        assert "shared record repo" not in run.everything_printed
        assert run.standard_error == ""
    # Journal entry, intervention, work block record and fix: each a local commit.
    assert len(eval_account.commit_subjects()) == commits_before + 4
    assert git(remote, "log", "--format=%s", "main").splitlines() == ["Start the record repo"]
