"""Outside-in tests for the notification shown on a background start or stop (ADR 0021, `goodall-fs9.28`).

A background start or stop is one made by something other than the
person's own command: signing in or out, a script or schedule (which say so
with `--background`), or goodall ending a work block by itself (an inferred
end, a damaged marker). Each shows exactly one notification; a start or
stop the person typed or clicked shows none.

Each test drives goodall through `cli.main` in a pretend account inside
tmp_path, with the fake runner. The notification is shown by the menu bar
icon's helper, run as a second, short-lived program with `--notify`
(goodall.notifications). Its program inside the pretend installed
goodall.app is scripted with `answer_with`, so every notification goodall
asks for is in `runner.calls` and no real one appears. A test that does not
expect one leaves the helper unscripted, so asking for one fails it loudly.
"""

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import cli, exit_codes, menu_bar_icon, work_blocks
from goodall.fake_runner import FakeBackgroundPrograms, FakeRunner, InProcessTelemetryReceiver, ScriptedAnswer
from goodall.runner import Command, CommandOutcome
from goodall.terminal import Terminal

EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "A test eval."
workstreams = ["engineering", "research"]
intervention_categories = ["infrastructure"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""

MONDAY_MORNING = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)

RECEIVER_COMMAND_START = [sys.executable, "-I", "-m", "goodall.telemetry_receiver"]


def git(working_directory: Path, *arguments: str) -> None:
    """Run git for the test's own setup."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    subprocess.run(["git", *arguments], cwd=working_directory, env=environment, capture_output=True, check=True)


@dataclass
class Account:
    """A pretend macOS user account set up for goodall, with the time goodall will see."""

    applications_folder: Path
    record_repo: Path
    log_directory: Path
    now: datetime = MONDAY_MORNING
    background: FakeBackgroundPrograms = field(default_factory=FakeBackgroundPrograms)

    @property
    def helper_program(self) -> Path:
        """The program inside the installed goodall.app that goodall runs to show a notification."""
        return self.applications_folder / "goodall.app" / "Contents" / "MacOS" / "goodall-menu-bar-icon"

    def install_helper(self) -> None:
        """Pretend the menu bar icon's helper is installed in the Applications folder."""
        self.helper_program.parent.mkdir(parents=True)
        self.helper_program.write_text("")

    def marker_file(self) -> Path:
        return self.log_directory / "work-blocks" / "active-work-block.json"

    def work_block_records(self) -> list[dict[str, object]]:
        found = []
        for record_file in sorted((self.record_repo / "work-blocks").glob("*.json")):
            found.append(json.loads(record_file.read_text()))
        return found


@pytest.fixture
def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Account]:
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "records"
    log_directory = tmp_path / "logs"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    applications_folder = tmp_path / "Applications"
    applications_folder.mkdir()
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", applications_folder)
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
        f'record_repo = "{record_repo}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
    account = Account(applications_folder, record_repo, log_directory)
    yield account
    account.background.stop_all()
    monkeypatch.undo()
    time.tzset()


@dataclass
class GoodallRun:
    exit_code: int
    standard_output: str
    standard_error: str
    runner: FakeRunner
    helper_program: Path

    @property
    def everything_printed(self) -> str:
        return " ".join((self.standard_output + "\n" + self.standard_error).split())

    def notifications(self) -> list[list[str]]:
        """The arguments after `--notify` each time goodall asked the helper to show a notification."""
        found = []
        for command in self.runner.calls:
            if command.arguments[0] == str(self.helper_program):
                assert command.arguments[1] == "--notify"
                found.append(command.arguments[2:])
        return found


