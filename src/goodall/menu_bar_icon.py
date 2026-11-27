"""Bringing the menu bar icon back: `goodall start` starts its helper when it is installed.

Called by `goodall start` (goodall.work_block_commands). The menu bar icon
is drawn by a small Swift helper, an application named goodall.app
(ADR 0024; its source is in `menu-bar-icon/`). "Quit goodall" in its menu
removes the icon, and the ADR says it returns at the next sign-in or when
the person runs `goodall start`. This module is the second half of that.
Starting it at sign-in is the LaunchAgent's job, which onboarding will
install; nothing here installs anything.

Decisions a reader would otherwise reverse-engineer:

- Where the helper is installed: `~/Applications/goodall.app`, in the home
  folder of the macOS user account that records. It has to be a stable
  place: macOS ties notification permission to where an application is,
  and the spike (`goodall-fs9.29`) was refused permission from a temporary
  folder and given it from `~/Applications`. The place uv installs goodall
  itself is not stable either, because its path contains the Python
  version. `~/Applications` needs no administrator, so it serves mixed
  account mode too.
- No helper installed means no program is started at all: goodall only
  looks for the folder. So `goodall start` is exactly as fast as before on
  a Mac without the helper.
- The helper is started with macOS's own `open`, through the runner
  (ADR 0011). `open` does nothing to an application that is already
  running, so goodall does not have to find out whether the helper is
  running first, and `-g` keeps it from taking the keyboard away from
  whatever the person is doing.
- How the helper finds goodall: whoever starts the helper tells it, in the
  environment variable GOODALL_COMMAND, which holds the full path of the
  `goodall` program. The helper is started by launchd or by `open`, which
  give it a minimal PATH with no goodall on it, so it cannot look goodall
  up itself, and searching likely folders would be logic in the Swift.
  Here the path is the `goodall` beside the Python that is running this
  very command, which is where uv, pipx and pip all put it; failing that,
  the one on the PATH.
- Nothing here can fail `goodall start`. The work block has started by
  then, and the icon is the indicator, not the recording: a helper that
  cannot be started is a warning.
"""

import sys
from pathlib import Path

from goodall.runner import Command, Runner
from goodall.terminal import Failure

# The helper's name as an installed application, and the folder in the home folder it is installed in.
APPLICATION_NAME = "goodall.app"
APPLICATIONS_FOLDER_NAME = "Applications"

# The environment variable that tells the helper the full path of the goodall program.
GOODALL_COMMAND_VARIABLE = "GOODALL_COMMAND"

# `open` only asks macOS to start the application and returns; it is quick
# or it is stuck, and a stuck one must not hold `goodall start` up for long.
OPEN_TIMEOUT_SECONDS = 5.0


def installed_helper() -> Path | None:
    """Where the menu bar icon's helper is installed in this macOS user account, or None when it is not."""
    helper = Path.home() / APPLICATIONS_FOLDER_NAME / APPLICATION_NAME
    try:
        if helper.is_dir():
            return helper
    except OSError:
        pass
    return None


def find_goodall_command(runner: Runner) -> Path | None:
    """The full path of the `goodall` program the helper should run, or None when goodall cannot say."""
    beside_python = Path(sys.executable).parent / "goodall"
    if beside_python.is_file():
        return beside_python
    return runner.find_program("goodall")


def bring_icon_back(runner: Runner) -> Failure | None:
    """Start the menu bar icon's helper if it is installed; a warning when that did not work, else None.

    Harmless when the helper is already running. Does nothing, and starts
    no program, when no helper is installed.
    """
    helper = installed_helper()
    if helper is None:
        return None
    goodall_command = find_goodall_command(runner)
    if goodall_command is None:
        return icon_not_started_failure("goodall could not work out where its own program is installed")
    result = runner.run(
        Command(
            ["open", "-g", "--env", f"{GOODALL_COMMAND_VARIABLE}={goodall_command}", str(helper)],
            timeout_seconds=OPEN_TIMEOUT_SECONDS,
        )
    )
    if result.problem is not None:
        return icon_not_started_failure(result.problem)
    return None


def icon_not_started_failure(problem: str) -> Failure:
    """The warning for a helper that is installed but could not be started."""
    return Failure(
        kind="menu_bar_icon_not_started",
        what_happened=f"The menu bar icon could not be started: {problem}.",
        why=(
            "The icon shows whether goodall is recording. Goodall is recording all the same: the icon only "
            "shows it, and goodall status says the same in a terminal."
        ),
        what_to_do_next=(
            "Run goodall start again to try once more; with a work block active it changes nothing else. The "
            "icon also returns the next time you sign in."
        ),
    )
