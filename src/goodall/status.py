"""`goodall status`: tells a contributor whether a work block is active.

Called by `cli.main` when someone runs `goodall status` or
`goodall status --json`. Agents use the exit code and the JSON document to
decide whether to proceed, so both are part of goodall's stable contract:
exit 0 while a work block is active, 1 when none is or goodall cannot tell.

In the order goodall checks:

1. Local settings are found, read and checked (goodall.local_settings).
   Everything wrong with the file is reported at once.
2. The record repo folder exists, and the log directory can be written to.
   An unreachable log directory is reported as "not recording" (ADR 0008):
   when goodall cannot write raw records, no work block can be active, so
   "cannot write records" and "not recording" are the same observable state.
3. The eval config is read from the record repo and checked, and local
   settings are checked against it: this account's contributor must be in
   the roster, and its main workstream one of the eval's workstreams
   (goodall.eval_config).
4. Goodall settles what earlier work blocks left behind, as `goodall start`
   and `goodall stop` do (goodall.work_block_commands): a forgotten work
   block is closed at its last heartbeat, a damaged marker is kept aside,
   ended work blocks' records waiting in the log directory are written
   into the record repo, and the agent sessions of ended work blocks that
   are still to be copied are copied into the log directory
   (goodall.agent_session_copies). So `goodall status` can commit and
   copy; it is not a read-only diagnostic like `goodall doctor`. An agent
   session that could not be copied appears in `warnings`.
5. Goodall lists each of this account's work blocks recorded with the
   workstream `unknown` that no work block fix has settled yet, with one
   whole `goodall fixblock` command per workstream to copy (goodall.fixblock).
   This part only reads the record repo's files.
6. Goodall reports the active work block, or that there is none and the
   commands that start one (text only; the JSON shape is unchanged). For
   an active work block it also reports its telemetry receiver: where it
   listens, or, when it is not running, a warning, and a note so the work
   block's record says its telemetry is incomplete
   (goodall.telemetry_receivers). `work_block.telemetry_receiver` in the
   JSON holds `running` and `port`. With no work block active, it says
   whether recording is paused (`goodall pause`, ADR 0021): the text names
   the paused work block and its workstream, and the JSON has `paused`
   (true or false) and `paused_work_block` (its `id`, `workstream` and
   `paused_at`, or null). Paused is still "not recording", so the exit
   code is 1 as for any account with no work block active.

Each result is built once as records and then shown either as text (results
on standard output, failures and warnings on standard error) or as one JSON
document on standard output, so the two renderings cannot drift apart. The
JSON document always has the keys `work_block_active`, `eval`,
`contributor`, `work_block`, `ended_work_blocks`,
`work_blocks_waiting_for_workstream`, `paused`, `paused_work_block`,
`warnings` and `failures`; `eval` and `contributor` are null whenever
`failures` is not empty, `work_block` is null whenever no work block is
active, and `paused_work_block` is null whenever `paused` is false.

The folder checks themselves live in goodall.folders, because
`goodall doctor` asks the same questions and must word the answers the same
way.
"""

from goodall import exit_codes, telemetry_capture, telemetry_receivers, work_blocks
from goodall.eval_config import EvalForThisAccount
from goodall.fixblock import WaitingWorkBlock, waiting_work_blocks
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import (
    Clock,
    Settling,
    describe_moment,
    find_eval_for_work_blocks,
    receiver_not_running_failure,
    settle,
    show_settling,
)


