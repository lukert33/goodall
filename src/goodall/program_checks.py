"""The diagnostic checks that need other programs: the tools, the macOS user account, and the record repo.

`goodall doctor` runs `PROGRAM_CHECKS` after the settings checks
(goodall.doctor). Every program they ask about goes through the runner in
the check context (goodall.runner), so a test decides what each program
does, and git runs for real.
"""

from dataclasses import dataclass
from pathlib import Path

from goodall.checks import Check, CheckContext, CheckOutcome, failed, passed
from goodall.runner import Command
from goodall.settings_checks import LOCAL_SETTINGS_VALID, RECORD_REPO_FOUND, valid_local_settings
from goodall.terminal import Failure

TOOLS_GROUP = "Tools"
EVAL_ACCOUNT_GROUP = "Eval account"
RECORD_REPO_GROUP = "Record repo"

GIT_INSTALLED = "tools.git_installed"
GH_INSTALLED = "tools.gh_installed"
SECRETS_MANAGER_INSTALLED = "tools.secrets_manager_installed"
STANDARD_ACCOUNT = "eval_account.standard_account"
RECORD_REPO_IS_GIT_REPOSITORY = "record_repo.is_git_repository"

# The command that lists this macOS user account's groups by name.
ACCOUNT_GROUPS_COMMAND = ["/usr/bin/id", "-Gn"]


@dataclass(frozen=True)
class RequiredProgram:
    """A program goodall needs, and what to tell a contributor when it is missing.

    - `name` is the command, as found on the PATH.
    - `described` names it for a person, such as "gh, GitHub's command-line tool".
    - `why` says what goodall uses it for.
    - `how_to_install` says where to get it, as the end of a sentence.
    """

    name: str
    described: str
    why: str
    how_to_install: str


GIT = RequiredProgram(
    name="git",
    described="git",
    why="Goodall keeps the eval's records in the record repo with git, and commits and pushes them.",
    how_to_install="with Apple's Command Line Tools, by running xcode-select --install in Terminal",
)

GH = RequiredProgram(
    name="gh",
    described="gh, GitHub's command-line tool,",
    why=("Goodall asks gh about the record repo on GitHub, so that goodall itself never holds a GitHub token."),
    how_to_install="from https://cli.github.com",
)

# The command of each secrets manager local settings can name. Local
# settings accept only these (goodall.local_settings), so a lookup cannot
# miss. This moves into the secrets manager module (ADR 0011) once that
# module exists.
SECRETS_MANAGER_PROGRAMS = {
    "1password": RequiredProgram(
        name="op",
        described="op, 1Password's command-line tool,",
        why=(
            "This eval's secrets are kept in 1Password, and goodall reaches them only through op: op checks "
            "that each secret reference points at a real item, and launches programs with the secrets they need."
        ),
        how_to_install="following https://developer.1password.com/docs/cli/get-started/",
    ),
}


def check_program_installed(context: CheckContext, program: RequiredProgram) -> CheckOutcome:
    """`program` is on this macOS user account's PATH.

    It is only looked for, never run, so the answer does not depend on the
    program working; checks that run it say so when it does not.
    """
    if context.runner.find_program(program.name) is not None:
        return passed()
    return failed([program_missing_failure(program)])


def program_missing_failure(program: RequiredProgram) -> Failure:
    """The failure for a required program that is not installed.

    The next step is for the owner of the Mac, not the contributor: an eval
    account is a standard account, which cannot install software.
    """
    return Failure(
        kind="program_missing",
        what_happened=f"{program.described} is not installed for this macOS user account (it is not on the PATH).",
        why=program.why,
        what_to_do_next=(
            f"Ask the owner of this Mac to install {program.name} {program.how_to_install}, from an "
            "administrator account, because a standard account cannot install software. Then run goodall "
            "doctor again."
        ),
    )


def check_git_installed(context: CheckContext) -> CheckOutcome:
    """git is installed."""
    return check_program_installed(context, GIT)


def check_gh_installed(context: CheckContext) -> CheckOutcome:
    """gh is installed."""
    return check_program_installed(context, GH)


def check_secrets_manager_installed(context: CheckContext) -> CheckOutcome:
    """The command of the secrets manager that local settings name is installed."""
    local_settings = valid_local_settings(context)
    return check_program_installed(context, SECRETS_MANAGER_PROGRAMS[local_settings.secrets_manager])


def check_standard_account(context: CheckContext) -> CheckOutcome:
    """This macOS user account is a standard account, not an administrator."""
    context.runner.run(Command(ACCOUNT_GROUPS_COMMAND))
    return passed()


def check_record_repo_is_git_repository(context: CheckContext) -> CheckOutcome:
    """The record repo folder is the top of a git repository."""
    local_settings = valid_local_settings(context)
    record_repo: Path = local_settings.record_repo.path
    context.runner.run(Command(["git", "rev-parse", "--show-toplevel"], working_directory=record_repo))
    return passed()


PROGRAM_CHECKS = [
    Check(
        id=GIT_INSTALLED,
        group=TOOLS_GROUP,
        summary="git is installed",
        prerequisites=[],
        run=check_git_installed,
    ),
    Check(
        id=GH_INSTALLED,
        group=TOOLS_GROUP,
        summary="gh is installed",
        prerequisites=[],
        run=check_gh_installed,
    ),
    Check(
        id=SECRETS_MANAGER_INSTALLED,
        group=TOOLS_GROUP,
        summary="The secrets manager's command is installed",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_secrets_manager_installed,
    ),
    Check(
        id=STANDARD_ACCOUNT,
        group=EVAL_ACCOUNT_GROUP,
        summary="This macOS user account is a standard account, not an administrator",
        prerequisites=[],
        run=check_standard_account,
    ),
    Check(
        id=RECORD_REPO_IS_GIT_REPOSITORY,
        group=RECORD_REPO_GROUP,
        summary="The record repo folder is a git repository",
        prerequisites=[RECORD_REPO_FOUND, GIT_INSTALLED],
        run=check_record_repo_is_git_repository,
    ),
]
