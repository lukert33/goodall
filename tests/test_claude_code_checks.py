"""Tests for reading one Claude Code hook command as a `goodall hook` command, or not."""

import pytest

from goodall.claude_code_checks import GoodallHookCommand, read_goodall_hook_command


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "/Users/ada/.local/bin/goodall hook UserPromptSubmit",
            GoodallHookCommand(program="/Users/ada/.local/bin/goodall", event_name="UserPromptSubmit"),
        ),
        ("goodall hook Stop", GoodallHookCommand(program="goodall", event_name="Stop")),
        ("  goodall   hook   Stop  ", GoodallHookCommand(program="goodall", event_name="Stop")),
        (
            "'/Users/ada/my tools/goodall' hook Stop",
            GoodallHookCommand(program="/Users/ada/my tools/goodall", event_name="Stop"),
        ),
        ("~/.local/bin/goodall hook", GoodallHookCommand(program="~/.local/bin/goodall", event_name=None)),
    ],
)
def test_a_goodall_hook_command_is_read_as_its_program_and_event(command: str, expected: GoodallHookCommand) -> None:
    assert read_goodall_hook_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "afplay /System/Sounds/Glass.aiff",
        "goodall status",
        "goodall hook Stop --verbose",
        "/Users/ada/bin/not-goodall hook Stop",
        "goodall hook 'Stop",
        "",
        "echo goodall hook Stop",
    ],
)
def test_anything_else_is_not_a_goodall_hook_command(command: str) -> None:
    assert read_goodall_hook_command(command) is None
