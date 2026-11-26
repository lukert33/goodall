"""The timeline: a derived database, one for each eval, of what happened in time order.

The timeline (GLOSSARY.md, ADR 0019) is one SQLite file in the log
directory, `timeline/timeline.sqlite3`, readable only by this macOS user
account. It lists every entry from every source application's copied
transcripts, from the copies of ActivityWatch's data, and from journal
entries and interventions. An entry holds only times, numbers and values
from fixed lists, plus a pointer to its place in a raw record
(goodall.timeline_entries is that rule).

It is derived (ADR 0003): nothing is stored only here. Goodall builds it
whole each time, from the raw records, into a new file that then replaces
the old one. So it can be deleted at any moment, a build that fails or is
killed leaves the last good file in place, and a change to its shape needs
no migration: a file whose versions are not this goodall's is simply built
again (is_current).

Who calls it: goodall.work_block_commands builds it when a work block has
ended, and goodall.timeline_command builds it when asked and reads it.
Python's own `sqlite3` is all it needs.

The shape, version SCHEMA_VERSION (docs/timeline.md has the same list with
every column's allowed values):

- `information`: one row for each fact about the file itself: the schema's
  version, the version of every set of rules that decided what is in it
  (invariant 3), goodall's version, when it was built, and how many
  records are in no entry because they carry no time.
- `raw_records`: one row for each raw record an entry points into: where
  it is kept (`log_directory` or `record_repo`) and its path inside. This
  is the only text in the file that is not from a fixed list.
- `work_blocks`: one row for each ended work block: when it started and
  ended, how it ended, and the pointer to its record.
- `entries`: one row for each entry, in time order. `ended_at` is
  `started_at` plus the duration, kept so that "everything that overlaps
  this work block" is one comparison.

Times are text in UTC, always written the same way
(`2026-11-16T09:35:00.123000Z`), so that sorting the text sorts the times.

Decisions a reader would otherwise reverse-engineer:

- Building never raises (build_timeline). It runs when a work block has
  just ended, and nothing that goes wrong with a derived file may make
  `goodall stop` fail.
- An entry that breaks the column rule is left out and counted, and the
  build says so. It is never written with the value blanked: a rule that
  produced a value off its list is a rule to fix.
- Which work block an entry belongs to is not stored. An entry with a
  duration can lie across two work blocks, and belonging is a question of
  overlap that the reader asks (read_entries_overlapping).
- Building reads every copied transcript again. That is seconds for an
  eval of a few days; if it grows too slow, the fix is to keep the rows of
  files that have not changed, which the pointer already identifies.
"""

import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import goodall
from goodall import timeline_activitywatch, timeline_claude, timeline_records
from goodall.activitywatch import RULES_VERSION as ACTIVITYWATCH_RULES_VERSION
from goodall.actor_labels import ACTOR_LABEL_RULES_VERSION
from goodall.human_actions import RULES_VERSION as HUMAN_ACTION_RULES_VERSION
from goodall.terminal import Failure
from goodall.timeline_entries import (
    SOURCES,
    TIMELINE_RULES_VERSION,
    RawRecordPointer,
    SourceEntries,
    TimelineEntry,
    TimelineWorkBlock,
    entry_problems,
)
from goodall.verbatim_copies import FILE_PERMISSIONS, TEMPORARY_SUFFIX, describe_error, make_folders

# Raise this whenever the tables or columns below change. A file with
# another version is built again, so nothing has to be converted.
SCHEMA_VERSION = "1"

# Where the timeline is, inside the log directory.
TIMELINE_FOLDER_NAME = "timeline"
TIMELINE_FILE_NAME = "timeline.sqlite3"

SCHEMA = """
CREATE TABLE information (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE raw_records (
    raw_record INTEGER PRIMARY KEY,
    kept_in TEXT NOT NULL,
    path TEXT NOT NULL,
    UNIQUE (kept_in, path)
);
CREATE TABLE work_blocks (
    work_block INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    end_state TEXT,
    raw_record INTEGER NOT NULL REFERENCES raw_records (raw_record)
);
CREATE TABLE entries (
    entry INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_seconds REAL,
    source TEXT NOT NULL,
    source_application TEXT,
    actor TEXT,
    kind TEXT NOT NULL,
    entry_type TEXT,
    label_rule TEXT,
    tool TEXT,
    application TEXT,
    category TEXT,
    intervention_category TEXT,
    outcome TEXT,
    in_subagent INTEGER NOT NULL,
    raw_record INTEGER NOT NULL REFERENCES raw_records (raw_record),
    position INTEGER
);
CREATE INDEX entries_by_time ON entries (started_at, ended_at);
"""

