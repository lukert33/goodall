"""Where the administrator install puts things on the Mac, and what each file it stages says.

Used by three callers that must agree exactly: the administrator install,
which stages these files (goodall.administrator_install); `goodall sign-in`,
which the first-run script runs at every sign-in (goodall.sign_in); and
`goodall doctor`'s read-only checks that they are protected
(goodall.first_sign_in_checks). The procedure is ADR 0022; what it does on
a real Mac is described, with what is not yet confirmed, in
docs/administrator-install.md.

Decisions a reader would otherwise reverse-engineer:

- Every place is somewhere only root can change and every account can
  read, so neither the eval account nor an agent working in it can alter
  what runs at the next sign-in. Never `/Users/Shared`, which every account
  can write (ADR 0022).
- Goodall itself is installed at `/usr/local/goodall`, with its command at
  `/usr/local/goodall/bin/goodall` (`INSTALLED_GOODALL`). ADR 0022 left the
  exact place to whoever built this. `/usr/local` is the traditional place
  for software an administrator adds for the whole Mac; on a Mac with an
  Apple chip, Homebrew lives in `/opt/homebrew` instead, so `/usr/local`
  stays root's. A folder of goodall's own keeps its whole uv tool
  environment, and the Python it runs on, in one place that is easy to
  check and to remove. The command is not put on any PATH: the first-run
  script, the menu bar icon's LaunchAgent and the Claude Code hooks all
  name its full path. Goodall cannot install itself there; an administrator
  runs the command in docs/administrator-install.md.
- What goodall stages for the first sign-in sits in
  `/Library/Application Support/goodall`, the folder macOS gives software
  for files shared by the whole Mac: the eval account's answers file, and
  the first-run script.
- The two LaunchAgents are in `/Library/LaunchAgents`, so launchd starts
  them at every sign-in to every account. launchd refuses one there that is
  not owned by root or that the group or others can write (launchctl(1)).
  The first-run script decides, through `goodall sign-in`, whether the
  account is the eval account; the menu bar icon's helper starts in every
  account and says in its menu when goodall is not set up there.
- The files are written as text here, by goodall, and copied into place
  with `install`, which sets owner and permissions as it copies.
- Every place is a module-level name, read when a function is called, so
  tests point each one at a temporary folder (tests/conftest.py) and no
  test can read or write the real one.
"""

import os
import plistlib
import pwd
from pathlib import Path

from goodall import menu_bar_icon, notifications

# The home folders of the Mac's accounts. A folder here with the new
# account's name means an account had it, even if it was deleted since.
USERS_FOLDER = Path("/Users")

# Where LaunchAgents for every account go (launchd.plist(5)).
LAUNCH_AGENTS_FOLDER = Path("/Library/LaunchAgents")

# Where the administrator install stages first sign-in setup.
STAGING_FOLDER = Path("/Library/Application Support/goodall")

# Where goodall is installed for every account; see the module docstring.
GOODALL_INSTALL_FOLDER = Path("/usr/local/goodall")

# The owner every staged file must have, and its group. Tests set
# ROOT_USER_ID to their own user id, so a file they made counts as root's.
ROOT_USER_ID = 0
ROOT_USER_NAME = "root"
ROOT_GROUP_NAME = "wheel"

# Permissions: readable, and for a program runnable, by every account;
# writable by the owner, root, alone.
READABLE_BY_EVERY_ACCOUNT = 0o644
RUNNABLE_BY_EVERY_ACCOUNT = 0o755

# The permission bits that let the group or other accounts change a file.
GROUP_OR_OTHERS_MAY_WRITE = 0o022

# The file names inside the staging folder, and the LaunchAgents' labels,
# which are also their file names. The labels start with the menu bar
# icon's bundle identifier, so every piece of goodall is named alike.
STAGED_ANSWERS_FILE_NAME = "eval-account-answers.toml"
FIRST_RUN_SCRIPT_NAME = "first-run"
FIRST_RUN_LABEL = "io.github.lukert33.goodall.first-run"
MENU_BAR_ICON_LABEL = "io.github.lukert33.goodall.menu-bar-icon"

