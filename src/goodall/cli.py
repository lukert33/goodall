"""The `goodall` command line: parses arguments and runs one subcommand.

Called by the `goodall` console script (see pyproject.toml) and by
`python -m goodall`. Tests call `main(argv, terminal=..., runner=...)`
directly with a captured terminal and a fake runner, and check the returned
exit code, so `main` returns a number instead of calling `sys.exit` itself.

`goodall status` (goodall.status) and `goodall doctor` (goodall.doctor)
are built. Every other subcommand is still a stub: it reports that it is not
built yet and returns `ERROR` (1), not `USAGE_ERROR` (2), because the
command line was valid and the feature is missing.

Design decisions a reader might otherwise reverse-engineer:

- The parser is built with plain `argparse` calls, one block per command, so
  the whole command surface can be read top to bottom in `build_parser`. There
  is deliberately no registry or plugin mechanism.
- argparse normally calls `sys.exit` on a usage error (and after `--help`).
  `main` catches that `SystemExit` and returns its code, so callers always get
  a number back.
- Everything goodall says goes through the `Terminal` passed to `main`
  (goodall.terminal). The one exception is argparse's own help and usage
  messages, which argparse prints itself.
- Every program goodall starts goes through the runner passed to `main`
  (goodall.runner, ADR 0011), which defaults to the real one. A test passes
  `goodall.fake_runner.FakeRunner` instead.
"""

import argparse
from pathlib import Path

