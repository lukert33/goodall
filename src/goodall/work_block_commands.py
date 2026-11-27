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
  work block cleanly, copy every agent session that has a record inside it
  into the log directory (goodall.agent_session_copies), copy
  ActivityWatch's data there too when the eval includes it
  (goodall.activitywatch_copies), write the work block's record into
  the record repo, and build the timeline again from all of it
  (goodall.timeline). If the record cannot be
  written yet it waits in the log directory, a warning says so, and the
  exit is still 0. With none active it says so and exits 1.

Settling, which `goodall status` does too: a work block nobody stopped is
closed at its last heartbeat (end state `inferred`) once it is overdue; a
marker goodall cannot read is moved aside and kept, and what can be
recovered from it is recorded with end state `abnormal`; every ended
work block's record still waiting in the log directory is written to the
record repo; the agent sessions of every ended work block that are still
to be copied are copied into the log directory, which covers a work block
goodall has just closed itself and any copy that failed earlier, and is
said in one sentence only when something was copied; ActivityWatch's data
is copied, for an eval that includes it, when a work block has ended since
its last copy (never otherwise, so nothing is asked of ActivityWatch while
a work block simply runs); the timeline is built again when settling
closed a work block; and any problem a
hook noted (goodall.hook_problems), such as a prompt whose input goodall
could not read, is reported once as a warning and filed away.

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
from goodall import (
    activitywatch_copies,
    agent_session_copies,
    claude_family,
    exit_codes,
    hook_problems,
    records,
    telemetry_capture,
    telemetry_receivers,
    timeline,
    work_blocks,
)
from goodall.eval_config import (
    EvalForThisAccount,
    activitywatch_settings_missing_failure,
    missing_activitywatch_settings,
    read_eval_for_this_account,
)
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


def run_start(
    terminal: Terminal, runner: Runner, clock: Clock, workstream: str | None, telemetry_port: int | None = None
) -> int:
    """Begin a work block and its telemetry receiver, say what happened, and return the exit code.

    `telemetry_port` is the port the person asked the receiver to listen
    on, or None for any free port (owner decision Q24). A receiver that
    does not start is a warning, not a failure: the work block has started
    and its agent sessions are copied when it ends (ADR 0002 calls the
    transcripts the record of last resort), and refusing to record at all
    would lose more.
    """
    if telemetry_port is not None and not MINIMUM_PORT <= telemetry_port <= MAXIMUM_PORT:
        terminal.show_failure(telemetry_port_invalid_failure())
        return exit_codes.USAGE_ERROR
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
        exit_code = report_already_active(terminal, active, workstream)
        make_sure_receiver_runs(terminal, runner, log_directory, active.id, telemetry_port, now)
        return exit_code

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
    # Started after the marker, so a receiver never runs without a work
    # block. Trap: if goodall were killed between the two, the work block
    # would run with no receiver; the hook does not notice that case (it
    # checks only a receiver goodall recorded), but status and stop do.
    receiver_start = telemetry_receivers.start_receiver(
        runner, log_directory, running_record.id, port_or_any(telemetry_port), now
    )
    terminal.show_paragraph(
        f"Started a work block on {workstream} at {describe_moment(running_record.started_at)}. "
        "Goodall is recording until you run goodall stop."
    )
    show_receiver_start(terminal, receiver_start, telemetry_port)
    return exit_codes.SUCCESS


# The ports a person may ask the receiver to listen on.
MINIMUM_PORT = 1
MAXIMUM_PORT = 65535


def port_or_any(telemetry_port: int | None) -> int:
    """The port asked for, or the receiver's word for any free port."""
    if telemetry_port is None:
        return telemetry_receivers.ANY_FREE_PORT
    return telemetry_port


def show_receiver_start(
    terminal: Terminal, receiver_start: telemetry_receivers.ReceiverStart, telemetry_port: int | None
) -> None:
    """Say prominently where the receiver listens, or warn that it did not start."""
    receiver_record = receiver_start.record
    if receiver_record is None:
        assert receiver_start.problem is not None
        terminal.show_warnings([receiver_not_started_failure(receiver_start.problem, telemetry_port)])
        return
    show_receiver_address(terminal, receiver_record)


