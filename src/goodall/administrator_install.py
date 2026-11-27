"""The administrator install: create the eval account and stage its first sign-in setup (ADR 0022).

Called by goodall.onboard when an answers file with `[eval_account]
create = true` is used in any account other than the one it names; in
practice an administrator account. It does not set up the account it runs
in. It stages, where only root can change it, everything the eval account's
first sign-in needs (goodall.system_places says where, and what each file
says), and creates the eval account with macOS's `sysadminctl`. The first
time someone signs in to that account, the first-run script runs
`goodall sign-in`, which sets the account up from the staged answers file
(goodall.sign_in). docs/administrator-install.md describes it for people,
with the assumptions not yet confirmed on a real Mac.

How it keeps its promises, and why:

- Every planned change, and every command it will run, is shown before the
  first one runs, and `--plan` shows the same and runs none. The terminal
  never asks (ADR 0012): supplying the answers file is the go-ahead.
- Goodall runs each privileged command itself (the owner's answer to Q84),
  through the runner with the terminal handed over, so `sudo` and
  `sysadminctl` ask for passwords themselves and goodall never sees, holds,
  stores or logs one. No password is ever on a command line, where every
  program on the Mac can read it: `-password -` and `-adminPassword -`
  make `sysadminctl` ask for each (sysadminctl -h).
- `-adminUser` names this account, so `sysadminctl` asks for this account's
  password and can give the new account a secure token on a Mac with
  FileVault. Whether it does is not confirmed (Q79).
- The account is created as a standard account: `sysadminctl` makes an
  administrator only when given `-admin`, which goodall never passes.
- Before anything runs, goodall checks that no account has the name, by
  the system's own account list (`dscl . -list /Users`, which only reads)
  and by the home folder a deleted account may have left behind.
- The staged files are copied into place with `install`, which sets their
  owner (root), group (wheel) and permissions as it copies, so there is no
  moment at which another account could change one.
- Order: the files are staged first and the account is created last. The
  staged answers file names the account, so if creating the account fails,
  running the same command again creates it, and once it exists a later
  run recognizes it as the one this install made rather than refusing it
  as taken.
- Goodall checks first that the pieces an administrator installs by hand
  are in place: goodall itself where every account can run it and only
  root can change it, and the menu bar icon, which is the recording
  indicator (ADR 0021). Without them the eval account's first sign-in
  could not work, so nothing is changed.

Running it again is safe: a file already in place, with the same text,
owner and permissions, is "unchanged", and nothing is run for it.
"""

import os
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from goodall import exit_codes, menu_bar_icon, system_places
from goodall.answers_file import Answers
from goodall.program_checks import ACCOUNT_GROUPS_COMMAND, ADMINISTRATOR_GROUPS
from goodall.runner import Command, OutputMode, Runner
from goodall.terminal import Failure, Terminal

SUDO = "/usr/bin/sudo"
SYSADMINCTL = "/usr/sbin/sysadminctl"
INSTALL = "/usr/bin/install"

# Lists every local account's short name, one per line, and changes nothing.
ACCOUNT_LIST_COMMAND = ["/usr/bin/dscl", ".", "-list", "/Users"]

# What a change does, as the plan and `--json` show it (the same words as
# goodall onboard's plan).
CREATE = "create"
CHANGE = "change"
UNCHANGED = "unchanged"

# Where a person reads about the install, and how to put goodall in place.
INSTALL_DOCUMENT = "docs/administrator-install.md in goodall's documentation"


def applies_to(answers: Answers) -> bool:
    """True when these answers ask for the administrator install rather than setting up this account.

    In the account the answers name, the same file sets that account up as
    usual: that is what first sign-in setup does with the staged copy.
    """
    eval_account = answers.eval_account
    if eval_account is None or not eval_account.create:
        return False
    return eval_account.name != system_places.current_account_name()


