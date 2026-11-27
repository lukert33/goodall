"""`goodall fixblock`: say which workstream a work block was really for.

Called by `cli.main` for `goodall fixblock <work block id> --workstream NAME`,
and by goodall.status, which lists the work blocks still waiting for their
workstream with the command to run for each.

Why a separate record: records are never rewritten (AGENTS.md invariant 2,
ADR 0003), so a work block whose workstream is `unknown` (goodall could not
read its marker) or simply wrong is corrected by a work block fix, a new
small record in the record repo's `work-block-fixes/` folder that names the
work block and gives the value (goodall.records, ADR 0014). Analysis reads
the two together through `records.effective_workstreams`.

What the command does, in order:

1. Find this account's eval: local settings, a record repo folder goodall
   can write in, the eval config.
2. Check what was typed: a work block id of the right shape and a
   `--workstream` that is one of the eval's workstreams and not `unknown`.
   Every mistake is reported at once, exit 2, with whole commands to copy.
   Nothing typed is ever repeated back unless it has passed its check,
   in case something private was pasted.
3. Find that work block's record in the record repo. When there is none,
   exit 1 and say what to check; when its record is still waiting in the
   log directory, say to run `goodall status` first, which writes it; when
   it is a deleted work block (`goodall delete`), say that it was deleted.
4. Build the fix, check it, and write it through goodall.record_repo, as
   journal entries are written: exit 0 once committed, and 1 when it was
   not committed.

Decisions a reader would otherwise reverse-engineer, all PROVISIONAL and
flagged for the project owner:

- The command always acts on one work block, and `--workstream` is the
  only value it sets today. Another value would be another option and
  another optional field of the fix, so neither shape changes.
- A work block whose workstream was known but wrong may be fixed too, and
  a work block may be fixed again: each fix is kept, the latest wins.
- Any contributor may fix any contributor's work block; the fix records
  who made it. `goodall status` lists only this account's own work blocks,
  because only their contributor knows which workstream they were for.
- There is no `--json`, matching `goodall start` and `goodall stop`; the
  fixes waiting to be made are in `goodall status --json`.
- The record repo is read file by file, without git, so listing what waits
  is read-only and quick: only work block records that mention `unknown`
  are parsed in full.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from goodall import exit_codes, records, work_blocks
from goodall.entries import check_record, find_eval_for_this_account
from goodall.eval_config import EvalForThisAccount
from goodall.record_repo import write_record
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import Clock, describe_moment
from goodall.work_block_deletion import read_deleted_work_block


def run_fixblock(
    terminal: Terminal, runner: Runner, clock: Clock, work_block_id: str | None, workstream: str | None
) -> int:
    """Record which workstream one work block was for, say what happened, and return the exit code."""
    eval_reading = find_eval_for_this_account()
    if isinstance(eval_reading, list):
        terminal.show_failures(eval_reading)
        return exit_codes.ERROR

    usage_failures = check_what_was_typed(eval_reading, work_block_id, workstream)
    if usage_failures:
        terminal.show_failures(usage_failures)
        return exit_codes.USAGE_ERROR
    # The checks above guarantee these; the assert tells the type checker.
    assert work_block_id is not None and workstream is not None

    record_repo = eval_reading.local_settings.record_repo.path
    work_block = read_work_block_record(record_repo, work_block_id)
    if work_block is None:
        log_directory = eval_reading.local_settings.log_directory.path
        deleted = read_deleted_work_block(record_repo, work_block_id)
        if deleted is not None:
            terminal.show_failure(work_block_deleted_failure(deleted))
        elif work_blocks.WorkBlockFiles(log_directory).unrecorded_file(work_block_id).exists():
            terminal.show_failure(work_block_still_waiting_failure(work_block_id))
        else:
            terminal.show_failure(work_block_not_found_failure(work_block_id, record_repo))
        return exit_codes.ERROR
    workstream_before = records.effective_workstreams([work_block], read_work_block_fixes(record_repo))[work_block.id]

    fix = records.build_work_block_fix(
        contributor=eval_reading.contributor.id,
        written_at=clock(),
        work_block=work_block.id,
        workstream=workstream,
    )
    problems = check_record(fix, eval_reading)
    if problems:
        terminal.show_failures(problems)
        return exit_codes.ERROR
    outcome = write_record(runner, record_repo, fix)
    if not outcome.committed:
        terminal.show_failures(outcome.failures)
        return exit_codes.ERROR

    terminal.show_paragraph(
        f"Recorded that the work block {describe_span(work_block)} counts towards {workstream}; it had been "
        f"recorded as {workstream_before}. Its record is unchanged, and this fix is kept beside it. "
        + "The fix is committed in the record repo on this Mac."
    )
    contributor = eval_reading.contributor
    terminal.show_facts(
        [
            ("Id", fix.id),
            ("File", str(outcome.path)),
            ("Work block", work_block.id),
            ("Workstream", workstream),
            ("By", f"{contributor.display_name} ({contributor.id})"),
        ]
    )
    return exit_codes.SUCCESS


def check_what_was_typed(
    eval_for_this_account: EvalForThisAccount, work_block_id: str | None, workstream: str | None
) -> list[Failure]:
    """Every mistake in the id and --workstream typed, as usage failures; empty when both are usable.

    The last failure carries whole commands to copy: one per workstream, for
    the id typed when it is well formed, or else for this account's first
    work block waiting for its workstream, or else for an example id.
    """
    eval_config = eval_for_this_account.eval_config
    workstream_list = ", ".join(eval_config.workstreams)
    failures = []
    example_id = records.EXAMPLE_RECORD_ID
    if work_block_id is None:
        failures.append(
            Failure(
                kind="fixblock_work_block_missing",
                what_happened="No work block id was given. Nothing was recorded.",
                why="goodall fixblock records a corrected value for one work block, named by its id.",
                what_to_do_next=(
                    "Put the work block's id first. goodall status lists each of your work blocks still waiting "
                    "for its workstream, with the command to run."
                ),
            )
        )
    elif not records.RECORD_ID_PATTERN.fullmatch(work_block_id):
        failures.append(
            Failure(
                kind="fixblock_work_block_invalid",
                what_happened="The id given is not a work block id. Nothing was recorded.",
                why=(
                    f"A work block id looks like {records.EXAMPLE_RECORD_ID}: the UTC time it started, the "
                    "contributor id and eight random characters. It is the name of the work block's record file, "
                    "without .json."
                ),
                what_to_do_next=(
                    "Copy the id from goodall status, or from the file's name in the work-blocks folder of the "
                    "record repo."
                ),
            )
        )
    else:
        example_id = work_block_id
    if work_block_id is None:
        waiting = work_blocks_waiting_for_workstream(eval_for_this_account)
        if waiting:
            example_id = waiting[0].id

    if workstream is None:
        failures.append(
            Failure(
                kind="fixblock_workstream_missing",
                what_happened="No --workstream was given. Nothing was recorded.",
                why="--workstream says which of the eval's workstreams the work block was for.",
                what_to_do_next=f"Add --workstream with one of: {workstream_list}.",
            )
        )
    elif workstream == records.UNKNOWN_WORKSTREAM:
        failures.append(
            Failure(
                kind="fixblock_workstream_unknown",
                what_happened=(
                    f"The --workstream given is {records.UNKNOWN_WORKSTREAM}, the placeholder goodall records when "
                    "it cannot tell which workstream a work block was for. Nothing was recorded."
                ),
                why="A fix says which workstream the work block was really for, so it names one of the eval's own.",
                what_to_do_next=f"Use one of: {workstream_list}.",
            )
        )
    elif workstream not in eval_config.workstreams:
        failures.append(
            Failure(
                kind="fixblock_workstream_not_in_eval",
                what_happened="The --workstream given is not one of this eval's workstreams. Nothing was recorded.",
                why="Each work block counts towards one of the eval's own workstreams, spelled exactly as listed.",
                what_to_do_next=(
                    f"Use one of: {workstream_list}, spelled exactly so. They are listed in "
                    f"{eval_config.config_file}; to add one, ask whoever runs the eval."
                ),
            )
        )

    if failures:
        last = failures[-1]
        failures[-1] = Failure(
            kind=last.kind,
            what_happened=last.what_happened,
            why=last.why,
            what_to_do_next=last.what_to_do_next,
            commands_to_try=fixblock_commands(example_id, eval_config.workstreams),
        )
    return failures


def fixblock_commands(work_block_id: str, workstreams: list[str]) -> tuple[str, ...]:
    """One whole `goodall fixblock` command per workstream, for a person to pick the right one and run it."""
    commands = []
    for workstream in workstreams:
        commands.append(f"goodall fixblock {work_block_id} --workstream {workstream}")
    return tuple(commands)


# ---------------------------------------------------------------------------
# Reading work block records and their fixes from the record repo
# ---------------------------------------------------------------------------


def read_work_block_record(record_repo: Path, work_block_id: str) -> records.WorkBlockRecord | None:
    """The work block record with this id in the record repo, or None when there is none goodall can read.

    `work_block_id` has already matched records.RECORD_ID_PATTERN, so it
    cannot name a file outside the work-blocks folder.
    """
    record_file = record_repo / records.WORK_BLOCKS_FOLDER / f"{work_block_id}.json"
    try:
        text = record_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    record = records.parse_work_block_record(text).record
    if record is None or record.id != work_block_id:
        return None
    return record


def read_work_block_fixes(record_repo: Path) -> list[records.WorkBlockFix]:
    """Every work block fix in the record repo that goodall can read; one it cannot is skipped."""
    fixes = []
    for fix_file in sorted_json_files(record_repo / records.WORK_BLOCK_FIXES_FOLDER):
        try:
            fix = records.parse_work_block_fix(fix_file.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            continue
        if fix is not None:
            fixes.append(fix)
    return fixes


def work_blocks_waiting_for_workstream(eval_for_this_account: EvalForThisAccount) -> list[records.WorkBlockRecord]:
    """This account's work blocks in the record repo that still count towards `unknown`, oldest first.

    Read-only, and quick however many work blocks there are: a record file
    is parsed in full only when its text mentions the reserved workstream.
    """
    record_repo = eval_for_this_account.local_settings.record_repo.path
    contributor_id = eval_for_this_account.contributor.id
    unknown_in_json = json.dumps(records.UNKNOWN_WORKSTREAM)
    candidates = []
    for record_file in sorted_json_files(record_repo / records.WORK_BLOCKS_FOLDER):
        try:
            text = record_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if unknown_in_json not in text:
            continue
        record = records.parse_work_block_record(text).record
        if record is not None and record.contributor == contributor_id:
            candidates.append(record)
    if not candidates:
        return []
    workstreams = records.effective_workstreams(candidates, read_work_block_fixes(record_repo))
    waiting = []
    for record in candidates:
        if workstreams[record.id] == records.UNKNOWN_WORKSTREAM:
            waiting.append(record)
    return waiting


def sorted_json_files(folder: Path) -> list[Path]:
    """The .json files in `folder`, in name order (so in time order); none when it cannot be read."""
    try:
        return sorted(folder.glob("*.json"))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# What a person reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaitingWorkBlock:
    """A work block waiting for its workstream, with the commands that would fix it, for status to show."""

    record: records.WorkBlockRecord
    commands: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        """The entry in `goodall status --json` under `work_blocks_waiting_for_workstream`. Times are UTC."""
        ended_at = None
        if self.record.ended_at is not None:
            ended_at = work_blocks.format_timestamp(self.record.ended_at)
        return {
            "id": self.record.id,
            "started_at": work_blocks.format_timestamp(self.record.started_at),
            "ended_at": ended_at,
            "commands": list(self.commands),
        }

    def as_warning(self) -> Failure:
        """The warning `goodall status` shows for it, ending in the commands to copy."""
        return Failure(
            kind="work_block_workstream_unknown",
            what_happened=(
                f"Your work block {describe_span(self.record)} is recorded with the workstream unknown, because "
                f"goodall could not read which workstream it was for. Its id is {self.record.id}."
            ),
            why=(
                "Each work block counts towards one of the eval's workstreams. Its record is kept as it is, and a "
                "fix recorded beside it says which workstream it was."
            ),
            what_to_do_next="Run the one command below that names the workstream it was for.",
            commands_to_try=self.commands,
        )


def waiting_work_blocks(eval_for_this_account: EvalForThisAccount) -> list[WaitingWorkBlock]:
    """This account's work blocks waiting for their workstream, each with its commands, for `goodall status`."""
    workstreams = eval_for_this_account.eval_config.workstreams
    waiting = []
    for record in work_blocks_waiting_for_workstream(eval_for_this_account):
        waiting.append(WaitingWorkBlock(record=record, commands=fixblock_commands(record.id, workstreams)))
    return waiting


