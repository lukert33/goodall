"""Tests for the `goodall` command-line skeleton in goodall.cli.

These call `main(argv, runner=FakeRunner())` directly and capture what it
prints with the standard library, so they need no pytest fixtures. The fake
runner has nothing scripted, so a command that tried to start a program
would fail its test instead of running it.

PLANNED_COMMANDS is the list of every command a user can run. When a command
is added to the parser it must be added here too, or
`test_planned_commands_match_parser` fails.
"""

import argparse
import contextlib
import io

from goodall import cli, exit_codes
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

# Each entry is the words a user types after `goodall`. The hook command also
# needs an event name, which `arguments_for` supplies.
PLANNED_COMMANDS = [
    "onboard",
    "doctor",
    "start",
    "stop",
    "fixblock",
    "status",
    "journal",
    "intervene",
    "hook",
    "sync",
    "logs",
    "export scout",
    "dry-run",
    "teardown",
    "release",
]


def arguments_for(command_name: str) -> list[str]:
    """Turn a command name into the argument list a user would type."""
    arguments = command_name.split(" ")
    if command_name == "hook":
        arguments.append("UserPromptSubmit")
    return arguments


def run_goodall(arguments: list[str]) -> tuple[int, str]:
    """Run goodall in-process and return its exit code and everything it printed."""
    captured_output = io.StringIO()
    with contextlib.redirect_stdout(captured_output), contextlib.redirect_stderr(captured_output):
        exit_code = cli.main(arguments, runner=FakeRunner())
    return exit_code, captured_output.getvalue()


def registered_command_names(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """List every runnable command the parser knows, as "start", "export scout" etc.

    argparse has no public way to list subcommands, so this reads the
    `_SubParsersAction` objects it keeps in `parser._actions`. A command with
    its own subcommands is a group, and only its children are listed.
    """
    command_names = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            child_names = registered_command_names(subparser, prefix + name + " ")
            if child_names:
                command_names.extend(child_names)
            else:
                command_names.append(prefix + name)
    return command_names


def test_planned_commands_match_parser() -> None:
    registered = registered_command_names(cli.build_parser())
    assert sorted(registered) == sorted(PLANNED_COMMANDS)


def test_help_lists_every_top_level_command() -> None:
    exit_code, output = run_goodall(["--help"])
    assert exit_code == exit_codes.SUCCESS
    for command_name in PLANNED_COMMANDS:
        top_level_word = command_name.split(" ")[0]
        assert top_level_word in output, f"{top_level_word} missing from help"


def test_group_help_lists_its_subcommands() -> None:
    exit_code, output = run_goodall(["export", "--help"])
    assert exit_code == exit_codes.SUCCESS
    assert "scout" in output


def test_help_describes_work_blocks_without_saying_session() -> None:
    # "Work block" is goodall's word for a logged stretch of work; "session"
    # is reserved for a Claude session, which top-level help never mentions.
    exit_code, output = run_goodall(["--help"])
    assert exit_code == exit_codes.SUCCESS
    assert "work block" in output
    assert "session" not in output.lower()


def test_old_session_command_is_unknown() -> None:
    # The work-block commands were `goodall session start|end|status` before
    # they became top-level. The old group must not linger as an alias.
    exit_code, output = run_goodall(["session", "start"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "invalid choice" in output


def test_start_accepts_a_workstream() -> None:
    # The option is accepted, so goodall goes on to look for local settings,
    # which this test has none of.
    exit_code, output = run_goodall(["start", "--workstream", "research"])
    assert exit_code == exit_codes.ERROR
    assert "No local settings were found" in output


def test_start_workstream_option_needs_a_name() -> None:
    exit_code, output = run_goodall(["start", "--workstream"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "--workstream" in output


def test_no_arguments_prints_help_and_is_usage_error() -> None:
    exit_code, output = run_goodall([])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "usage: goodall" in output


def test_group_without_subcommand_is_usage_error() -> None:
    exit_code, output = run_goodall(["export"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "usage: goodall export" in output


# Commands that do something real, so they no longer report "not built yet".
BUILT_COMMANDS = ["status", "doctor", "journal", "intervene", "start", "stop", "fixblock", "hook"]


def test_every_stub_returns_error_and_names_itself() -> None:
    for command_name in PLANNED_COMMANDS:
        if command_name in BUILT_COMMANDS:
            continue
        exit_code, output = run_goodall(arguments_for(command_name))
        assert exit_code == exit_codes.ERROR, f"goodall {command_name} returned {exit_code}"
        assert f"goodall {command_name} is not built yet" in output


def test_no_stub_points_a_researcher_at_goodall_s_own_tooling() -> None:
    # A researcher reads these; goodall's issue tracker means nothing to them.
    for command_name in PLANNED_COMMANDS:
        if command_name in BUILT_COMMANDS:
            continue
        _, output = run_goodall(arguments_for(command_name))
        assert "bd " not in output, f"goodall {command_name} mentions bd"
        assert "goodall repo" not in output, f"goodall {command_name} mentions the goodall repo"


def test_onboard_stub_says_how_to_set_up_an_eval_account_by_hand() -> None:
    # "Run goodall onboard" is the first next step a new contributor sees,
    # so the stub must not be a dead end.
    exit_code, output = run_goodall(["onboard"])

    assert exit_code == exit_codes.ERROR
    one_line = " ".join(output.split())
    assert "ask whoever runs the eval" in one_line.lower()
    assert "local-settings.toml" in one_line
    assert "~/.config/goodall/" in one_line


class KeyboardInput(io.StringIO):
    """Standard input that says it is a terminal: a person typing, nothing piped."""

    def isatty(self) -> bool:
        return True


def test_hook_without_event_name_run_by_a_person_is_an_error_that_shows_the_fix() -> None:
    # Claude Code reads exit 2 as "refuse the prompt" or "do not stop", so a
    # hook command missing its event name must exit 1, which Claude Code
    # shows as a hook error and otherwise ignores.
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=io.StringIO(), standard_error=standard_error, standard_input=KeyboardInput())

    exit_code = cli.main(["hook"], terminal=terminal, runner=FakeRunner())

    assert exit_code == exit_codes.ERROR
    assert "goodall hook UserPromptSubmit" in standard_error.getvalue()


def test_hook_without_event_name_from_claude_code_is_an_error_that_prints_nothing() -> None:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output, standard_error, standard_input=io.StringIO("{cut short"))

    exit_code = cli.main(["hook"], terminal=terminal, runner=FakeRunner())

    assert exit_code == exit_codes.ERROR
    assert standard_output.getvalue() == standard_error.getvalue() == ""


def test_unknown_command_is_usage_error() -> None:
    exit_code, output = run_goodall(["no-such-command"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "invalid choice" in output
