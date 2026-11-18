"""Starting, checking and stopping a work block's telemetry receiver, through the runner.

`goodall start` starts a receiver for the work block it begins, and
`goodall stop` stops it; settling (goodall.work_block_commands) stops the
receiver of a work block that ended without `goodall stop`, and any other
left running. `goodall status` asks whether it is running. Where its files
live, and the checks that need no program, are in goodall.telemetry_capture.

The receiver is `python -I -m goodall.telemetry_receiver DIRECTORY --port N`,
started as a background program (goodall.runner, ADR 0011) by the same
Python that runs goodall, so it is the same goodall. `-I` (isolated mode)
keeps the folder goodall was run from, and PYTHON* environment variables,
out of its imports, so a file planted where someone runs `goodall start`
cannot change what runs.

Making sure the receiver on the port is goodall's own, without any network
call of goodall's (ADR 0011): the port goodall shows is the one the
receiver itself printed after binding it, read from the very process goodall
started, so another program that already had the port makes the receiver
fail to start rather than go unnoticed. From then on, a receiver whose
process is still the recorded one (same id, same start time) still holds
that port, because a listening socket is never taken from its owner. Once
it has stopped, another program may take the port, which is why a receiver
found not running is always reported.
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from goodall import telemetry_capture
from goodall.runner import (
    BackgroundOutcome,
    BackgroundProgram,
    BackgroundStart,
    BackgroundState,
    BackgroundStop,
    Command,
    Runner,
)
from goodall.telemetry_capture import ReceiverRecord

# Any free port, chosen by the operating system (owner decision Q24).
ANY_FREE_PORT = 0


def receiver_command(telemetry_directory: Path, port: int) -> Command:
    """The command that runs a receiver writing into `telemetry_directory` and listening on `port`."""
    return Command(
        [sys.executable, "-I", "-m", "goodall.telemetry_receiver", str(telemetry_directory), "--port", str(port)]
    )


@dataclass(frozen=True)
class ReceiverStart:
    """What came of starting a receiver: its saved record, or a problem saying why it did not start."""

    record: ReceiverRecord | None
    problem: str | None = None


def start_receiver(runner: Runner, log_directory: Path, work_block_id: str, port: int, now: datetime) -> ReceiverStart:
    """Start the receiver for a work block, save its record, and say how it went.

    When it does not start, that is noted as the receiver being down, so the
    work block's record says its telemetry was not fully captured.
    """
    try:
        telemetry_directory = telemetry_capture.make_telemetry_directory(log_directory, work_block_id)
    except OSError as error:
        telemetry_capture.note_receiver_down(log_directory, work_block_id, now, telemetry_capture.DID_NOT_START)
        return ReceiverStart(
            None, f"goodall could not create its telemetry folder in the log directory: {error.strerror}"
        )
    start = runner.start_background(receiver_command(telemetry_directory, port))
    listening_port = None
    if start.program is not None:
        listening_port = port_in(start.ready_line)
        if listening_port is None:
            runner.stop_background(start.program)
    if start.program is None or listening_port is None:
        telemetry_capture.note_receiver_down(log_directory, work_block_id, now, telemetry_capture.DID_NOT_START)
        return ReceiverStart(None, describe_start_problem(start))
    receiver_record = ReceiverRecord(work_block_id, listening_port, now, start.program.as_json())
    telemetry_capture.save_receiver_record(log_directory, receiver_record)
    return ReceiverStart(receiver_record)


def port_in(ready_line: str) -> int | None:
    """The port in the receiver's ready line, {"port": N}, or None when the line is not that."""
    try:
        values = json.loads(ready_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(values, dict):
        return None
    port = values.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    return port


def describe_start_problem(start: BackgroundStart) -> str:
    """Why the receiver did not start, as the end of a sentence.

    Worded here rather than taken from `start.problem`, because that quotes
    the whole command, which names the log directory.
    """
    if start.outcome == BackgroundOutcome.EXITED:
        error_lines = start.standard_error.strip().splitlines()
        if error_lines:
            return error_lines[0]
        return f"it stopped straight away, with exit code {start.exit_code}"
    if start.outcome == BackgroundOutcome.NOT_FOUND:
        return "the Python that runs goodall could not be found"
    if start.outcome == BackgroundOutcome.COULD_NOT_START:
        return f"it could not be started: {start.start_error}"
    if start.outcome == BackgroundOutcome.TIMED_OUT:
        return f"it did not say it was listening within {start.command.timeout_seconds:g} s"
    return "it did not say which port it was listening on"


@dataclass(frozen=True)
class ReceiverStatus:
    """The receiver of one work block as goodall finds it: its record, if any, and whether it is running."""

    record: ReceiverRecord | None
    running: bool


def receiver_status(runner: Runner, log_directory: Path, work_block_id: str) -> ReceiverStatus:
    """Whether the work block's recorded receiver is still the running process goodall started."""
    receiver_record = telemetry_capture.receiver_record_for(log_directory, work_block_id)
    if receiver_record is None:
        return ReceiverStatus(None, False)
    program = BackgroundProgram.from_json(receiver_record.program)
    if program is None:
        return ReceiverStatus(receiver_record, False)
    return ReceiverStatus(receiver_record, runner.check_background(program) == BackgroundState.RUNNING)


def stop_receiver(runner: Runner, log_directory: Path, work_block_id: str, now: datetime, note_if_down: bool) -> None:
    """Stop the work block's receiver, if it has one, and forget its record.

    With `note_if_down`, a receiver that had already stopped before it was
    asked is noted as down, so the work block's record says its telemetry
    is incomplete. A process that is no longer the recorded receiver is
    never signalled (goodall.runner).
    """
    receiver_record = telemetry_capture.receiver_record_for(log_directory, work_block_id)
    if receiver_record is not None:
        program = BackgroundProgram.from_json(receiver_record.program)
        stopped = BackgroundStop.ALREADY_ENDED
        if program is not None:
            stopped = runner.stop_background(program)
        if note_if_down and stopped in (BackgroundStop.ALREADY_ENDED, BackgroundStop.NOT_OURS):
            telemetry_capture.note_receiver_down(log_directory, work_block_id, now, telemetry_capture.NOT_RUNNING)
    telemetry_capture.remove_receiver_record(log_directory, work_block_id)


def finish_telemetry(runner: Runner, log_directory: Path, work_block_id: str, now: datetime) -> str | None:
    """Stop an ending work block's receiver and say what its record says about telemetry.

    Stopped first, so every request it acknowledged is on disk and counted.
    """
    stop_receiver(runner, log_directory, work_block_id, now, note_if_down=True)
    return telemetry_capture.telemetry_outcome(log_directory, work_block_id)


def stop_receivers_left_running(
    runner: Runner, log_directory: Path, active_work_block_id: str | None, now: datetime
) -> None:
    """Stop every receiver whose work block is not the active one, as settling does.

    Such a receiver belongs to a work block that has ended some other way
    (an end interrupted by a crash, say). Its work block's record is already
    written, so it is only stopped, never noted. A record goodall cannot
    read is removed, since it could never be used to stop anything.
    """
    for record_file in telemetry_capture.receiver_record_files(log_directory):
        receiver_record = telemetry_capture.read_receiver_record(record_file)
        if receiver_record is None:
            record_file.unlink(missing_ok=True)
            continue
        if receiver_record.work_block == active_work_block_id:
            continue
        stop_receiver(runner, log_directory, receiver_record.work_block, now, note_if_down=False)
