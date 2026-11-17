"""Screen tests for goodall.terminal, the only module that prints.

These compare whole screens, at the fixed width used when output is not a
terminal, with colour off, plus a test with colour on. The screens are the
contract: a future replacement for the terminal module would have to
produce them too.
"""

import io
import json
import re

import pytest

from goodall.terminal import (
    FIXED_WIDTH,
    CheckCounts,
    CheckResult,
    CheckStatus,
    Failure,
    Terminal,
    clamp_width,
    count_checks,
)


class FakeTerminalStream(io.StringIO):
    """A captured stream that claims to be a terminal, to switch colour on.

    It has no file descriptor, so the terminal module cannot measure it and
    falls back to the fixed width; that keeps the screen predictable.
    """

    def isatty(self) -> bool:
        return True


EXAMPLE_FAILURE = Failure(
    kind="example_missing",
    what_happened="No example file was found at /tmp/an/example/folder/with/a/rather/long/name/example.toml.",
    why=(
        "The example file tells goodall which example to use. Without it goodall cannot choose, "
        "so it stops here rather than guess."
    ),
    what_to_do_next="Run goodall onboard.",
)

EXPECTED_SCREEN = """\
FAIL  No example file was found at
      /tmp/an/example/folder/with/a/rather/long/name/example.toml.

Why   The example file tells goodall which example to use. Without it goodall
      cannot choose, so it stops here rather than guess.

Next  Run goodall onboard.
"""


def test_failure_screen_without_colour() -> None:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)

    terminal.show_failure(EXAMPLE_FAILURE)

    assert standard_error.getvalue() == EXPECTED_SCREEN
    assert standard_output.getvalue() == ""


def test_failure_screen_with_colour_reads_the_same_once_colour_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    standard_error = FakeTerminalStream()
    terminal = Terminal(standard_output=io.StringIO(), standard_error=standard_error)

    terminal.show_failure(EXAMPLE_FAILURE)

    screen = standard_error.getvalue()
    assert screen.startswith("\x1b[1;31mFAIL\x1b[0m  No example")
    without_colour = screen.replace("\x1b[1;31m", "").replace("\x1b[1m", "").replace("\x1b[0m", "")
    assert without_colour == EXPECTED_SCREEN


def test_no_color_variable_turns_colour_off_even_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    standard_error = FakeTerminalStream()
    terminal = Terminal(standard_output=io.StringIO(), standard_error=standard_error)

    terminal.show_failure(EXAMPLE_FAILURE)

    assert standard_error.getvalue() == EXPECTED_SCREEN


SECOND_FAILURE = Failure(
    kind="second_example",
    what_happened="A second thing went wrong.",
    why="Two problems can be found at once.",
    what_to_do_next="Fix both.",
)


def test_several_failures_are_separated_by_a_blank_line() -> None:
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=io.StringIO(), standard_error=standard_error)

    terminal.show_failures([EXAMPLE_FAILURE, SECOND_FAILURE])

    assert standard_error.getvalue() == (
        EXPECTED_SCREEN
        + "\n"
        + "FAIL  A second thing went wrong.\n\nWhy   Two problems can be found at once.\n\nNext  Fix both.\n"
    )


def test_json_failure_is_one_document_on_standard_output() -> None:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)

    terminal.write_json({"failure": EXAMPLE_FAILURE.as_json()})

    assert standard_error.getvalue() == ""
    assert json.loads(standard_output.getvalue()) == {
        "failure": {
            "kind": "example_missing",
            "what_happened": EXAMPLE_FAILURE.what_happened,
            "why": EXAMPLE_FAILURE.why,
            "what_to_do_next": "Run goodall onboard.",
        }
    }


def test_facts_line_up_and_blocks_are_separated_by_a_blank_line() -> None:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)

    terminal.show_facts([("Eval", "goodall-pilot"), ("Contributor", "Ada Lovelace (ada-lovelace)")])
    terminal.show_paragraph(
        "No work block is active. This sentence is long enough that it has to wrap onto a second line at the "
        "fixed width."
    )

    assert standard_error.getvalue() == ""
    assert standard_output.getvalue() == (
        "Eval         goodall-pilot\n"
        "Contributor  Ada Lovelace (ada-lovelace)\n"
        "\n"
        "No work block is active. This sentence is long enough that it has to wrap onto a\n"
        "second line at the fixed width.\n"
    )


def test_a_long_fact_wraps_under_its_value_column() -> None:
    standard_output = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=io.StringIO())

    terminal.show_facts([("Why", "word " * 20)])

    assert standard_output.getvalue() == (
        "Why  word word word word word word word word word word word word word word word\n"
        "     word word word word word\n"
    )


def test_output_that_is_not_a_terminal_uses_the_fixed_width() -> None:
    assert FIXED_WIDTH == 80


def test_measured_width_is_clamped_to_a_readable_range() -> None:
    assert clamp_width(20) == 40
    assert clamp_width(72) == 72
    assert clamp_width(300) == 100


# The check report, which goodall doctor prints. Two groups and every status,
# so the layout of each kind of line is pinned down. The words pass, FAIL,
# warn, skip and unverified are always printed, so the screen reads without
# colour. The status column is as wide as its longest word, "unverified", so
# every summary starts in the same column.