def show_receiver_address(terminal: Terminal, receiver_record: telemetry_capture.ReceiverRecord) -> None:
    """The receiver's address as a fact of its own, and what Claude Code needs to send there."""
    terminal.show_facts([("Telemetry receiver", f"{receiver_record.endpoint_url} (port {receiver_record.port})")])
    terminal.show_paragraph(
        f"Goodall's telemetry receiver is listening on port {receiver_record.port}. Claude Code sends its "
        f"telemetry there when OTEL_EXPORTER_OTLP_ENDPOINT is {receiver_record.endpoint_url}."
    )


def make_sure_receiver_runs(
    terminal: Terminal,
    runner: Runner,
    log_directory: Path,
    work_block_id: str,
    telemetry_port: int | None,
    now: datetime,
) -> None:
    """For a work block already active: say where its receiver listens, or start it again if it has stopped.

    Restarted on the port it had, unless the person asked for another, so
    Claude Code, which was told that port, reaches it again. The time it was
    down is noted, so the work block's record says its telemetry is incomplete.
    """
    status = telemetry_receivers.receiver_status(runner, log_directory, work_block_id)
    if status.running and status.record is not None:
        message = f"Its telemetry receiver is listening on {status.record.endpoint_url}."
        if telemetry_port is not None and telemetry_port != status.record.port:
            message += (
                f" It keeps that port while it runs; to use port {telemetry_port}, run goodall stop, then goodall "
                f"start --telemetry-port {telemetry_port}."
            )
        terminal.show_paragraph(message)
        return
    telemetry_capture.note_receiver_down(log_directory, work_block_id, now, telemetry_capture.NOT_RUNNING)
    port = telemetry_port
    if port is None and status.record is not None:
        port = status.record.port
    receiver_start = telemetry_receivers.start_receiver(runner, log_directory, work_block_id, port_or_any(port), now)
    if receiver_start.record is not None:
        terminal.show_paragraph("Its telemetry receiver was not running, so goodall started it again.")
    show_receiver_start(terminal, receiver_start, telemetry_port)


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

    Exit 0 once the work block has ended, whether its record is already in
    the record repo or is safely waiting in the log directory (a warning then
    says so: nothing needs running again, and the next goodall start, stop or
    status writes it); 1 only when there was no work block to stop. The rule
    is in "Command-line conventions" in AGENTS.md. The same holds for its
    agent sessions: one that could not be copied yet is a warning, because
    its source application still holds it and goodall tries again. And for
    ActivityWatch's data, which ActivityWatch itself keeps: a copy that
    fails is a warning saying it is taken at the next goodall start, stop
    or status.

    Settling is told to leave ActivityWatch alone here, because stop takes
    that copy itself once the work block has ended: one copy, holding the
    whole work block, not one before its end and another after.

    Once the copies are made and the record is written, the timeline is
    built again (goodall.timeline); a problem there is a warning too.

    `Claude sessions` counts those the work block's heartbeats named, which
    only Claude Code's hooks leave; `Agent sessions copied` counts those,
    of every source application, with a record inside the work block.
    """
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        terminal.show_failures(eval_reading)
        return exit_codes.ERROR
    log_directory = eval_reading.local_settings.log_directory.path
    now = clock()
    settling = settle(runner, eval_reading, now, copy_activitywatch_now=False)
    show_settling(terminal, settling)
    active = work_blocks.active_work_block(log_directory, now)
    if active is None:
        # Nothing for stop to end, but a work block that settling has just
        # closed, or an earlier copy that failed, may still want its copy.
        show_activitywatch_copy(terminal, copy_activitywatch(runner, eval_reading, now))
    if active is None and settling.closings:
        terminal.show_warnings(timeline_warnings(build_timeline(eval_reading, now)))
        # The work block the person meant to stop had been forgotten, and
        # settling has just closed it at its last heartbeat and said so.
        # That is what they asked for. If its record could not be written
        # yet, settling has already shown that as a warning, and the record
        # waits safely, so this is still success.
        return exit_codes.SUCCESS
    if active is None:
        terminal.show_failure(nothing_to_stop_failure())
        return exit_codes.ERROR

    telemetry = telemetry_receivers.finish_telemetry(runner, log_directory, active.id, now)
    ended_record = end_work_block(
        log_directory, active, now, "clean", telemetry, eval_reading.eval_config.activitywatch
    )
    copies = agent_session_copies.copy_work_block(log_directory, ended_record.id, now)
    copy_warnings = []
    if copies.problems:
        copy_warnings.append(agent_sessions_not_copied_failure(copies))
    activitywatch = copy_activitywatch(runner, eval_reading, now)
    if activitywatch.problem is not None:
        copy_warnings.append(activitywatch_not_copied_failure(activitywatch.problem))
    waiting_file = work_blocks.WorkBlockFiles(log_directory).unrecorded_file(ended_record.id)
    write_of_this_block = write_one_unrecorded_record(runner, eval_reading, waiting_file)
    # Last, because the timeline is worked out from the copies just made
    # and points at the work block's record, wherever that now is.
    timeline_build = build_timeline(eval_reading, now)
    copy_warnings.extend(timeline_warnings(timeline_build))
    if not write_of_this_block.committed:
        # The work block is ended and its record is safe, so what the person
        # asked for is done: a warning, not a failure, and exit 0. An agent
        # that saw exit 1 would run goodall stop again and be told no work
        # block is active.
        terminal.show_warnings(
            [record_waiting_failure(ended_record, waiting_file, write_of_this_block.failures), *copy_warnings]
        )
        return exit_codes.SUCCESS

    terminal.show_paragraph(
        f"Stopped your work block on {ended_record.workstream}. It ran from "
        f"{describe_moment(ended_record.started_at)} to {describe_moment(now)} ({describe_length(ended_record)}). "
        + where_the_record_went(write_of_this_block.push_state)
    )
    facts = [
        ("Id", ended_record.id),
        ("File", str(write_of_this_block.path)),
        ("Claude sessions", str(len(ended_record.claude_sessions))),
        ("Agent sessions copied", str(copies.agent_sessions_copied)),
    ]
    if timeline_build.built:
        facts.append(("Timeline", "built; run goodall timeline to read this work block in time order"))
    telemetry_description = describe_telemetry(log_directory, ended_record)
    if telemetry_description is not None:
        facts.append(("Telemetry", telemetry_description))
    if activitywatch.copied:
        facts.append(("ActivityWatch", f"copied, {describe_buckets(activitywatch.bucket_count)}"))
    terminal.show_facts(facts)
    terminal.show_warnings(write_of_this_block.failures + copy_warnings)
    return exit_codes.SUCCESS


def describe_telemetry(log_directory: Path, record: records.WorkBlockRecord) -> str | None:
    """What an ended work block's record says about its telemetry, for a person; None when it says nothing."""
    request_count = telemetry_capture.stored_request_count(log_directory, record.id)
    requests = f"{request_count} requests"
    if request_count == 1:
        requests = "1 request"
    if record.telemetry == "captured":
        return f"captured, {requests}"
    if record.telemetry == "incomplete":
        return f"incomplete: the receiver was not running for part of the work block; {requests} kept"
    if record.telemetry == "missing":
        return "missing: no telemetry reached goodall's receiver"
    return None


