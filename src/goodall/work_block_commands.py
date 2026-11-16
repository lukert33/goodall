"""`goodall start` and `goodall stop`: a contributor begins and ends a work block.

Called by `cli.main`. A work block is a logged stretch of one contributor's
work on one workstream; while one runs, its marker sits in the log
directory (goodall.work_blocks), and the prompt gate lets prompts through.

What each command does, in order:

- `goodall start [--workstream NAME]`: find this account's eval (local
  settings, the record repo folder, a log directory goodall can write to,
  the eval config); check the workstream, which defaults to this account's
  main workstream; settle what earlier work blocks left behind (below);
  then, unless a work block is already active, observe the machine and
  create the marker. With one already active it changes nothing, says which
  one and since when, and exits 0, so running it twice is harmless.
- `goodall stop`: find the eval and settle, as above; then end the active
  work block cleanly and write its record into the record repo. With none
  active it says so and exits 1.

Settling, which `goodall status` does too: a work block nobody stopped is
closed at its last heartbeat (end state `inferred`) once it is overdue; a
marker goodall cannot read is moved aside and kept, and what can be
recovered from it is recorded with end state `abnormal`; and every ended
work block's record still waiting in the log directory is written to the
record repo.

A work block's record is written to the record repo ONCE, when it ends,
because records are never rewritten. Until it is committed it waits in the
log directory, so the work is never lost when the record repo cannot take
it (no git identity, the folder missing, a refused commit): the command
says so plainly and the next start, stop or status writes it. That one-file
form is PROVISIONAL, the answer to an open question for the project owner
(one file at the end, against a file at the start and another at the end).

Times are stored in UTC and shown in this Mac's local time with its zone.
The clock is passed in, so tests can set it.
"""

import platform
import re
import socket
from collections.abc import Callable
from datetime import datetime

import goodall
from goodall import exit_codes, records, work_blocks
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.folders import describe_folder_problem, log_directory_unreachable_failure, record_repo_missing_failure
from goodall.local_settings import read_local_settings
from goodall.runner import Command, Runner
from goodall.terminal import Failure, Terminal

# What goodall asks for the time. The real one is `datetime.now(UTC)`;
# tests pass one they set by hand.
Clock = Callable[[], datetime]

# A version number at the start of what `claude --version` prints, such as
# 2.3.4 in "2.3.4 (Claude Code)".
VERSION_PATTERN = re.compile(r"\d+(\.\d+)+[0-9A-Za-z.+-]*")


def run_start(terminal: Terminal, runner: Runner, clock: Clock, workstream: str | None) -> int:
    """Begin a work block, say what happened, and return the exit code."""
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        terminal.show_failures(eval_reading)
        return exit_codes.ERROR
    if workstream is None:
        workstream = eval_reading.local_settings.main_workstream
    if workstream not in eval_reading.eval_config.workstreams:
        terminal.show_failure(workstream_unknown_failure(eval_reading))
        return exit_codes.USAGE_ERROR

    log_directory = eval_reading.local_settings.log_directory.path
    now = clock()
    active = work_blocks.active_work_block(log_directory, now)
    if active is not None:
        return report_already_active(terminal, active, workstream)

    running_record = records.build_work_block_record(
        contributor=eval_reading.contributor.id,
        workstream=workstream,
        started_at=now,
        machine=observe_machine_facts(runner),
    )
    created = work_blocks.create_marker(log_directory, running_record)
    if not created:
        # Another goodall start won the race between the check above and
        # now; report its work block as the active one.
        winner = work_blocks.active_work_block(log_directory, now)
        if winner is not None:
            return report_already_active(terminal, winner, workstream)
    work_blocks.record_heartbeat(log_directory, now)
    terminal.show_paragraph(
        f"Started a work block on {workstream} at {describe_moment(running_record.started_at)}. "
        "Goodall is recording until you run goodall stop."
    )
    return exit_codes.SUCCESS


