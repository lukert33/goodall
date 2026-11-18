"""The diagnostic check of the telemetry receiver, under the heading "Telemetry".

`goodall doctor` runs `TELEMETRY_CHECKS` last (goodall.doctor). One check:
"The telemetry receiver is running for the active work block". Claude Code
drops its telemetry silently when nothing is listening, so a receiver that
has stopped is otherwise noticed only by the prompt gate, `goodall status`
or `goodall start`.

How it decides, reusing what goodall already trusts rather than a new test
(goodall.telemetry_receivers explains why these two are sound):

1. Is a work block active? If not, there is no receiver to check, and the
   check passes with a note saying so: no work block is an everyday state,
   not a fault.
2. Is the receiver goodall recorded for it still the same process, with the
   same id and start time? It asks the runner, which asks `ps`. A process
   with the recorded id but a different start time is another program, so
   the receiver is reported as not goodall's own.
3. Does that process hold the work block's receipt log locked? A running
   receiver holds that lock for as long as it runs, so a receiver that does
   not is writing somewhere else (its telemetry folder was moved, say).

A receiver that is down, not goodall's own, or not writing is a warning,
not a failure, so on its own it leaves doctor's exit code at 0 (owner
decision 2026-11-18: "Warn, like status"). The work block still records
and its transcripts are still copied; only telemetry is lost, which is
goodall working in a reduced way, the meaning of a warning
(goodall.checks). `goodall status` warns about the same state. Every
warning still carries its next step.

No network call (AGENTS.md invariant 9): the receiver is never sent a
request, and its port is never probed.

Only looks (AGENTS.md invariant 5), and three traps that follow from it:

- The hook and `goodall status` write a note when they find the receiver
  down, so the work block's record says its telemetry is incomplete. This
  check does not: doctor writes nothing, and the next of those to run will
  note it.
- Trying the receipt-log lock takes it for a moment when nobody holds it.
  A receiver being started again takes that lock without waiting, so doctor
  holding it then could stop the receiver from starting. So the lock is
  tried only once step 2 has found the recorded receiver running, when it
  already holds the lock and trying it takes nothing. (A receiver that dies
  in the moment between steps 2 and 3, and is started again in that same
  moment, could still meet the lock held; that window is far shorter than
  starting a receiver takes.)
- It starts and stops nothing: the next step is the person's to take.

It waits for the log directory check, which waits for valid local settings,
so missing settings stay one failure. A log directory that is unreachable
is only a warning there, and here it reads as no work block being active,
as it does for the prompt gate.
"""

from goodall import telemetry_capture, work_blocks
from goodall.checks import Check, CheckContext, CheckOutcome, passed, warned
from goodall.runner import BackgroundProgram, BackgroundState
from goodall.settings_checks import LOG_DIRECTORY_REACHABLE, valid_local_settings
from goodall.terminal import Failure
from goodall.work_block_commands import receiver_not_running_failure

GROUP = "Telemetry"

RECEIVER_RUNNING = "telemetry.receiver_running"

NO_WORK_BLOCK_NOTE = (
    "No work block is active, so there is no receiver to check. Running goodall start begins a work block and its "
    "receiver."
)


def check_receiver_running(context: CheckContext) -> CheckOutcome:
    """The receiver recorded for the active work block is running, is goodall's own, and holds its receipt log."""
    local_settings = valid_local_settings(context)
    if context.now is None:
        raise RuntimeError("Doctor must give the checks the time now, so they can tell which work block is active.")
    log_directory = local_settings.log_directory.path
    active = work_blocks.active_work_block(log_directory, context.now)
    if active is None:
        return passed(note=NO_WORK_BLOCK_NOTE)

    receiver_record = telemetry_capture.receiver_record_for(log_directory, active.id)
    if receiver_record is None:
        return warned([receiver_not_running_failure()])
    program = BackgroundProgram.from_json(receiver_record.program)
    if program is None:
        return warned([receiver_not_running_failure()])
    state = context.runner.check_background(program)
    if state == BackgroundState.REPLACED:
        return warned([receiver_not_ours_failure()])
    if state != BackgroundState.RUNNING:
        return warned([receiver_not_running_failure()])

    # Only now, with the recorded receiver running and so holding the lock;
    # see the module docstring.
    if not telemetry_capture.receiver_is_listening(log_directory, active.id):
        return warned([receiver_not_writing_failure()])
    return passed()


def receiver_not_ours_failure() -> Failure:
    """The warning for a recorded receiver whose process id now belongs to another program."""
    return Failure(
        kind="telemetry_receiver_not_ours",
        what_happened=(
            "Goodall's telemetry receiver for this work block is not running: the process goodall recorded for it "
            "has ended, and another program now has its process id."
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


def receiver_not_writing_failure() -> Failure:
    """The warning for a receiver that is running but is not writing this work block's telemetry folder."""
    return Failure(
        kind="telemetry_receiver_not_writing",
        what_happened=(
            "Goodall's telemetry receiver for this work block is running, but it is not writing to the work block's "
            "telemetry folder in the log directory."
        ),
        why=(
            "A running receiver keeps its receipt log in that folder locked, and this one does not, so the folder was "
            "probably moved or replaced while it ran. What it receives now does not reach the work block's record."
        ),
        what_to_do_next=(
            "Run goodall stop, then goodall start, to end this work block and begin a new one with a new receiver."
        ),
        commands_to_try=("goodall stop", "goodall start"),
    )


TELEMETRY_CHECKS = [
    Check(
        id=RECEIVER_RUNNING,
        group=GROUP,
        summary="The telemetry receiver is running for the active work block",
        prerequisites=[LOG_DIRECTORY_REACHABLE],
        run=check_receiver_running,
    ),
]