@dataclass(frozen=True)
class PrivilegedChange:
    """One change the administrator install would make to the Mac, and the commands that make it.

    - `id` is stable, for agents; `what` names it for a person.
    - `action` is CREATE, CHANGE or UNCHANGED; an unchanged one has no commands.
    - `details` say what exactly it does.
    """

    id: str
    what: str
    path: Path
    action: str
    details: list[str] = field(default_factory=list)
    commands: list[Command] = field(default_factory=list)

    def as_json(self, done: bool) -> dict[str, object]:
        """This change as `--json` shows it: the same keys as goodall onboard's, and the commands."""
        commands = []
        for command in self.commands:
            commands.append(command.as_typed)
        return {
            "id": self.id,
            "what": self.what,
            "path": str(self.path),
            "action": self.action,
            "details": list(self.details),
            "commands": commands,
            "done": done,
        }


@dataclass(frozen=True)
class InstallReport:
    """What one run came to, in the same shape and outcomes as goodall onboard's report."""

    answers_file: Path
    plan_only: bool
    outcome: str
    changes: list[PrivilegedChange] = field(default_factory=list)
    done_ids: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        changes = []
        for change in self.changes:
            changes.append(change.as_json(change.id in self.done_ids))
        failures = []
        for failure in self.failures:
            failures.append(failure.as_json())
        return {
            "answers_file": str(self.answers_file),
            "plan_only": self.plan_only,
            "outcome": self.outcome,
            "changes": changes,
            "warnings": [],
            "failures": failures,
        }


def run_administrator_install(
    terminal: Terminal, runner: Runner, answers: Answers, *, plan_only: bool, as_json: bool
) -> int:
    """Check, plan, show, and unless `plan_only` make the changes; return the exit code."""
    report = administrator_install_report(terminal, runner, answers, plan_only=plan_only, as_json=as_json)
    if report.outcome in ["rejected", "stopped"]:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def administrator_install_report(
    terminal: Terminal, runner: Runner, answers: Answers, *, plan_only: bool, as_json: bool
) -> InstallReport:
    """Do what `run_administrator_install` does, and return the report instead of the exit code.

    The setup page's waiting command needs the outcome: answers refused
    before anything changed ("rejected") can be corrected on the page that
    is still open, so it keeps waiting.
    """
    assert answers.eval_account is not None and answers.eval_account.name is not None
    account_name = answers.eval_account.name
    failures = check_ready(runner, answers)
    account_exists = False
    if not failures:
        account_reading = account_already_made(runner, account_name)
        if isinstance(account_reading, Failure):
            failures.append(account_reading)
        else:
            account_exists = account_reading
    if failures:
        report = InstallReport(answers.answers_file, plan_only, "rejected", failures=failures)
        show_report(terminal, report, account_name, as_json)
        return report

    # The files to copy into place are written first to a folder only this
    # account can open, and the folder goes when the run ends, so `--plan`
    # leaves nothing behind either.
    with tempfile.TemporaryDirectory(prefix="goodall-administrator-install-") as source_folder:
        changes = plan_changes(answers, Path(source_folder), account_exists)
        if not as_json:
            show_plan(terminal, answers, changes)
        changes_to_make = []
        for change in changes:
            if change.action != UNCHANGED:
                changes_to_make.append(change)
        if plan_only:
            report = InstallReport(answers.answers_file, True, "planned", changes)
            show_report(terminal, report, account_name, as_json)
            return report
        if not changes_to_make:
            report = InstallReport(answers.answers_file, False, "nothing_to_change", changes)
            show_report(terminal, report, account_name, as_json)
            return report
        done_ids: list[str] = []
        for change in changes_to_make:
            for command in change.commands:
                result = runner.run(command)
                if not result.succeeded:
                    problem = result.problem or f"`{command.as_typed}` did not work"
                    report = InstallReport(
                        answers.answers_file,
                        False,
                        "stopped",
                        changes,
                        done_ids,
                        [stopped_failure(change, problem)],
                    )
                    show_report(terminal, report, account_name, as_json)
                    return report
            done_ids.append(change.id)
    report = InstallReport(answers.answers_file, False, "applied", changes, done_ids)
    show_report(terminal, report, account_name, as_json)
    return report


