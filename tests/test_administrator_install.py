"""Outside-in tests for the administrator install: `goodall onboard --answers` with `[eval_account] create = true`.

Run in an administrator account, the answers file asks goodall to create
the eval account and stage its first sign-in setup (ADR 0022). Each test
drives goodall through `cli.main` with the fake runner. No real account is
created and nothing is written outside tmp_path: every privileged command
exists only as arguments handed to the fake runner, whose pretend `sudo`
does to the isolated folders of tests/conftest.py what the real command
would do to the Mac, so a test can look at the result.

The terminal never asks: standard input fails the test if goodall reads
from it.
"""

import io
import json
import os
import plistlib
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import cli, exit_codes, menu_bar_icon, system_places
from goodall.fake_runner import FakeRunner, ScriptedAnswer
from goodall.runner import Command, CommandOutcome, OutputMode
from goodall.terminal import Terminal

NOW = datetime(2026, 11, 27, 9, 30, 0, tzinfo=UTC)
EVAL_ACCOUNT_NAME = "goodall-eval"
ADMINISTRATOR_ACCOUNT_NAME = "goodall-administrator"
SUDO = "/usr/bin/sudo"
SYSADMINCTL = "/usr/sbin/sysadminctl"
INSTALL = "/usr/bin/install"
ACCOUNT_LIST_COMMAND = ["/usr/bin/dscl", ".", "-list", "/Users"]
GROUPS_COMMAND = ["/usr/bin/id", "-Gn"]

# Accounts every Mac has, and one other person's, by made-up name.
OTHER_ACCOUNTS = "_www\ndaemon\nnobody\nroot\ngoodall-someone-else\n"


class NoKeyboard(io.StringIO):
    """Standard input that fails the test if goodall ever tries to read an answer from it."""

    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("goodall tried to read from the terminal; the terminal never asks")


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give git a name and email in the temporary home folder, for the tests that onboard this account."""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    home = Path(os.environ["HOME"])
    (home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")


@pytest.fixture(autouse=True)
def administrator_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a made-up administrator account, so no real account name reaches a command line here."""
    monkeypatch.setattr(system_places, "current_account_name", lambda: ADMINISTRATOR_ACCOUNT_NAME)


@pytest.fixture(autouse=True)
def files_made_by_this_test_count_as_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test cannot make a file owned by root, so the files it makes stand for root's."""
    monkeypatch.setattr(system_places, "ROOT_USER_ID", os.getuid())


def answers_text(tmp_path: Path, eval_account: str) -> str:
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
record_repo = "~/evals/goodall-pilot-records"
log_directory = "~/evals/goodall-pilot-logs.noindex"

[claude_code]
goodall_program = "{system_places.installed_goodall()}"
{eval_account}"""


CREATE_EVAL_ACCOUNT = f"""
[eval_account]
create = true
name = "{EVAL_ACCOUNT_NAME}"
"""


def write_answers(tmp_path: Path, eval_account: str = CREATE_EVAL_ACCOUNT) -> Path:
    answers_file = tmp_path / "answers.toml"
    answers_file.write_text(answers_text(tmp_path, eval_account))
    return answers_file


def install_goodall_for_every_account() -> Path:
    """Pretend an administrator installed goodall where every account can run it."""
    goodall = system_places.installed_goodall()
    goodall.parent.mkdir(parents=True)
    goodall.write_text("#!/bin/sh\n")
    goodall.chmod(0o755)
    return goodall


def install_menu_bar_icon() -> None:
    """Pretend the menu bar icon's helper is in the Applications folder."""
    program = system_places.menu_bar_icon_program()
    program.parent.mkdir(parents=True)
    program.write_text("")