def end_work_block(
    log_directory: Path,
    running: work_blocks.RunningWorkBlock,
    ended_at: datetime,
    end_state: str,
    telemetry: str | None,
    activitywatch_included: bool,
) -> records.WorkBlockRecord:
    """End a running work block: save its finished record in the log directory, then remove its marker.

    The record gets every Claude session its heartbeats carried, and
    `telemetry`, which the caller works out once the work block's receiver
    has stopped (goodall.telemetry_receivers.finish_telemetry). The order
    matters: first the note that its agent sessions are still to be copied
    (goodall.agent_session_copies) and, when the eval includes ActivityWatch
    (`activitywatch_included`), the note that its data is to be copied
    (goodall.activitywatch_copies), then the record, and the marker goes
    last, so neither the copies nor the work block are ever lost in between.
    Returns the finished record.
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
        telemetry=telemetry,
    )
    agent_session_copies.note_work_block_to_copy(log_directory, ended_record.id, ended_record.started_at, ended_at)
    if activitywatch_included:
        activitywatch_copies.note_copy_wanted(log_directory, ended_at)
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
    """What settling did before a command's own work: work blocks closed, records written, agent sessions copied.

    `closings` are sentences for a person, one per work block goodall closed
    itself (forgotten or damaged); `writes` are the waiting records it tried
    to write into the record repo; `copies` is what copying
    the agent sessions of ended work blocks into the log directory did;
    `activitywatch` is what copying ActivityWatch's data did, which is
    nothing unless a work block had ended since its last copy;
    `noted_hook_problems` is what hooks noted since a command last reported
    it, or None.
    """

    closings: list[str] = field(default_factory=list)
    writes: list[UnrecordedWrite] = field(default_factory=list)
    copies: agent_session_copies.CopyResult = field(default_factory=agent_session_copies.CopyResult)
    activitywatch: activitywatch_copies.CopyResult = field(default_factory=activitywatch_copies.CopyResult)
    noted_hook_problems: hook_problems.HookProblems | None = None
    # Warnings from building the timeline again, which settling does when it closed a work block.
    timeline_warnings: list[Failure] = field(default_factory=list)

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
        if self.copies.problems:
            warnings.append(agent_sessions_not_copied_failure(self.copies))
        if self.activitywatch.problem is not None:
            warnings.append(activitywatch_not_copied_failure(self.activitywatch.problem))
        warnings.extend(self.timeline_warnings)
        if self.noted_hook_problems is not None and self.noted_hook_problems.unreadable_inputs > 0:
            warnings.append(hook_input_unreadable_failure(self.noted_hook_problems))
        if self.noted_hook_problems is not None and self.noted_hook_problems.unknown_events > 0:
            warnings.append(hook_event_unknown_failure(self.noted_hook_problems))
        return warnings

    @property
    def paragraphs(self) -> list[str]:
        """What a person reads about settling: each closing, then each record written."""
        paragraphs = list(self.closings)
        for write in self.writes:
            if write.committed and write.record is not None:
                paragraphs.append(
                    f"Recorded your work block {describe_workstream(write.record)}, which ran from "
                    f"{describe_moment(write.record.started_at)} to {describe_ended_at(write.record)}. "
                    + where_the_record_went(write.push_state)
                )
        copies_paragraph = describe_agent_session_copies(self.copies)
        if copies_paragraph is not None:
            paragraphs.append(copies_paragraph)
        if self.activitywatch.copied:
            paragraphs.append(describe_activitywatch_copy(self.activitywatch))
        return paragraphs


def settle(
    runner: Runner, eval_reading: EvalForThisAccount, now: datetime, copy_activitywatch_now: bool = True
) -> Settling:
    """Tidy up what earlier work blocks left behind, before start, stop or status does its own work.

    1. A work block that is overdue (no heartbeat for longer than
       work_blocks.IDLE_LIMIT) was forgotten: it is closed at its last
       heartbeat, with the end state `inferred`, so a forgotten goodall stop
       does not count the night as work.
    2. Every ended work block's record still waiting in the log directory
       is written into the record repo.
    3. The agent sessions of every ended work block that are still to be
       copied are copied into the log directory
       (goodall.agent_session_copies). That is a work block closed in
       step 1, which nobody stopped, and any whose copy failed earlier.
    4. For an eval that includes ActivityWatch, its data is copied into the
       log directory when a work block has ended since its last copy
       (goodall.activitywatch_copies): again one closed in step 1, or one
       whose copy failed earlier. `goodall stop` passes
       `copy_activitywatch_now=False`, because it takes that copy itself
       after ending the work block.
    5. What hooks noted they could not do is taken, to be reported once.

    The telemetry receiver of each work block closed in step 1 is stopped
    before its record is written, and any other receiver whose work block
    is not the active one is stopped too (goodall.telemetry_receivers).
    """
    log_directory = eval_reading.local_settings.log_directory.path
    closings = []
    marker = work_blocks.read_marker(log_directory)
    if isinstance(marker, work_blocks.DamagedMarker):
        closing = close_damaged_work_block(runner, eval_reading, marker, now)
        if closing is not None:
            closings.append(closing)
    if isinstance(marker, work_blocks.RunningWorkBlock) and marker.is_overdue(now):
        telemetry = telemetry_receivers.finish_telemetry(runner, log_directory, marker.id, now)
        end_work_block(
            log_directory,
            marker,
            marker.last_heartbeat_at,
            "inferred",
            telemetry,
            eval_reading.eval_config.activitywatch,
        )
        closings.append(
            f"Your work block on {marker.record.workstream}, started {describe_moment(marker.record.started_at)}, "
            f"was left running: nobody ran goodall stop, and nothing happened in it for more than "
            f"{describe_duration(work_blocks.IDLE_LIMIT)}. Goodall closed it at its last heartbeat, "
            f"{describe_moment(marker.last_heartbeat_at)}, so the time after that is not counted as work."
        )
    active = work_blocks.active_work_block(log_directory, now)
    active_work_block_id = None
    if active is not None:
        active_work_block_id = active.id
    telemetry_receivers.stop_receivers_left_running(runner, log_directory, active_work_block_id, now)
    activitywatch = activitywatch_copies.CopyResult()
    if copy_activitywatch_now:
        activitywatch = copy_activitywatch(runner, eval_reading, now)
    settling = Settling(
        closings=closings,
        writes=write_unrecorded_records(runner, eval_reading),
        copies=agent_session_copies.copy_ended_work_blocks(log_directory, now),
        activitywatch=activitywatch,
        noted_hook_problems=hook_problems.take_hook_problems(log_directory, now),
    )
    if closings and copy_activitywatch_now:
        # A work block has just ended and its copies are made, so the
        # timeline is built again. goodall stop does this itself, after
        # the ActivityWatch copy it takes.
        built = timeline_warnings(build_timeline(eval_reading, now))
        settling = dataclasses.replace(settling, timeline_warnings=built)
    return settling


def build_timeline(eval_reading: EvalForThisAccount, now: datetime) -> timeline.BuildResult:
    """Build this eval's timeline again, from its log directory and record repo (goodall.timeline).

    Called once a work block has ended and its copies are made, and by
    `goodall timeline`. Never raises for anything on the disk.
    """
    return timeline.build_timeline(
        eval_reading.local_settings.log_directory.path,
        eval_reading.local_settings.record_repo.path,
        tuple(eval_reading.eval_config.intervention_categories),
        now,
    )


def timeline_warnings(build: timeline.BuildResult) -> list[Failure]:
    """The warning for a timeline build that had problems; none when it had none.

    Always a warning when a work block ends, never a failure: the timeline
    is derived, the raw records it is built from are untouched, and
    `goodall timeline --rebuild` builds it again.
    """
    if not build.problems:
        return []
    return [timeline.build_problems_failure(build)]


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
            " Not even when that work block started could be recovered, so no record of it was written. Please tell "
            "the researcher who runs the eval, with the path above."
        )
    telemetry = telemetry_receivers.finish_telemetry(runner, log_directory, recovered.id, now)
    recovered = dataclasses.replace(recovered, telemetry=telemetry)
    assert recovered.ended_at is not None
    agent_session_copies.note_work_block_to_copy(log_directory, recovered.id, recovered.started_at, recovered.ended_at)
    if eval_reading.eval_config.activitywatch:
        activitywatch_copies.note_copy_wanted(log_directory, recovered.ended_at)
    work_blocks.save_unrecorded(log_directory, recovered)
    if recovered.workstream == records.UNKNOWN_WORKSTREAM:
        return message + (
            f" The marker did not say which workstream that work block was for, so it is recorded with the "
            f"workstream unknown, from {describe_moment(recovered.started_at)} to {describe_ended_at(recovered)}, "
            "with the end state abnormal. Nothing is lost: once it is in the record repo, goodall status shows the "
            "goodall fixblock command that says which workstream it was."
        )
    return message + (
        f" What could be recovered is recorded as a work block on {recovered.workstream} from "
        f"{describe_moment(recovered.started_at)} to {describe_ended_at(recovered)}, its last heartbeat, with the "
        "end state abnormal."
    )


def recover_work_block(
    runner: Runner, eval_reading: EvalForThisAccount, damaged: work_blocks.DamagedMarker
) -> records.WorkBlockRecord | None:
    """Rebuild what can be trusted of a damaged marker's work block, ended at its last heartbeat; None if too little.

    Only observations are used, never guesses, because the owner would
    rather keep a work block with a gap in it than lose the activity:

    - The workstream comes from the marker. When the marker names none the
      eval has, it is recorded as `unknown` (records.UNKNOWN_WORKSTREAM),
      a word no eval may use for a real workstream, and the person says
      which it was later with `goodall fixblock`. Nothing more marks it as a
      placeholder: the reserved word and the end state `abnormal` say it.
    - The contributor falls back to this account's own, since an eval
      account belongs to one contributor.
    - The start falls back to when the marker file last changed. With not
      even that, None is returned and nothing is recorded, because any
      start goodall wrote would be invented and would look real.
    - The machine facts, if lost, are observed again now, on what is the
      same Mac.
    """
    values = damaged.values
    workstream = values.get("workstream")
    if not isinstance(workstream, str) or workstream not in eval_reading.eval_config.workstreams:
        workstream = records.UNKNOWN_WORKSTREAM
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


def describe_agent_session_copies(copies: agent_session_copies.CopyResult) -> str | None:
    """What a person reads about the agent sessions settling copied, or None when there is nothing to say."""
    sentences = []
    if copies.agent_sessions_copied == 1:
        sentences.append("Copied 1 agent session of a work block that had ended into the log directory.")
    elif copies.agent_sessions_copied > 1:
        sentences.append(
            f"Copied {copies.agent_sessions_copied} agent sessions of work blocks that had ended into the log "
            "directory."
        )
    if copies.earlier_copies_kept > 0:
        sentences.append(
            f"{copies.earlier_copies_kept} earlier copies no longer matched the start of their files, so they "
            f"were kept, unchanged, in {claude_family.SUPERSEDED_FOLDER_NAME} in the log directory."
        )
    if not sentences:
        return None
    return " ".join(sentences)


def copy_activitywatch(
    runner: Runner, eval_reading: EvalForThisAccount, now: datetime
) -> activitywatch_copies.CopyResult:
    """Copy ActivityWatch's data into the log directory, if the eval includes it and a copy is wanted.

    For an eval without ActivityWatch nothing is asked or written (ADR
    0016). Local settings that lack its port or identifier are a problem
    like any other: the copy stays wanted until they are recorded.
    """
    if not eval_reading.eval_config.activitywatch:
        return activitywatch_copies.CopyResult()
    local_settings = eval_reading.local_settings
    log_directory = local_settings.log_directory.path
    if not activitywatch_copies.copy_is_wanted(log_directory):
        return activitywatch_copies.CopyResult()
    port = local_settings.activitywatch_port
    device_id = local_settings.activitywatch_device_id
    if port is None or device_id is None:
        missing_settings = missing_activitywatch_settings(local_settings, eval_reading.eval_config)
        return activitywatch_copies.CopyResult(
            problem=activitywatch_settings_missing_failure(local_settings, eval_reading.eval_config, missing_settings)
        )
    return activitywatch_copies.copy_if_wanted(
        runner, log_directory, port, device_id, local_settings.settings_file, now
    )


def show_activitywatch_copy(terminal: Terminal, activitywatch: activitywatch_copies.CopyResult) -> None:
    """Say that ActivityWatch's data was copied, or warn that it was not; say nothing when no copy was wanted."""
    if activitywatch.copied:
        terminal.show_paragraph(describe_activitywatch_copy(activitywatch))
    if activitywatch.problem is not None:
        terminal.show_warnings([activitywatch_not_copied_failure(activitywatch.problem)])


