"""Behavior tests for the menu bar icon's menu: what it shows after each thing that happens.

The helper (`menu-bar-icon/`) is split in two (ADR 0025).
`MenuModel.swift` decides everything the icon and its menu show, and uses
no AppKit; `main.swift` only draws what the model says and runs goodall.
These tests exercise the model.

Swift's own test frameworks come with Xcode, and goodall builds with only
Apple's Command Line Tools, so the model is tested from pytest instead. A
small driver program, `tests/menu_bar_icon_driver/main.swift`, is compiled
together with `MenuModel.swift` into tmp_path. Each test hands it a list of
events as JSON (goodall answered, a menu item was chosen, a command
finished, the menu was opened), and it prints the menu as it would be drawn
after each event. The tests are skipped where there is no Swift compiler,
which includes the Linux runners CI uses; CI's Mac job runs them.

What these tests cannot see is AppKit itself: how an open menu redraws, and
what the binoculars look like. tests/test_menu_bar_icon_helper.py keeps
source checks for those, and docs/menu-bar-icon.md says what to check by
hand.
"""

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from goodall import cli
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

REPOSITORY_ROOT = Path(__file__).parent.parent
MODEL_SOURCE = REPOSITORY_ROOT / "menu-bar-icon" / "MenuModel.swift"
DRIVER_SOURCE = Path(__file__).parent / "menu_bar_icon_driver" / "main.swift"

# The log file's path as the menu shows it when GOODALL_MENU_BAR_LOG is not set.
LOG_PATH_FOR_PEOPLE = "~/Library/Logs/goodall/menu-bar-icon.log"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="the menu bar icon's model is Swift, and compiling it needs macOS with the Swift compiler",
)


