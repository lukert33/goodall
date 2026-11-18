"""Read an eval's records for `goodall logs`, without ever changing a file.

Called by goodall.logs, which shows what this module finds. Everything here
only opens files for reading, so `goodall logs` can be run on a log directory
that is read-only, on a drive mounted read-only, or while another goodall
command is writing (AGENTS.md invariants 2 and 5 apply in spirit: raw records
stay as they are, and looking changes nothing).

What is read, and from where:

- The record repo: `work-blocks/`, `work-block-fixes/`, `journal/` and
  `interventions/`, one file per record (docs/record-schemas.md).
- The log directory (ADR 0015): `transcripts/<project folder>/<session id>.jsonl`,
  `telemetry/<work block id>/` (ADR 0013), `activitywatch/<capture moment>/`
  (ADR 0016), and goodall's own bookkeeping under `work-blocks/`: the marker
  of a running work block and the records of ended ones still waiting to be
  written into the record repo.

Design decisions a reader would otherwise reverse-engineer:

- Nothing here is fatal. A file that cannot be read or parsed becomes a
  `FileProblem` or a `RawFile` flagged `unparseable`, and the rest is still
  shown, because the point of a viewer is to see what is wrong with a record.
- Nothing here settles anything. Unlike `goodall status`, a running work
  block that is overdue is reported as overdue, not closed, and a record
  waiting in the log directory is shown as waiting, not written. A damaged
  marker is reported where it is, not moved aside.
- Transcript and telemetry content is never returned, only counts and
  whether every line is JSON, so a caller cannot print a private prompt by
  accident. `goodall logs --raw` is the one deliberate way to see content,
  and it lives in goodall.logs.
- Telemetry is checked with `telemetry_receipts.check_receipts`, the same
  function `goodall doctor` will use, so the two agree on what a gap or a
  repeated request is.
- ActivityWatch copies are not matched to work blocks: ADR 0016 names a copy
  folder after the moment of capture, which covers whatever happened since
  the last copy, not one work block. They are counted for the whole eval.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from goodall import records, telemetry_receipts, work_blocks

# Where each work block comes from, so a caller can say what state it is in.
FROM_RECORD_REPO = "record_repo"
WAITING_IN_LOG_DIRECTORY = "waiting"
RUNNING = "running"

# What a raw file is flagged as.
READABLE = "readable"
UNPARSEABLE = "unparseable"

TRANSCRIPTS_FOLDER_NAME = "transcripts"
TELEMETRY_FOLDER_NAME = "telemetry"
ACTIVITYWATCH_FOLDER_NAME = "activitywatch"

# The files in a work block's telemetry folder, in the order they are shown.
TELEMETRY_FILE_NAMES = (*telemetry_receipts.TELEMETRY_FILE_NAMES, telemetry_receipts.RECEIPT_FILE_NAME)


@dataclass(frozen=True)
class FileProblem:
    """A file in the record repo or log directory that goodall could not read as the record it should be.

    `usable` is True when a record could still be built from it (the file
    has a fault, such as an unknown key, but the record is shown anyway).
    Each message is a full sentence from goodall.records and never repeats a
    value that might be secret.
    """

    path: Path
    record_type: str
    messages: tuple[str, ...]
    usable: bool

    def as_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "record_type": self.record_type,
            "problems": list(self.messages),
            "usable": self.usable,
        }


@dataclass
class RecordRepoContents:
    """Every record goodall could read from the record repo, and every file it could not."""

    work_blocks: list[records.WorkBlockRecord] = field(default_factory=list)
    fixes: list[records.WorkBlockFix] = field(default_factory=list)
    journal_entries: list[records.JournalEntry] = field(default_factory=list)
    interventions: list[records.Intervention] = field(default_factory=list)
    problems: list[FileProblem] = field(default_factory=list)


@dataclass(frozen=True)
class WorkBlockView:
    """One work block as `goodall logs` shows it: its record and where it came from.

    `source` is FROM_RECORD_REPO, WAITING_IN_LOG_DIRECTORY (ended, not yet
    written to the record repo) or RUNNING (its marker is in place).
    `overdue` is True for a running work block whose last heartbeat is older
    than the idle limit: `goodall status` would close it. `claude_sessions`
    are the Claude session ids it covers; for a running work block they come
    from its heartbeats, since its record does not list them yet.
    """

    record: records.WorkBlockRecord
    source: str
    workstream: str
    claude_sessions: tuple[str, ...]
    overdue: bool = False


@dataclass(frozen=True)
class RawFile:
    """One raw record file, and whether goodall could read it as the JSON Lines file it should be.

    `line_count` is None when the file could not be opened. A file with
    lines that are not JSON is `unparseable`, with `problem` saying how
    many and where the first is, never what they contain.
    """

    path: Path
    status: str
    problem: str | None
    line_count: int | None

    def as_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "status": self.status,
            "problem": self.problem,
            "line_count": self.line_count,
        }


@dataclass(frozen=True)
class TelemetrySummary:
    """What a work block's telemetry folder holds and whether its receipts agree with its lines.

    `receipt_problems` are goodall's own sentences about gaps, lines with no
    receipt and unreadable receipts; `repeated_requests` counts groups of
    receipts with the same body hash (a request the sender retried, which
    readers drop all but one of, ADR 0013). A folder with no receipt log
    (telemetry from a Collector someone ran themselves) has none of either:
    its absence is allowed, so it is reported as `has_receipt_log` False.
    `unreadable` is set when a file could not be opened at all.
    """

    directory: Path
    line_count: int
    receipt_count: int
    has_receipt_log: bool
    receipt_problems: tuple[str, ...]
    repeated_requests: int
    unreadable: str | None

    def as_json(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "line_count": self.line_count,
            "receipt_count": self.receipt_count,
            "has_receipt_log": self.has_receipt_log,
            "receipt_problems": list(self.receipt_problems),
            "repeated_requests": self.repeated_requests,
            "unreadable": self.unreadable,
        }


# ---------------------------------------------------------------------------
# The record repo
# ---------------------------------------------------------------------------


def read_record_repo(record_repo: Path) -> RecordRepoContents:
    """Read every record in the record repo; a file that cannot be read is listed in `problems`."""
    contents = RecordRepoContents()
    contents.work_blocks = read_folder(
        record_repo / records.WORK_BLOCKS_FOLDER, ".json", records.parse_work_block_record, contents.problems
    )
    contents.fixes = read_folder(
        record_repo / records.WORK_BLOCK_FIXES_FOLDER, ".json", records.parse_work_block_fix, contents.problems
    )
    contents.journal_entries = read_folder(
        record_repo / records.JOURNAL_FOLDER, ".md", records.parse_journal_entry, contents.problems
    )
    contents.interventions = read_folder(
        record_repo / records.INTERVENTIONS_FOLDER, ".md", records.parse_intervention, contents.problems
    )
    return contents


def read_folder[RecordType](
    folder: Path,
    suffix: str,
    parse: Callable[[str], records.ParseResult[RecordType]],
    problems: list[FileProblem],
) -> list[RecordType]:
    """Parse every `suffix` file in `folder`, in name order; add one FileProblem for each that is not clean."""
    found: list[RecordType] = []
    for record_file in sorted_files(folder, suffix):
        record_type = RECORD_TYPE_OF_FOLDER.get(folder.name, folder.name)
        try:
            text = record_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as read_error:
            problems.append(
                FileProblem(
                    path=record_file,
                    record_type=record_type,
                    messages=(f"The file cannot be read as text: {describe_read_error(read_error)}.",),
                    usable=False,
                )
            )
            continue
        result = parse(text)
        if result.problems:
            messages = []
            for problem in result.problems:
                messages.append(problem.message)
            problems.append(
                FileProblem(
                    path=record_file,
                    record_type=record_type,
                    messages=tuple(messages),
                    usable=result.record is not None,
                )
            )
        if result.record is not None:
            found.append(result.record)
    return found


RECORD_TYPE_OF_FOLDER = {
    records.WORK_BLOCKS_FOLDER: records.WORK_BLOCK_RECORD_TYPE,
    records.WORK_BLOCK_FIXES_FOLDER: records.WORK_BLOCK_FIX_RECORD_TYPE,
    records.JOURNAL_FOLDER: records.JOURNAL_ENTRY_RECORD_TYPE,
    records.INTERVENTIONS_FOLDER: records.INTERVENTION_RECORD_TYPE,
}


def sorted_files(folder: Path, suffix: str) -> list[Path]:
    """The files in `folder` ending in `suffix`, in name order (so in time order); none when it cannot be listed."""
    try:
        return sorted(folder.glob(f"*{suffix}"))
    except OSError:
        return []


def describe_read_error(error: OSError | UnicodeDecodeError) -> str:
    """A reason for a failed read, in words, without the path or any content."""
    if isinstance(error, UnicodeDecodeError):
        return "it is not UTF-8 text"
    return error.strerror or "it could not be opened"


# ---------------------------------------------------------------------------
# Work blocks: recorded, waiting and running
# ---------------------------------------------------------------------------


def find_work_blocks(
    log_directory: Path, contents: RecordRepoContents, now: datetime
) -> tuple[list[WorkBlockView], list[FileProblem]]:
    """Every work block goodall knows of, newest first, and the bookkeeping files it could not read.

    Recorded work blocks come from the record repo. Beside them: records
    waiting in the log directory for `goodall status` to write them (unless
    the record repo already has that id), and the running work block, if
    its marker is readable. Each work block's workstream is its effective
    one, with every work block fix applied (ADR 0014).
    """
    problems: list[FileProblem] = []
    records_found = list(contents.work_blocks)
    sources = {record.id: FROM_RECORD_REPO for record in records_found}

    for waiting_file in work_blocks.unrecorded_files(log_directory):
        waiting_record = read_waiting_record(waiting_file, problems)
        if waiting_record is not None and waiting_record.id not in sources:
            records_found.append(waiting_record)
            sources[waiting_record.id] = WAITING_IN_LOG_DIRECTORY

    running = work_blocks.read_marker(log_directory)
    overdue = False
    if isinstance(running, work_blocks.DamagedMarker):
        problems.append(
            FileProblem(
                path=work_blocks.WorkBlockFiles(log_directory).marker,
                record_type="marker",
                messages=(
                    "The marker of the running work block cannot be read as a work block record. "
                    "`goodall status` keeps it aside and records what it can.",
                ),
                usable=False,
            )
        )
    elif running is not None and running.id not in sources:
        records_found.append(running.record)
        sources[running.id] = RUNNING
        overdue = running.is_overdue(now)

    workstreams = records.effective_workstreams(records_found, contents.fixes)
    views = []
    for record in records_found:
        claude_sessions = record.claude_sessions
        if sources[record.id] == RUNNING:
            claude_sessions = work_blocks.read_claude_sessions(log_directory, record.id)
        views.append(
            WorkBlockView(
                record=record,
                source=sources[record.id],
                workstream=workstreams[record.id],
                claude_sessions=claude_sessions,
                overdue=overdue and sources[record.id] == RUNNING,
            )
        )
    views.sort(key=lambda view: (view.record.started_at, view.record.id), reverse=True)
    return views, problems


def read_waiting_record(waiting_file: Path, problems: list[FileProblem]) -> records.WorkBlockRecord | None:
    """Read an ended work block's record from the log directory; add a FileProblem when it is not clean."""
    try:
        text = waiting_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as read_error:
        problems.append(
            FileProblem(
                path=waiting_file,
                record_type=records.WORK_BLOCK_RECORD_TYPE,
                messages=(f"The file cannot be read as text: {describe_read_error(read_error)}.",),
                usable=False,
            )
        )
        return None
    result = records.parse_work_block_record(text)
    if result.problems:
        messages = []
        for problem in result.problems:
            messages.append(problem.message)
        problems.append(
            FileProblem(
                path=waiting_file,
                record_type=records.WORK_BLOCK_RECORD_TYPE,
                messages=tuple(messages),
                usable=result.record is not None,
            )
        )
    return result.record