def describe_activitywatch_copy(activitywatch: activitywatch_copies.CopyResult) -> str:
    """What a person reads about a copy of ActivityWatch's data that was taken. Nothing from the data is said."""
    return f"Copied ActivityWatch's data ({describe_buckets(activitywatch.bucket_count)}) into the log directory."


def describe_buckets(count: int) -> str:
    """`1 bucket`, `2 buckets`."""
    if count == 1:
        return "1 bucket"
    return f"{count} buckets"


def describe_times(count: int) -> str:
    """`1 time`, `3 times`."""
    if count == 1:
        return "1 time"
    return f"{count} times"


def show_settling(terminal: Terminal, settling: Settling) -> None:
    """Show what settling did: what was closed and recorded as paragraphs, records still waiting as warnings."""
    for paragraph in settling.paragraphs:
        terminal.show_paragraph(paragraph)
    terminal.show_warnings(settling.warnings)


def describe_workstream(record: records.WorkBlockRecord) -> str:
    """`on research`, or `with the workstream unknown` for a work block goodall could not attribute."""
    if record.workstream == records.UNKNOWN_WORKSTREAM:
        return f"with the workstream {records.UNKNOWN_WORKSTREAM}"
    return f"on {record.workstream}"


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


def telemetry_port_invalid_failure() -> Failure:
    """The failure for a --telemetry-port that no program could listen on."""
    return Failure(
        kind="telemetry_port_invalid",
        what_happened="The --telemetry-port given is not a port. No work block was started.",
        why=f"A port is a whole number from {MINIMUM_PORT} to {MAXIMUM_PORT}.",
        what_to_do_next=(
            "Give a port such as 4318, the usual one for OpenTelemetry over HTTP, or leave --telemetry-port out "
            "and goodall picks a free one and shows it."
        ),
        commands_to_try=("goodall start --telemetry-port 4318",),
    )