# Checking that the Mac is ready, before anything is planned. Each check
# only looks.


def check_ready(runner: Runner, answers: Answers) -> list[Failure]:
    """Every reason the install cannot go ahead on this Mac, from this account, with these answers."""
    failures = []
    administrator_failure = check_administrator(runner)
    if administrator_failure is not None:
        failures.append(administrator_failure)
    installed_goodall = system_places.installed_goodall()
    if answers.goodall_program is not None and Path(answers.goodall_program) != installed_goodall:
        failures.append(hooks_goodall_not_for_every_account_failure(answers))
    goodall_problem = describe_protection(installed_goodall)
    if goodall_problem is not None:
        failures.append(goodall_not_installed_for_every_account_failure(goodall_problem))
    if menu_bar_icon.installed_helper() is None:
        failures.append(menu_bar_icon_not_installed_failure())
    return failures


def check_administrator(runner: Runner) -> Failure | None:
    """None when this account is an administrator account, else the failure saying where to run the install."""
    result = runner.run(Command(ACCOUNT_GROUPS_COMMAND))
    if not result.succeeded:
        return not_administrator_failure(f"goodall could not ask which groups it is in: {result.problem}")
    for group_name in result.standard_output.split():
        if group_name in ADMINISTRATOR_GROUPS:
            return None
    return not_administrator_failure("it is a standard account")


def describe_protection(path: Path) -> str | None:
    """None when `path` is a file owned by root that no other account can change; otherwise what is wrong.

    Used for goodall's own command and, by `goodall doctor`, for every
    staged file (goodall.first_sign_in_checks).
    """
    try:
        path_status = path.stat()
    except FileNotFoundError:
        return "there is nothing there"
    except OSError as stat_error:
        return f"it cannot be reached: {stat_error.strerror}"
    if not stat.S_ISREG(path_status.st_mode):
        return "it is not a file"
    if path_status.st_uid != system_places.ROOT_USER_ID:
        return "it is not owned by root, so the account that owns it can change it"
    if path_status.st_mode & system_places.GROUP_OR_OTHERS_MAY_WRITE:
        return "it can be changed by accounts other than root"
    return None


def account_already_made(runner: Runner, account_name: str) -> bool | Failure:
    """False when no account has the name; True when one does that this install made; else the failure.

    An account counts as made by this install when the staged answers file,
    which only root can write, names it: a run that staged the files and then
    created the account, or a run that stopped in between.
    """
    result = runner.run(Command(ACCOUNT_LIST_COMMAND))
    if not result.succeeded:
        return account_list_unknown_failure(result.problem or "it printed nothing")
    existing_names = result.standard_output.split()
    home_folder = system_places.USERS_FOLDER / account_name
    home_folder_exists = os.path.lexists(home_folder)
    if account_name not in existing_names and not home_folder_exists:
        return False
    if staged_answers_name() == account_name and account_name in existing_names:
        return True
    return account_name_taken_failure(account_name, home_folder, home_folder_exists)


def staged_answers_name() -> str | None:
    """The eval account the staged answers file names, when it is there and protected; otherwise None."""
    staged_file = system_places.staged_answers_file()
    if describe_protection(staged_file) is not None:
        return None
    return eval_account_name_in(staged_file)


