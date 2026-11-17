"""Where the `goodall` command starts: sends `goodall hook` one way and everything else another.

The `goodall` console script (pyproject.toml) and `python -m goodall` both
call `main` here. It exists for speed. Claude Code runs `goodall hook` before
every prompt and after every tool call, so its start-up time is paid
constantly (ADR 0005). goodall.cli imports every command, the runner and
argparse, none of which the hook needs, so `goodall hook ...` goes straight
to goodall.hook and never loads them.

That is why the imports below sit inside `main` rather than at the top of
the file: each path loads only its own modules. `goodall hook --help` goes
to goodall.cli, which prints the help argparse builds.
"""

import sys

HELP_FLAGS = ["-h", "--help"]


def main() -> int:
    """Run goodall with the real command line and return its exit code."""
    arguments = sys.argv[1:]
    is_hook = len(arguments) > 0 and arguments[0] == "hook"
    if is_hook and not any(argument in HELP_FLAGS for argument in arguments):
        from goodall.hook import run_hook_command

        return run_hook_command(arguments[1:])

    from goodall.cli import main as run_command_line

    return run_command_line(arguments)