def receiver_not_started_failure(problem: str, telemetry_port: int | None) -> Failure:
    """The warning for a work block that started while its telemetry receiver did not."""
    what_to_do_next = (
        "Run goodall start again: with a work block already active it starts only the receiver. If it fails "
        "again, tell the researcher who runs the eval."
    )
    if telemetry_port is not None:
        what_to_do_next = (
            f"If another program is using port {telemetry_port}, stop it, or choose another port and tell Claude "
            "Code, then run goodall start --telemetry-port with that port: with a work block already active it "
            "starts only the receiver."
        )
    return Failure(
        kind="telemetry_receiver_not_started",
        what_happened=(
            f"Your work block is recording, but goodall's telemetry receiver did not start: {problem}. Claude Code's "
            "telemetry is not being kept."
        ),
        why=(
            "Claude Code drops its telemetry silently when nothing is listening. Your transcripts are still copied, "
            "so the record of what the agents did is kept."
        ),
        what_to_do_next=what_to_do_next,
    )


def receiver_not_running_failure() -> Failure:
    """The warning for an active work block whose telemetry receiver has stopped."""
    return Failure(
        kind="telemetry_receiver_not_running",
        what_happened=(
            "Goodall's telemetry receiver for this work block is not running, so Claude Code's telemetry is not "
            "being kept."
        ),
        why=(
            "Claude Code drops its telemetry silently when nothing is listening. Your work block is still recording "
            "and your transcripts are still copied; its record will say the telemetry is incomplete."
        ),
        what_to_do_next=(
            "Run goodall start: with a work block already active, it starts only the receiver again, on the same port."
        ),
        commands_to_try=("goodall start",),
    )