@pytest.fixture(scope="module")
def driver(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the driver and the model once for this file, and return the program's path."""
    program = tmp_path_factory.mktemp("menu-bar-icon-driver") / "menu-model-driver"
    subprocess.run(
        ["swiftc", "-target", "arm64-apple-macos13.0", str(MODEL_SOURCE), str(DRIVER_SOURCE), "-o", str(program)],
        check=True,
        capture_output=True,
        text=True,
    )
    return program


def run_driver(driver: Path, events: list[dict[str, Any]], goodall_is_known: bool = True) -> list[dict[str, Any]]:
    """Hand the events to the driver and return the menu it would draw after each one."""
    driver_input = {"goodall_is_known": goodall_is_known, "log_path_for_people": LOG_PATH_FOR_PEOPLE, "events": events}
    completed = subprocess.run(
        [str(driver)], input=json.dumps(driver_input), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    renderings: list[dict[str, Any]] = json.loads(completed.stdout)
    assert len(renderings) == len(events)
    return renderings


def icon_state_output(
    state: str,
    status_line: str,
    record_work_block: bool,
    pause: bool,
    stop_recording: bool,
) -> str:
    """What `goodall icon-state --json` prints, for an account whose eval has two workstreams."""
    document = {
        "schema_version": 1,
        "state": state,
        "status_line": status_line,
        "account_mode": "dedicated",
        "work_block": None,
        "paused_work_block": None,
        "workstreams": [{"name": "research", "main": True}, {"name": "go-to-market", "main": False}],
        "enabled": {"record_work_block": record_work_block, "pause": pause, "stop_recording": stop_recording},
        "setup": {"usable": True, "reason": None},
        "watch_folder": "/Volumes/EvalLogs/goodall-pilot/work-blocks",
    }
    if state == "recording":
        document["work_block"] = {
            "id": "20261116T093000Z-ada-lovelace-1a2b3c4d",
            "workstream": "research",
            "started_at": "2026-11-16T09:30:00Z",
        }
    return json.dumps(document, indent=2)


NOT_RECORDING = icon_state_output("not_recording", "Not recording", True, False, False)
RECORDING = icon_state_output("recording", "Recording research since 09:30", False, True, True)
PAUSED = icon_state_output("paused", "Paused on research since 10:15", True, False, True)


def answered(output: str) -> dict[str, Any]:
    """The event: goodall answered `icon-state --json` with this output."""
    return {"event": "state_answered", "output": output}


def chosen(*titles: str) -> dict[str, Any]:
    """The event: the person chose the menu item with these titles, outermost first."""
    return {"event": "item_chosen", "titles": list(titles)}


def finished(exit_code: int) -> dict[str, Any]:
    """The event: the menu command that was running ended with this exit code."""
    return {"event": "command_finished", "exit_code": exit_code}


def menu_opened() -> dict[str, Any]:
    """The event: the person opened the menu."""
    return {"event": "menu_opened"}


def row_titles(rendering: dict[str, Any]) -> list[str]:
    titles = []
    for row in rendering["rows"]:
        titles.append(row["title"])
    return titles


def row(rendering: dict[str, Any], title: str) -> dict[str, Any]:
    candidate: dict[str, Any]
    for candidate in rendering["rows"]:
        if candidate["title"] == title:
            return candidate
    raise AssertionError(f"no row titled {title!r} in {row_titles(rendering)}")


def enabled_titles(rendering: dict[str, Any]) -> list[str]:
    titles = []
    for candidate in rendering["rows"]:
        if candidate["enabled"]:
            titles.append(candidate["title"])
    return titles


def test_the_not_recording_menu_offers_every_workstream_with_the_main_one_first(driver: Path) -> None:
    [rendering] = run_driver(driver, [answered(NOT_RECORDING)])

    assert rendering["look"] == "not_recording"
    assert rendering["failure_mark"] is False
    assert rendering["tool_tip"] == "Not recording"
    assert row_titles(rendering) == [
        "Not recording",
        "",
        "Record work block",
        "Pause",
        "Stop recording",
        "",
        "Settings…",
        "Quit goodall",
    ]
    assert enabled_titles(rendering) == ["Record work block", "Settings…", "Quit goodall"]
    workstream_rows = row(rendering, "Record work block")["submenu"]
    assert workstream_rows == [
        {
            "title": "research (main)",
            "enabled": True,
            "separator": False,
            "arguments": ["start", "--workstream", "research"],
            "submenu": None,
        },
        {
            "title": "go-to-market",
            "enabled": True,
            "separator": False,
            "arguments": ["start", "--workstream", "go-to-market"],
            "submenu": None,
        },
    ]


def test_each_look_follows_goodalls_state_and_the_tool_tip_is_the_status_line(driver: Path) -> None:
    renderings = run_driver(driver, [answered(RECORDING), answered(PAUSED), answered(NOT_RECORDING)])

    looks = []
    tool_tips = []
    for rendering in renderings:
        looks.append(rendering["look"])
        tool_tips.append(rendering["tool_tip"])
    assert looks == ["recording", "paused", "not_recording"]
    assert tool_tips == ["Recording research since 09:30", "Paused on research since 10:15", "Not recording"]


def test_the_items_enabled_are_exactly_the_ones_goodall_enables(driver: Path) -> None:
    recording, paused = run_driver(driver, [answered(RECORDING), answered(PAUSED)])

    assert enabled_titles(recording) == ["Pause", "Stop recording", "Settings…", "Quit goodall"]
    assert enabled_titles(paused) == ["Record work block", "Stop recording", "Settings…", "Quit goodall"]


def test_each_menu_item_runs_its_one_goodall_command(driver: Path) -> None:
    [rendering] = run_driver(driver, [answered(RECORDING)])

    assert row(rendering, "Pause")["arguments"] == ["pause"]
    assert row(rendering, "Stop recording")["arguments"] == ["stop"]
    assert row(rendering, "Settings…")["arguments"] == ["settings"]


@pytest.mark.parametrize(
    ("starting_state", "titles", "busy_line"),
    [
        (NOT_RECORDING, ["Record work block", "go-to-market"], "Preparing to record…"),
        (RECORDING, ["Pause"], "Pausing…"),
        (RECORDING, ["Stop recording"], "Stopping…"),
        (NOT_RECORDING, ["Settings…"], "Opening settings…"),
        (RECORDING, ["Quit goodall"], "Stopping…"),
    ],
)
def test_while_a_command_runs_the_menu_says_what_it_is_doing_and_offers_nothing(
    driver: Path, starting_state: str, titles: list[str], busy_line: str
) -> None:
    # The owner's copy note of 2026-11-27: not "Working…", because in goodall
    # "work" means a work block. Every item is gray, Quit goodall included,
    # so a second click cannot start a second command.
    _, running = run_driver(driver, [answered(starting_state), chosen(*titles)])

    assert running["rows"][0]["title"] == busy_line
    assert enabled_titles(running) == []
    assert running["running_arguments"] is not None


def test_the_menu_keeps_its_busy_line_until_goodall_says_what_the_command_changed(driver: Path) -> None:
    # Found by clicking on 2026-11-27: after Stop recording, an open menu
    # flashed "Recording research since ..." before settling on "Not
    # recording", because it drew the answer goodall gave before the command.
    renderings = run_driver(
        driver, [answered(RECORDING), chosen("Stop recording"), finished(0), answered(NOT_RECORDING)]
    )
    _, running, finished_but_not_answered, settled = renderings

    assert running["rows"][0]["title"] == "Stopping…"
    assert finished_but_not_answered["rows"][0]["title"] == "Stopping…"
    assert enabled_titles(finished_but_not_answered) == []
    assert settled["rows"][0]["title"] == "Not recording"
    assert settled["running_arguments"] is None
    assert enabled_titles(settled) == ["Record work block", "Settings…", "Quit goodall"]


def test_an_answer_that_arrives_while_a_command_runs_does_not_end_the_busy_line(driver: Path) -> None:
    # The folder watch can ask goodall again while a command is still running.
    renderings = run_driver(driver, [answered(RECORDING), chosen("Pause"), answered(RECORDING)])

    assert renderings[-1]["rows"][0]["title"] == "Pausing…"
    assert enabled_titles(renderings[-1]) == []


def test_a_failed_command_adds_two_gray_lines_and_a_mark_that_clears_when_the_menu_opens(driver: Path) -> None:
    renderings = run_driver(
        driver,
        [answered(NOT_RECORDING), chosen("Settings…"), finished(1), answered(NOT_RECORDING), menu_opened()],
    )
    _, _, failed, settled, opened = renderings

    assert failed["failure_mark"] is True
    assert settled["failure_mark"] is True
    assert row_titles(settled)[:3] == [
        "Not recording",
        '"Settings…" did not work.',
        f"Details are in {LOG_PATH_FOR_PEOPLE}",
    ]
    assert row(settled, '"Settings…" did not work.')["enabled"] is False
    assert opened["failure_mark"] is False
    assert row_titles(opened)[:3] == row_titles(settled)[:3]


def test_a_failed_workstream_names_the_record_work_block_item(driver: Path) -> None:
    renderings = run_driver(
        driver,
        [answered(NOT_RECORDING), chosen("Record work block", "go-to-market"), finished(1), answered(NOT_RECORDING)],
    )

    assert row_titles(renderings[-1])[1] == '"Record work block" did not work.'


def test_a_command_that_works_clears_an_earlier_failure(driver: Path) -> None:
    renderings = run_driver(
        driver,
        [
            answered(NOT_RECORDING),
            chosen("Settings…"),
            finished(1),
            answered(NOT_RECORDING),
            chosen("Record work block", "research (main)"),
            finished(0),
            answered(RECORDING),
        ],
    )

    assert row_titles(renderings[-1])[:2] == ["Recording research since 09:30", ""]
    assert renderings[-1]["failure_mark"] is False


def test_quit_while_recording_stops_first(driver: Path) -> None:
    _, quitting = run_driver(driver, [answered(RECORDING), chosen("Quit goodall")])

    assert quitting["quit"] == "stop_then_leave"
    assert quitting["running_arguments"] == ["stop"]


def test_quit_while_paused_stops_first(driver: Path) -> None:
    # Paused is not recording, but the paused work block is still open to
    # continue; Stop recording, which Quit goodall does, finishes it.
    _, quitting = run_driver(driver, [answered(PAUSED), chosen("Quit goodall")])

    assert quitting["quit"] == "stop_then_leave"


def test_quit_when_nothing_is_recording_leaves_at_once(driver: Path) -> None:
    _, quitting = run_driver(driver, [answered(NOT_RECORDING), chosen("Quit goodall")])

    assert quitting["quit"] == "leave_now"
    assert quitting["running_arguments"] is None


def test_an_unreadable_answer_says_goodall_did_not_answer_and_offers_only_settings_and_quit(driver: Path) -> None:
    [rendering] = run_driver(driver, [answered("Traceback (most recent call last):\n")])

    assert row_titles(rendering)[:2] == ["goodall did not answer.", "Run goodall status in Terminal to see why."]
    assert rendering["look"] == "not_recording"
    assert enabled_titles(rendering) == ["Settings…", "Quit goodall"]


def test_a_helper_not_told_where_goodall_is_offers_only_quit(driver: Path) -> None:
    [rendering] = run_driver(driver, [menu_opened()], goodall_is_known=False)

    assert row_titles(rendering)[:2] == [
        "This icon was not told where goodall is.",
        "Choose Quit goodall, then run goodall start in Terminal.",
    ]
    assert enabled_titles(rendering) == ["Quit goodall"]


def test_the_menu_draws_goodalls_real_answer_when_setup_is_not_usable(driver: Path) -> None:
    # The real `goodall icon-state --json`, not a hand-written document, so a
    # change to the document that the Swift cannot read fails here. With no
    # local settings (the test's isolated config folder is empty), setup is
    # not usable and goodall says why.
    standard_output = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=io.StringIO())
    cli.main(["icon-state", "--json"], terminal=terminal, runner=FakeRunner(real_programs=[]))
    document = json.loads(standard_output.getvalue())
    assert document["setup"]["usable"] is False

    [rendering] = run_driver(driver, [answered(standard_output.getvalue())])

    assert row_titles(rendering)[:2] == [document["status_line"], document["setup"]["reason"]]
    assert enabled_titles(rendering) == ["Settings…", "Quit goodall"]


def test_no_menu_text_says_eval(driver: Path) -> None:
    # The menu never says "eval" (ADR 0021). Every state and failure line is
    # drawn once here and every title checked, submenus included.
    renderings = run_driver(
        driver,
        [
            answered(NOT_RECORDING),
            answered(RECORDING),
            answered(PAUSED),
            chosen("Settings…"),
            finished(1),
            answered(NOT_RECORDING),
            answered("not JSON"),
        ],
    )
    rows_to_check = []
    for rendering in renderings:
        rows_to_check.extend(rendering["rows"])
    titles = []
    while rows_to_check:
        current = rows_to_check.pop()
        titles.append(current["title"])
        if current["submenu"]:
            rows_to_check.extend(current["submenu"])
    for title in titles:
        assert "eval" not in title.lower()