# ---------------------------------------------------------------------------
# Raw records in the log directory
# ---------------------------------------------------------------------------


def find_transcripts(log_directory: Path, claude_session: str) -> list[Path]:
    """The copies of one Claude session's transcript in the log directory's mirror, in path order.

    `claude_session` has matched records.CLAUDE_SESSION_ID_PATTERN, so it
    cannot reach outside the transcripts folder. Usually one copy, in the
    project folder Claude Code used; more than one when the session id turns
    up under two project folders.
    """
    try:
        return sorted((log_directory / TRANSCRIPTS_FOLDER_NAME).glob(f"*/{claude_session}.jsonl"))
    except OSError:
        return []


def check_jsonl_file(path: Path) -> RawFile:
    """Flag a JSON Lines file readable or unparseable, and count its lines. Never returns a line's content.

    Every line that is not blank must be one JSON value. A last line cut
    short (a copy or a receiver stopped mid-write) is therefore reported,
    not skipped. The file is read a line at a time, so a transcript of
    hundreds of megabytes costs time but not memory.
    """
    line_count = 0
    bad_line_count = 0
    first_bad_line = 0
    try:
        with path.open("rb") as opened_file:
            for line_number, line in enumerate(opened_file, start=1):
                if line.strip() == b"":
                    continue
                line_count += 1
                try:
                    json.loads(line)
                except ValueError:
                    bad_line_count += 1
                    if first_bad_line == 0:
                        first_bad_line = line_number
    except OSError as open_error:
        return RawFile(
            path=path,
            status=UNPARSEABLE,
            problem=f"The file cannot be read: {describe_read_error(open_error)}.",
            line_count=None,
        )
    if bad_line_count == 0:
        return RawFile(path=path, status=READABLE, problem=None, line_count=line_count)
    noun = "line is" if bad_line_count == 1 else "lines are"
    return RawFile(
        path=path,
        status=UNPARSEABLE,
        problem=f"{bad_line_count} {noun} not valid JSON, the first is line {first_bad_line}.",
        line_count=line_count,
    )