def run_status(terminal: Terminal, runner: Runner, clock: Clock, as_json: bool) -> int:
    """Report the state of this macOS user account's eval and return the exit code."""
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        return report_failures(terminal, eval_reading, as_json)
    now = clock()
    log_directory = eval_reading.local_settings.log_directory.path
    settling = settle(runner, eval_reading, now)
    active = work_blocks.active_work_block(log_directory, now)
    receiver = None
    receiver_warnings = []
    if active is not None:
        receiver = telemetry_receivers.receiver_status(runner, log_directory, active.id)
        if not receiver.running:
            # Noted, so the work block's record says its telemetry is incomplete.
            telemetry_capture.note_receiver_down(log_directory, active.id, now, telemetry_capture.NOT_RUNNING)
            receiver_warnings.append(receiver_not_running_failure())
    # Paused only while no work block is active: settling has already
    # forgotten a pause left behind by a work block that is running.
    pause = None
    if active is None:
        pause = work_blocks.read_pause(log_directory)
    waiting = waiting_work_blocks(eval_reading)
    if as_json:
        terminal.write_json(
            status_document(eval_reading, settling, active, waiting, receiver, receiver_warnings, pause)
        )
    else:
        show_settling(terminal, settling)
        waiting_warnings = list(receiver_warnings)
        for waiting_work_block in waiting:
            waiting_warnings.append(waiting_work_block.as_warning())
        terminal.show_warnings(waiting_warnings)
        show_status(terminal, eval_reading, active, receiver, pause)
    if active is None:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def show_status(
    terminal: Terminal,
    eval_for_this_account: EvalForThisAccount,
    active: work_blocks.RunningWorkBlock | None,
    receiver: telemetry_receivers.ReceiverStatus | None,
    pause: work_blocks.Pause | None,
) -> None:
    """Show the eval, the contributor, and the active work block, the pause, or that there is neither, as text."""
    contributor = eval_for_this_account.contributor
    facts = [
        ("Eval", eval_for_this_account.eval_config.name),
        ("Contributor", f"{contributor.display_name} ({contributor.id})"),
    ]
    if active is None and pause is not None:
        facts.append(("Workstream", pause.workstream))
        facts.append(("Paused", describe_moment(pause.paused_at)))
        facts.append(("Paused work block", pause.work_block_id))
        terminal.show_facts(facts)
        terminal.show_paragraph(
            f"Goodall is not recording: recording is paused. You paused your work block on {pause.workstream} "
            f"({pause.work_block_id}) at {describe_moment(pause.paused_at)}."
        )
        terminal.show_paragraph(
            f"To record again, run goodall start, which starts a new work block on {pause.workstream} that "
            "continues the paused one. To finish instead, run goodall stop."
        )
        return
    if active is None:
        main_workstream = eval_for_this_account.local_settings.main_workstream
        facts.append(("Workstream", f"{main_workstream}, your main workstream"))
        terminal.show_facts(facts)
        terminal.show_paragraph("No work block is active, so nothing is being recorded.")
        show_how_to_start(terminal, eval_for_this_account)
        return
    facts.append(("Workstream", active.record.workstream))
    facts.append(("Started", describe_moment(active.record.started_at)))
    facts.append(("Last activity", describe_moment(active.last_heartbeat_at)))
    facts.append(("Telemetry receiver", describe_receiver(receiver)))
    terminal.show_facts(facts)
    terminal.show_paragraph(
        f"A work block is active on {active.record.workstream} since {describe_moment(active.record.started_at)}, "
        "so goodall is recording. Run goodall stop when you finish."
    )


def describe_receiver(receiver: telemetry_receivers.ReceiverStatus | None) -> str:
    """The telemetry receiver's state for a person: where it listens, or that it is not running."""
    if receiver is not None and receiver.running and receiver.record is not None:
        return f"listening on {receiver.record.endpoint_url}"
    return "not running"


def receiver_document(receiver: telemetry_receivers.ReceiverStatus | None) -> dict[str, object]:
    """The telemetry receiver's state in `goodall status --json`: running or not, and its port (null if none)."""
    port = None
    if receiver is not None and receiver.record is not None:
        port = receiver.record.port
    return {"running": receiver is not None and receiver.running, "port": port}


def show_how_to_start(terminal: Terminal, eval_for_this_account: EvalForThisAccount) -> None:
    """Say which command begins a work block, once per reading of "no work block is active".

    Text only: the JSON document keeps its stable shape, and an agent that
    reads it already knows `goodall start` (the prompt gate names it too).
    The command and the workstream names are in their own paragraph, with the
    names last, so a long list wraps after the command, never inside it.
    """
    main_workstream = eval_for_this_account.local_settings.main_workstream
    terminal.show_paragraph(f"To start a work block on {main_workstream}, your main workstream, run goodall start.")
    other_workstreams = []
    for workstream in eval_for_this_account.eval_config.workstreams:
        if workstream != main_workstream:
            other_workstreams.append(workstream)
    if other_workstreams:
        names = ", ".join(other_workstreams)
        terminal.show_paragraph(f"For another, run goodall start --workstream NAME ({names}).")