def nothing_to_stop_failure() -> Failure:
    """The failure for goodall stop with no work block active."""
    return Failure(
        kind="no_work_block_active",
        what_happened="No work block is active, so there was nothing to stop.",
        why="goodall stop ends the work block that goodall start began, and none is running now.",
        what_to_do_next="To begin one, run goodall start. To check what goodall is recording, run goodall status.",
    )


# "Run the command again", as the record repo module's next steps say it for a
# command that wrote nothing. It would mislead here: the record is already
# saved, so what to run is the command that writes it.
RUN_AGAIN_PATTERN = re.compile(r"run the command again", re.IGNORECASE)


def record_waiting_failure(
    ended_record: records.WorkBlockRecord, waiting_file: Path, failures: list[Failure]
) -> Failure:
    """The warning for a work block that has ended but whose record has not reached the record repo yet.

    Shown as a warning, never a failure: the work block is stopped and the
    record is safe, so the command succeeded. It says so in plain words,
    where the record waits (`waiting_file`), why it is waiting (the record
    repo module's own words), and that nothing needs to be run again,
    because goodall writes the record itself next time.
    """
    reasons = []
    next_steps = []
    for failure in failures:
        reasons.append(failure.what_happened)
        next_steps.append(RUN_AGAIN_PATTERN.sub("run goodall status to write it now", failure.what_to_do_next))
    what_to_do_next = " ".join(next_steps)
    if what_to_do_next == "":
        what_to_do_next = "Run goodall doctor to see what is wrong with the record repo."
    return Failure(
        kind="work_block_record_waiting",
        what_happened=(
            f"Your work block {describe_workstream(ended_record)} is stopped. Its record is saved on this Mac and has "
            "not reached the record repo yet. " + " ".join(reasons)
        ),
        why=(
            f"Nothing is lost and nothing needs to be run again: the record is kept in {waiting_file}, and goodall "
            "writes it by itself the next time you run goodall start, goodall stop or goodall status."
        ),
        what_to_do_next=what_to_do_next,
    )