def summarize_telemetry(log_directory: Path, work_block_id: str) -> TelemetrySummary | None:
    """What a work block's telemetry folder holds, or None when it has no folder.

    The one place that asks `check_receipts`, so a list and a detail view
    never disagree. A folder that cannot be read is a summary with
    `unreadable` set, not an error.
    """
    directory = log_directory / TELEMETRY_FOLDER_NAME / work_block_id
    if not directory.is_dir():
        return None
    has_receipt_log = (directory / telemetry_receipts.RECEIPT_FILE_NAME).is_file()
    try:
        check = telemetry_receipts.check_receipts(directory)
    except OSError as read_error:
        return TelemetrySummary(
            directory=directory,
            line_count=0,
            receipt_count=0,
            has_receipt_log=has_receipt_log,
            receipt_problems=(),
            repeated_requests=0,
            unreadable=describe_read_error(read_error),
        )
    receipt_problems: tuple[str, ...] = ()
    if has_receipt_log:
        receipt_problems = tuple(check.problems())
    return TelemetrySummary(
        directory=directory,
        line_count=check.line_count,
        receipt_count=check.receipt_count,
        has_receipt_log=has_receipt_log,
        receipt_problems=receipt_problems,
        repeated_requests=len(check.repeated_bodies),
        unreadable=None,
    )


def telemetry_files(log_directory: Path, work_block_id: str) -> list[Path]:
    """The telemetry and receipt files that exist for a work block, in the order TELEMETRY_FILE_NAMES gives."""
    directory = log_directory / TELEMETRY_FOLDER_NAME / work_block_id
    found = []
    for file_name in TELEMETRY_FILE_NAMES:
        if (directory / file_name).is_file():
            found.append(directory / file_name)
    return found


def count_activitywatch_copies(log_directory: Path) -> int:
    """How many capture folders `activitywatch/` holds; 0 when there is none or it cannot be listed."""
    folder = log_directory / ACTIVITYWATCH_FOLDER_NAME
    copies = 0
    try:
        for entry in folder.iterdir():
            if entry.is_dir():
                copies += 1
    except OSError:
        return 0
    return copies