def status_document(
    eval_for_this_account: EvalForThisAccount,
    settling: Settling,
    active: work_blocks.RunningWorkBlock | None,
    waiting: list[WaitingWorkBlock],
    receiver: telemetry_receivers.ReceiverStatus | None,
    receiver_warnings: list[Failure],
    pause: work_blocks.Pause | None,
) -> dict[str, object]:
    """The `goodall status --json` document when the eval was found. Times are UTC, as in records.

    Work blocks waiting for their workstream are listed under their own key,
    not in `warnings`, so an agent can read each one's id and commands. The
    active work block's telemetry receiver is under `work_block`, and a
    receiver not running is one of the `warnings`. `paused` is true while
    recording is paused, and `paused_work_block` then says which work block
    was paused; an agent that reads `work_block_active` alone still gets
    the right answer, false.
    """
    contributor = eval_for_this_account.contributor
    log_directory = eval_for_this_account.local_settings.log_directory.path
    work_block = None
    if active is not None:
        work_block = {
            "id": active.id,
            "workstream": active.record.workstream,
            "started_at": work_blocks.format_timestamp(active.record.started_at),
            "last_heartbeat_at": work_blocks.format_timestamp(active.last_heartbeat_at),
            "claude_sessions": list(work_blocks.read_claude_sessions(log_directory, active.id)),
            "telemetry_receiver": receiver_document(receiver),
        }
    ended_work_blocks = []
    for write in settling.writes:
        if write.record is None or write.record.ended_at is None:
            continue
        ended_work_blocks.append(
            {
                "id": write.record.id,
                "workstream": write.record.workstream,
                "started_at": work_blocks.format_timestamp(write.record.started_at),
                "ended_at": work_blocks.format_timestamp(write.record.ended_at),
                "end_state": write.record.end_state,
                "recorded": write.committed,
            }
        )
    paused_work_block = None
    if pause is not None:
        paused_work_block = {
            "id": pause.work_block_id,
            "workstream": pause.workstream,
            "paused_at": work_blocks.format_timestamp(pause.paused_at),
        }
    waiting_documents = []
    for waiting_work_block in waiting:
        waiting_documents.append(waiting_work_block.as_json())
    warning_documents = []
    for warning in settling.warnings + receiver_warnings:
        warning_documents.append(warning.as_json())
    return {
        "work_block_active": active is not None,
        "eval": {"name": eval_for_this_account.eval_config.name},
        "contributor": {
            "id": contributor.id,
            "display_name": contributor.display_name,
            "main_workstream": eval_for_this_account.local_settings.main_workstream,
        },
        "work_block": work_block,
        "ended_work_blocks": ended_work_blocks,
        "work_blocks_waiting_for_workstream": waiting_documents,
        "paused": pause is not None,
        "paused_work_block": paused_work_block,
        "warnings": warning_documents,
        "failures": [],
    }


def report_failures(terminal: Terminal, failures: list[Failure], as_json: bool) -> int:
    """Show `failures` as text or as JSON, and return the error exit code.

    The JSON document has the same keys whatever went wrong. No work block
    can be active while goodall is failing, so `work_block_active` is false
    and `work_block` null, and `eval` and `contributor` are null because
    goodall has not confirmed them. `paused` is false for the same reason:
    goodall could not look.
    """
    if as_json:
        failure_documents = []
        for failure in failures:
            failure_documents.append(failure.as_json())
        terminal.write_json(
            {
                "work_block_active": False,
                "eval": None,
                "contributor": None,
                "work_block": None,
                "ended_work_blocks": [],
                "work_blocks_waiting_for_workstream": [],
                "paused": False,
                "paused_work_block": None,
                "warnings": [],
                "failures": failure_documents,
            }
        )
    else:
        terminal.show_failures(failures)
    return exit_codes.ERROR
