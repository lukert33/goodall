"""Outside-in tests for `goodall start` bringing the menu bar icon back (ADR 0024).

Each test drives `goodall start` through `cli.main`, in a pretend account
inside tmp_path, with the fake runner: `open`, the macOS program that
starts an application, is scripted, so no test starts a real helper, and a
test that forgets to script it fails. The "installed helper" is an empty
folder named goodall.app in a pretend Applications folder, which stands in
for the Mac's real `/Applications`.

The helper itself is tested in tests/test_menu_bar_icon_helper.py.
"""

import io
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import cli, exit_codes, menu_bar_icon
from goodall.fake_runner import (
    FAKE_PROGRAM_FOLDER,
    FakeBackgroundPrograms,
    FakeRunner,
    InProcessTelemetryReceiver,
    ScriptedAnswer,
)
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


def git(working_directory: Path, *arguments: str) -> None:
    """Run git for the test's own setup."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    subprocess.run(["git", *arguments], cwd=working_directory, env=environment, capture_output=True, check=True)


@dataclass
class Account:
    """A pretend macOS user account set up for goodall."""

    home_folder: Path
    applications_folder: Path
    log_directory: Path
    background: FakeBackgroundPrograms

    @property
    def installed_helper(self) -> Path:
        return self.applications_folder / "goodall.app"

    def install_helper(self) -> None:
        """Pretend the menu bar icon's helper is installed: goodall only looks for the folder."""
        self.installed_helper.mkdir(parents=True)


