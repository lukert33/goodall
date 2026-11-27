"""Outside-in tests for `goodall sign-in`, which the first-run script runs at every sign-in to every account.

The administrator install (tests/test_administrator_install.py) stages an
answers file naming the eval account, a first-run script and a LaunchAgent
that runs it (ADR 0022). These tests start where that leaves off: the
staged answers file is in the isolated staging folder of tests/conftest.py,
and each test runs `goodall sign-in` through `cli.main` as launchd would,
in a pretend account whose name the test chooses. Nothing real is set up:
HOME, local settings and Claude Code's settings are temporary folders, git
runs for real only inside them, and every other program is the fake
runner's.

The terminal never asks: standard input fails the test if goodall reads
from it.
"""

import io
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import cli, exit_codes, system_places
from goodall.claude_code_checks import goodall_hook_commands_by_event
from goodall.fake_runner import FakeBackgroundPrograms, FakeRunner, InProcessTelemetryReceiver, ScriptedAnswer
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.runner import Command, CommandOutcome
from goodall.terminal import Terminal

EVAL_ACCOUNT_NAME = "goodall-eval"
OTHER_ACCOUNT_NAME = "goodall-someone-else"
MONDAY_MORNING = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
RECEIVER_COMMAND_START = [sys.executable, "-I", "-m", "goodall.telemetry_receiver"]


class NoKeyboard(io.StringIO):
    """Standard input that fails the test if goodall ever tries to read an answer from it."""

    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("goodall tried to read from the terminal; the terminal never asks")


def answers_text(tmp_path: Path) -> str:
    return f"""schema_version = 1
account_mode = "dedicated"
eval = "new"

[new_eval]
name = "goodall-pilot"
description = "Building a product to first recurring revenue with AI agents."
workstreams = ["engineering", "research"]

[[new_eval.scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]

[contributor]
id = "ada-lovelace"
display_name = "Ada Lovelace"
main_workstream = "research"

[folders]
record_repo = "{tmp_path / "evals" / "goodall-pilot-records"}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[eval_account]
create = true
name = "{EVAL_ACCOUNT_NAME}"
"""


@dataclass
class Mac:
    """The pretend Mac after the administrator install, and the account that is signing in."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    background: FakeBackgroundPrograms
    now: datetime = MONDAY_MORNING

    def sign_in_as(self, account_name: str) -> None:
        self.monkeypatch.setattr(system_places, "current_account_name", lambda: account_name)

    @property
    def home(self) -> Path:
        return Path(os.environ["HOME"])

    @property
    def log_directory(self) -> Path:
        return self.tmp_path / "evals" / "goodall-pilot-logs.noindex"

    @property
    def sign_in_log(self) -> Path:
        return self.home / "Library" / "Logs" / "goodall" / "sign-in.log"

    def marker_file(self) -> Path:
        return self.log_directory / "work-blocks" / "active-work-block.json"

    def give_git_an_identity(self) -> None:
        """What Part 2 of docs/setup-by-hand.md asks of the eval account; see the known gap in sign_in."""
        (self.home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")


@pytest.fixture
def mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Mac]:
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(system_places, "ROOT_USER_ID", os.getuid())
    staging_folder = system_places.STAGING_FOLDER
    staging_folder.mkdir(parents=True)
    staged_answers = system_places.staged_answers_file()
    staged_answers.write_text(answers_text(tmp_path))
    staged_answers.chmod(0o644)
    helper = system_places.menu_bar_icon_program()
    helper.parent.mkdir(parents=True)
    helper.write_text("")
    pretend = Mac(tmp_path, monkeypatch, FakeBackgroundPrograms())
    pretend.give_git_an_identity()
    yield pretend
    pretend.background.stop_all()


@dataclass
class GoodallRun:
    exit_code: int
    output: str
    errors: str
    runner: FakeRunner

    @property
    def screen(self) -> str:
        return " ".join((self.output + "\n" + self.errors).split())

    def notifications(self) -> list[list[str]]:
        found = []
        for command in self.runner.calls:
            if command.arguments[0] == str(system_places.menu_bar_icon_program()):
                found.append(command.arguments[2:])
        return found

    def opened(self) -> list[list[str]]:
        found = []
        for command in self.runner.calls:
            if command.arguments[0] == "/usr/bin/open":
                found.append(command.arguments)
        return found


def run_goodall(mac: Mac, arguments: list[str]) -> GoodallRun:
    runner = FakeRunner(real_programs=["git"], background=mac.background)
    runner.answer(["claude", "--version"], standard_output="2.3.4 (Claude Code)\n")
    runner.answer_background(RECEIVER_COMMAND_START, InProcessTelemetryReceiver)

    def works(command: Command) -> ScriptedAnswer:
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0)

    runner.answer_with("open", works)
    runner.answer_with("/usr/bin/open", works)
    runner.answer_with(str(system_places.menu_bar_icon_program()), works)
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output, standard_error, standard_input=NoKeyboard())
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: mac.now)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue(), runner)


def snapshot(folder: Path) -> dict[Path, bytes | None]:
    """Every file and folder under `folder`, with each file's bytes."""
    found: dict[Path, bytes | None] = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found[path] = path.read_bytes()
        else:
            found[path] = None
    return found


