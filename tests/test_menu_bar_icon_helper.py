"""Tests for the menu bar icon's helper, the Swift program in `menu-bar-icon/` (ADR 0024).

Three tests read the Swift source as text, to hold it to rules a compiler
does not check: it never says "eval", every goodall command it runs is one
the command line has, and it draws its own binoculars and shows no clock.

The fourth builds the real helper with `menu-bar-icon/build.sh` into
tmp_path. It is skipped where there is no Swift compiler, which includes
the Linux runners CI uses; CI's separate Mac job runs the same build script
instead (.github/workflows/quality-gates.yml). It proves
the source compiles and the application is whole and signed; it never
starts the helper it built.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from goodall import cli

REPOSITORY_ROOT = Path(__file__).parent.parent
HELPER_FOLDER = REPOSITORY_ROOT / "menu-bar-icon"


def test_the_helper_source_never_says_eval() -> None:
    # The menu and the notifications never say "eval" (ADR 0021). The whole
    # source is held to it, comments too, so no string can slip through.
    source = (HELPER_FOLDER / "main.swift").read_text()
    assert "eval" not in source.lower()


def test_every_goodall_command_the_helper_runs_is_one_the_command_line_has() -> None:
    # Each menu item runs one goodall command (ADR 0024). The helper keeps
    # them in one list, one per line, which this test reads.
    source = (HELPER_FOLDER / "main.swift").read_text()
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
