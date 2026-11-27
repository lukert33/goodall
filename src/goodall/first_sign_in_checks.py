"""`goodall doctor`'s checks of first sign-in setup: that what the administrator install staged is still protected.

Part of `goodall doctor` (goodall.doctor). The administrator install
(goodall.administrator_install) stages a first-run script, the eval
account's answers file and two LaunchAgents, all owned by root, and relies
on goodall being installed for every account (goodall.system_places says
where each is). What runs at the next sign-in is only as safe as those
files: if the eval account, or an agent working in it, could change one,
it could change what is recorded. So doctor checks, and only checks, that
each is owned by root and that no other account can change it, and that
the LaunchAgents still run what goodall put in them.

Decisions a reader would otherwise reverse-engineer:

- These checks look at files shared by the whole Mac, not at this
  account's settings, so they have no prerequisites and run whatever else
  failed.
- A Mac with none of the staged files passes, with a note: an eval account
  may be made and set up by hand (ADR 0022 keeps that route), and then
  there is nothing here to protect.
- The next step for a file in the wrong hands is the administrator install
  run again, which puts each file back exactly, or, for a file that is
  only owned or permitted wrongly, the two commands that fix it, for an
  administrator to run. Doctor runs neither (AGENTS.md invariant 5).
"""

import plistlib
import shlex
from pathlib import Path

from goodall import menu_bar_icon, system_places
from goodall.administrator_install import INSTALL_DOCUMENT, describe_protection, folder_in_place, octal
from goodall.checks import Check, CheckContext, CheckOutcome, failed, passed
from goodall.terminal import Failure

GROUP = "First sign-in"

NOTHING_STAGED_NOTE = (
    "No first sign-in setup is installed on this Mac, so there is nothing to check: this account was set up by "
    "hand, or the administrator install has not been run."
)


def anything_staged() -> bool:
    """True when any piece of first sign-in setup is on this Mac."""
    pieces = [
        system_places.staged_answers_file(),
        system_places.first_run_script(),
        system_places.first_run_agent_file(),
        system_places.menu_bar_icon_agent_file(),
    ]
    for piece in pieces:
        try:
            piece.lstat()
            return True
        except OSError:
            continue
    return False


def check_goodall_for_every_account(context: CheckContext) -> CheckOutcome:
    """goodall is installed where every account can run it and only root can change it."""
    if not anything_staged():
        return passed(NOTHING_STAGED_NOTE)
    goodall = system_places.installed_goodall()
    problem = describe_protection(goodall)
    if problem is None:
        return passed()
    return failed([goodall_not_protected_failure(goodall, problem)])


def check_staged_files_protected(context: CheckContext) -> CheckOutcome:
    """The staging folder, the first-run script and the staged answers file are root's alone."""
    if not anything_staged():
        return passed(NOTHING_STAGED_NOTE)
    failures = []
    folder = system_places.STAGING_FOLDER
    if not folder_in_place(folder):
        failures.append(staged_file_not_protected_failure(folder, "it is not a folder only root can change", 0o755))
    for staged_file, mode in [
        (system_places.first_run_script(), system_places.RUNNABLE_BY_EVERY_ACCOUNT),
        (system_places.staged_answers_file(), system_places.READABLE_BY_EVERY_ACCOUNT),
    ]:
        problem = describe_protection(staged_file)
        if problem is not None:
            failures.append(staged_file_not_protected_failure(staged_file, problem, mode))
    if failures:
        return failed(failures)
    return passed()


def check_launch_agents_in_place(context: CheckContext) -> CheckOutcome:
    """Both LaunchAgents are there, are root's alone, and run what the administrator install put in them."""
    if not anything_staged():
        return passed(NOTHING_STAGED_NOTE)
    failures: list[Failure] = []
    first_run_agent = read_agent(system_places.first_run_agent_file(), failures)
    if first_run_agent is not None and first_run_agent.get("ProgramArguments") != [
        str(system_places.first_run_script())
    ]:
        failures.append(
            agent_differs_failure(
                system_places.first_run_agent_file(),
                f"it does not run the first-run script, {system_places.first_run_script()}",
            )
        )
    icon_agent = read_agent(system_places.menu_bar_icon_agent_file(), failures)
    if icon_agent is not None:
        if icon_agent.get("ProgramArguments") != [str(system_places.menu_bar_icon_program())]:
            failures.append(
                agent_differs_failure(
                    system_places.menu_bar_icon_agent_file(),
                    f"it does not run the menu bar icon's helper, {system_places.menu_bar_icon_program()}",
                )
            )
        environment = icon_agent.get("EnvironmentVariables")
        wanted_environment = {menu_bar_icon.GOODALL_COMMAND_VARIABLE: str(system_places.installed_goodall())}
        if environment != wanted_environment:
            failures.append(
                agent_differs_failure(
                    system_places.menu_bar_icon_agent_file(),
                    f"it does not tell the helper that goodall is {system_places.installed_goodall()}",
                )
            )
        if icon_agent.get("KeepAlive") != {"SuccessfulExit": False}:
            failures.append(
                agent_differs_failure(
                    system_places.menu_bar_icon_agent_file(),
                    "its KeepAlive is not SuccessfulExit false, so it would not bring the icon back after a crash, "
                    "or would undo Quit goodall",
                )
            )
    if failures:
        return failed(failures)
    return passed()