@dataclass
class Mac:
    """The pretend Mac: what its accounts and the administrator's terminal look like to the fake runner."""

    accounts: str = OTHER_ACCOUNTS
    groups: str = "staff everyone admin"
    failing_program: str | None = None
    screen: io.StringIO = field(default_factory=io.StringIO)
    # What the screen said when the first privileged command ran.
    screen_before_first_privileged_command: str | None = None

    def runner(self) -> FakeRunner:
        runner = FakeRunner()
        runner.answer(GROUPS_COMMAND, standard_output=self.groups + "\n")
        runner.answer(ACCOUNT_LIST_COMMAND, standard_output=self.accounts)
        runner.answer_with(SUDO, self.pretend_sudo)
        return runner

    def pretend_sudo(self, command: Command) -> ScriptedAnswer:
        """Do to the isolated folders what the real privileged command would do to the Mac."""
        if self.screen_before_first_privileged_command is None:
            self.screen_before_first_privileged_command = self.screen.getvalue()
        arguments = command.arguments[1:]
        if arguments[0] == self.failing_program:
            return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=1)
        if arguments[0] == SYSADMINCTL:
            name = arguments[arguments.index("-addUser") + 1]
            (system_places.USERS_FOLDER / name).mkdir()
            self.accounts += name + "\n"
            return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=0)
        if arguments[0] == INSTALL and "-d" in arguments:
            folder = Path(arguments[-1])
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(int(arguments[arguments.index("-m") + 1], 8))
            return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=0)
        if arguments[0] == INSTALL:
            source, destination = Path(arguments[-2]), Path(arguments[-1])
            shutil.copyfile(source, destination)
            destination.chmod(int(arguments[arguments.index("-m") + 1], 8))
            return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=0)
        raise AssertionError(f"The test's pretend sudo does not know {command.as_typed}")


@dataclass
class GoodallRun:
    exit_code: int
    output: str
    errors: str
    runner: FakeRunner

    @property
    def privileged_commands(self) -> list[Command]:
        found = []
        for command in self.runner.calls:
            if command.arguments[0] == SUDO:
                found.append(command)
        return found

    @property
    def screen(self) -> str:
        return " ".join((self.output + self.errors).split())


def run_goodall(arguments: list[str], mac: Mac | None = None) -> GoodallRun:
    if mac is None:
        mac = Mac()
    mac.screen = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(mac.screen, standard_error, standard_input=NoKeyboard())
    runner = mac.runner()
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: NOW)
    return GoodallRun(exit_code, mac.screen.getvalue(), standard_error.getvalue(), runner)


@pytest.fixture
def ready_mac() -> Mac:
    """A Mac where an administrator has installed goodall and the menu bar icon for every account."""
    install_goodall_for_every_account()
    install_menu_bar_icon()
    return Mac()


def staged_files() -> list[Path]:
    return [
        system_places.staged_answers_file(),
        system_places.first_run_script(),
        system_places.first_run_agent_file(),
        system_places.menu_bar_icon_agent_file(),
    ]