# Where the program of goodall's own Python environment is, inside its folder.
INSTALLED_GOODALL_IN_FOLDER = Path("bin") / "goodall"


def installed_goodall() -> Path:
    """Where goodall's own command is, once an administrator has installed it for every account."""
    return GOODALL_INSTALL_FOLDER / INSTALLED_GOODALL_IN_FOLDER


def staged_answers_file() -> Path:
    """The eval account's answers file, staged by the administrator install."""
    return STAGING_FOLDER / STAGED_ANSWERS_FILE_NAME


def first_run_script() -> Path:
    """The first-run script, which every account's sign-in runs."""
    return STAGING_FOLDER / FIRST_RUN_SCRIPT_NAME


def first_run_agent_file() -> Path:
    """The LaunchAgent that runs the first-run script at every sign-in."""
    return LAUNCH_AGENTS_FOLDER / f"{FIRST_RUN_LABEL}.plist"


def menu_bar_icon_agent_file() -> Path:
    """The LaunchAgent that starts the menu bar icon's helper at every sign-in."""
    return LAUNCH_AGENTS_FOLDER / f"{MENU_BAR_ICON_LABEL}.plist"


def menu_bar_icon_program() -> Path:
    """The helper's program inside goodall.app, which the menu bar icon's LaunchAgent runs."""
    return menu_bar_icon.APPLICATIONS_FOLDER / menu_bar_icon.APPLICATION_NAME / notifications.HELPER_PROGRAM


def first_run_script_text() -> str:
    """The first-run script: a few lines that hand over to `goodall sign-in`, where every decision is.

    `exec` replaces the shell with goodall, so what launchd sees ending is
    goodall itself.
    """
    return (
        "#!/bin/sh\n"
        "# goodall's first-run script, staged by the administrator install (ADR 0022).\n"
        "# Owned by root and read-only to every other account. launchd runs it at every\n"
        "# sign-in to every account; goodall sign-in does nothing unless this is the\n"
        "# eval account, sets that account up the first time, and starts recording.\n"
        f'exec "{installed_goodall()}" sign-in\n'
    )


def first_run_agent_text() -> str:
    """The first-run LaunchAgent: run the first-run script once, at sign-in, in a graphical session.

    `LimitLoadToSessionType` Aqua keeps it to a person signing in at the
    screen, not a remote shell. It is not kept alive: the script runs once
    per sign-in and ends.
    """
    agent: dict[str, object] = {
        "Label": FIRST_RUN_LABEL,
        "ProgramArguments": [str(first_run_script())],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
    }
    return plistlib.dumps(agent).decode("utf-8")


def menu_bar_icon_agent_text() -> str:
    """The menu bar icon's LaunchAgent: start the helper at sign-in and bring it back if it dies.

    `KeepAlive` with `SuccessfulExit` false restarts the helper only when it
    ends with an error: "Quit goodall" exits with 0, so it is not undone
    (ADR 0024). GOODALL_COMMAND tells the helper where goodall is, because
    launchd gives it no PATH with goodall on it (goodall.menu_bar_icon).
    """
    agent: dict[str, object] = {
        "Label": MENU_BAR_ICON_LABEL,
        "ProgramArguments": [str(menu_bar_icon_program())],
        "EnvironmentVariables": {menu_bar_icon.GOODALL_COMMAND_VARIABLE: str(installed_goodall())},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(agent).decode("utf-8")


def current_account_name() -> str:
    """The short name of the macOS user account goodall is running in, as the system's account list gives it.

    Asked of the system by user id, not read from USER or LOGNAME, which
    any program can set to anything before it starts goodall.
    """
    return pwd.getpwuid(os.getuid()).pw_name