def read_agent(agent_file: Path, failures: list[Failure]) -> dict[str, object] | None:
    """The LaunchAgent at `agent_file`, when it is there, root's alone and readable; else record why not."""
    problem = describe_protection(agent_file)
    if problem is not None:
        failures.append(staged_file_not_protected_failure(agent_file, problem, system_places.READABLE_BY_EVERY_ACCOUNT))
        return None
    try:
        agent = plistlib.loads(agent_file.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        failures.append(agent_differs_failure(agent_file, "it is not a property list launchd can read"))
        return None
    if not isinstance(agent, dict):
        failures.append(agent_differs_failure(agent_file, "it is not a property list launchd can read"))
        return None
    return agent


FIRST_SIGN_IN_CHECKS: list[Check] = [
    Check(
        id="first_sign_in.goodall_for_every_account",
        group=GROUP,
        summary="goodall is installed for every account, and only root can change it",
        prerequisites=[],
        run=check_goodall_for_every_account,
    ),
    Check(
        id="first_sign_in.staged_files_protected",
        group=GROUP,
        summary=(
            "The first-run script and the staged answers file are owned by root, and no other account can change them"
        ),
        prerequisites=[],
        run=check_staged_files_protected,
    ),
    Check(
        id="first_sign_in.launch_agents_in_place",
        group=GROUP,
        summary="The LaunchAgents are in place and run the first-run script and the menu bar icon",
        prerequisites=[],
        run=check_launch_agents_in_place,
    ),
]


WHY_PROTECTED = (
    "launchd runs it at every sign-in to every account. If any account but root could change it, the eval account, "
    "or an agent working in it, could change what goodall records."
)


def goodall_not_protected_failure(goodall: Path, problem: str) -> Failure:
    return Failure(
        kind="goodall_not_installed_for_every_account",
        what_happened=f"goodall is not installed for every account at {goodall}: {problem}.",
        why=(
            "The first-run script, the menu bar icon's LaunchAgent and the eval account's Claude Code hooks all run "
            "goodall from there, so only root may be able to change it."
        ),
        what_to_do_next=f"From an administrator account, install goodall there as {INSTALL_DOCUMENT} shows.",
    )


def staged_file_not_protected_failure(path: Path, problem: str, mode: int) -> Failure:
    quoted = shlex.quote(str(path))
    return Failure(
        kind="first_sign_in_file_not_protected",
        what_happened=f"{path} is not protected: {problem}.",
        why=WHY_PROTECTED,
        what_to_do_next=(
            "From an administrator account, run the administrator install again (goodall onboard --answers with the "
            f"same answers file), which puts it back exactly, or fix its owner and permissions with the commands "
            f"below. Then run goodall doctor again. {INSTALL_DOCUMENT} says more."
        ),
        commands_to_try=(
            f"sudo chown {system_places.ROOT_USER_NAME}:{system_places.ROOT_GROUP_NAME} {quoted}",
            f"sudo chmod {octal(mode)[1:]} {quoted}",
        ),
    )


def agent_differs_failure(agent_file: Path, problem: str) -> Failure:
    return Failure(
        kind="first_sign_in_agent_differs",
        what_happened=f"The LaunchAgent {agent_file} is not as the administrator install wrote it: {problem}.",
        why=WHY_PROTECTED,
        what_to_do_next=(
            "From an administrator account, run the administrator install again (goodall onboard --answers with the "
            f"same answers file), which writes it again exactly. {INSTALL_DOCUMENT} says more."
        ),
    )
