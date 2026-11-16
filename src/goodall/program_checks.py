"""The diagnostic checks that need other programs: the tools, the macOS user account, and the record repo.

`goodall doctor` runs `PROGRAM_CHECKS` after the settings checks
(goodall.doctor). Every program they ask about goes through the runner in
the check context (goodall.runner), so a test decides what each program
does, and git runs for real.

They fall under three headings, each with its own id prefix:

- "Tools": git, gh and the secrets manager's command are installed. One
  check per program, so each missing program is one failure naming it.
- "Eval account": this macOS user account is a standard account, not an
  administrator.
- "Record repo": the record repo folder is a git repository. Later checks
  on the record repo (its remote and visibility) belong here too.

The dependency edges, chosen so one broken thing is one failure:

- git and gh need nothing else, so they are checked even when local
  settings are missing.
- The secrets manager's command waits for valid local settings, because
  they name the secrets manager.
- The account check needs nothing else.
- The git repository check waits for the record repo folder and for git:
  without git it cannot ask, and the report should say "git is missing"
  once, not again under the record repo.

Like every check, these only look (ADR 0012): programs are looked for on
the PATH without being run, `id -Gn` and `git rev-parse` only read, and no
check that could print a secret runs here.
"""

from dataclasses import dataclass
from pathlib import Path

from goodall.checks import Check, CheckContext, CheckOutcome, failed, passed
from goodall.local_settings import LocalSettings
from goodall.runner import Command, CommandOutcome, first_non_blank_line
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

# The command that lists this macOS user account's groups by name. It is
# named by its full path, the same on macOS and Linux, so a program called
# `id` placed earlier on the PATH cannot answer for it.
ACCOUNT_GROUPS_COMMAND = ["/usr/bin/id", "-Gn"]

# Membership of any of these groups makes an account an administrator. On
# macOS that is `admin`, and `wheel` for root. Goodall targets macOS, but its
# tests and its developers also run on Linux, where `admin` does not exist
# and an account that can become root is in `sudo` or `wheel`. No standard
# macOS account is in either, so listing them costs nothing on a Mac.
ADMINISTRATOR_GROUPS = ["admin", "wheel", "sudo"]


@dataclass(frozen=True)
class RequiredProgram:
    """A program goodall needs, and what to tell a contributor when it is missing.

    - `name` is the command, as found on the PATH.
    - `described` names it for a person, at the start of a sentence, such
      as "gh, GitHub's command-line tool,".
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
    why="Goodall asks gh about the record repo on GitHub, so that goodall itself never holds a GitHub token.",
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
    """This macOS user account is a standard account, not an administrator.

    It asks `id -Gn` for the account's groups by name. On macOS an
    administrator is exactly an account in the `admin` group.
    """
    command = Command(ACCOUNT_GROUPS_COMMAND)
    result = context.runner.run(command)
    group_names = result.standard_output.split()
    # Trap: a command that failed printed no groups, which would look like
    # "no admin group". Not knowing must fail, never pass. Every account is
    # in at least one group, so an empty answer is not an answer either.
    if not result.succeeded or not group_names:
        problem = result.problem or f"`{command.as_typed}` listed no groups"
        return failed([account_groups_unknown_failure(problem)])
    for group_name in group_names:
        if group_name in ADMINISTRATOR_GROUPS:
            return failed([account_is_administrator_failure(group_name)])
    return passed()


def account_groups_unknown_failure(problem: str) -> Failure:
    """The failure for when goodall could not find out this account's groups."""
    return Failure(
        kind="account_groups_unknown",
        what_happened=f"Goodall could not tell whether this macOS user account is an administrator: {problem}.",
        why="Goodall reads the account's groups to confirm it is a standard account; without them it cannot.",
        what_to_do_next=(
            f"Run {' '.join(ACCOUNT_GROUPS_COMMAND)} in Terminal: it should print this account's groups on one "
            "line. If it shows an error, ask the owner of this Mac to look at it. Then run goodall doctor again."
        ),
    )


