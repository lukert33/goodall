"""Tests for the menu bar icon's helper, the Swift program in `menu-bar-icon/` (ADR 0024).

What the menu shows after each thing that happens is tested by running the
helper's model, in tests/test_menu_bar_icon_model.py (ADR 0025). The tests
here cover what that cannot: they read the Swift source as text, to hold it
to rules a compiler does not check (it never says "eval", every goodall
command it runs is one the command line has, it draws its own binoculars
and shows no clock, it refills one menu rather than replacing it, and its
notifications come from itself, ask nothing and use the argument and exit
codes goodall.notifications expects).

The last test builds the real helper with `menu-bar-icon/build.sh` into
tmp_path. It is skipped where there is no Swift compiler, which includes
the Linux runners CI uses; CI's separate Mac job runs the same build script
instead (.github/workflows/quality-gates.yml). It proves
the source compiles and the application is whole and signed. It starts the
helper it built only once, in notify mode with a wrong argument, which
exits at once without drawing an icon or showing a notification.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from goodall import cli, notifications

REPOSITORY_ROOT = Path(__file__).parent.parent
HELPER_FOLDER = REPOSITORY_ROOT / "menu-bar-icon"


def helper_source() -> str:
    """Every Swift file of the helper, joined: main.swift and MenuModel.swift."""
    sources = []
    for swift_file in sorted(HELPER_FOLDER.glob("*.swift")):
        sources.append(swift_file.read_text())
    return "\n".join(sources)


def test_the_helper_source_never_says_eval() -> None:
    # The menu and the notifications never say "eval" (ADR 0021). The whole
    # source is held to it, comments too, so no string can slip through.
    source = helper_source()
    assert "eval" not in source.lower()


def test_every_goodall_command_the_helper_runs_is_one_the_command_line_has() -> None:
    # Each menu item runs one goodall command (ADR 0024). The helper keeps
    # them in one list, one per line, which this test reads.
    source = helper_source()
    command_names = []
    for line in source.splitlines():
        if "// goodall-command:" in line:
            command_names.append(line.split("// goodall-command:")[1].split()[0])
    assert sorted(command_names) == ["icon-state", "pause", "settings", "start", "stop"]
    parser = cli.build_parser()
    for command_name in command_names:
        # A name the parser does not know makes argparse exit.
        if command_name == "start":
            parser.parse_args(["start", "--workstream", "research"])
        elif command_name == "icon-state":
            parser.parse_args(["icon-state", "--json"])
        else:
            parser.parse_args([command_name])


def test_the_helper_draws_binoculars_and_shows_no_clock() -> None:
    # The owner's decisions of 2026-11-27. The icon is goodall's binoculars,
    # drawn by the helper. It shows no running time: a clock that reads 0:00
    # for the first minute looks broken, and the icon's job is only to say
    # whether goodall is recording.
    source = (HELPER_FOLDER / "main.swift").read_text()
    assert "func binocularsImage(" in source
    assert "systemSymbolName" not in source
    assert "elapsed" not in source
    assert "clockTick" not in source


def test_the_recording_look_is_orange() -> None:
    # The owner's choice of 2026-11-27: a bright citrus orange, not red.
    source = (HELPER_FOLDER / "main.swift").read_text()
    assert ".systemOrange" in source
    assert ".systemRed" not in source


def test_the_menu_is_refilled_in_place_so_an_open_menu_shows_a_finished_command() -> None:
    # Found by clicking on 2026-11-27: a menu opened while a command ran kept
    # its busy line after the command finished, because each redraw handed
    # the icon a new menu and an open menu goes on showing the old one. The
    # helper makes one menu at startup and refills that same menu instead.
    source = (HELPER_FOLDER / "main.swift").read_text()
    assert source.count("statusItem.menu =") == 1
    assert "menu.removeAllItems()" in source


def test_the_helper_shows_notifications_itself_and_never_through_osascript() -> None:
    # The owner's decision of 2026-11-26: notifications come from the
    # helper, under goodall's own name, never from osascript, which shows
    # them as Script Editor's and often drops them (ADR 0024).
    source = helper_source()
    assert "UNUserNotificationCenter" in source
    assert "osascript" not in source
    assert "NSAppleScript" not in source


def test_a_notification_asks_nothing_and_has_no_buttons() -> None:
    # The terminal never asks, and nor does a notification (ADR 0024): no
    # actions, no categories that would carry them, and no text reply.
    source = helper_source()
    assert "UNNotificationAction" not in source
    assert "UNNotificationCategory" not in source
    assert "UNTextInputNotificationAction" not in source


def test_the_helpers_notify_argument_and_exit_codes_are_the_ones_goodall_uses() -> None:
    # goodall.notifications runs the helper with these and reads its exit code.
    source = (HELPER_FOLDER / "main.swift").read_text()
    assert f'let notifyArgument = "{notifications.NOTIFY_ARGUMENT}"' in source
    assert f"let notificationShownExitCode: Int32 = {notifications.SHOWN}" in source
    assert f"let notificationsNotAllowedExitCode: Int32 = {notifications.NOT_ALLOWED}" in source
    assert f"let notificationsNotAskedYetExitCode: Int32 = {notifications.NOT_ASKED_YET}" in source


def test_the_helper_asks_for_notification_permission_only_when_the_model_says_to() -> None:
    # When to ask is decided in the Python and the model (ADR 0025), not in
    # main.swift; main.swift asks in exactly one place.
    source = (HELPER_FOLDER / "main.swift").read_text()
    assert source.count("requestAuthorization(") == 1
    assert "if self.model.stateAnswered(" in source


needs_swift = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None or shutil.which("codesign") is None,
    reason="building the menu bar icon's helper needs macOS with the Swift compiler",
)


@needs_swift
def test_the_build_script_makes_a_signed_goodall_app_from_the_swift_source(tmp_path: Path) -> None:
    build = subprocess.run(
        [str(HELPER_FOLDER / "build.sh"), str(tmp_path)], capture_output=True, text=True, timeout=300
    )

    assert build.returncode == 0, build.stdout + build.stderr
    app = tmp_path / "goodall.app"
    with (app / "Contents" / "Info.plist").open("rb") as plist_file:
        info = plistlib.load(plist_file)
    assert info["CFBundleName"] == "goodall"
    assert info["CFBundleIdentifier"] == "io.github.lukert33.goodall"
    # No Dock icon and no application menu: the helper lives in the menu bar only.
    assert info["LSUIElement"] is True
    executable = app / "Contents" / "MacOS" / info["CFBundleExecutable"]
    assert os.access(executable, os.X_OK)
    verify = subprocess.run(["codesign", "--verify", "--strict", str(app)], capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr
    # The built app goes where the caller says, never into the repository's tracked files.
    assert str(app) in build.stdout

    # In notify mode the helper wants exactly a title and a body. Given
    # anything else it says so and exits 2 at once, showing nothing and
    # drawing no icon: the one way to run the real program here without
    # putting a notification on the screen of whoever runs the tests.
    wrong_use = subprocess.run(
        [str(executable), notifications.NOTIFY_ARGUMENT, "only a title"], capture_output=True, text=True, timeout=10
    )
    assert wrong_use.returncode == 2
    assert "--notify needs a title and a body" in wrong_use.stderr
