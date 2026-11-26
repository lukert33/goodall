"""`goodall delete <work block id>`: show, and with `--delete-captured-data` delete, what one work block captured.

Called by `cli.main`. A person may delete what one work block captured
(ADR 0021); goodall.work_block_deletion works out which files that is and
deletes them. This module reads what was typed, refuses what must not be
deleted, and says what would happen or what did.

What the command does, in order:

1. Find this account's eval, as `goodall start` does: local settings, the
   record repo, a log directory goodall can write in, the eval config.
2. Check the id typed. A missing or malformed one is a usage error (exit
   2), and what was typed is never repeated back unless it is an id.
3. Find the work block. A work block already deleted is said so, exit 0.
   One that is not there, is still recording, is another contributor's, or
   whose receiver is still running is refused, exit 1, with the next step.
4. Plan, reading only. Anything goodall could not read refuses the whole
   deletion, exit 1: it never guesses.
5. Without `--delete-captured-data`, say what would be deleted, in kinds
   and counts, what would be left and why, and the exact command that
   deletes it, and exit 2.
6. With it, check the record repo can take a commit, delete, replace the
   work block's record, and say what was done: exit 0, with warnings for
   what was left in place.

Decisions a reader would otherwise reverse-engineer:

- The terminal never asks a question (AGENTS.md invariant 9), so the
  confirmation is a flag typed on purpose, with a name that says what it
  does. It is spelled out rather than `--yes` or `--force`, so nobody types
  it from habit.
- The preview exits 2 (a usage error), not 0. The command line was missing
  the one argument that makes it act, and nothing was attempted, which is
  what 2 means here. A script or agent that looks only at the exit code
  then never mistakes a preview for a deletion; with 0, `goodall delete X
  && echo deleted` would say deleted when nothing was. The preview is on
  standard output, as is `--json`, which says `"deleted": false`.
- Nothing read from a raw record is ever shown: only kinds, counts, ids and
  where things are.
- A replacement written but not committed is a warning and exit 0: the
  data is deleted, which is what was asked, and the warning gives the git
  command to finish. One not written at all is exit 1, and running the
  command again finishes it, because the work block's record still says
  when it ran.
"""

from pathlib import Path

from goodall import exit_codes, records, telemetry_capture, work_block_deletion
from goodall.record_repo import check_record_repo_ready
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import Clock, describe_moment, find_eval_for_work_blocks
from goodall.work_block_deletion import DeletionPlan

CONFIRMING_FLAG = "--delete-captured-data"


def confirm_command(work_block_id: str) -> str:
    """The exact command that deletes what this work block captured."""
    return f"goodall delete {work_block_id} {CONFIRMING_FLAG}"


def run_delete(
    terminal: Terminal, runner: Runner, clock: Clock, work_block_id: str | None, confirmed: bool, as_json: bool
) -> int:
    """Show or delete what one work block captured, say what happened, and return the exit code."""
    report = Report(terminal, as_json)
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        return report.failures(eval_reading, exit_codes.ERROR)
    usage_failure = check_id_typed(work_block_id)
    if usage_failure is not None:
        return report.failures([usage_failure], exit_codes.USAGE_ERROR)
    assert work_block_id is not None
    report.work_block_id = work_block_id

    record_repo = eval_reading.local_settings.record_repo.path
    log_directory = eval_reading.local_settings.log_directory.path
    now = clock()
    if work_block_deletion.is_active(log_directory, work_block_id):
        return report.failures([still_recording_failure(work_block_id)], exit_codes.ERROR)
    found = work_block_deletion.find_work_block(record_repo, log_directory, work_block_id)
    if found.failure is not None:
        return report.failures([found.failure], exit_codes.ERROR)
    if found.already_deleted is not None:
        return report.already_deleted(found.already_deleted)
    if found.work_block is None:
        return report.failures([not_found_failure(work_block_id, record_repo)], exit_codes.ERROR)
    if found.work_block.record.contributor != eval_reading.contributor.id:
        return report.failures([not_yours_failure(work_block_id)], exit_codes.ERROR)
    if telemetry_capture.receiver_is_listening(log_directory, work_block_id):
        return report.failures([receiver_running_failure(work_block_id)], exit_codes.ERROR)

    plan = work_block_deletion.plan_deletion(record_repo, log_directory, found.work_block, now)
    if plan.problems:
        return report.failures([plan_problems_failure(plan.problems)], exit_codes.ERROR)
    if not confirmed:
        return report.preview(plan)

    readiness_failure = check_record_repo_ready(runner, record_repo)
    if readiness_failure is not None:
        return report.failures([readiness_failure], exit_codes.ERROR, plan)
    outcome = work_block_deletion.carry_out(runner, record_repo, log_directory, plan, now, eval_reading.contributor.id)
    if outcome.failure is not None:
        return report.failures([outcome.failure], exit_codes.ERROR, plan)
    assert outcome.record is not None
    if not outcome.record.written:
        return report.failures(outcome.record.failures, exit_codes.ERROR, plan)
    return report.deleted(plan, outcome.record.path, outcome.record.committed, outcome.record.failures)