def permissions(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_administrator_install_creates_a_standard_eval_account_with_sysadminctl(
    tmp_path: Path, ready_mac: Mac
) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    account_creations = []
    for command in run.privileged_commands:
        if command.arguments[1] == SYSADMINCTL:
            account_creations.append(command.arguments)
    assert account_creations == [
        [
            SUDO,
            SYSADMINCTL,
            "-addUser",
            EVAL_ACCOUNT_NAME,
            "-password",
            "-",
            "-adminUser",
            ADMINISTRATOR_ACCOUNT_NAME,
            "-adminPassword",
            "-",
        ]
    ]
    # A standard account: sysadminctl makes an administrator only when asked with -admin.
    assert "-admin" not in account_creations[0]
    assert (system_places.USERS_FOLDER / EVAL_ACCOUNT_NAME).is_dir()


def test_no_password_ever_appears_on_a_command_line_and_every_privileged_command_gets_the_terminal(
    tmp_path: Path, ready_mac: Mac
) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert run.privileged_commands != []
    for command in run.privileged_commands:
        assert command.output == OutputMode.HAND_OVER, command.as_typed
        for position, argument in enumerate(command.arguments):
            if argument.lower().endswith("password"):
                # "-" makes sysadminctl ask for the password itself.
                assert command.arguments[position + 1] == "-", command.as_typed


def test_the_install_stages_first_sign_in_setup_where_only_root_can_change_it(tmp_path: Path, ready_mac: Mac) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert system_places.staged_answers_file().read_bytes() == answers_file.read_bytes()
    assert permissions(system_places.staged_answers_file()) == 0o644
    script = system_places.first_run_script()
    assert permissions(script) == 0o755
    assert f'exec "{system_places.installed_goodall()}" sign-in' in script.read_text()

    first_run_agent = plistlib.loads(system_places.first_run_agent_file().read_bytes())
    assert first_run_agent["ProgramArguments"] == [str(script)]
    assert first_run_agent["RunAtLoad"] is True
    icon_agent = plistlib.loads(system_places.menu_bar_icon_agent_file().read_bytes())
    assert icon_agent["ProgramArguments"] == [str(system_places.menu_bar_icon_program())]
    assert icon_agent["EnvironmentVariables"] == {"GOODALL_COMMAND": str(system_places.installed_goodall())}
    assert icon_agent["KeepAlive"] == {"SuccessfulExit": False}
    for agent_file in [system_places.first_run_agent_file(), system_places.menu_bar_icon_agent_file()]:
        assert permissions(agent_file) == 0o644

    # Every staged file is installed owned by root, in root's group.
    for command in run.privileged_commands:
        if command.arguments[1] == INSTALL:
            assert command.arguments[2:7] in [["-o", "root", "-g", "wheel", "-m"], ["-d", "-o", "root", "-g", "wheel"]]


def test_nothing_is_ever_staged_in_the_folder_every_account_can_write(tmp_path: Path, ready_mac: Mac) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    for command in run.privileged_commands:
        assert "/Users/Shared" not in command.as_typed


def test_every_planned_change_and_command_is_shown_before_the_first_privileged_command(
    tmp_path: Path, ready_mac: Mac
) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    shown_first = ready_mac.screen_before_first_privileged_command
    assert shown_first is not None
    for command in run.privileged_commands:
        assert command.as_typed in shown_first
    assert "never sees" in " ".join(shown_first.split())


def test_the_plan_shows_every_privileged_command_and_runs_none(tmp_path: Path, ready_mac: Mac) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file), "--plan"], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert run.privileged_commands == []
    assert f"{SUDO} {SYSADMINCTL} -addUser {EVAL_ACCOUNT_NAME}" in run.output
    assert "Nothing was changed" in run.screen
    for staged_file in staged_files():
        assert not staged_file.exists()
    assert not (system_places.USERS_FOLDER / EVAL_ACCOUNT_NAME).exists()


def test_an_existing_account_name_is_refused_with_a_next_step_and_nothing_is_run(
    tmp_path: Path, ready_mac: Mac
) -> None:
    ready_mac.accounts += EVAL_ACCOUNT_NAME + "\n"
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert "already has an account" in run.screen
    assert "[eval_account] name" in run.screen
    for staged_file in staged_files():
        assert not staged_file.exists()


def test_a_home_folder_left_by_a_deleted_account_of_that_name_is_refused_too(tmp_path: Path, ready_mac: Mac) -> None:
    (system_places.USERS_FOLDER / EVAL_ACCOUNT_NAME).mkdir()
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert str(system_places.USERS_FOLDER / EVAL_ACCOUNT_NAME) in run.screen


def test_declining_to_create_an_eval_account_creates_nothing_and_stages_nothing(tmp_path: Path, ready_mac: Mac) -> None:
    answers_file = write_answers(tmp_path, eval_account="\n[eval_account]\ncreate = false\n")

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    # Declining means this account is set up as answered, as without the table.
    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert run.privileged_commands == []
    assert list(system_places.USERS_FOLDER.iterdir()) == []
    for staged_file in staged_files():
        assert not staged_file.exists()


def test_a_standard_account_is_told_to_run_the_install_from_an_administrator_account(
    tmp_path: Path, ready_mac: Mac
) -> None:
    ready_mac.groups = "staff everyone"
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert "administrator account" in run.screen


