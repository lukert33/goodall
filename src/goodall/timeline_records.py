"""Timeline entries from what a person wrote on purpose, and the work blocks the timeline is read by.

Reads the record repo: journal entries, interventions and work block
records (docs/record-schemas.md). It also reads the ended work blocks'
records still waiting in the log directory, so a work block whose record
has not reached the record repo yet is in the timeline all the same.

Who calls it: goodall.timeline, when it builds the timeline. The rules
below are numbered in docs/timeline.md; change both together and raise
TIMELINE_RULES_VERSION (goodall.timeline_entries).

1. A journal entry is one entry, of kind `journal_entry`, at the moment it
   was written. Its actor is `human`: `goodall journal` refuses to run
   inside an agent.
2. An intervention is one entry, of kind `intervention`, at the moment it
   happened (not when it was written down). It keeps its category, when
   the eval config lists it, and the person's decision as its outcome.
3. A work block is not an entry. Each ended one is kept beside the
   entries, with when it ran and how it ended, so that a reader can ask
   for the entries of one work block. A work block still running is not
   in the timeline.

What is kept is never the words: no body, no workstream's name. The
pointer leads to the record's file.
"""

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from goodall import records, work_blocks
from goodall.timeline_entries import (
    IN_LOG_DIRECTORY,
    IN_RECORD_REPO,
    KIND_INTERVENTION,
    KIND_JOURNAL_ENTRY,
    OTHER_INTERVENTION_CATEGORY,
    SOURCE_RECORD_REPO,
    RawRecordPointer,
    SourceEntries,
    TimelineEntry,
    TimelineWorkBlock,
)


def read_record_files[RecordType](
    record_repo: Path,
    folder_name: str,
    pattern: str,
    parse: Callable[[str], records.ParseResult[RecordType]],
    problems: list[str],
) -> list[tuple[RecordType, RawRecordPointer]]:
    """Every record in one folder of the record repo that can be read, each with the pointer to its file.

    A file that cannot be read, or is not a record, becomes a problem
    naming the file and is left out.
    """
    found: list[tuple[RecordType, RawRecordPointer]] = []
    folder = record_repo / folder_name
    if not folder.is_dir():
        return found
    for record_file in sorted(folder.glob(pattern)):
        relative_path = PurePosixPath(folder_name, record_file.name)
        try:
            record = parse(record_file.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            record = None
        if record is None:
            problems.append(f"The record {relative_path} in the record repo could not be read.")
            continue
        found.append((record, RawRecordPointer(IN_RECORD_REPO, relative_path)))
    return found


def entries_from_records(record_repo: Path, intervention_categories: tuple[str, ...]) -> SourceEntries:
    """An entry for every journal entry and every intervention in the record repo (rules 1 and 2)."""
    gathered = SourceEntries()
    journal_entries = read_record_files(
        record_repo, records.JOURNAL_FOLDER, "*.md", records.parse_journal_entry, gathered.problems
    )
    for journal_entry, raw_record in journal_entries:
        gathered.entries.append(
            TimelineEntry(
                started_at=journal_entry.written_at,
                duration_seconds=None,
                source=SOURCE_RECORD_REPO,
                kind=KIND_JOURNAL_ENTRY,
                actor="human",
                raw_record=raw_record,
                position=None,
            )
        )
    interventions = read_record_files(
        record_repo, records.INTERVENTIONS_FOLDER, "*.md", records.parse_intervention, gathered.problems
    )
    for intervention, raw_record in interventions:
        category = intervention.category
        if category not in intervention_categories:
            category = OTHER_INTERVENTION_CATEGORY
        gathered.entries.append(
            TimelineEntry(
                started_at=intervention.occurred_at,
                duration_seconds=None,
                source=SOURCE_RECORD_REPO,
                kind=KIND_INTERVENTION,
                actor="human",
                raw_record=raw_record,
                position=None,
                intervention_category=category,
                outcome=intervention.decision,
            )
        )
    return gathered


def ended_work_blocks(record_repo: Path, log_directory: Path, problems: list[str]) -> list[TimelineWorkBlock]:
    """Every ended work block, oldest first: those in the record repo, then those still waiting to be written there.

    A work block found in both places (its record was committed a moment
    ago and the waiting file is not removed yet) is taken once, from the
    record repo.
    """
    found: list[TimelineWorkBlock] = []
    record_ids = set()
    in_record_repo = read_record_files(
        record_repo, records.WORK_BLOCKS_FOLDER, "*.json", records.parse_work_block_record, problems
    )
    waiting: list[tuple[records.WorkBlockRecord, RawRecordPointer]] = []
    for waiting_file in work_blocks.unrecorded_files(log_directory):
        try:
            record = records.parse_work_block_record(waiting_file.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            record = None
        if record is None:
            # goodall status reports a waiting record it cannot read; the timeline only leaves it out.
            continue
        relative_path = PurePosixPath(waiting_file.relative_to(log_directory).as_posix())
        waiting.append((record, RawRecordPointer(IN_LOG_DIRECTORY, relative_path)))
    for record, raw_record in [*in_record_repo, *waiting]:
        if record.ended_at is None or record.id in record_ids:
            continue
        record_ids.add(record.id)
        end_state = record.end_state if record.end_state in records.END_STATES else None
        found.append(TimelineWorkBlock(record.started_at, record.ended_at, end_state, raw_record))
    return sorted(found, key=lambda work_block: (work_block.started_at, str(work_block.raw_record.path)))