def account_is_administrator_failure(group_name: str) -> Failure:
    """The failure for an eval account that is an administrator.

    The short reason is part of what happened, not only of `why`, because
    the report shows what happened and the next step; a researcher who sees
    no reason may take the rule for fussiness.
    """
    return Failure(
        kind="account_is_administrator",
        what_happened=(
            f"This macOS user account is an administrator (it is in the {group_name} group). An eval needs a "
            "standard account: it cannot reach other accounts' files or install software, which is what makes "
            "it safe to hand to an agent."
        ),
        why=(
            "The agents in an eval act with this account's rights. An administrator can install software, "
            "change system settings and read other accounts' files, so an agent could too."
        ),
        what_to_do_next=(
            "Run the eval in a standard account. From an administrator account, open System Settings, then "
            "Users & Groups, and either create a new standard account for this eval or turn off "
            "administrator rights for this one. Then run goodall doctor again in the eval account."
        ),
    )


def check_record_repo_is_git_repository(context: CheckContext) -> CheckOutcome:
    """The record repo folder is the top of a git repository.

    Asks real git, through the runner, for the top folder of the repository
    the record repo folder is in. `rev-parse` only reads, so doctor stays
    read-only.
    """
    local_settings = valid_local_settings(context)
    result = context.runner.run(
        Command(["git", "rev-parse", "--show-toplevel"], working_directory=local_settings.record_repo.path)
    )
    if result.outcome == CommandOutcome.COMPLETED and not result.succeeded:
        git_said = first_non_blank_line(result.standard_error) or f"nothing, and exited with code {result.exit_code}"
        return failed([record_repo_not_git_repository_failure(local_settings, git_said)])
    if result.problem is not None:
        return failed([record_repo_git_unanswered_failure(local_settings, result.problem)])
    # Trap: git also answers for a plain folder inside some other repository
    # (a home folder kept in git, say), naming that repository's top folder.
    # resolve() both, because git names the real path and a path in local
    # settings may pass through a link (on macOS, /var is /private/var).
    top_folder = Path(result.standard_output.strip())
    if top_folder.resolve() != local_settings.record_repo.path.resolve():
        return failed([record_repo_inside_other_repository_failure(local_settings, top_folder)])
    return passed()


def record_repo_inside_other_repository_failure(local_settings: LocalSettings, top_folder: Path) -> Failure:
    """The failure for a record repo folder that is only a folder inside another repository."""
    record_repo = local_settings.record_repo
    return Failure(
        kind="record_repo_inside_other_repository",
        what_happened=(
            f"The record repo folder {record_repo.as_written} is not a git repository of its own: it is inside "
            f"the git repository {top_folder}."
        ),
        why=(
            "Goodall commits the eval's records in the record repo, so they would go into the repository around "
            "it instead."
        ),
        what_to_do_next=(
            f"Clone the eval's record repo with git clone, into {record_repo.path} (moving this folder aside "
            f"first), or set record_repo in {local_settings.settings_file} to the folder where you cloned it. "
            "Then run goodall doctor again."
        ),
    )


def record_repo_not_git_repository_failure(local_settings: LocalSettings, git_said: str) -> Failure:
    """The failure for a record repo folder git does not accept as a repository.

    Worded "a git repository that git will use" because git also refuses a
    real repository owned by another macOS user account; git's own words
    say which it is.
    """
    record_repo = local_settings.record_repo
    return Failure(
        kind="record_repo_not_git_repository",
        what_happened=(
            f"The record repo folder {record_repo.as_written} is not a git repository that git will use. "
            f"git said: {git_said}"
        ),
        why="Goodall keeps the eval's records with git, so the record repo must be a clone of the eval's repository.",
        what_to_do_next=(
            f"Clone the eval's record repo with git clone, into {record_repo.path} (moving this folder aside "
            f"first), or set record_repo in {local_settings.settings_file} to the folder where you cloned it. "
            "Then run goodall doctor again."
        ),
    )


def record_repo_git_unanswered_failure(local_settings: LocalSettings, problem: str) -> Failure:
    """The failure for when git could not be asked about the record repo folder at all."""
    record_repo = local_settings.record_repo
    return Failure(
        kind="record_repo_git_unanswered",
        what_happened=(
            f"Goodall could not ask git whether the record repo folder {record_repo.as_written} is a git "
            f"repository: {problem}."
        ),
        why="Goodall keeps the eval's records with git, so it must be able to run git in the record repo.",
        what_to_do_next=f"Run git status in {record_repo.path} to see what git says, then run goodall doctor again.",
    )


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