def check_id_typed(work_block_id: str | None) -> Failure | None:
    """The usage failure for a missing or malformed work block id, or None when it is one."""
    if work_block_id is None:
        return Failure(
            kind="delete_work_block_missing",
            what_happened="No work block id was given. Nothing was deleted.",
            why="goodall delete deletes what one work block captured, named by its id.",
            what_to_do_next="Put the work block's id after goodall delete, as the record repo's file names show it.",
            commands_to_try=(confirm_command(records.EXAMPLE_RECORD_ID).removesuffix(" " + CONFIRMING_FLAG),),
        )
    if not records.RECORD_ID_PATTERN.fullmatch(work_block_id):
        return Failure(
            kind="delete_work_block_invalid",
            what_happened="The id given is not a work block id. Nothing was deleted.",
            why=(
                f"A work block id looks like {records.EXAMPLE_RECORD_ID}: the UTC time it started, the contributor "
                "id and eight random characters. It is the name of the work block's record file, without .json."
            ),
            what_to_do_next="Copy the id from the file's name in the work-blocks folder of the record repo.",
        )
    return None


# ---------------------------------------------------------------------------
# What a person reads, or an agent parses


class Report:
    """Says what `goodall delete` found or did, as text or as one JSON document with the same keys every time."""

    def __init__(self, terminal: Terminal, as_json: bool) -> None:
        self.terminal = terminal
        self.as_json = as_json
        self.work_block_id: str | None = None

    def document(
        self,
        plan: DeletionPlan | None,
        deleted: bool = False,
        already_deleted: bool = False,
        record_path: Path | None = None,
        committed: bool = False,
        warnings: list[Failure] | None = None,
        failures: list[Failure] | None = None,
    ) -> dict[str, object]:
        """The `--json` document. Its keys never change; a value that does not apply is empty or null."""
        kinds: list[dict[str, object]] = []
        agent_sessions: list[dict[str, str]] = []
        other_work_blocks: list[str] = []
        left_in_place: list[dict[str, object]] = []
        if plan is not None:
            for kind in plan.kinds:
                kinds.append({"kind": kind.kind, "files": kind.file_count})
            for agent_session in plan.agent_sessions:
                agent_sessions.append({"app_family": agent_session.app_family.name, "id": agent_session.id})
            other_work_blocks = plan.other_work_blocks_lost_transcripts
            for failure in plan.left_in_place:
                left_in_place.append(failure.as_json())
        record = None
        if record_path is not None:
            record = {"path": str(record_path), "committed": committed}
        confirm = None
        if self.work_block_id is not None and not already_deleted:
            confirm = confirm_command(self.work_block_id)
        return {
            "work_block": self.work_block_id,
            "deleted": deleted,
            "already_deleted": already_deleted,
            "confirm_command": confirm,
            "kinds": kinds,
            "agent_sessions": agent_sessions,
            "other_work_blocks_lost_transcripts": other_work_blocks,
            "record": record,
            "left_in_place": left_in_place,
            "warnings": failure_documents(warnings or []),
            "failures": failure_documents(failures or []),
        }

    def failures(self, failures: list[Failure], exit_code: int, plan: DeletionPlan | None = None) -> int:
        """Report what stopped the command and return `exit_code`."""
        if self.as_json:
            self.terminal.write_json(self.document(plan, failures=failures))
        else:
            self.terminal.show_failures(failures)
        return exit_code

    def already_deleted(self, deleted: records.DeletedWorkBlock) -> int:
        """Say the work block was deleted before; there is nothing left to do, so exit 0."""
        if self.as_json:
            self.terminal.write_json(self.document(None, already_deleted=True))
            return exit_codes.SUCCESS
        self.terminal.show_paragraph(
            f"What work block {deleted.id} captured was already deleted, on {describe_moment(deleted.deleted_at)}. "
            "There is nothing left to delete."
        )
        return exit_codes.SUCCESS

    def preview(self, plan: DeletionPlan) -> int:
        """Say what would be deleted and left, and the command that deletes it; exit 2 (see the module docstring)."""
        if self.as_json:
            self.terminal.write_json(self.document(plan))
            return exit_codes.USAGE_ERROR
        record = plan.work_block.record
        assert record.ended_at is not None
        self.terminal.show_paragraph(
            f"Nothing has been deleted. With {CONFIRMING_FLAG}, goodall deletes what work block {record.id} captured "
            f"(on {record.workstream}, from {describe_moment(record.started_at)} to "
            f"{describe_moment(record.ended_at)}), and replaces its record in the record repo with one that says "
            "only that a work block was deleted at your request, and when. This cannot be undone."
        )
        self.show_kinds(plan)
        self.terminal.show_warnings(plan.left_in_place)
        self.terminal.show_commands((confirm_command(record.id),))
        return exit_codes.USAGE_ERROR

    def deleted(self, plan: DeletionPlan, record_path: Path, committed: bool, commit_failures: list[Failure]) -> int:
        """Say what was deleted and what was left in place; exit 0."""
        warnings = list(plan.left_in_place)
        if not committed:
            warnings.extend(commit_failures)
        if self.as_json:
            document = self.document(
                plan, deleted=True, record_path=record_path, committed=committed, warnings=commit_failures
            )
            self.terminal.write_json(document)
            return exit_codes.SUCCESS
        committed_sentence = "That is committed in the record repo on this Mac, and git history is left as it was."
        if not committed:
            committed_sentence = "That is written in the record repo but not committed yet; see below."
        self.terminal.show_paragraph(
            f"Deleted what work block {plan.work_block.id} captured. Its record in the record repo now says only "
            f"that a work block was deleted at your request, and when. {committed_sentence}"
        )
        self.show_kinds(plan)
        self.terminal.show_warnings(warnings)
        return exit_codes.SUCCESS

    def show_kinds(self, plan: DeletionPlan) -> None:
        """The kinds deleted, or to delete, with counts, and the other work blocks that lose a transcript."""
        facts = []
        for kind in plan.kinds:
            facts.append((work_block_deletion.KIND_LABELS[kind.kind], describe_kind(plan, kind)))
        if not facts:
            facts.append(("Captured data", "none found in the log directory"))
        other_work_blocks = plan.other_work_blocks_lost_transcripts
        if other_work_blocks:
            facts.append(("Other work blocks", f"{len(other_work_blocks)}, which lose a transcript too, whole:"))
            for other_work_block in other_work_blocks:
                facts.append(("", other_work_block))
        self.terminal.show_facts(facts)