def run_goodall(account: Account, arguments: list[str], helper_exit_code: int | None = None) -> GoodallRun:
    """Run goodall in the account. The helper exits with `helper_exit_code`; None leaves it unscripted.

    `open`, which `goodall start` uses to bring the icon back once the
    helper is installed, is always scripted to work.
    """
    runner = FakeRunner(real_programs=["git"], background=account.background)
    runner.answer(["claude", "--version"], standard_output="2.3.4 (Claude Code)\n")
    runner.answer_background(RECEIVER_COMMAND_START, InProcessTelemetryReceiver)
    runner.answer_with("open", lambda command: ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0))
    if helper_exit_code is not None:
        what_the_helper_prints = ""
        if helper_exit_code == 1:
            what_the_helper_prints = "The notification could not be shown: a test failure.\n"
        if helper_exit_code == 3:
            what_the_helper_prints = "Notifications are not allowed for goodall.\n"
        answer = ScriptedAnswer(
            outcome=CommandOutcome.COMPLETED, exit_code=helper_exit_code, standard_error=what_the_helper_prints
        )

        def helper(command: Command) -> ScriptedAnswer:
            return answer

        runner.answer_with(str(account.helper_program), helper)
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: account.now)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue(), runner, account.helper_program)


def leave_the_work_block_idle_overnight(account: Account) -> None:
    """The work block's last heartbeat is at 17:02; nobody stops it, and the next command runs the next morning.

    A heartbeat is kept only while the work block is not yet idle, so the
    day has one every two hours up to the last.
    """
    for hour in [11, 13, 15]:
        work_blocks.record_heartbeat(account.log_directory, datetime(2026, 11, 16, hour, 0, tzinfo=UTC))
    assert work_blocks.record_heartbeat(account.log_directory, datetime(2026, 11, 16, 17, 2, tzinfo=UTC)) is not None
    account.now = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The acceptance criteria of goodall-fs9.28
# ---------------------------------------------------------------------------


def test_a_background_start_shows_exactly_one_notification_with_the_reason_and_the_workstream(
    account: Account,
) -> None:
    account.install_helper()

    start = run_goodall(account, ["start", "--background", "script"], helper_exit_code=0)

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert start.notifications() == [["Recording started", "On research, started by a script or schedule."]]
    assert start.standard_error == ""


def test_a_background_stop_shows_exactly_one_notification_with_the_reason_and_the_workstream(
    account: Account,
) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    account.now = MONDAY_MORNING + timedelta(hours=1)

    stop = run_goodall(account, ["stop", "--background", "script"], helper_exit_code=0)

    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    assert stop.notifications() == [["Recording stopped", "On research, stopped by a script or schedule."]]
    assert stop.standard_error == ""


def test_start_and_stop_typed_by_a_person_show_no_notification(account: Account) -> None:
    # The helper is installed but not scripted: asking it for a
    # notification would fail this test.
    account.install_helper()

    start = run_goodall(account, ["start"])
    account.now = MONDAY_MORNING + timedelta(hours=1)
    stop = run_goodall(account, ["stop"])

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    assert start.notifications() == []
    assert stop.notifications() == []


def test_a_failed_notification_is_a_warning_and_the_work_block_still_starts(account: Account) -> None:
    account.install_helper()

    start = run_goodall(account, ["start", "--background", "script"], helper_exit_code=1)

    assert start.exit_code == exit_codes.SUCCESS
    assert account.marker_file().exists()
    assert "Started a work block on research" in start.everything_printed
    assert "WARN goodall could not show a notification that recording started" in start.everything_printed
    assert "a test failure" in start.everything_printed