from goodall import exit_codes, records
from goodall.doctor import run_doctor
from goodall.entries import DECISION_MEANINGS, EntryRequest, InterventionDetails, run_intervene, run_journal
from goodall.runner import RealRunner, Runner
from goodall.status import run_status
from goodall.terminal import Failure, Terminal


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for every planned goodall command.

    Each runnable command stores its full name (for example "export scout")
    as `command_name`, which `main` uses to report which stub ran. A command
    that only groups others (`export`) stores its own parser as
    `group_parser`, so `goodall export` on its own can print that group's
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
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print the results as one JSON document on standard output, for agents and scripts.",
    )
    doctor.set_defaults(command_name="doctor")

    # The three work-block commands are top-level, not grouped, because a
    # contributor types them every day and the shortest names win.
    start = commands.add_parser("start", help="Begin a work block, which opens the prompt gate.")
    start.add_argument(
        "--workstream",
        metavar="<name>",
        help="The workstream this work block is for. Defaults to your main workstream from local settings.",
    )
    start.set_defaults(command_name="start")

    stop = commands.add_parser("stop", help="End the current work block, which closes the prompt gate.")
    stop.set_defaults(command_name="stop")

    status = commands.add_parser("status", help="Show whether a work block is active.")
    status.add_argument(
        "--json",
        action="store_true",
        help="Print the result as one JSON document on standard output, for agents and scripts.",
    )
    status.set_defaults(command_name="status")

    journal = commands.add_parser(
        "journal",
        help="Add an entry to the eval journal in the record repo.",
        description="Add a journal entry: a note about what was decided or done, and by whom.",
        epilog=(
            "examples:\n"
            '  goodall journal "Chose Postgres over SQLite; I decided, the agent wrote the comparison."\n'
            "  pbpaste | goodall journal              (several lines, copied first)\n"
            "  goodall journal --file ~/Desktop/note.txt"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_entry_text_arguments(journal, "What was decided or done, and by whom.")
    journal.set_defaults(command_name="journal")

    # The decisions' meanings come from goodall.entries, so help and failure
    # messages explain them in the same words.
    decision_lines = []
    for decision, meaning in DECISION_MEANINGS.items():
        decision_lines.append(f"  {decision.ljust(10)} {meaning}")
    intervene = commands.add_parser(
        "intervene",
        help="Add an intervention to the record repo: a moment you stepped in for or over an agent.",
        description=(
            "Add an intervention: a moment when you overrode, corrected, redirected or rescued an agent, "
            "or had to act because the agent could not."
        ),
        epilog=(
            "examples:\n"
            "  goodall intervene --category agent-limitation --decision modify \\\n"
            '      "It tried to delete a failing test; I told it to fix the date parsing."\n'
            "  pbpaste | goodall intervene --category infrastructure --decision escalate\n"
            "  goodall intervene --category policy-mandated --decision approve --file ~/Desktop/review.txt\n"
            "\n"
            "decisions:\n" + "\n".join(decision_lines)
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_entry_text_arguments(intervene, "What happened and why.")
    intervene.add_argument(
        "--category",
        metavar="<category>",
        help="Which of the eval's intervention categories this is, from intervention_categories in eval.toml.",
    )
    intervene.add_argument(
        "--decision",
        metavar="<decision>",
        help="What you decided: approve, modify, reject, escalate or terminate (see below).",
    )
    intervene.add_argument(
        "--interrupted",
        choices=records.INTERRUPTED_ACTIVITIES,
        help=(
            "Only if you cut the agent short: what it was doing. generate means writing its reply, tool_call "
            "running a tool, between_turns neither."
        ),
    )
    intervene.add_argument(
        "--claude-session",
        metavar="<session id>",
        help="The Claude session it happened in, as the Session ID that /status shows in Claude Code.",
    )
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

    dry_run = commands.add_parser("dry-run", help="Rehearse a whole work block without recording a real eval.")
    dry_run.set_defaults(command_name="dry-run")

    teardown = commands.add_parser("teardown", help="Remove goodall's setup from this account after an eval.")
    teardown.set_defaults(command_name="teardown")

    release = commands.add_parser("release", help="Prepare the record repo for publication.")
    release.set_defaults(command_name="release")

    return parser


def add_entry_text_arguments(parser: argparse.ArgumentParser, text_help: str) -> None:
    """Add the ways of giving an entry's text, and --json, which journal and intervene share.

    The text is optional to argparse because it may instead come from
    --file or from standard input; goodall.entries reports a missing text
    itself, with examples to copy.
    """
    parser.add_argument("text", nargs="?", help=text_help + " Leave it out to pipe the text in instead.")
    parser.add_argument(
        "--file",
        metavar="<path>",
        type=Path,
        help="Read the text from this plain text file, for an entry written in TextEdit or another editor.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the result as one JSON document on standard output, for agents and scripts.",
    )


def run_stub(command_name: str, terminal: Terminal) -> int:
    """Report that a planned command has no implementation yet.

    Returns ERROR rather than USAGE_ERROR because the user typed a valid
    command; it is goodall that cannot do it yet.
    """
    terminal.show_failure(
        Failure(
            kind="not_built",
            what_happened=f"goodall {command_name} is not built yet. Nothing was changed.",
            why="This command is planned, but nobody has written it yet.",
            what_to_do_next="Check bd ready in the goodall repo for progress.",
        )
    )
    return exit_codes.ERROR


def main(argv: list[str] | None = None, terminal: Terminal | None = None, runner: Runner | None = None) -> int:
    """Run goodall with the given arguments and return its exit code.

    `argv` defaults to the real command line. `terminal` defaults to one
    writing to the real standard output and standard error. `runner`
    defaults to the real runner, which starts real programs. Passing all
    three lets a test run any command without a subprocess, decide what
    every program it runs does, and read back what it printed.
    """
    if terminal is None:
        terminal = Terminal()
    if runner is None:
        runner = RealRunner()
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
        # No command, or a group like `export` without its subcommand:
        # show the most specific help available and treat it as a usage error.
        group_parser = getattr(arguments, "group_parser", parser)
        group_parser.print_help(terminal.standard_error)
        return exit_codes.USAGE_ERROR

    if command_name == "status":
        return run_status(terminal, as_json=arguments.json)
    if command_name == "doctor":
        return run_doctor(terminal, runner, as_json=arguments.json)
    if command_name == "journal":
        entry_request = EntryRequest(text=arguments.text, as_json=arguments.json, text_file=arguments.file)
        return run_journal(terminal, runner, entry_request)
    if command_name == "intervene":
        entry_request = EntryRequest(text=arguments.text, as_json=arguments.json, text_file=arguments.file)
        intervention_details = InterventionDetails(
            category=arguments.category,
            decision=arguments.decision,
            interrupted=arguments.interrupted,
            claude_session=arguments.claude_session,
        )
        return run_intervene(terminal, runner, entry_request, intervention_details)

    return run_stub(command_name, terminal)
