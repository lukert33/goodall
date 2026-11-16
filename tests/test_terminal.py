"""Screen tests for goodall.terminal, the only module that prints.

These compare whole screens, at the fixed width used when output is not a
terminal, with colour off, plus a test with colour on. The screens are the
contract: a future replacement for the terminal module would have to
produce them too.
"""

import io
import json

import pytest

from goodall.terminal import FIXED_WIDTH, Failure, Terminal, clamp_width


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
