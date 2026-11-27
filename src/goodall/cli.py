"""The `goodall` command line: parses arguments and runs one subcommand.

Called by the `goodall` console script (see pyproject.toml) and by
`python -m goodall`. Tests call `main(argv, terminal=..., runner=...)`
directly with a captured terminal and a fake runner, and check the returned
exit code, so `main` returns a number instead of calling `sys.exit` itself.

`goodall status` (goodall.status), `goodall doctor` (goodall.doctor),
`goodall journal` and `goodall intervene` (goodall.entries),
`goodall start` and `goodall stop` (goodall.work_block_commands),
`goodall fixblock` (goodall.fixblock), `goodall timeline`
(goodall.timeline_command), `goodall export scout`
(goodall.scout_export), and `goodall hook` (goodall.hook) are built. The `goodall` command itself starts
in goodall.entry_point, which sends `goodall hook` to goodall.hook without
loading this module, for speed; `main` routes it there too, so both agree.
Every other subcommand is still a stub: it reports that it is not
built yet and returns `ERROR` (1), not `USAGE_ERROR` (2), because the
command line was valid and the feature is missing.

Design decisions a reader might otherwise reverse-engineer:

- The parser is built with plain `argparse` calls, one block per command, so
  the whole command surface can be read top to bottom in `build_parser`. There
  is deliberately no registry or plugin mechanism.
- argparse normally calls `sys.exit` on a usage error (and after `--help`
  or `--version`). `main` catches that `SystemExit` and returns its code, so
  callers always get a number back.
- Everything goodall says goes through the `Terminal` passed to `main`
  (goodall.terminal). The one exception is argparse's own help, usage and
  `--version` messages, which argparse prints itself. `goodall --version`
  exists so that `goodall doctor` can check that the goodall a Claude Code
  hook names really starts, without it doing anything else.
- Every program goodall starts goes through the runner passed to `main`
  (goodall.runner, ADR 0011), which defaults to the real one. A test passes
  `goodall.fake_runner.FakeRunner` instead.
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from goodall import __version__, exit_codes, records
from goodall.doctor import run_doctor
from goodall.entries import DECISION_MEANINGS, EntryRequest, InterventionDetails, run_intervene, run_journal
from goodall.fixblock import run_fixblock
from goodall.hook import run_hook
from goodall.runner import RealRunner, Runner
from goodall.scout_export import run_export_scout
from goodall.status import run_status
from goodall.terminal import Failure, Terminal
from goodall.timeline_command import TimelineRequest, run_timeline
from goodall.work_block_commands import Clock, run_start, run_stop


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
    parser.add_argument("--version", action="version", version=f"goodall {__version__}")
    commands = parser.add_subparsers(title="commands", metavar="<command>")

    onboard = commands.add_parser("onboard", help="Set up this eval account and its record repo for a new eval.")
    onboard.set_defaults(command_name="onboard")

    doctor = commands.add_parser("doctor", help="Check this account and eval setup without changing anything.")
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print the results as one JSON document on standard output, for agents and scripts.",
    )
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="Ask nothing over the network: the checks that would ask GitHub are reported as unverified.",
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
    start.add_argument(
        "--telemetry-port",
        metavar="<port>",
        type=int,
        help=(
            "The port goodall's telemetry receiver listens on, which Claude Code's OTEL_EXPORTER_OTLP_ENDPOINT "
            "must name. Defaults to a free port, which goodall shows."
        ),
    )
    start.set_defaults(command_name="start")

    stop = commands.add_parser("stop", help="End the current work block, which closes the prompt gate.")
    stop.set_defaults(command_name="stop")

    fixblock = commands.add_parser(
        "fixblock",
        help="Say which workstream a work block was really for.",
        description=(
            "Record which workstream one work block was for, in a work block fix kept beside its record. The "
            "record itself is never changed. Use it for a work block recorded with the workstream unknown (goodall "
            "status lists each one with the command to run), or one recorded on the wrong workstream. Fixing a "
            "work block again is allowed: the latest fix counts, and every fix is kept."
        ),
        epilog=(
            "example:\n"
            f"  goodall fixblock {records.EXAMPLE_RECORD_ID} --workstream research\n"
            "\n"
            "goodall status shows each of your work blocks waiting for its workstream, with the command to copy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Optional to argparse so goodall reports a missing id itself, with
    # whole commands to copy, rather than argparse's bare usage line.
    fixblock.add_argument(
        "work_block_id",
        nargs="?",
        metavar="<work block id>",
        help="The id of the work block, such as goodall status shows it.",
    )
    fixblock.add_argument(
        "--workstream",
        metavar="<name>",
        help="The workstream the work block was for: one of the workstreams in eval.toml, never unknown.",
    )
    fixblock.set_defaults(command_name="fixblock")

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
        description=(
            "Add a journal entry: a note about what was decided or done, and by whom. "
            "Run it yourself in the Terminal app: it refuses to run from inside Claude Code, "
            "because an entry is your own account."
        ),
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
            "or had to act because the agent could not. Run it yourself in the Terminal app: it refuses to "
            "run from inside Claude Code, because an intervention is your own account."
        ),
        epilog=(
            "examples:\n"
            "  goodall intervene --category agent-limitation --decision modify \\\n"
            '      "It tried to delete a failing test; I told it to fix the date parsing."\n'
            "  pbpaste | goodall intervene --category infrastructure --decision escalate\n"
            "  goodall intervene --category policy-mandated --decision approve --occurred-at 14:05 \\\n"
            "      --file ~/Desktop/review.txt\n"
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
    intervene.add_argument(
        "--occurred-at",
        metavar="<time>",
        help=(
            "When it happened, if not just now, on this Mac's clock: a time such as 14:05 (the most recent "
            "14:05), or a date and time such as '2026-11-16 14:05'."
        ),
    )
    intervene.set_defaults(command_name="intervene")

    timeline = commands.add_parser(
        "timeline", help="Show one ended work block in time order: who did what, when, with no private text."
    )
    # Named as goodall fixblock names one: the id by itself, first.
    timeline.add_argument(
        "work_block_id",
        nargs="?",
        metavar="<work block id>",
        help="The id of the work block to show, as goodall stop shows it. Leave it out for the last one.",
    )
    timeline.add_argument(
        "--rebuild", action="store_true", help="Build the timeline again from the raw records before showing it."
    )
    timeline.add_argument("--all", dest="show_all", action="store_true", help="Show bookkeeping entries too.")
    timeline.add_argument(
        "--pointers", action="store_true", help="Show where each entry is in a raw record: the file and the line."
    )
    timeline.add_argument("--json", action="store_true", help="Print the entries as one JSON document.")
    timeline.set_defaults(command_name="timeline")

    hook = commands.add_parser("hook", help="Handle a Claude Code hook event (called by Claude Code, not by people).")
    # Optional to argparse so a missing event name is reported by
    # goodall.hook, which exits 1 rather than argparse's 2: Claude Code reads
    # 2 as "refuse the prompt" or "do not stop" (see goodall.hook).
    hook.add_argument("event_name", nargs="?", help="The Claude Code hook event, for example UserPromptSubmit.")
    hook.set_defaults(command_name="hook")

    sync = commands.add_parser(
        "sync", help="Copy what ended work blocks left in each application that is not yet in the log directory."
    )
    sync.set_defaults(command_name="sync")

    logs = commands.add_parser("logs", help="Show where this eval's raw logs are and what they contain.")
    logs.set_defaults(command_name="logs")

    export = commands.add_parser("export", help="Export the record for analysis.")
    export.set_defaults(group_parser=export)
    export_commands = export.add_subparsers(title="export commands", metavar="<export command>")

    export_scout = export_commands.add_parser(
        "scout",
        help="Build an Inspect Scout database from the raw record.",
        description=(
            "Build an Inspect Scout database of this eval: the Claude Code transcripts in the log directory, "
            "imported by Scout's own importer, with goodall's work blocks, journal entries, interventions, human "
            "actions and actor labels added. The database is derived data: each export rebuilds it. Needs "
            "goodall's optional scout extra (inspect-ai and inspect-scout)."
        ),
    )
    export_scout.add_argument(
        "--output",
        metavar="<folder>",
        type=Path,
        help="Where to put the database. Defaults to exports/scout in the log directory.",
    )
    export_scout.add_argument(
        "--json",
        action="store_true",
        help="Print the result as one JSON document on standard output, for agents and scripts.",
    )
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


# What a person does by hand while a stub is not built. Working rule in
# AGENTS.md (until goodall reaches version 0.9): a message may point at a
# command that is not built, provided its stub says how to do the same step by
# hand. So a command that a message points at needs a real entry here, and
# tests/test_cli.py fails when it has none.
BY_HAND_STEPS = {
    "onboard": (
        "ask whoever runs the eval for this eval account's local settings file, local-settings.toml, or write "
        "it yourself, and put it in the folder ~/.config/goodall/ in this macOS user account's home folder "
        "(goodall status names each setting it needs and shows an example line). Then add the Claude Code "
        "hooks as docs/claude-code-hooks.md in goodall's documentation shows, and run goodall status."
    ),
}
NOTHING_BY_HAND = "Nothing for now: this command will come in a later version of goodall."


def run_stub(command_name: str, terminal: Terminal) -> int:
    """Report that a planned command has no implementation yet, and what to do by hand.

    Returns ERROR rather than USAGE_ERROR because the user typed a valid
    command; it is goodall that cannot do it yet.

    The reader is a researcher, so the next step never mentions goodall's
    own development tools. `goodall onboard` gets a way to set up by hand,
    because "Run goodall onboard" is the next step when local settings are
    missing, and that must not lead to a dead end.
    """
    by_hand = BY_HAND_STEPS.get(command_name)
    if by_hand is None:
        by_hand = NOTHING_BY_HAND
    else:
        by_hand = by_hand[0].upper() + by_hand[1:]
    terminal.show_failure(
        Failure(
            kind="not_built",
            what_happened=f"goodall {command_name} is not built yet. Nothing was changed.",
            why="This command is planned for a later version of goodall.",
            what_to_do_next=f"By hand: {by_hand}",
        )
    )
    return exit_codes.ERROR


def main(
    argv: list[str] | None = None,
    terminal: Terminal | None = None,
    runner: Runner | None = None,
    clock: Clock | None = None,
) -> int:
    """Run goodall with the given arguments and return its exit code.

    `argv` defaults to the real command line. `terminal` defaults to one
    writing to the real standard output and standard error. `runner`
    defaults to the real runner, which starts real programs. `clock`
    defaults to the real time in UTC; the work-block commands ask it what
    time it is. Passing all four lets a test run any command without a
    subprocess, decide what every program it runs does, set the time, and
    read back what was printed.
    """
    if terminal is None:
        terminal = Terminal()
    if runner is None:
        runner = RealRunner()
    if clock is None:
        clock = real_clock
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
        return run_status(terminal, runner, clock, as_json=arguments.json)
    if command_name == "start":
        return run_start(
            terminal, runner, clock, workstream=arguments.workstream, telemetry_port=arguments.telemetry_port
        )
    if command_name == "stop":
        return run_stop(terminal, runner, clock)
    if command_name == "fixblock":
        return run_fixblock(terminal, runner, clock, arguments.work_block_id, arguments.workstream)
    if command_name == "doctor":
        return run_doctor(terminal, runner, clock, as_json=arguments.json, offline=arguments.offline)
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
            occurred_at=arguments.occurred_at,
        )
        return run_intervene(terminal, runner, entry_request, intervention_details)

    if command_name == "export scout":
        return run_export_scout(terminal, arguments.output, as_json=arguments.json)

    if command_name == "timeline":
        timeline_request = TimelineRequest(
            work_block_id=arguments.work_block_id,
            rebuild=arguments.rebuild,
            show_all=arguments.show_all,
            show_pointers=arguments.pointers,
            as_json=arguments.json,
        )
        return run_timeline(terminal, clock, timeline_request)

    if command_name == "hook":
        return run_hook(arguments.event_name, terminal, clock)

    return run_stub(command_name, terminal)


def real_clock() -> datetime:
    """The time now, in UTC: the clock goodall uses outside tests."""
    return datetime.now(UTC)
