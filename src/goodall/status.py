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
   into the record repo, and Claude Code transcripts the hooks missed are
   copied into the log directory. So `goodall status` can commit, push and
   copy; it is not a read-only diagnostic like `goodall doctor`. A
   transcript that could not be copied appears in `warnings`.
5. Goodall reports the active work block, or that there is none.

Each result is built once as records and then shown either as text (results
on standard output, failures and warnings on standard error) or as one JSON
document on standard output, so the two renderings cannot drift apart. The
JSON document always has the keys `work_block_active`, `eval`,
`contributor`, `work_block`, `ended_work_blocks`, `warnings` and
`failures`; `eval` and `contributor` are null whenever `failures` is not
empty, and `work_block` is null whenever no work block is active.

The folder checks themselves live in goodall.folders, because
`goodall doctor` asks the same questions and must word the answers the same
way.
"""

from goodall import exit_codes, work_blocks
from goodall.eval_config import EvalForThisAccount
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import (
    Clock,
    Settling,
    describe_moment,
    find_eval_for_work_blocks,
    settle,
    show_settling,
)


def run_status(terminal: Terminal, runner: Runner, clock: Clock, as_json: bool) -> int:
    """Report the state of this macOS user account's eval and return the exit code."""
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        return report_failures(terminal, eval_reading, as_json)
    now = clock()
    settling = settle(runner, eval_reading, now)
    active = work_blocks.active_work_block(eval_reading.local_settings.log_directory.path, now)
    if as_json:
        terminal.write_json(status_document(eval_reading, settling, active))
    else:
        show_settling(terminal, settling)
        show_status(terminal, eval_reading, active)
    if active is None:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def show_status(
    terminal: Terminal, eval_for_this_account: EvalForThisAccount, active: work_blocks.RunningWorkBlock | None
) -> None:
    """Show the eval, the contributor, and the active work block or that there is none, as text."""
    contributor = eval_for_this_account.contributor
    facts = [
        ("Eval", eval_for_this_account.eval_config.name),
        ("Contributor", f"{contributor.display_name} ({contributor.id})"),
    ]
    if active is None:
        main_workstream = eval_for_this_account.local_settings.main_workstream
        facts.append(("Workstream", f"{main_workstream}, your main workstream"))
        terminal.show_facts(facts)
        terminal.show_paragraph("No work block is active, so nothing is being recorded.")
        return
    facts.append(("Workstream", active.record.workstream))
    facts.append(("Started", describe_moment(active.record.started_at)))
    facts.append(("Last activity", describe_moment(active.last_heartbeat_at)))
    terminal.show_facts(facts)
    terminal.show_paragraph(
        f"A work block is active on {active.record.workstream} since {describe_moment(active.record.started_at)}, "
        "so goodall is recording. Run goodall stop when you finish."
    )


def status_document(
    eval_for_this_account: EvalForThisAccount, settling: Settling, active: work_blocks.RunningWorkBlock | None
) -> dict[str, object]:
    """The `goodall status --json` document when the eval was found. Times are UTC, as in records."""
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
    warning_documents = []
    for warning in settling.warnings:
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
        "warnings": warning_documents,
        "failures": [],
    }


def report_failures(terminal: Terminal, failures: list[Failure], as_json: bool) -> int:
    """Show `failures` as text or as JSON, and return the error exit code.

    The JSON document has the same keys whatever went wrong. No work block
    can be active while goodall is failing, so `work_block_active` is false
    and `work_block` null, and `eval` and `contributor` are null because
    goodall has not confirmed them.
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
                "warnings": [],
                "failures": failure_documents,
            }
        )
    else:
        terminal.show_failures(failures)
    return exit_codes.ERROR