def describe_kind(plan: DeletionPlan, kind: work_block_deletion.KindToDelete) -> str:
    """How much of one kind there is, for a person: `1 (3 files)`, `4 files`, `1 file`."""
    files = f"{kind.file_count} files"
    if kind.file_count == 1:
        files = "1 file"
    if kind.kind == work_block_deletion.AGENT_SESSION_COPIES:
        return f"{len(plan.agent_sessions)} ({files})"
    return files


def failure_documents(failures: list[Failure]) -> list[dict[str, object]]:
    documents = []
    for failure in failures:
        documents.append(failure.as_json())
    return documents


# ---------------------------------------------------------------------------
# Failures


def still_recording_failure(work_block_id: str) -> Failure:
    return Failure(
        kind="delete_work_block_active",
        what_happened=f"Work block {work_block_id} is the active work block, so nothing was deleted.",
        why="Goodall is still recording it: what it captures is not all copied yet, and more may be on the way.",
        what_to_do_next="Stop it first with goodall stop, then run this command again.",
        commands_to_try=("goodall stop", confirm_command(work_block_id)),
    )


def not_found_failure(work_block_id: str, record_repo: Path) -> Failure:
    return Failure(
        kind="delete_work_block_not_found",
        what_happened=(
            f"No work block with the id {work_block_id} is in the record repo at {record_repo}, or waiting in the log "
            "directory. Nothing was deleted."
        ),
        why="Goodall deletes only what a work block it can find captured, so it never deletes by guesswork.",
        what_to_do_next="Check the id against the file names in the work-blocks folder of the record repo.",
    )


def not_yours_failure(work_block_id: str) -> Failure:
    return Failure(
        kind="delete_work_block_not_yours",
        what_happened=f"Work block {work_block_id} is another contributor's, so nothing was deleted.",
        why="What a work block captured is its contributor's to delete, from their own eval account.",
        what_to_do_next="Ask that contributor to run goodall delete in their own eval account.",
    )


def receiver_running_failure(work_block_id: str) -> Failure:
    return Failure(
        kind="delete_receiver_running",
        what_happened=(
            f"The telemetry receiver of work block {work_block_id} is still running, so nothing was deleted."
        ),
        why="It may still be writing that work block's telemetry, which would be left behind.",
        what_to_do_next="Run goodall status, which stops a receiver left running, then run this command again.",
        commands_to_try=("goodall status", confirm_command(work_block_id)),
    )


def plan_problems_failure(problems: list[str]) -> Failure:
    return Failure(
        kind="delete_cannot_tell",
        what_happened="Goodall could not read everything it needs to tell what this work block captured: "
        + " ".join(problems)
        + " Nothing was deleted.",
        why="A deletion that guessed could leave a transcript behind, so goodall deletes nothing until it can tell.",
        what_to_do_next="Fix what is named above, then run this command again.",
    )