@pytest.fixture
def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Account]:
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "records"
    log_directory = tmp_path / "logs"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    # Beside the home folder, not inside it: the real one belongs to the whole Mac.
    applications_folder = tmp_path / "Applications"
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
    account = Account(home_folder, applications_folder, log_directory, FakeBackgroundPrograms())
    yield account
    account.background.stop_all()
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def goodall_beside_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend goodall is installed as uv installs it: a `goodall` program beside the Python running it."""
    bin_folder = tmp_path / "tool" / "bin"
    bin_folder.mkdir(parents=True)
    (bin_folder / "python").write_text("")
    (bin_folder / "goodall").write_text("")
    monkeypatch.setattr(sys, "executable", str(bin_folder / "python"))
    return bin_folder / "goodall"


@dataclass
class GoodallRun:
    exit_code: int
    standard_output: str
    standard_error: str
    runner: FakeRunner

    @property
    def everything_printed(self) -> str:
        return " ".join((self.standard_output + "\n" + self.standard_error).split())

    def open_commands(self) -> list[Command]:
        """Every time goodall ran `open`, the program that starts an application."""
        found = []
        for command in self.runner.calls:
            if command.arguments[0] == "open":
                found.append(command)
        return found


def run_start(account: Account, open_exit_code: int | None = 0, goodall_on_path: bool = False) -> GoodallRun:
    """Run `goodall start`. `open` exits with `open_exit_code`; None leaves it unscripted, so running it fails."""
    installed_programs = []
    if goodall_on_path:
        installed_programs.append("goodall")
    runner = FakeRunner(installed_programs=installed_programs, real_programs=["git"], background=account.background)
    runner.answer(["claude", "--version"], standard_output="2.3.4 (Claude Code)\n")
    runner.answer_background([sys.executable, "-I", "-m", "goodall.telemetry_receiver"], InProcessTelemetryReceiver)
    if open_exit_code is not None:
        what_open_prints = ""
        if open_exit_code != 0:
            what_open_prints = "The application cannot be opened for an unexpected reason\n"
        scripted_answer = ScriptedAnswer(
            outcome=CommandOutcome.COMPLETED, exit_code=open_exit_code, standard_error=what_open_prints
        )
        runner.answer_with("open", lambda command: scripted_answer)
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(["start"], terminal=terminal, runner=runner, clock=lambda: MONDAY_MORNING)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue(), runner)


# ---------------------------------------------------------------------------
# goodall start brings the icon back
# ---------------------------------------------------------------------------


def test_start_opens_the_installed_helper_in_the_background_and_tells_it_where_goodall_is(
    account: Account, goodall_beside_python: Path
) -> None:
    account.install_helper()

    start = run_start(account)

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    [open_command] = start.open_commands()
    assert open_command.arguments == [
        "open",
        "-g",
        "--env",
        f"GOODALL_COMMAND={goodall_beside_python}",
        str(account.installed_helper),
    ]
    assert start.standard_error == ""


def test_the_helper_is_looked_for_in_the_applications_folder_the_whole_mac_shares() -> None:
    # The owner's decision of 2026-11-27: one copy in `/Applications` serves
    # every macOS user account, and only an administrator can change it
    # (ADR 0022). Asked of a fresh Python, because every test here runs with
    # the folder pointed somewhere else (tests/conftest.py).
    asked = subprocess.run(
        [sys.executable, "-c", "from goodall import menu_bar_icon; print(menu_bar_icon.APPLICATIONS_FOLDER)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert asked.stdout.strip() == "/Applications"


def test_a_helper_in_the_home_folder_is_not_the_installed_one(account: Account) -> None:
    # A copy in `~/Applications` is one the account itself could have put
    # there or changed. `open` is not scripted, so running it would fail.
    (account.home_folder / "Applications" / "goodall.app").mkdir(parents=True)

    start = run_start(account, open_exit_code=None)

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert start.open_commands() == []


def test_start_without_an_installed_helper_starts_no_program_for_it(account: Account) -> None:
    # `open` is not scripted, so running it would fail this test.
    start = run_start(account, open_exit_code=None)

    assert start.exit_code == exit_codes.SUCCESS, start.standard_error
    assert start.open_commands() == []
    assert "icon" not in start.everything_printed


def test_start_while_a_work_block_is_already_active_still_brings_the_icon_back(
    account: Account, goodall_beside_python: Path
) -> None:
    # "Quit goodall" removes the icon, and the person may start recording
    # again from a terminal, or run goodall start only to get the icon back.
    run_start(account, open_exit_code=None)
    account.install_helper()

    start = run_start(account)

    assert start.exit_code == exit_codes.SUCCESS
    assert "already" in start.everything_printed
    assert len(start.open_commands()) == 1


def test_start_still_succeeds_with_a_warning_when_the_helper_cannot_be_opened(
    account: Account, goodall_beside_python: Path
) -> None:
    account.install_helper()

    start = run_start(account, open_exit_code=1)

    assert start.exit_code == exit_codes.SUCCESS
    assert "Started a work block on research" in start.everything_printed
    assert (account.log_directory / "work-blocks" / "active-work-block.json").exists()
    assert "WARN The menu bar icon could not be started" in start.everything_printed
    assert "Goodall is recording all the same" in start.everything_printed
    assert "The application cannot be opened for an unexpected reason" in start.everything_printed


def test_start_finds_goodall_on_the_path_when_none_is_beside_python(
    account: Account, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account.install_helper()
    lonely_bin_folder = tmp_path / "lonely" / "bin"
    lonely_bin_folder.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(lonely_bin_folder / "python"))

    start = run_start(account, goodall_on_path=True)

    [open_command] = start.open_commands()
    assert f"GOODALL_COMMAND={FAKE_PROGRAM_FOLDER / 'goodall'}" in open_command.arguments


def test_start_warns_and_opens_nothing_when_it_cannot_say_where_goodall_is(
    account: Account, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A helper started without knowing where goodall is could run nothing.
    account.install_helper()
    lonely_bin_folder = tmp_path / "lonely" / "bin"
    lonely_bin_folder.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(lonely_bin_folder / "python"))

    start = run_start(account, open_exit_code=None)

    assert start.exit_code == exit_codes.SUCCESS
    assert start.open_commands() == []
    assert "WARN The menu bar icon could not be started" in start.everything_printed