def test_goodall_not_installed_for_every_account_is_refused_with_where_to_install_it(tmp_path: Path) -> None:
    install_menu_bar_icon()
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)])

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert str(system_places.installed_goodall()) in run.screen
    assert "docs/administrator-install.md" in run.screen


def test_goodall_installed_where_others_can_change_it_is_refused(tmp_path: Path, ready_mac: Mac) -> None:
    system_places.installed_goodall().chmod(0o777)
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert "can be changed" in run.screen


def test_a_mac_without_the_menu_bar_icon_is_refused_because_it_is_the_recording_indicator(tmp_path: Path) -> None:
    install_goodall_for_every_account()
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)])

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert str(menu_bar_icon.APPLICATIONS_FOLDER / "goodall.app") in run.screen


def test_a_privileged_command_that_fails_stops_the_install_and_says_how_to_go_on(
    tmp_path: Path, ready_mac: Mac
) -> None:
    ready_mac.failing_program = INSTALL
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.ERROR
    assert len(run.privileged_commands) == 1
    for staged_file in staged_files():
        assert not staged_file.exists()
    assert not (system_places.USERS_FOLDER / EVAL_ACCOUNT_NAME).exists()
    assert "Stopped" in run.screen
    assert "again" in run.screen


def test_when_creating_the_account_fails_running_again_creates_it(tmp_path: Path, ready_mac: Mac) -> None:
    # For example a mistyped password: the staged files are in place, the account is not.
    ready_mac.failing_program = SYSADMINCTL
    answers_file = write_answers(tmp_path)
    failed_run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)
    assert failed_run.exit_code == exit_codes.ERROR
    ready_mac.failing_program = None

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    assert len(run.privileged_commands) == 1
    assert run.privileged_commands[0].arguments[1] == SYSADMINCTL
    assert (system_places.USERS_FOLDER / EVAL_ACCOUNT_NAME).is_dir()


def test_running_the_install_again_changes_nothing_and_runs_no_privileged_command(
    tmp_path: Path, ready_mac: Mac
) -> None:
    answers_file = write_answers(tmp_path)
    first_run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)
    assert first_run.exit_code == exit_codes.SUCCESS, first_run.screen

    second_run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert second_run.exit_code == exit_codes.SUCCESS, second_run.screen
    assert second_run.privileged_commands == []
    assert "Nothing to change" in second_run.screen


def test_hooks_that_would_run_a_goodall_other_than_the_one_for_every_account_are_refused(
    tmp_path: Path, ready_mac: Mac
) -> None:
    answers_file = write_answers(tmp_path)
    answers_file.write_text(
        answers_file.read_text().replace(str(system_places.installed_goodall()), "/Users/ada/.local/bin/goodall")
    )

    run = run_goodall(["onboard", "--answers", str(answers_file)], ready_mac)

    assert run.exit_code == exit_codes.ERROR
    assert run.privileged_commands == []
    assert "[claude_code] goodall_program" in run.screen
    assert str(system_places.installed_goodall()) in run.screen


def test_the_install_as_json_lists_each_change_with_its_command(tmp_path: Path, ready_mac: Mac) -> None:
    answers_file = write_answers(tmp_path)

    run = run_goodall(["onboard", "--answers", str(answers_file), "--plan", "--json"], ready_mac)

    assert run.exit_code == exit_codes.SUCCESS, run.screen
    document = json.loads(run.output)
    assert document["outcome"] == "planned"
    ids = []
    for change in document["changes"]:
        ids.append(change["id"])
        assert change["action"] == "create"
        assert change["done"] is False
        assert change["commands"] != []
    # The account comes last, so a failed creation can be run again (goodall.administrator_install).
    assert ids == [
        "staging_folder",
        "staged_answers",
        "first_run_script",
        "first_run_agent",
        "menu_bar_icon_agent",
        "eval_account",
    ]