# The names in `information` whose values must match this goodall's for a
# file to be read as it is, with the value each must have.
VERSIONS = {
    "schema_version": SCHEMA_VERSION,
    "timeline_rules_version": TIMELINE_RULES_VERSION,
    "actor_label_rules_version": ACTOR_LABEL_RULES_VERSION,
    "human_action_rules_version": HUMAN_ACTION_RULES_VERSION,
    "activitywatch_rules_version": ACTIVITYWATCH_RULES_VERSION,
}


@dataclass
class BuildResult:
    """What building the timeline did.

    `built` is false when no new file could be written; the last good one,
    if any, is then still in place. `problems` holds a plain sentence for
    each raw record that could not be read and for anything left out;
    a timeline can be built and still have problems.
    """

    timeline_file: Path
    built: bool = False
    entry_count: int = 0
    problems: list[str] = field(default_factory=list)


def build_problems_failure(result: BuildResult) -> Failure:
    """The warning, or the failure, for a build that had problems; it names files and never what is in them."""
    if result.built:
        what_happened = "The timeline was built, but not from everything. " + " ".join(result.problems)
    else:
        what_happened = "The timeline could not be built. " + " ".join(result.problems)
    return Failure(
        kind="timeline_incomplete" if result.built else "timeline_not_built",
        what_happened=what_happened,
        why=(
            "The timeline is worked out from the raw records in the log directory and the record repo, which are "
            "untouched: nothing is lost, and it can be built again at any time."
        ),
        what_to_do_next=(
            "Check that the log directory has free space and that this account can read and write it, then run "
            "goodall timeline --rebuild. If it keeps happening, tell the researcher who runs the eval."
        ),
        commands_to_try=("goodall timeline --rebuild",),
    )


def timeline_file_in(log_directory: Path) -> Path:
    """Where an eval's timeline is."""
    return log_directory / TIMELINE_FOLDER_NAME / TIMELINE_FILE_NAME


