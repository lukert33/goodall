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
recovered from it is recorded with end state `abnormal`; every ended
work block's record still waiting in the log directory is written to the
record repo; and every Claude Code transcript that changed since it was
last copied is copied into the log directory (goodall.transcripts), which
is said in one sentence only when something was copied.

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

import dataclasses
import platform
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import goodall
from goodall import exit_codes, records, transcripts, work_blocks
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.folders import describe_folder_problem, log_directory_unreachable_failure, record_repo_missing_failure
from goodall.local_settings import read_local_settings
from goodall.record_repo import PushState, write_record
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
    settling = settle(runner, eval_reading, now)
    show_settling(terminal, settling)
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
        # A marker is there that is not an active work block: another
        # goodall is settling it at this very moment. Never claim a start
        # that did not happen.
        terminal.show_failure(marker_in_the_way_failure())
        return exit_codes.ERROR
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


def run_stop(terminal: Terminal, runner: Runner, clock: Clock) -> int:
    """End the active work block cleanly, write its record, say what happened, and return the exit code.

    Exit 0 once the work block has ended and its record is committed in the
    record repo; 1 when no work block was active, or when its record could
    not be written yet (it then waits in the log directory, and the person
    is told so).
    """
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        terminal.show_failures(eval_reading)
        return exit_codes.ERROR
    log_directory = eval_reading.local_settings.log_directory.path
    now = clock()
    settling = settle(runner, eval_reading, now)
    show_settling(terminal, settling)
    active = work_blocks.active_work_block(log_directory, now)
    if active is None and settling.closings:
        # The work block the person meant to stop had been forgotten, and
        # settling has just closed it at its last heartbeat and said so.
        # That is what they asked for, as long as its record is written.
        if settling.warnings:
            return exit_codes.ERROR
        return exit_codes.SUCCESS
    if active is None:
        terminal.show_failure(nothing_to_stop_failure())
        return exit_codes.ERROR

    ended_record = end_work_block(log_directory, active, now, "clean")
    waiting_file = work_blocks.WorkBlockFiles(log_directory).unrecorded_file(ended_record.id)
    write_of_this_block = write_one_unrecorded_record(runner, eval_reading, waiting_file)
    if not write_of_this_block.committed:
        terminal.show_failure(record_waiting_failure(ended_record, waiting_file, write_of_this_block.failures))
        return exit_codes.ERROR

    terminal.show_paragraph(
        f"Stopped your work block on {ended_record.workstream}. It ran from "
        f"{describe_moment(ended_record.started_at)} to {describe_moment(now)} ({describe_length(ended_record)}). "
        + where_the_record_went(write_of_this_block.push_state)
    )
    terminal.show_facts(
        [
            ("Id", ended_record.id),
            ("File", str(write_of_this_block.path)),
            ("Claude sessions", str(len(ended_record.claude_sessions))),
        ]
    )
    terminal.show_warnings(write_of_this_block.failures)
    return exit_codes.SUCCESS


def end_work_block(
    log_directory: Path, running: work_blocks.RunningWorkBlock, ended_at: datetime, end_state: str
) -> records.WorkBlockRecord:
    """End a running work block: save its finished record in the log directory, then remove its marker.

    The record gets every Claude session its heartbeats carried. The order
    matters: the record is saved before the marker goes, so the work block
    is never lost in between. Returns the finished record.
    """
    ended_at = ended_at.astimezone(UTC).replace(microsecond=0)
    # A clock set back (by hand, or by a time-zone mistake) must not make a
    # work block end before it began, which the records module refuses.
    ended_at = max(ended_at, running.record.started_at)
    ended_record = dataclasses.replace(
        running.record,
        ended_at=ended_at,
        end_state=end_state,
        claude_sessions=work_blocks.read_claude_sessions(log_directory, running.id),
    )
    work_blocks.save_unrecorded(log_directory, ended_record)
    work_blocks.remove_marker(log_directory, running.id)
    return ended_record