def eval_account_name_in(answers_file: Path) -> str | None:
    """The `[eval_account] name` an answers file gives, or None when it gives none or cannot be read.

    Reads only that one answer, so `goodall sign-in` in any other account
    can tell it has nothing to do without checking the rest of the file.
    """
    try:
        table = tomllib.loads(answers_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    eval_account = table.get("eval_account")
    if not isinstance(eval_account, dict):
        return None
    name = eval_account.get("name")
    if isinstance(name, str):
        return name
    return None


# Planning: work out each change and the commands that make it.


def plan_changes(answers: Answers, source_folder: Path, account_exists: bool) -> list[PrivilegedChange]:
    """Every change, in the order it is made: the staged files first, the account last (see the module docstring)."""
    assert answers.eval_account is not None and answers.eval_account.name is not None
    account_name = answers.eval_account.name
    changes = [plan_staging_folder()]
    staged_files = [
        (
            "staged_answers",
            "Answers for the eval account",
            system_places.staged_answers_file(),
            answers.answers_file.read_text(encoding="utf-8"),
            system_places.READABLE_BY_EVERY_ACCOUNT,
            f"a copy of {answers.answers_file}, which {account_name}'s first sign-in sets the account up from",
        ),
        (
            "first_run_script",
            "First-run script",
            system_places.first_run_script(),
            system_places.first_run_script_text(),
            system_places.RUNNABLE_BY_EVERY_ACCOUNT,
            f"runs {system_places.installed_goodall()} sign-in, which does nothing in any account but {account_name}",
        ),
        (
            "first_run_agent",
            "First-run LaunchAgent",
            system_places.first_run_agent_file(),
            system_places.first_run_agent_text(),
            system_places.READABLE_BY_EVERY_ACCOUNT,
            "runs the first-run script at every sign-in, in every account",
        ),
        (
            "menu_bar_icon_agent",
            "Menu bar icon LaunchAgent",
            system_places.menu_bar_icon_agent_file(),
            system_places.menu_bar_icon_agent_text(),
            system_places.READABLE_BY_EVERY_ACCOUNT,
            (
                "starts the menu bar icon at every sign-in, telling it where goodall is, and brings it back if it "
                "stops with an error, but not after Quit goodall"
            ),
        ),
    ]
    for change_id, what, destination, text, mode, detail in staged_files:
        changes.append(plan_staged_file(change_id, what, destination, text, mode, detail, source_folder))
    changes.append(plan_eval_account(account_name, account_exists))
    return changes


def plan_staging_folder() -> PrivilegedChange:
    """The folder the staged files go in, owned by root."""
    folder = system_places.STAGING_FOLDER
    details = ["owned by root, readable by every account, changeable by root alone"]
    if folder_in_place(folder):
        return PrivilegedChange("staging_folder", "Staging folder", folder, UNCHANGED)
    command = privileged(
        [
            INSTALL,
            "-d",
            "-o",
            system_places.ROOT_USER_NAME,
            "-g",
            system_places.ROOT_GROUP_NAME,
            "-m",
            octal(system_places.RUNNABLE_BY_EVERY_ACCOUNT),
            str(folder),
        ]
    )
    return PrivilegedChange("staging_folder", "Staging folder", folder, CREATE, details, [command])


def plan_staged_file(
    change_id: str, what: str, destination: Path, text: str, mode: int, detail: str, source_folder: Path
) -> PrivilegedChange:
    """One file to copy into place, owned by root with `mode`; unchanged when it is already exactly so."""
    if file_in_place(destination, text, mode):
        return PrivilegedChange(change_id, what, destination, UNCHANGED)
    action = CREATE
    if os.path.lexists(destination):
        action = CHANGE
    source = source_folder / destination.name
    source.write_text(text, encoding="utf-8")
    command = privileged(
        [
            INSTALL,
            "-o",
            system_places.ROOT_USER_NAME,
            "-g",
            system_places.ROOT_GROUP_NAME,
            "-m",
            octal(mode),
            str(source),
            str(destination),
        ]
    )
    details = [detail, f"owned by root, permissions {octal(mode)}"]
    return PrivilegedChange(change_id, what, destination, action, details, [command])


def plan_eval_account(account_name: str, account_exists: bool) -> PrivilegedChange:
    """The eval account, created as a standard account by `sysadminctl`, which asks for each password itself."""
    home_folder = system_places.USERS_FOLDER / account_name
    if account_exists:
        return PrivilegedChange("eval_account", f"Eval account {account_name}", home_folder, UNCHANGED)
    command = privileged(
        [
            SYSADMINCTL,
            "-addUser",
            account_name,
            "-password",
            "-",
            "-adminUser",
            system_places.current_account_name(),
            "-adminPassword",
            "-",
        ]
    )
    details = [
        "a standard account, not an administrator",
        "sysadminctl asks you for this account's password, then for the new account's password",
    ]
    return PrivilegedChange("eval_account", f"Eval account {account_name}", home_folder, CREATE, details, [command])


def privileged(arguments: list[str]) -> Command:
    """A command run with `sudo`, with the terminal handed over so macOS asks for the password itself."""
    return Command([SUDO, *arguments], output=OutputMode.HAND_OVER)


def octal(mode: int) -> str:
    """Permissions as `install -m` and a person read them, such as 0644."""
    return f"{mode:04o}"


def file_in_place(path: Path, text: str, mode: int) -> bool:
    """True when `path` already holds `text`, owned by root, with exactly `mode`."""
    if describe_protection(path) is not None:
        return False
    if stat.S_IMODE(path.stat().st_mode) != mode:
        return False
    try:
        return path.read_text(encoding="utf-8") == text
    except (OSError, UnicodeDecodeError):
        return False


def folder_in_place(folder: Path) -> bool:
    """True when `folder` is a folder owned by root that no other account can change."""
    try:
        folder_status = folder.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(folder_status.st_mode) or folder_status.st_uid != system_places.ROOT_USER_ID:
        return False
    return folder_status.st_mode & system_places.GROUP_OR_OTHERS_MAY_WRITE == 0


# Showing what is planned and what happened.


def show_plan(terminal: Terminal, answers: Answers, changes: list[PrivilegedChange]) -> None:
    """The whole plan, then every command goodall will run, before any of them runs."""
    assert answers.eval_account is not None
    terminal.show_paragraph(
        f"Administrator install from the answers file {answers.answers_file}: goodall stages first sign-in setup "
        f"for the eval account {answers.eval_account.name} and creates that account. Nothing is set up in this "
        "account."
    )
    rows = []
    for change in changes:
        rows.append([change.action, change.what, str(change.path)])
    terminal.show_rows(["Change", "What", "Path"], rows)
    commands = []
    for change in changes:
        if change.details:
            terminal.show_paragraph(f"{change.what}: " + "; ".join(change.details) + ".")
        for command in change.commands:
            commands.append(command.as_typed)
    if commands:
        terminal.show_paragraph(
            "Goodall runs these commands, in this order, each with the terminal handed over to it. macOS asks for "
            "your administrator password, and sysadminctl asks you to choose the new account's password; goodall "
            "never sees either."
        )
        terminal.show_commands(tuple(commands), label_word="Runs")


def show_report(terminal: Terminal, report: InstallReport, account_name: str, as_json: bool) -> None:
    """How the run ended: the JSON document, or the closing words and any failure."""
    if as_json:
        terminal.write_json(report.as_json())
        return
    if report.failures:
        terminal.show_failures(report.failures)
    if report.outcome == "planned":
        terminal.show_paragraph(
            "Nothing was changed: this was the plan only. Run the same command without --plan to make these changes."
        )
    elif report.outcome == "nothing_to_change":
        terminal.show_paragraph(
            f"Nothing to change: the eval account {account_name} and its first sign-in setup are already in place."
        )
    elif report.outcome == "applied":
        terminal.show_paragraph(
            f"Done. Next: sign out of this account and sign in as {account_name} with the password you chose. "
            "macOS shows its own setup screens first; after them goodall sets up the account by itself and "
            f"starts recording. This has not yet been tried on a real Mac: {INSTALL_DOCUMENT} lists what to check."
        )
    elif report.outcome == "stopped":
        terminal.show_paragraph(
            f"Stopped after {len(report.done_ids)} changes. Running the same command again is safe: it makes only "
            "the changes still missing."
        )


# Failures. Each says what happened, why it matters and what to do next.


def not_administrator_failure(reason: str) -> Failure:
    return Failure(
        kind="administrator_install_needs_administrator",
        what_happened=(
            f"The administrator install cannot run in this macOS user account: {reason}. Nothing was changed."
        ),
        why="Creating an account and installing for every account on the Mac needs an administrator account.",
        what_to_do_next=(
            "Sign in to an administrator account on this Mac and run the same goodall onboard command there. "
            "To set up this account instead, remove the [eval_account] section from the answers file."
        ),
    )


def hooks_goodall_not_for_every_account_failure(answers: Answers) -> Failure:
    return Failure(
        kind="administrator_install_hooks_goodall",
        what_happened=(
            f"In the answers file at {answers.answers_file}, [claude_code] goodall_program is not "
            f"{system_places.installed_goodall()}. Nothing was changed."
        ),
        why=(
            "The eval account's Claude Code hooks must run the goodall installed for every account, which the eval "
            "account can run and cannot change, not a goodall in another account's home folder."
        ),
        what_to_do_next=(
            f'Set goodall_program = "{system_places.installed_goodall()}" under [claude_code], or remove the line, '
            "which means the same for the eval account."
        ),
    )


def goodall_not_installed_for_every_account_failure(problem: str) -> Failure:
    return Failure(
        kind="goodall_not_installed_for_every_account",
        what_happened=(
            f"goodall is not installed where every account can run it: at {system_places.installed_goodall()}, "
            f"{problem}. Nothing was changed."
        ),
        why=(
            "The eval account's first sign-in, its Claude Code hooks and the menu bar icon all run goodall from "
            "there. It must be owned by root so that neither the eval account nor an agent in it can change "
            "what runs."
        ),
        what_to_do_next=(
            f"Install goodall there from this administrator account, as {INSTALL_DOCUMENT} shows, then run the "
            "same goodall onboard command again."
        ),
    )


def menu_bar_icon_not_installed_failure() -> Failure:
    helper = menu_bar_icon.APPLICATIONS_FOLDER / menu_bar_icon.APPLICATION_NAME
    return Failure(
        kind="menu_bar_icon_not_installed",
        what_happened=f"The menu bar icon is not installed: there is no {helper}. Nothing was changed.",
        why=(
            "In an eval account, signing in means recording, and the menu bar icon is what shows the person that "
            "goodall is recording (ADR 0021)."
        ),
        what_to_do_next=(
            "Build the menu bar icon and copy it to the Applications folder, as Part 1 of docs/setup-by-hand.md "
            "shows, then run the same goodall onboard command again."
        ),
    )


def account_list_unknown_failure(problem: str) -> Failure:
    return Failure(
        kind="account_list_unknown",
        what_happened=f"goodall could not list the accounts on this Mac: {problem}. Nothing was changed.",
        why="goodall creates the eval account only once it knows no account already has its name.",
        what_to_do_next="Run dscl . -list /Users in Terminal to see why it fails, then run goodall onboard again.",
    )


def account_name_taken_failure(account_name: str, home_folder: Path, home_folder_exists: bool) -> Failure:
    if home_folder_exists:
        where = f"the home folder {home_folder} is there, left by an account of that name"
    else:
        where = "it is in this Mac's list of accounts"
    return Failure(
        kind="eval_account_name_taken",
        what_happened=(
            f"This Mac already has an account named {account_name}, or had one: {where}. Nothing was changed."
        ),
        why=(
            "An eval account starts empty and is used for one eval only, so goodall never takes over an account "
            "or a home folder that already exists."
        ),
        what_to_do_next=(
            "Choose another name for [eval_account] name in the answers file and run goodall onboard again. To "
            "use the existing account instead, sign in to it and run goodall onboard there without the "
            "[eval_account] section."
        ),
    )


def stopped_failure(change: PrivilegedChange, problem: str) -> Failure:
    return Failure(
        kind="administrator_install_stopped",
        what_happened=f"{change.what} was not made: {problem}.",
        why="goodall stops at the first command that does not work, so it never stages half a setup unnoticed.",
        what_to_do_next=(
            "If you mistyped a password, run the same goodall onboard command again: it makes only what is still "
            "missing. If it fails again, the message above is macOS's own; the changes made so far are listed "
            "above."
        ),
    )
