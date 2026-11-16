"""The diagnostic checks that need other programs: the tools, the macOS user account, and the record repo.

`goodall doctor` runs `PROGRAM_CHECKS` after the settings checks
(goodall.doctor). Every program they ask about goes through the runner in
the check context (goodall.runner), so a test decides what each program
does, and git runs for real.
"""

from pathlib import Path

from goodall.checks import Check, CheckContext, CheckOutcome, passed
from goodall.runner import Command
from goodall.settings_checks import LOCAL_SETTINGS_VALID, RECORD_REPO_FOUND, valid_local_settings

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


def check_git_installed(context: CheckContext) -> CheckOutcome:
    """git is installed."""
    context.runner.find_program("git")
    return passed()


def check_gh_installed(context: CheckContext) -> CheckOutcome:
    """gh is installed."""
    context.runner.find_program("gh")
    return passed()


def check_secrets_manager_installed(context: CheckContext) -> CheckOutcome:
    """The secrets manager's command is installed."""
    context.runner.find_program("op")
    return passed()


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