@dataclass(frozen=True)
class UnrecordedWrite:
    """What came of writing one waiting work block record into the record repo.

    `record` is None when the waiting file could not be read as a record;
    it is then left where it is, never deleted. `committed`, `path`,
    `push_state` and `failures` are as goodall.record_repo reports them.
    """

    waiting_file: Path
    record: records.WorkBlockRecord | None
    committed: bool
    path: Path | None = None
    push_state: PushState = PushState.NOT_ATTEMPTED
    failures: list[Failure] = field(default_factory=list)


def write_unrecorded_records(runner: Runner, eval_reading: EvalForThisAccount) -> list[UnrecordedWrite]:
    """Write every ended work block record waiting in the log directory into the record repo.

    Each one committed is removed from the log directory, since the record
    repo now holds it; any other stays, to be tried again by the next
    goodall start, stop or status.
    """
    log_directory = eval_reading.local_settings.log_directory.path
    writes = []
    for waiting_file in work_blocks.unrecorded_files(log_directory):
        # Trap: an end interrupted after its record was saved leaves the
        # marker behind. It must go before the waiting record does, or the
        # marker would look like a running work block again.
        work_blocks.remove_marker(log_directory, waiting_file.stem)
        writes.append(write_one_unrecorded_record(runner, eval_reading, waiting_file))
    return writes


