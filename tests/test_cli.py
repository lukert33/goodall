"""Tests for the `goodall` command-line skeleton in goodall.cli.

These call `main(argv)` directly and capture what it prints with the standard
library, so they need no pytest fixtures.

PLANNED_COMMANDS is the list of every command a user can run. When a command
is added to the parser it must be added here too, or
`test_planned_commands_match_parser` fails.
"""

import argparse
import contextlib
import io

from goodall import cli, exit_codes

# Each entry is the words a user types after `goodall`. The hook command also
# needs an event name, which `arguments_for` supplies.
PLANNED_COMMANDS = [
    "onboard",
    "doctor",
    "session start",
    "session end",
    "session status",
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
        exit_code = cli.main(arguments)
    return exit_code, captured_output.getvalue()


def registered_command_names(parser: argparse.ArgumentParser, prefix: str = "") -> list[str]:
    """List every runnable command the parser knows, as "session start" etc.

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
    session_exit_code, session_output = run_goodall(["session", "--help"])
    assert session_exit_code == exit_codes.SUCCESS
    for subcommand in ["start", "end", "status"]:
        assert subcommand in session_output

    export_exit_code, export_output = run_goodall(["export", "--help"])
    assert export_exit_code == exit_codes.SUCCESS
    assert "scout" in export_output


def test_no_arguments_prints_help_and_is_usage_error() -> None:
    exit_code, output = run_goodall([])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "usage: goodall" in output


def test_group_without_subcommand_is_usage_error() -> None:
    exit_code, output = run_goodall(["session"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "usage: goodall session" in output


def test_every_stub_returns_error_and_names_itself() -> None:
    for command_name in PLANNED_COMMANDS:
        exit_code, output = run_goodall(arguments_for(command_name))
        assert exit_code == exit_codes.ERROR, f"goodall {command_name} returned {exit_code}"
        assert f"goodall {command_name} is not built yet" in output


def test_hook_without_event_name_is_usage_error() -> None:
    exit_code, output = run_goodall(["hook"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "event_name" in output


def test_unknown_command_is_usage_error() -> None:
    exit_code, output = run_goodall(["no-such-command"])
    assert exit_code == exit_codes.USAGE_ERROR
    assert "invalid choice" in output