def test_a_failed_notification_is_a_warning_and_the_work_block_still_ends(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    account.now = MONDAY_MORNING + timedelta(hours=1)

    stop = run_goodall(account, ["stop", "--background", "script"], helper_exit_code=1)

    assert stop.exit_code == exit_codes.SUCCESS
    assert not account.marker_file().exists()
    [record] = account.work_block_records()
    assert record["end_state"] == "clean"
    assert "WARN goodall could not show a notification that recording stopped" in stop.everything_printed


# ---------------------------------------------------------------------------
# Why a notification could not be shown
# ---------------------------------------------------------------------------


def test_notifications_turned_off_in_system_settings_are_a_warning_that_says_where_to_turn_them_on(
    account: Account,
) -> None:
    account.install_helper()

    start = run_goodall(account, ["start", "--background", "script"], helper_exit_code=3)

    assert start.exit_code == exit_codes.SUCCESS
    assert account.marker_file().exists()
    assert "macOS is not letting goodall show notifications" in start.everything_printed
    assert "System Settings > Notifications > goodall" in start.everything_printed


def test_a_background_start_with_no_helper_installed_still_starts_and_warns(account: Account) -> None:
    start = run_goodall(account, ["start", "--background", "script"])

    assert start.exit_code == exit_codes.SUCCESS
    assert account.marker_file().exists()
    assert "WARN goodall could not show a notification that recording started" in start.everything_printed
    assert "menu bar icon is not installed" in start.everything_printed


def test_a_helper_that_never_answers_is_a_warning(account: Account) -> None:
    account.install_helper()
    runner_answer = ScriptedAnswer(outcome=CommandOutcome.TIMED_OUT)
    runner = FakeRunner(real_programs=["git"], background=account.background)
    runner.answer(["claude", "--version"], standard_output="2.3.4 (Claude Code)\n")
    runner.answer_background(RECEIVER_COMMAND_START, InProcessTelemetryReceiver)
    runner.answer_with("open", lambda command: ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0))
    runner.answer_with(str(account.helper_program), lambda command: runner_answer)
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=io.StringIO(), standard_error=standard_error)

    exit_code = cli.main(
        ["start", "--background", "script"], terminal=terminal, runner=runner, clock=lambda: account.now
    )

    assert exit_code == exit_codes.SUCCESS
    assert "timed out" in standard_error.getvalue()


# ---------------------------------------------------------------------------
# Each kind of background start and stop
# ---------------------------------------------------------------------------


def test_a_start_at_sign_in_says_so(account: Account) -> None:
    account.install_helper()

    start = run_goodall(account, ["start", "--background", "sign-in"], helper_exit_code=0)

    assert start.notifications() == [["Recording started", "On research, because you signed in."]]


def test_a_stop_at_sign_out_says_so(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])

    stop = run_goodall(account, ["stop", "--background", "sign-out"], helper_exit_code=0)

    assert stop.notifications() == [["Recording stopped", "On research, because you signed out."]]


def test_a_background_start_on_another_workstream_names_it(account: Account) -> None:
    account.install_helper()

    start = run_goodall(account, ["start", "--workstream", "engineering", "--background", "script"], helper_exit_code=0)

    assert start.notifications() == [["Recording started", "On engineering, started by a script or schedule."]]


def test_a_pause_by_a_script_shows_that_recording_stopped(account: Account) -> None:
    # A pause ends the work block (ADR 0021), so it is a stop.
    account.install_helper()
    run_goodall(account, ["start"])

    pause = run_goodall(account, ["pause", "--background", "script"], helper_exit_code=0)

    assert pause.exit_code == exit_codes.SUCCESS, pause.standard_error
    assert pause.notifications() == [["Recording stopped", "On research, paused by a script or schedule."]]


def test_a_background_start_while_a_work_block_is_already_active_shows_nothing(account: Account) -> None:
    # Nothing started, so there is nothing the person does not already know.
    account.install_helper()
    run_goodall(account, ["start"])

    start = run_goodall(account, ["start", "--background", "sign-in"])

    assert start.exit_code == exit_codes.SUCCESS
    assert start.notifications() == []


def test_a_background_stop_with_nothing_to_stop_shows_nothing(account: Account) -> None:
    account.install_helper()

    stop = run_goodall(account, ["stop", "--background", "sign-out"])

    assert stop.exit_code == exit_codes.ERROR
    assert stop.notifications() == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["start", "--background", "sign-out"],
        ["stop", "--background", "sign-in"],
        ["pause", "--background", "sign-in"],
        ["start", "--background", "whenever"],
    ],
)
def test_a_background_reason_that_does_not_fit_the_command_is_a_usage_error(
    account: Account, arguments: list[str]
) -> None:
    run = run_goodall(account, arguments)

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert not account.marker_file().exists()