def write_one_unrecorded_record(
    runner: Runner, eval_reading: EvalForThisAccount, waiting_file: Path
) -> UnrecordedWrite:
    """Write one waiting work block record into the record repo; remove it from the log directory once committed."""
    try:
        parse_result = records.parse_work_block_record(waiting_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        parse_result = records.ParseResult(record=None, problems=[])
    record = parse_result.record
    if record is None:
        return UnrecordedWrite(
            waiting_file, None, committed=False, failures=[unreadable_waiting_record_failure(waiting_file)]
        )
    eval_config = eval_reading.eval_config
    contributor_ids = []
    for contributor in eval_config.contributors:
        contributor_ids.append(contributor.id)
    problems = parse_result.problems + records.validate_record(
        record, contributor_ids=contributor_ids, workstreams=eval_config.workstreams
    )
    if problems:
        return UnrecordedWrite(
            waiting_file, record, committed=False, failures=[record_refused_failure(waiting_file, problems)]
        )
    outcome = write_record(runner, eval_reading.local_settings.record_repo.path, record)
    if outcome.committed:
        waiting_file.unlink(missing_ok=True)
    return UnrecordedWrite(
        waiting_file,
        record,
        committed=outcome.committed,
        path=outcome.path,
        push_state=outcome.push_state,
        failures=outcome.failures,
    )


@dataclass(frozen=True)
class Settling:
    """What settling did before a command's own work: work blocks it closed, records it wrote, transcripts it copied.

    `closings` are sentences for a person, one per work block goodall closed
    itself (forgotten or damaged); `writes` are the waiting records it tried
    to write into the record repo; `transcript_sweep` is what the sweep of
    Claude Code's transcripts into the log directory did.
    """

    closings: list[str] = field(default_factory=list)
    writes: list[UnrecordedWrite] = field(default_factory=list)
    transcript_sweep: transcripts.SweepResult = field(default_factory=transcripts.SweepResult)

    @property
    def warnings(self) -> list[Failure]:
        """A failure for each record still waiting in the log directory, to show as a warning."""
        warnings = []
        for write in self.writes:
            if write.committed:
                continue
            if write.record is None:
                warnings.extend(write.failures)
            else:
                warnings.append(record_waiting_failure(write.record, write.waiting_file, write.failures))
        if self.transcript_sweep.problems:
            warnings.append(transcripts_not_copied_failure(self.transcript_sweep.problems))
        return warnings

    @property
    def paragraphs(self) -> list[str]:
        """What a person reads about settling: each closing, then each record written."""
        paragraphs = list(self.closings)
        for write in self.writes:
            if write.committed and write.record is not None:
                paragraphs.append(
                    f"Recorded your work block on {write.record.workstream}, which ran from "
                    f"{describe_moment(write.record.started_at)} to {describe_ended_at(write.record)}. "
                    + where_the_record_went(write.push_state)
                )
        transcript_paragraph = describe_transcript_sweep(self.transcript_sweep)
        if transcript_paragraph is not None:
            paragraphs.append(transcript_paragraph)
        return paragraphs


def settle(runner: Runner, eval_reading: EvalForThisAccount, now: datetime) -> Settling:
    """Tidy up what earlier work blocks left behind, before start, stop or status does its own work.

    1. A work block that is overdue (no heartbeat for longer than
       work_blocks.IDLE_LIMIT) was forgotten: it is closed at its last
       heartbeat, with the end state `inferred`, so a forgotten goodall stop
       does not count the night as work.
    2. Every ended work block's record still waiting in the log directory
       is written into the record repo.
    3. Every Claude Code transcript that changed since it was last copied
       is copied into the log directory (goodall.transcripts.sweep). The
       hooks copy as Claude works, but a killed Claude Code fires none, so
       this is what guarantees nothing is left behind.
    """
    log_directory = eval_reading.local_settings.log_directory.path
    closings = []
    marker = work_blocks.read_marker(log_directory)
    if isinstance(marker, work_blocks.DamagedMarker):
        closing = close_damaged_work_block(runner, eval_reading, marker, now)
        if closing is not None:
            closings.append(closing)
    if isinstance(marker, work_blocks.RunningWorkBlock) and marker.is_overdue(now):
        end_work_block(log_directory, marker, marker.last_heartbeat_at, "inferred")
        closings.append(
            f"Your work block on {marker.record.workstream}, started {describe_moment(marker.record.started_at)}, "
            f"was left running: nobody ran goodall stop, and nothing happened in it for more than "
            f"{describe_duration(work_blocks.IDLE_LIMIT)}. Goodall closed it at its last heartbeat, "
            f"{describe_moment(marker.last_heartbeat_at)}, so the time after that is not counted as work."
        )
    return Settling(
        closings=closings,
        writes=write_unrecorded_records(runner, eval_reading),
        transcript_sweep=transcripts.sweep(transcripts.claude_projects_folder(), log_directory, now),
    )


def close_damaged_work_block(
    runner: Runner, eval_reading: EvalForThisAccount, damaged: work_blocks.DamagedMarker, now: datetime
) -> str | None:
    """Move a damaged marker aside, keep it, and record what can be recovered with the end state `abnormal`.

    Returns what to tell the person, or None when another goodall moved
    the marker first (and so tells them itself).
    """
    log_directory = eval_reading.local_settings.log_directory.path
    recovered = recover_work_block(runner, eval_reading, damaged)
    kept_marker = work_blocks.move_damaged_marker_aside(log_directory, now)
    if kept_marker is None:
        return None
    message = (
        "Goodall could not read the marker of your work block, so it could not tell whether that work block was "
        f"still running. The marker is kept, unchanged, at {kept_marker}."
    )
    if recovered is None:
        return message + (
            " Nothing could be recovered from it, so no record of that work block was written. Please tell the "
            "researcher who runs the eval, with the path above."
        )
    work_blocks.save_unrecorded(log_directory, recovered)
    return message + (
        f" What could be recovered is recorded as a work block on {recovered.workstream} from "
        f"{describe_moment(recovered.started_at)} to {describe_ended_at(recovered)}, its last heartbeat, with the "
        "end state abnormal."
    )


def recover_work_block(
    runner: Runner, eval_reading: EvalForThisAccount, damaged: work_blocks.DamagedMarker
) -> records.WorkBlockRecord | None:
    """Rebuild what can be trusted of a damaged marker's work block, ended at its last heartbeat; None if too little.

    Only observations are used, never guesses. The workstream must be in
    the marker, because nothing else says which one it was. The contributor
    falls back to this account's own, since an eval account belongs to one
    contributor; the start falls back to when the marker file was made;
    the machine facts, if lost, are observed again now, on what is the same
    Mac. PROVISIONAL: the owner may prefer to record nothing but the kept
    marker.
    """
    values = damaged.values
    workstream = values.get("workstream")
    if not isinstance(workstream, str) or workstream not in eval_reading.eval_config.workstreams:
        return None
    contributor = values.get("contributor")
    if not isinstance(contributor, str) or not records.IDENTIFIER_PATTERN.fullmatch(contributor):
        contributor = eval_reading.contributor.id
    started_at_text = values.get("started_at")
    started_at = damaged.modified_at
    if isinstance(started_at_text, str) and records.TIMESTAMP_PATTERN.fullmatch(started_at_text):
        started_at = datetime.fromisoformat(started_at_text)
    if started_at is None:
        return None
    work_block_id = values.get("id")
    if not isinstance(work_block_id, str) or not records.RECORD_ID_PATTERN.fullmatch(work_block_id):
        work_block_id = records.make_record_id(started_at, contributor)

    log_directory = eval_reading.local_settings.log_directory.path
    heartbeat_file = work_blocks.WorkBlockFiles(log_directory).heartbeat_file(work_block_id)
    recovered = records.WorkBlockRecord(
        schema_version=records.SCHEMA_VERSION,
        id=work_block_id,
        contributor=contributor,
        workstream=workstream,
        started_at=started_at,
        ended_at=work_blocks.read_last_heartbeat_at(heartbeat_file, started_at),
        end_state="abnormal",
        machine=recover_machine_facts(runner, values.get("machine")),
        claude_sessions=work_blocks.read_claude_sessions(log_directory, work_block_id),
    )
    if records.validate_record(recovered):
        return None
    return recovered


def recover_machine_facts(runner: Runner, machine_values: object) -> records.MachineFacts:
    """The machine facts a damaged marker still holds, or, when they are lost, the same facts observed now."""
    if isinstance(machine_values, dict):
        hostname = machine_values.get("hostname")
        macos_version = machine_values.get("macos_version")
        goodall_version = machine_values.get("goodall_version")
        claude_code_version = machine_values.get("claude_code_version")
        if (
            isinstance(hostname, str)
            and isinstance(goodall_version, str)
            and (macos_version is None or isinstance(macos_version, str))
            and (claude_code_version is None or isinstance(claude_code_version, str))
        ):
            return records.MachineFacts(hostname, macos_version, goodall_version, claude_code_version)
    return observe_machine_facts(runner)


def describe_transcript_sweep(sweep_result: transcripts.SweepResult) -> str | None:
    """What a person reads about the transcripts the sweep copied, or None when it copied none."""
    if sweep_result.claude_sessions_copied == 0:
        return None
    if sweep_result.claude_sessions_copied == 1:
        paragraph = "Brought the copy of 1 Claude session's transcript up to date in the log directory."
    else:
        paragraph = (
            f"Brought the copies of {sweep_result.claude_sessions_copied} Claude sessions' transcripts up to date "
            "in the log directory."
        )
    if sweep_result.earlier_copies_kept > 0:
        paragraph += (
            f" {sweep_result.earlier_copies_kept} earlier copies no longer matched the start of their transcripts, "
            f"so they were kept, unchanged, in {transcripts.SUPERSEDED_FOLDER_NAME} in the log directory."
        )
    return paragraph


def show_settling(terminal: Terminal, settling: Settling) -> None:
    """Show what settling did: what was closed and recorded as paragraphs, records still waiting as warnings."""
    for paragraph in settling.paragraphs:
        terminal.show_paragraph(paragraph)
    terminal.show_warnings(settling.warnings)


def describe_ended_at(record: records.WorkBlockRecord) -> str:
    """When an ended work block ended, for a person."""
    assert record.ended_at is not None
    return describe_moment(record.ended_at)


def find_write(writes: list[UnrecordedWrite], work_block_id: str) -> UnrecordedWrite | None:
    """The write of one work block's record among `writes`, or None when it was not attempted."""
    for write in writes:
        if write.record is not None and write.record.id == work_block_id:
            return write
    return None


def where_the_record_went(push_state: PushState) -> str:
    """One sentence saying the record is kept and whether others can see it yet, as journal entries say it."""
    if push_state == PushState.PUSHED:
        return "Its record is in the record repo and was sent to the shared record repo."
    if push_state == PushState.PENDING:
        return (
            "Its record is saved in the record repo on this Mac and will be sent to the shared record repo the "
            "next time goodall records something."
        )
    return "Its record is in the record repo, which has no shared copy to send it to, so it is kept on this Mac."


def describe_length(record: records.WorkBlockRecord) -> str:
    """How long an ended work block ran, as `2 h 5 min`."""
    assert record.ended_at is not None
    return describe_duration(record.ended_at - record.started_at)


def describe_duration(duration: timedelta) -> str:
    """A length of time for a person, in whole minutes: `2 h 5 min`, `40 min`."""
    total_minutes = int(duration.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0 and hours > 0:
        return f"{hours} h"
    if hours == 0:
        return f"{minutes} min"
    return f"{hours} h {minutes} min"


def marker_in_the_way_failure() -> Failure:
    """The failure for a start that found a work block marker it could neither use nor replace."""
    return Failure(
        kind="work_block_marker_in_the_way",
        what_happened="No work block was started: another goodall command was tidying up the last one just now.",
        why="Goodall keeps one work block at a time, and will not start a new one until the last is closed.",
        what_to_do_next="Run goodall start again. If this keeps happening, run goodall status to see what it says.",
    )


def nothing_to_stop_failure() -> Failure:
    """The failure for goodall stop with no work block active."""
    return Failure(
        kind="no_work_block_active",
        what_happened="No work block is active, so there was nothing to stop.",
        why="goodall stop ends the work block that goodall start began, and none is running now.",
        what_to_do_next="To begin one, run goodall start. To check what goodall is recording, run goodall status.",
    )


def record_waiting_failure(
    ended_record: records.WorkBlockRecord, waiting_file: Path, failures: list[Failure]
) -> Failure:
    """The failure for a work block that has ended but whose record could not be written to the record repo yet.

    It says plainly that the work block did end, that nothing is lost, where
    the record waits (`waiting_file`), and what went wrong, in the record
    repo module's own words.
    """
    reasons = []
    next_steps = []
    for failure in failures:
        reasons.append(failure.what_happened)
        next_steps.append(failure.what_to_do_next)
    what_to_do_next = " ".join(next_steps)
    if what_to_do_next == "":
        what_to_do_next = "Run goodall doctor to see what is wrong with the record repo."
    return Failure(
        kind="work_block_record_waiting",
        what_happened=(
            f"Your work block on {ended_record.workstream} has ended, so goodall is no longer recording, but its "
            "record could not be written to the record repo yet. " + " ".join(reasons)
        ),
        why=(
            f"Nothing is lost: the record is kept in {waiting_file} until it can be written. Goodall writes it "
            "the next time you run goodall start, goodall stop or goodall status."
        ),
        what_to_do_next=what_to_do_next,
    )


def transcripts_not_copied_failure(problems: list[str]) -> Failure:
    """The warning for Claude sessions whose transcripts the sweep could not copy into the log directory."""
    return Failure(
        kind="transcripts_not_copied",
        what_happened=(
            f"Goodall could not copy the transcripts of {len(problems)} Claude session(s) into the log directory. "
            + " ".join(problems)
        ),
        why=(
            "Transcripts are the record of what the agents did. Claude Code still holds them, so nothing is lost "
            "yet, and goodall tries again each time a hook fires and each time you run goodall start, stop or status."
        ),
        what_to_do_next=(
            "Check that the log directory has free space and that this account can write to it, then run goodall "
            "status again. If it keeps failing, tell the researcher who runs the eval."
        ),
    )


def unreadable_waiting_record_failure(waiting_file: Path) -> Failure:
    """The failure for a waiting work block record in the log directory that goodall cannot read back."""
    return Failure(
        kind="work_block_record_unreadable",
        what_happened=f"Goodall could not read the waiting work block record {waiting_file}, so it was not written.",
        why="Goodall keeps an ended work block's record there until the record repo takes it, and never deletes it.",
        what_to_do_next=(
            "Leave the file where it is and tell the researcher who runs the eval, with the path above, so they "
            "can look at it."
        ),
    )


def record_refused_failure(waiting_file: Path, problems: list[records.RecordProblem]) -> Failure:
    """The failure for a waiting work block record that does not fit this eval's config, say after a rename."""
    messages = []
    for problem in problems:
        messages.append(problem.message)
    return Failure(
        kind="work_block_record_refused",
        what_happened=f"The waiting work block record {waiting_file} was not written: " + " ".join(messages),
        why="Records are kept in the shape the eval config expects, so goodall checks each one before writing it.",
        what_to_do_next=(
            "Leave the file where it is and tell the researcher who runs the eval, with the path above. If a "
            "workstream was renamed, they can add the old name back to eval.toml so the record can be written."
        ),
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