def describe_span(work_block: records.WorkBlockRecord) -> str:
    """`from Mon 16 Nov 2026, 09:30 GMT to Mon 16 Nov 2026, 10:10 GMT`, in this Mac's local time."""
    if work_block.ended_at is None:
        return f"started {describe_moment(work_block.started_at)}"
    return f"from {describe_moment(work_block.started_at)} to {describe_moment(work_block.ended_at)}"


def work_block_not_found_failure(work_block_id: str, record_repo: Path) -> Failure:
    """The failure for a well-formed id with no work block record in the record repo.

    The id is repeated back: it has matched the record id pattern, which
    nothing private can.
    """
    return Failure(
        kind="fixblock_work_block_not_found",
        what_happened=(
            f"No work block record with the id {work_block_id} is in the record repo at {record_repo}. "
            "Nothing was recorded."
        ),
        why="A fix is recorded only for a work block goodall can find, so it never names one that does not exist.",
        what_to_do_next=(
            "Check the id against goodall status, which lists each of your work blocks still waiting for its "
            "workstream. If the work block was recorded on another Mac, bring this Mac's copy of the record repo "
            "up to date first."
        ),
    )


def work_block_deleted_failure(deleted: records.DeletedWorkBlock) -> Failure:
    """The failure for the id of a deleted work block (`goodall delete`): there is no workstream left to correct."""
    return Failure(
        kind="fixblock_work_block_deleted",
        what_happened=(
            f"What work block {deleted.id} captured was deleted, on {describe_moment(deleted.deleted_at)}. "
            "Nothing was recorded."
        ),
        why=(
            "Its record in the record repo now says only that a work block was deleted, and no longer counts "
            "towards any workstream, so there is nothing for a fix to correct."
        ),
        what_to_do_next=(
            "Nothing needs doing for this work block. To fix another, run goodall status, which lists each of "
            "your work blocks still waiting for its workstream, with the command to run."
        ),
        commands_to_try=("goodall status",),
    )


def work_block_still_waiting_failure(work_block_id: str) -> Failure:
    """The failure for a work block whose record has ended but not yet reached the record repo."""
    return Failure(
        kind="fixblock_work_block_waiting",
        what_happened=(
            f"The record of the work block {work_block_id} is still waiting on this Mac and has not reached the "
            "record repo yet, so it cannot be fixed yet. Nothing was recorded."
        ),
        why="A fix is kept beside the work block's record in the record repo.",
        what_to_do_next=(
            "First run goodall status, which writes waiting records into the record repo, then run this command again."
        ),
    )
