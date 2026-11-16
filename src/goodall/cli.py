"""The `goodall` command line: parses arguments and runs one subcommand.

Called by the `goodall` console script (see pyproject.toml) and by
`python -m goodall`. Tests call `main(argv)` directly and check the returned
exit code, so `main` returns a number instead of calling `sys.exit` itself.

Every subcommand is a stub for now. Each one prints that it is not built yet
and returns `ERROR` (1), not `USAGE_ERROR` (2): the command line was valid,
the feature is missing.

Design decisions a reader might otherwise reverse-engineer:

- The parser is built with plain `argparse` calls, one block per command, so
  the whole command surface can be read top to bottom in `build_parser`. There
  is deliberately no registry or plugin mechanism.
- argparse normally calls `sys.exit` on a usage error (and after `--help`).
  `main` catches that `SystemExit` and returns its code, so callers always get
  a number back.
- Output is printed directly here for now. AGENTS.md invariant 9 says all
  terminal input and output goes through one UI module; the stubs move to
  that module when it exists.
"""

import argparse
import sys

from goodall import exit_codes


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for every planned goodall command.

    Each runnable command stores its full name (for example "session start")
    as `command_name`, which `main` uses to report which stub ran. A command
    that only groups others (`session`, `export`) stores its own parser as
    `group_parser`, so `goodall session` on its own can print that group's
    help.
    """
    parser = argparse.ArgumentParser(
        prog="goodall",
        description="Record what a human and their AI agents each did during an open-world eval.",
    )
    commands = parser.add_subparsers(title="commands", metavar="<command>")

    onboard = commands.add_parser("onboard", help="Set up this eval account and its record repo for a new eval.")
    onboard.set_defaults(command_name="onboard")

    doctor = commands.add_parser("doctor", help="Check this account and eval setup without changing anything.")
    doctor.set_defaults(command_name="doctor")

    session = commands.add_parser("session", help="Start, end or inspect a work block (a logged stretch of work).")
    session.set_defaults(group_parser=session)
    session_commands = session.add_subparsers(title="session commands", metavar="<session command>")

    session_start = session_commands.add_parser("start", help="Begin a work block, which opens the prompt gate.")
    session_start.set_defaults(command_name="session start")

    session_end = session_commands.add_parser("end", help="End the current work block, which closes the prompt gate.")
    session_end.set_defaults(command_name="session end")

    session_status = session_commands.add_parser("status", help="Show whether a work block is active.")
    session_status.set_defaults(command_name="session status")

    journal = commands.add_parser("journal", help="Add an entry to the eval journal in the record repo.")
    journal.set_defaults(command_name="journal")

    intervene = commands.add_parser("intervene", help="Log a researcher intervention in the record repo.")
    intervene.set_defaults(command_name="intervene")

    hook = commands.add_parser("hook", help="Handle a Claude Code hook event (called by Claude Code, not by people).")
    hook.add_argument("event_name", help="The Claude Code hook event, for example UserPromptSubmit.")
    hook.set_defaults(command_name="hook")

    sync = commands.add_parser("sync", help="Copy any Claude Code transcripts not yet captured into the log directory.")
    sync.set_defaults(command_name="sync")

    logs = commands.add_parser("logs", help="Show where this eval's raw logs are and what they contain.")
    logs.set_defaults(command_name="logs")

    export = commands.add_parser("export", help="Export the record for analysis.")
    export.set_defaults(group_parser=export)
    export_commands = export.add_subparsers(title="export commands", metavar="<export command>")

    export_scout = export_commands.add_parser("scout", help="Build an Inspect Scout database from the raw record.")
    export_scout.set_defaults(command_name="export scout")

    dry_run = commands.add_parser("dry-run", help="Rehearse a full session end to end without recording a real eval.")
    dry_run.set_defaults(command_name="dry-run")

    teardown = commands.add_parser("teardown", help="Remove goodall's setup from this account after an eval.")
    teardown.set_defaults(command_name="teardown")

    release = commands.add_parser("release", help="Prepare the record repo for publication.")
    release.set_defaults(command_name="release")

    return parser


def run_stub(command_name: str) -> int:
    """Report that a planned command has no implementation yet.

    Returns ERROR rather than USAGE_ERROR because the user typed a valid
    command; it is goodall that cannot do it yet.
    """
    print(
        f"goodall {command_name} is not built yet. "
        "Nothing was changed. Check `bd ready` in the goodall repo for progress.",
        file=sys.stderr,
    )
    return exit_codes.ERROR


def main(argv: list[str] | None = None) -> int:
    """Run goodall with the given arguments and return its exit code.

    `argv` defaults to the real command line. Passing a list lets tests run
    commands without a subprocess.
    """
    parser = build_parser()

    try:
        arguments = parser.parse_args(argv)
    except SystemExit as parser_exit:
        # argparse exits with 2 on a usage error and 0 after printing --help.
        # Trap: the code can be None, which means success.
        if parser_exit.code is None:
            return exit_codes.SUCCESS
        return int(parser_exit.code)

    command_name = getattr(arguments, "command_name", None)
    if command_name is None:
        # No command, or a group like `session` without its subcommand:
        # show the most specific help available and treat it as a usage error.
        group_parser = getattr(arguments, "group_parser", parser)
        group_parser.print_help(sys.stderr)
        return exit_codes.USAGE_ERROR

    return run_stub(command_name)