def format_time(moment: datetime) -> str:
    """A moment as the timeline writes it: UTC to the microsecond, so that sorting the text sorts the times."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text: str) -> datetime:
    """A moment read back from the timeline."""
    return datetime.fromisoformat(text)


def build_timeline(
    log_directory: Path, record_repo: Path, intervention_categories: tuple[str, ...], now: datetime
) -> BuildResult:
    """Build the eval's timeline again from its raw records, and replace the file with the new one.

    Never raises, whatever goes wrong: it becomes one of the result's
    problems, and the last good file stays in place.
    """
    result = BuildResult(timeline_file_in(log_directory))
    try:
        gather_and_write(result, log_directory, record_repo, intervention_categories, now)
    except Exception as error:
        # Catching every error is deliberate, and this is the one place the
        # timeline does it. The timeline is built once a work block has
        # ended and its record is written, so a build that raised would
        # make `goodall stop` fail after it had done what was asked, which
        # AGENTS.md's exit code rule forbids. And the build reads raw
        # records in shapes nobody has seen yet: the rules below turn the
        # shapes they know of into problems, and this turns the rest into
        # one. Only the kind of error is said, never its message, because a
        # message can quote the raw record that caused it.
        result.built = False
        result.entry_count = 0
        result.problems.append(f"Goodall met an error it did not expect while building it ({type(error).__name__}).")
    return result


def gather_and_write(
    result: BuildResult,
    log_directory: Path,
    record_repo: Path,
    intervention_categories: tuple[str, ...],
    now: datetime,
) -> None:
    """Read every raw record, check every entry against the column rule, and write the new file; fill in `result`.

    What it expects to go wrong (a file that cannot be read, an entry off
    its lists, a disk that is full) becomes one of the result's problems
    here. Anything else is raised, for build_timeline to turn into one.
    """
    gathered = SourceEntries()
    gathered.add(timeline_claude.entries_from_transcripts(log_directory))
    gathered.add(timeline_activitywatch.entries_from_activitywatch_copies(log_directory))
    gathered.add(timeline_records.entries_from_records(record_repo, intervention_categories))
    result.problems.extend(gathered.problems)
    work_blocks = timeline_records.ended_work_blocks(record_repo, log_directory, result.problems)

    entries = []
    columns_off_their_lists: set[str] = set()
    refused_count = 0
    for entry in sorted(gathered.entries, key=time_order):
        problems = entry_problems(entry, intervention_categories)
        if problems:
            refused_count += 1
            columns_off_their_lists.update(problems)
            continue
        entries.append(entry)
    if refused_count > 0:
        result.problems.append(
            f"{refused_count} entries were left out of the timeline because they broke its rule about what an entry "
            f"may hold ({'; '.join(sorted(columns_off_their_lists))}). Either one of goodall's rules is at fault "
            "or a raw record holds something no rule expected."
        )

    information = dict(VERSIONS)
    information["goodall_version"] = goodall.__version__
    information["built_at"] = format_time(now)
    information["records_without_a_time"] = str(gathered.without_a_time)
    try:
        write_timeline(log_directory, information, work_blocks, entries)
    except (OSError, sqlite3.Error) as error:
        result.problems.append(f"The timeline could not be written: {describe_database_error(error)}.")
        return
    result.built = True
    result.entry_count = len(entries)


def time_order(entry: TimelineEntry) -> tuple[datetime, int, str, int]:
    """What entries are sorted by: time first, then the same order on every build for entries at one moment.

    Python's sort keeps entries that compare equal in the order they were
    made, so the entries of one transcript line stay in the line's order.
    """
    position = -1 if entry.position is None else entry.position
    return (entry.started_at, SOURCES.index(entry.source), str(entry.raw_record.path), position)


def describe_database_error(error: OSError | sqlite3.Error) -> str:
    """What went wrong writing or reading the file, in a few words."""
    if isinstance(error, OSError):
        return describe_error(error)
    return str(error)


def write_timeline(
    log_directory: Path, information: dict[str, str], work_blocks: list[TimelineWorkBlock], entries: list[TimelineEntry]
) -> None:
    """Write a whole new timeline file, then rename it over the old one.

    The new file is made owner-only before SQLite opens it, because SQLite
    would otherwise create it readable by other accounts. Raises OSError or
    sqlite3.Error, leaving the old file as it was.
    """
    folder = make_folders(log_directory, [TIMELINE_FOLDER_NAME])
    temporary_file = folder / f".{TIMELINE_FILE_NAME}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
    try:
        os.close(os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS))
        connection = sqlite3.connect(temporary_file)
        try:
            connection.executescript(SCHEMA)
            with connection:
                insert_everything(connection, information, work_blocks, entries)
        finally:
            connection.close()
        os.replace(temporary_file, folder / TIMELINE_FILE_NAME)
    finally:
        temporary_file.unlink(missing_ok=True)
        # What SQLite leaves beside a file it was writing when it was interrupted.
        Path(f"{temporary_file}-journal").unlink(missing_ok=True)


def insert_everything(
    connection: sqlite3.Connection,
    information: dict[str, str],
    work_blocks: list[TimelineWorkBlock],
    entries: list[TimelineEntry],
) -> None:
    """Insert every row. Entries are numbered in the order given, which is time order."""
    for name, value in information.items():
        connection.execute("INSERT INTO information (name, value) VALUES (?, ?)", (name, value))
    raw_record_numbers: dict[RawRecordPointer, int] = {}

    def raw_record_number(raw_record: RawRecordPointer) -> int:
        """The row of a raw record, inserted the first time an entry points into it."""
        number = raw_record_numbers.get(raw_record)
        if number is None:
            number = len(raw_record_numbers) + 1
            raw_record_numbers[raw_record] = number
            connection.execute(
                "INSERT INTO raw_records (raw_record, kept_in, path) VALUES (?, ?, ?)",
                (number, raw_record.kept_in, str(raw_record.path)),
            )
        return number

    for work_block in work_blocks:
        connection.execute(
            "INSERT INTO work_blocks (started_at, ended_at, end_state, raw_record) VALUES (?, ?, ?, ?)",
            (
                format_time(work_block.started_at),
                format_time(work_block.ended_at),
                work_block.end_state,
                raw_record_number(work_block.raw_record),
            ),
        )
    for entry in entries:
        ended_at = entry.started_at
        if entry.duration_seconds is not None:
            ended_at = entry.started_at + timedelta(seconds=entry.duration_seconds)
        connection.execute(
            "INSERT INTO entries (started_at, ended_at, duration_seconds, source, source_application, actor, kind, "
            "entry_type, label_rule, tool, application, category, intervention_category, outcome, in_subagent, "
            "raw_record, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                format_time(entry.started_at),
                format_time(ended_at),
                entry.duration_seconds,
                entry.source,
                entry.source_application,
                entry.actor,
                entry.kind,
                entry.entry_type,
                entry.label_rule,
                entry.tool,
                entry.application,
                entry.category,
                entry.intervention_category,
                entry.outcome,
                1 if entry.in_subagent else 0,
                raw_record_number(entry.raw_record),
                entry.position,
            ),
        )


# ---------------------------------------------------------------------------
# Reading


def open_to_read(timeline_file: Path) -> sqlite3.Connection:
    """Open the timeline so that nothing can be written to it. Raises sqlite3.Error when it cannot be opened."""
    return sqlite3.connect(f"file:{quote(str(timeline_file))}?mode=ro", uri=True)


def read_information(timeline_file: Path) -> dict[str, str]:
    """The facts the timeline holds about itself. Raises sqlite3.Error when the file is not a timeline."""
    connection = open_to_read(timeline_file)
    try:
        return dict(connection.execute("SELECT name, value FROM information"))
    finally:
        connection.close()


def is_current(timeline_file: Path) -> bool:
    """True when the file is there and was built by this goodall's schema and rules.

    False means "build it again", whatever the reason: no file yet, a file
    that is not a timeline, or one from other versions.
    """
    if not timeline_file.is_file():
        return False
    try:
        information = read_information(timeline_file)
    except sqlite3.Error:
        return False
    for name, value in VERSIONS.items():
        if information.get(name) != value:
            return False
    return True


def read_work_blocks(timeline_file: Path) -> list[TimelineWorkBlock]:
    """Every ended work block in the timeline, oldest first. Raises sqlite3.Error."""
    connection = open_to_read(timeline_file)
    try:
        rows = connection.execute(
            "SELECT work_blocks.started_at, work_blocks.ended_at, end_state, kept_in, path FROM work_blocks "
            "JOIN raw_records USING (raw_record) ORDER BY work_blocks.started_at, work_block"
        ).fetchall()
    finally:
        connection.close()
    work_blocks = []
    for started_at, ended_at, end_state, kept_in, path in rows:
        raw_record = RawRecordPointer(kept_in, PurePosixPath(path))
        work_blocks.append(TimelineWorkBlock(parse_time(started_at), parse_time(ended_at), end_state, raw_record))
    return work_blocks


def read_entries_overlapping(timeline_file: Path, started_at: datetime, ended_at: datetime) -> list[TimelineEntry]:
    """Every entry that happened at least partly between two moments, in time order. Raises sqlite3.Error.

    Both ends count, and so does the whole of the last second, as for the
    rule that says which records belong to a work block
    (goodall.source_applications.is_inside_work_block). An entry with a
    duration is in when any part of it is.
    """
    connection = open_to_read(timeline_file)
    try:
        rows = connection.execute(
            "SELECT started_at, duration_seconds, source, kind, actor, kept_in, path, position, source_application, "
            "entry_type, label_rule, tool, application, category, intervention_category, outcome, in_subagent "
            "FROM entries JOIN raw_records USING (raw_record) "
            "WHERE started_at < ? AND ended_at >= ? ORDER BY started_at, entry",
            (format_time(ended_at + timedelta(seconds=1)), format_time(started_at)),
        ).fetchall()
    finally:
        connection.close()
    entries = []
    for row in rows:
        entries.append(
            TimelineEntry(
                started_at=parse_time(row[0]),
                duration_seconds=row[1],
                source=row[2],
                kind=row[3],
                actor=row[4],
                raw_record=RawRecordPointer(row[5], PurePosixPath(row[6])),
                position=row[7],
                source_application=row[8],
                entry_type=row[9],
                label_rule=row[10],
                tool=row[11],
                application=row[12],
                category=row[13],
                intervention_category=row[14],
                outcome=row[15],
                in_subagent=row[16] == 1,
            )
        )
    return entries