def report_already_active(terminal: Terminal, active: work_blocks.RunningWorkBlock, workstream_asked_for: str) -> int:
    """Say that a work block is already active, which one and since when; exit 0, since nothing is wrong.

    When the person asked for a different workstream, say how to switch,
    because goodall will not end a work block they did not ask to end.
    """
    active_workstream = active.record.workstream
    message = (
        f"A work block is already active on {active_workstream} since {describe_moment(active.record.started_at)}, "
        "so nothing was changed. Goodall is recording until you run goodall stop."
    )
    if workstream_asked_for != active_workstream:
        message += (
            f" To work on {workstream_asked_for} instead, run goodall stop, then goodall start --workstream "
            f"{workstream_asked_for}."
        )
    terminal.show_paragraph(message)
    return exit_codes.SUCCESS


def workstream_unknown_failure(eval_for_this_account: EvalForThisAccount) -> Failure:
    """The failure for a --workstream that is not one of the eval's workstreams.

    The name given is not repeated back: it is matched exactly, and the list
    shows the right spelling. Checked here rather than by argparse, because
    the valid names are the eval's own.
    """
    eval_config = eval_for_this_account.eval_config
    commands_to_try = []
    for workstream in eval_config.workstreams:
        commands_to_try.append(f"goodall start --workstream {workstream}")
    return Failure(
        kind="workstream_unknown",
        what_happened="The --workstream given is not one of this eval's workstreams. No work block was started.",
        why="Each work block is counted towards one of the eval's own workstreams, spelled exactly as listed.",
        what_to_do_next=(
            f"Use one of: {', '.join(eval_config.workstreams)}, spelled exactly so, or leave --workstream out to "
            f"use your main workstream. They are listed in {eval_config.config_file}; to add one, ask whoever runs "
            "the eval."
        ),
        commands_to_try=tuple(commands_to_try),
    )


def find_eval_for_work_blocks() -> EvalForThisAccount | list[Failure]:
    """Read local settings, check both folders, and read the eval config, as `goodall status` does.

    The log directory must be writable, because that is where a work block
    lives: when it cannot be reached, goodall is not recording (ADR 0008).
    """
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        return settings_reading
    settings = settings_reading
    folder_failures: list[Failure] = []
    record_repo_problem = describe_folder_problem(settings.record_repo.path, needs_writing=False)
    if record_repo_problem is not None:
        folder_failures.append(record_repo_missing_failure(settings, record_repo_problem))
    log_directory_problem = describe_folder_problem(settings.log_directory.path, needs_writing=True)
    if log_directory_problem is not None:
        folder_failures.append(log_directory_unreachable_failure(settings, log_directory_problem))
    if folder_failures:
        return folder_failures
    return read_eval_for_this_account(settings)


def observe_machine_facts(runner: Runner) -> records.MachineFacts:
    """Observe the facts a work block records about the machine it runs on (ADR 0009).

    The macOS version is None on any other system. Claude Code's version is
    asked of `claude --version` through the runner, and is None when it is
    not installed, does not answer in time, or prints no version number.
    """
    macos_version: str | None = platform.mac_ver()[0]
    if macos_version == "":
        macos_version = None
    claude_code_version = None
    version_result = runner.run(Command(["claude", "--version"]))
    if version_result.succeeded:
        version_match = VERSION_PATTERN.search(version_result.standard_output)
        if version_match is not None:
            claude_code_version = version_match.group(0)
    return records.MachineFacts(
        hostname=socket.gethostname(),
        macos_version=macos_version,
        goodall_version=goodall.__version__,
        claude_code_version=claude_code_version,
    )


def describe_moment(moment: datetime) -> str:
    """A moment in this Mac's local time, with its zone, for a person: `Mon 16 Nov 2026, 09:30 GMT`."""
    return moment.astimezone().strftime("%a %d %b %Y, %H:%M %Z")