# ---------------------------------------------------------------------------
# goodall ending a work block by itself
# ---------------------------------------------------------------------------


def test_a_work_block_goodall_closes_after_it_was_left_idle_shows_one_notification(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    leave_the_work_block_idle_overnight(account)

    status = run_goodall(account, ["status"], helper_exit_code=0)

    assert status.notifications() == [
        [
            "Recording stopped",
            "On research. Nothing happened for more than 3 h, so goodall stopped recording at the last activity, "
            "Mon 16 Nov 2026, 17:02 GMT.",
        ]
    ]
    [record] = account.work_block_records()
    assert record["end_state"] == "inferred"


def test_a_typed_start_that_first_closes_an_idle_work_block_shows_only_that_it_stopped(account: Account) -> None:
    # The person chose the new start, which shows nothing; goodall chose to
    # close the old work block, which shows one notification.
    account.install_helper()
    run_goodall(account, ["start"])
    leave_the_work_block_idle_overnight(account)

    start = run_goodall(account, ["start"], helper_exit_code=0)

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    [notification] = start.notifications()
    assert notification[0] == "Recording stopped"


def test_a_background_stop_that_finds_the_work_block_already_idle_shows_one_notification(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    leave_the_work_block_idle_overnight(account)

    stop = run_goodall(account, ["stop", "--background", "script"], helper_exit_code=0)

    assert stop.exit_code == exit_codes.SUCCESS, stop.standard_error
    [notification] = stop.notifications()
    assert "Nothing happened for more than 3 h" in notification[1]


def test_a_work_block_goodall_closes_because_its_marker_is_damaged_shows_one_notification(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    work_blocks.record_heartbeat(account.log_directory, MONDAY_MORNING + timedelta(minutes=40))
    marker = json.loads(account.marker_file().read_text())
    del marker["machine"]
    account.marker_file().write_text(json.dumps(marker))
    account.now = MONDAY_MORNING + timedelta(hours=1)

    status = run_goodall(account, ["status"], helper_exit_code=0)

    assert status.notifications() == [
        [
            "Recording stopped",
            "On research. goodall could not read what it was recording, so it stopped. Run goodall status in "
            "Terminal for details.",
        ]
    ]


def test_a_damaged_marker_that_lost_its_workstream_says_the_workstream_is_unknown(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    account.marker_file().write_text('{"started_at": "2026-11-16T09:30:00Z"')
    account.now = MONDAY_MORNING + timedelta(hours=1)

    status = run_goodall(account, ["status"], helper_exit_code=0)

    [notification] = status.notifications()
    assert notification[1].startswith("Workstream unknown. ")


def test_a_notification_about_a_work_block_goodall_closed_that_fails_is_a_warning(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    leave_the_work_block_idle_overnight(account)

    status = run_goodall(account, ["status"], helper_exit_code=1)

    assert "WARN goodall could not show a notification that recording stopped" in status.everything_printed
    [record] = account.work_block_records()
    assert record["end_state"] == "inferred"


def test_status_json_includes_a_notification_that_failed_among_its_warnings(account: Account) -> None:
    account.install_helper()
    run_goodall(account, ["start"])
    leave_the_work_block_idle_overnight(account)

    status = run_goodall(account, ["status", "--json"], helper_exit_code=1)

    document = json.loads(status.standard_output)
    kinds = []
    for warning in document["warnings"]:
        kinds.append(warning["kind"])
    assert "notification_not_shown" in kinds


def test_the_menu_bar_icons_record_and_stop_items_show_no_notification(account: Account) -> None:
    # The icon's items run exactly `goodall start --workstream NAME` and
    # `goodall stop` (menu-bar-icon/MenuModel.swift): a person's click.
    account.install_helper()

    start = run_goodall(account, ["start", "--workstream", "research"])
    stop = run_goodall(account, ["stop"])

    assert start.notifications() == []
    assert stop.notifications() == []