REPORT_RESULTS = [
    CheckResult(id="settings.found", group="Settings", status=CheckStatus.PASS, summary="Settings are found"),
    CheckResult(
        id="settings.valid",
        group="Settings",
        status=CheckStatus.FAIL,
        summary="Settings are valid",
        failures=[EXAMPLE_FAILURE, SECOND_FAILURE],
    ),
    CheckResult(
        id="settings.folder",
        group="Settings",
        status=CheckStatus.SKIP,
        summary="The folder named in the settings exists, which is a long enough summary to wrap",
        waiting_for="settings.valid",
    ),
    CheckResult(
        id="tools.drive",
        group="Tools",
        status=CheckStatus.WARN,
        summary="The drive is connected",
        failures=[
            Failure(kind="drive", what_happened="Not recording.", why="No drive.", what_to_do_next="Plug it in.")
        ],
    ),
    CheckResult(id="tools.git", group="Tools", status=CheckStatus.PASS, summary="git is installed"),
    CheckResult(
        id="tools.keys",
        group="Tools",
        status=CheckStatus.UNVERIFIED,
        summary="The keys are in the drawer",
        failures=[
            Failure(
                kind="keys_not_checked",
                what_happened="The drawer is locked, so goodall could not look.",
                why="Goodall does not force drawers.",
                what_to_do_next="Look in the drawer yourself.",
            )
        ],
    ),
]

EXPECTED_REPORT_SCREEN = """\
Settings
  pass        Settings are found
  FAIL        Settings are valid
              No example file was found at
              /tmp/an/example/folder/with/a/rather/long/name/example.toml.
              Next: Run goodall onboard.

              A second thing went wrong.
              Next: Fix both.

  skip        The folder named in the settings exists, which is a long enough
              summary to wrap
              Waiting for: Settings are valid

Tools
  warn        The drive is connected
              Not recording.
              Next: Plug it in.

  pass        git is installed
  unverified  The keys are in the drawer
              The drawer is locked, so goodall could not look.
              Next: Look in the drawer yourself.

2 passed, 1 warned, 1 failed, 1 skipped, 1 unverified
"""


def test_check_report_screen_without_colour() -> None:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)

    terminal.show_check_report(REPORT_RESULTS)

    assert standard_output.getvalue() == EXPECTED_REPORT_SCREEN
    assert standard_error.getvalue() == ""


def test_check_report_with_colour_reads_the_same_once_colour_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    standard_output = FakeTerminalStream()
    terminal = Terminal(standard_output=standard_output, standard_error=io.StringIO())

    terminal.show_check_report(REPORT_RESULTS)

    screen = standard_output.getvalue()
    assert "\x1b[1;31mFAIL\x1b[0m        Settings are valid" in screen
    without_colour = re.sub("\x1b\\[[0-9;]*m", "", screen)
    assert without_colour == EXPECTED_REPORT_SCREEN


def test_check_counts_and_json_come_from_the_same_results() -> None:
    assert count_checks(REPORT_RESULTS) == CheckCounts(passed=2, warned=1, failed=1, skipped=1, unverified=1)
    assert REPORT_RESULTS[1].as_json() == {
        "id": "settings.valid",
        "group": "Settings",
        "status": "fail",
        "summary": "Settings are valid",
        "detail": EXAMPLE_FAILURE.what_happened + " " + SECOND_FAILURE.what_happened,
        "next_step": "Run goodall onboard. Fix both.",
        "waiting_for": None,
        "note": None,
        "failures": [EXAMPLE_FAILURE.as_json(), SECOND_FAILURE.as_json()],
    }
    assert REPORT_RESULTS[2].as_json()["waiting_for"] == "settings.valid"
    assert REPORT_RESULTS[0].as_json()["detail"] is None
    assert REPORT_RESULTS[0].as_json()["next_step"] is None
    # Unverified is its own status, neither a pass nor a failure.
    assert REPORT_RESULTS[5].as_json()["status"] == "unverified"
    assert REPORT_RESULTS[5].as_json()["next_step"] == "Look in the drawer yourself."


def test_a_passing_check_with_a_note_shows_the_note_beneath_it_and_in_json() -> None:
    result = CheckResult(
        id="secrets.env_file",
        group="Secrets",
        status=CheckStatus.PASS,
        summary="The env file holds only secret references",
        note="There is no env file, which is fine if this eval uses no secrets, as many small evals do not.",
    )
    standard_output = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=io.StringIO())

    terminal.show_check_report([result, REPORT_RESULTS[4]])

    assert standard_output.getvalue() == (
        "Secrets\n"
        "  pass        The env file holds only secret references\n"
        "              There is no env file, which is fine if this eval uses no secrets,\n"
        "              as many small evals do not.\n"
        "\n"
        "Tools\n"
        "  pass        git is installed\n"
        "\n"
        "2 passed, 0 warned, 0 failed, 0 skipped, 0 unverified\n"
    )
    assert result.as_json()["note"] == result.note
    assert REPORT_RESULTS[4].as_json()["note"] is None