def test_in_any_other_account_sign_in_exits_having_changed_nothing(mac: Mac) -> None:
    mac.sign_in_as(OTHER_ACCOUNT_NAME)
    before = snapshot(mac.tmp_path)
    home_before = snapshot(mac.home)
    config_before = snapshot(Path(os.environ["GOODALL_CONFIG_DIR"]))

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert run.runner.calls == []
    assert snapshot(mac.tmp_path) == before
    assert snapshot(mac.home) == home_before
    assert snapshot(Path(os.environ["GOODALL_CONFIG_DIR"])) == config_before
    assert "not the eval account" in run.screen


def test_with_no_first_sign_in_setup_staged_sign_in_does_nothing(mac: Mac) -> None:
    system_places.staged_answers_file().unlink()
    mac.sign_in_as(EVAL_ACCOUNT_NAME)
    home_before = snapshot(mac.home)

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert run.runner.calls == []
    assert snapshot(mac.home) == home_before


def test_the_first_sign_in_sets_up_the_eval_account_and_starts_recording_with_a_notification(mac: Mac) -> None:
    mac.sign_in_as(EVAL_ACCOUNT_NAME)

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    settings = read_local_settings()
    assert isinstance(settings, LocalSettings), settings
    assert settings.account_mode == "dedicated"
    # The hooks run the goodall installed for every account, never one in a home folder.
    hooks = json.loads((Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json").read_text())["hooks"]
    for commands in goodall_hook_commands_by_event(hooks).values():
        assert commands[0].program == str(system_places.installed_goodall())
    assert mac.marker_file().exists()
    marker = json.loads(mac.marker_file().read_text())
    assert marker["workstream"] == "research"
    assert run.notifications() == [["Recording started", "On research, because you signed in."]]


def test_the_first_sign_in_says_what_it_does_at_each_step_and_shows_it_to_the_person(mac: Mac) -> None:
    mac.sign_in_as(EVAL_ACCOUNT_NAME)

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    log = " ".join(mac.sign_in_log.read_text().split())
    assert "First sign-in" in log
    assert "Local settings" in log
    assert "Claude Code hooks" in log
    assert "Started a work block on research" in log
    assert run.opened() == [["/usr/bin/open", "-t", str(mac.sign_in_log)]]


def test_a_second_sign_in_repeats_no_setup_step(mac: Mac) -> None:
    mac.sign_in_as(EVAL_ACCOUNT_NAME)
    first = run_goodall(mac, ["sign-in"])
    assert first.exit_code == exit_codes.SUCCESS, first.screen
    mac.now = MONDAY_MORNING + timedelta(hours=1)
    run_goodall(mac, ["stop", "--background", "sign-out"])
    settings_file = Path(os.environ["GOODALL_CONFIG_DIR"]) / "local-settings.toml"
    claude_settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    setup_before = {settings_file: settings_file.read_bytes(), claude_settings: claude_settings.read_bytes()}
    mac.now = MONDAY_MORNING + timedelta(days=1)

    second = run_goodall(mac, ["sign-in"])

    assert second.exit_code == exit_codes.SUCCESS, second.screen
    assert "Setting up this macOS user account" not in second.screen
    for path, contents in setup_before.items():
        assert path.read_bytes() == contents
    assert second.opened() == []
    assert mac.marker_file().exists()
    assert second.notifications() == [["Recording started", "On research, because you signed in."]]


def test_a_sign_in_with_a_work_block_already_active_starts_nothing(mac: Mac) -> None:
    mac.sign_in_as(EVAL_ACCOUNT_NAME)
    run_goodall(mac, ["sign-in"])
    work_block_before = json.loads(mac.marker_file().read_text())["id"]
    mac.now = MONDAY_MORNING + timedelta(minutes=30)

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert json.loads(mac.marker_file().read_text())["id"] == work_block_before
    assert run.notifications() == []
    assert "already active" in run.screen


def test_starting_a_work_block_at_sign_in_can_be_turned_off_in_local_settings(mac: Mac) -> None:
    mac.sign_in_as(EVAL_ACCOUNT_NAME)
    run_goodall(mac, ["sign-in"])
    run_goodall(mac, ["stop"])
    settings_file = Path(os.environ["GOODALL_CONFIG_DIR"]) / "local-settings.toml"
    settings_file.write_text(settings_file.read_text() + "start_at_sign_in = false\n")
    mac.now = MONDAY_MORNING + timedelta(days=1)

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert not mac.marker_file().exists()
    assert run.notifications() == []
    assert "start_at_sign_in" in run.screen


def test_a_sign_out_stop_after_a_sign_in_start_says_so(mac: Mac) -> None:
    # The stop half of ADR 0021's sign-in and sign-out. What triggers it at
    # sign-out is not built: it waits for the hand test (goodall-ayy.11).
    mac.sign_in_as(EVAL_ACCOUNT_NAME)
    run_goodall(mac, ["sign-in"])
    mac.now = MONDAY_MORNING + timedelta(hours=1)

    stop = run_goodall(mac, ["stop", "--background", "sign-out"])

    assert stop.exit_code == exit_codes.SUCCESS, stop.screen
    assert not mac.marker_file().exists()
    assert stop.notifications() == [["Recording stopped", "On research, because you signed out."]]


def test_first_sign_in_setup_that_fails_says_what_to_do_and_is_tried_again_at_the_next_sign_in(mac: Mac) -> None:
    (mac.home / ".gitconfig").unlink()
    mac.sign_in_as(EVAL_ACCOUNT_NAME)

    failed = run_goodall(mac, ["sign-in"])

    assert failed.exit_code == exit_codes.ERROR
    assert not mac.marker_file().exists()
    log = " ".join(mac.sign_in_log.read_text().split())
    assert "git" in log
    assert "next time you sign in" in log
    assert failed.opened() == [["/usr/bin/open", "-t", str(mac.sign_in_log)]]

    mac.give_git_an_identity()
    retried = run_goodall(mac, ["sign-in"])

    assert retried.exit_code == exit_codes.SUCCESS, retried.screen
    assert mac.marker_file().exists()


def test_a_staged_answers_file_another_account_could_change_is_not_used(mac: Mac) -> None:
    system_places.staged_answers_file().chmod(0o666)
    mac.sign_in_as(EVAL_ACCOUNT_NAME)

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.ERROR
    assert isinstance(read_local_settings(), list)
    assert not mac.marker_file().exists()
    assert "administrator" in run.screen


def test_a_start_at_sign_in_setting_that_is_not_true_or_false_is_refused_with_a_correct_line(mac: Mac) -> None:
    mac.sign_in_as(EVAL_ACCOUNT_NAME)
    run_goodall(mac, ["sign-in"])
    run_goodall(mac, ["stop"])
    settings_file = Path(os.environ["GOODALL_CONFIG_DIR"]) / "local-settings.toml"
    settings_file.write_text(settings_file.read_text() + 'start_at_sign_in = "no"\n')

    run = run_goodall(mac, ["sign-in"])

    assert run.exit_code == exit_codes.ERROR
    assert not mac.marker_file().exists()
    assert "start_at_sign_in = false" in run.screen