def agent_sessions_not_copied_failure(copies: agent_session_copies.CopyResult) -> Failure:
    """The warning for an ended work block whose agent sessions could not all be copied into the log directory."""
    return Failure(
        kind="agent_sessions_not_copied",
        what_happened=(
            "Goodall could not copy everything of the agent sessions of a work block that has ended into the log "
            "directory. " + " ".join(copies.problems)
        ),
        why=(
            "The agent sessions are the record of what the agents did. Their source application still holds them, "
            "so nothing is lost yet, and goodall tries again each time you run goodall start, stop or status."
        ),
        what_to_do_next=(
            "Check that the log directory has free space and that this account can write to it, then run goodall "
            "status. If it keeps failing, tell the researcher who runs the eval."
        ),
    )


def activitywatch_not_copied_failure(problem: Failure) -> Failure:
    """The warning for ActivityWatch's data that could not be copied into the log directory yet.

    `problem` is why, in the words of whichever module found it; its next
    step is kept, since it is what puts things right. A warning, never a
    failure: the work block is ended and recorded, and ActivityWatch keeps
    its own history, so the copy taken later holds the same events.
    """
    return Failure(
        kind="activitywatch_not_copied",
        what_happened=("Goodall did not copy ActivityWatch's data into the log directory. " + problem.what_happened),
        why=(
            "ActivityWatch is the record of what you did outside the agents. It keeps its own history, so nothing "
            "is lost yet and your work block is not affected: goodall takes the copy the next time you run goodall "
            "start, stop or status, from where the last copy ended."
        ),
        what_to_do_next=problem.what_to_do_next + " Then run goodall status, which takes the copy.",
        commands_to_try=("goodall status",),
    )


def hook_input_unreadable_failure(problems: hook_problems.HookProblems) -> Failure:
    """The warning for prompts the gate let through although it could not read what Claude Code sent.

    Shown once: settling has already filed the note away (goodall.hook_problems).
    """
    since = ""
    if problems.unreadable_inputs_since is not None:
        since = f" since {describe_moment(problems.unreadable_inputs_since)}"
    return Failure(
        kind="hook_input_unreadable",
        what_happened=(
            f"Goodall could not read what Claude Code sent with a prompt {describe_times(problems.unreadable_inputs)}"
            f"{since}. Those prompts were sent and your work block kept recording, but their Claude sessions are "
            "not linked to the work block."
        ),
        why=(
            "Claude Code sends each prompt's details to goodall's prompt gate. When goodall cannot read them, it "
            "lets the prompt through rather than block your work, and keeps a note of when, in "
            f"{problems.filed_at}, so the researcher can link those Claude sessions later. Their transcripts are "
            "still copied."
        ),
        what_to_do_next=(
            "Tell the researcher who runs the eval: goodall may need updating for this version of Claude Code. "
            "Nothing needs to be run again."
        ),
    )


def hook_event_unknown_failure(problems: hook_problems.HookProblems) -> Failure:
    """The warning for goodall hooks that ran without knowing which Claude Code event they were for.

    Shown once: settling has already filed the note away (goodall.hook_problems).
    """
    since = ""
    if problems.unknown_events_since is not None:
        since = f" since {describe_moment(problems.unknown_events_since)}"
    return Failure(
        kind="hook_event_unknown",
        what_happened=(
            f"A goodall hook ran {describe_times(problems.unknown_events)}{since} without being told which Claude "
            "Code event it was for, or for an event goodall does not handle."
        ),
        why=(
            "Each goodall hook in Claude Code's settings names its event, such as goodall hook UserPromptSubmit. "
            "One that does not may leave prompts ungated or unrecorded. The moments are kept in "
            f"{problems.filed_at}."
        ),
        what_to_do_next=(
            "Check the hooks in this account's Claude Code settings, ~/.claude/settings.json, against "
            "docs/claude-code-hooks.md in goodall, and correct any command that does not end in one of the events "
            "listed there. If you did not set them up, tell the researcher who runs the eval."
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
