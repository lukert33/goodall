"""Goodall's own records: work blocks, journal entries, interventions, work block fixes and deleted work blocks.

This module defines the five kinds of record that goodall and contributors
write into an eval's record repo, and turns each between a Python value and
the text of its file. The commands that write records (`goodall journal`,
`goodall intervene`, the work-block commands `goodall start` and
`goodall stop`, and `goodall fixblock`) and the export to Inspect will call
it. It reads and writes no
files and runs no programs: every function here takes values or text and
returns values or text, so it is easy to test and safe to call anywhere.

The schemas are described for people in docs/record-schemas.md, and every
shape here is PROVISIONAL: each record carries `schema_version` so a later
goodall can tell old files from new ones.

Design decisions a reader would otherwise have to reverse-engineer:

- One file per record, so two contributors never edit the same file. A
  file's name is its record id: a UTC timestamp, the contributor id and a
  random suffix (see `make_record_id`).
- Records are never rewritten (ADR 0003), with one exception the project
  owner decided (ADR 0021): when a person deletes what a work block
  captured (`goodall delete`, goodall.work_block_deletion), its record is
  replaced by a deleted work block, which keeps only its id, who asked and
  when. Git history is left alone, so the deletion itself stays visible.
- A work block record that needs
  a value corrected, such as the workstream `unknown` that goodall records
  when it cannot tell, gets a work block fix: a new small record that names
  the work block and gives the corrected value (ADR 0014). Analysis asks
  `effective_workstreams` which workstream each work block counts towards.
- Work block records and work block fixes are data, so they are JSON.
  Journal entries and
  interventions are prose written by people, so they are Markdown with a
  metadata header. The header is TOML between two `+++` lines: the standard
  library can read TOML (`tomllib`), the project already uses TOML for its
  settings, and YAML would need a third-party package. The standard library
  cannot write TOML, so this module has a writer of its own that covers only
  the value types records use (whole numbers, strings and UTC timestamps).
- Field values are borrowed from Inspect's event types (ApprovalEvent's
  decision, InterruptEvent's `interrupted`, InputEvent's outcome) so an export
  can map records to Inspect events without a translation table. Goodall does
  not import inspect-ai; the values are copied here as plain strings.
- Bad input never raises. Parsing returns the record (when one could be
  built) together with a list of problems; validating returns a list of
  problems. Each problem says what to fix. Rendering is the exception: it
  raises `ValueError` if asked to render a record that fails validation,
  because that is a bug in the calling code, and an invalid file must never
  reach the record repo.
- Standard library only, so the module is safe to use from hook code
  (ADR 0005).
"""

import json
import re
import secrets
import tomllib
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

# The version of the schemas in this file. A file with a higher number was
# written by a newer goodall; one with a lower number needs migrating.
SCHEMA_VERSION = 1

# The value of `record_type` in each kind of file. It makes a file
# self-describing even when it is moved out of its folder.
WORK_BLOCK_RECORD_TYPE = "work_block"
JOURNAL_ENTRY_RECORD_TYPE = "journal_entry"
INTERVENTION_RECORD_TYPE = "intervention"
WORK_BLOCK_FIX_RECORD_TYPE = "work_block_fix"
DELETED_WORK_BLOCK_RECORD_TYPE = "deleted_work_block"

# The folder, inside the record repo, that holds each kind of record.
WORK_BLOCKS_FOLDER = "work-blocks"
JOURNAL_FOLDER = "journal"
INTERVENTIONS_FOLDER = "interventions"
WORK_BLOCK_FIXES_FOLDER = "work-block-fixes"

# The line that opens and closes the metadata header of a Markdown record.
# `+++` is the usual marker for a TOML header (`---` usually means YAML).
HEADER_FENCE = "+++"

# How a work block ended. "inferred" means nobody closed it and its end was
# taken from its last heartbeat (the glossary's "inferred end").
END_STATES = ("clean", "abnormal", "inferred")

# Whether a work block's telemetry was captured (PROVISIONAL, the owner's to
# confirm): `captured` means requests were stored and goodall never found its
# receiver down; `incomplete` that requests were stored but the receiver was
# down for part of the work block; `missing` that no request was stored at
# all. A record without the field says nothing either way, because it was
# written before goodall captured telemetry, or goodall never tried to.
TELEMETRY_STATES = ("captured", "incomplete", "missing")

# The workstream recorded when goodall cannot tell which workstream a work
# block was for: its marker was damaged and named none it could use. Never a
# real workstream (the eval config reader refuses it as a name), and allowed
# only on a work block that ended `abnormal`, since only that path writes it.
# A person says which workstream it was with `goodall fixblock`, which
# writes a work block fix beside the record (see `effective_workstreams`).
UNKNOWN_WORKSTREAM = "unknown"

# What the person decided, in Inspect's ApprovalEvent `decision` values.
DECISIONS = ("approve", "modify", "reject", "escalate", "terminate")

# What the agent was doing when the person cut it short, in Inspect's
# InterruptEvent `interrupted` values.
INTERRUPTED_ACTIVITIES = ("generate", "tool_call", "between_turns")

# How a question the agent asked the person ended, in Inspect's InputEvent
# `outcome` values.
QUESTION_OUTCOMES = ("accepted", "declined", "cancelled")

# The intervention categories an eval config starts with. The eval config's
# own list is the one that counts; these are only its defaults.
DEFAULT_INTERVENTION_CATEGORIES = ("policy-mandated", "infrastructure", "agent-limitation", "freely-delegable")


@dataclass(frozen=True)
class RecordProblem:
    """One thing wrong with a record or its file, and what to do about it.

    `field` names the field at fault, or is empty when the problem is with the
    file as a whole. `message` is a full sentence that ends by saying what to
    change. A message never repeats a value that might be secret.
    """

    field: str
    message: str


@dataclass(frozen=True)
class JournalEntry:
    """A contributor's written note about what was decided or done, and by whom."""

    schema_version: int
    id: str
    contributor: str
    written_at: datetime
    work_block: str | None
    body: str


@dataclass(frozen=True)
class Intervention:
    """A moment when a person overrode, corrected, redirected or rescued an agent.

    `occurred_at` is when it happened; `written_at` is when the contributor
    wrote it down, which can be later. The three Inspect-shaped fields say
    what the person decided (`decision`), what the agent was doing if the
    person cut it short (`interrupted`), and how it ended if the moment was
    the person answering the agent's question (`question_outcome`).
    """

    schema_version: int
    id: str
    contributor: str
    occurred_at: datetime
    written_at: datetime
    work_block: str | None
    claude_session: str | None
    category: str
    decision: str
    interrupted: str | None
    question_outcome: str | None
    body: str


@dataclass(frozen=True)
class MachineFacts:
    """What goodall observed about the machine a work block ran on.

    Observed when the work block starts, never configured (ADR 0009).
    `macos_version` is None on any other system (goodall's own tests run on
    Linux); `claude_code_version` is None when Claude Code was not found.
    """

    hostname: str
    macos_version: str | None
    goodall_version: str
    claude_code_version: str | None


@dataclass(frozen=True)
class WorkBlockRecord:
    """A logged stretch of one contributor's work on one workstream.

    `ended_at` and `end_state` are both None while the work block is still
    running, and both set once it has ended. `claude_sessions` holds the
    Claude Code session ids of the Claude sessions that fell inside it. It is
    a tuple, not a list, so a record cannot be changed after it is built.
    `telemetry` is one of TELEMETRY_STATES once the work block has ended, or
    None when goodall does not say (while running, and in records written
    before goodall captured telemetry).
    """

    schema_version: int
    id: str
    contributor: str
    workstream: str
    started_at: datetime
    ended_at: datetime | None
    end_state: str | None
    machine: MachineFacts
    claude_sessions: tuple[str, ...]
    telemetry: str | None = None


@dataclass(frozen=True)
class WorkBlockFix:
    """A corrected value for one work block, recorded beside its record, which is never changed.

    `work_block` is the id of the work block it fixes. Each value it can
    correct is an optional field named as in the work block record, None
    when this fix leaves that value alone; at least one is set. Today the
    only one is `workstream`; another value would be another optional field,
    so the shape never changes. When a work block has several fixes, the one
    written last wins (`effective_workstreams`).
    """

    schema_version: int
    id: str
    contributor: str
    written_at: datetime
    work_block: str
    workstream: str | None


@dataclass(frozen=True)
class DeletedWorkBlock:
    """What is left of a work block whose captured data a person deleted: that it was, and when.

    It takes the work block record's place, at the same file name and with
    the same `id`, so journal entries, interventions and fixes that name the
    work block still name something. Everything else the record held (its
    times, workstream, machine facts, Claude sessions) is dropped, because
    a person asked for the work block to be forgotten. `contributor` is who
    asked. `other_work_blocks_lost_transcripts` names the other work blocks
    that lost a transcript with it: a transcript is deleted whole, and one
    agent session can have records inside several work blocks.
    """

    schema_version: int
    id: str
    contributor: str
    deleted_at: datetime
    other_work_blocks_lost_transcripts: tuple[str, ...]


# Any one of goodall's own records.
Record = WorkBlockRecord | JournalEntry | Intervention | WorkBlockFix | DeletedWorkBlock


@dataclass(frozen=True)
class ParseResult[RecordType]:
    """What parsing a file's text produced.

    `record` is None when the text was too broken to build a record from;
    `problems` then says why. A record can also come back together with
    problems that did not stop it being built, such as an unknown key.
    """

    record: RecordType | None
    problems: list[RecordProblem]


# ---------------------------------------------------------------------------
# Record ids and file names
# ---------------------------------------------------------------------------


def make_record_id(moment: datetime, contributor: str, suffix: str | None = None) -> str:
    """Make the id, and so the file name, of a new record.

    The id looks like `20261116T093000Z-ada-lovelace-7f3a9c2e`:

    - the UTC time first, in a fixed-width form, so a folder listing sorts
      records in time order;
    - the contributor id next, so two contributors writing in the same second
      can never produce the same name;
    - eight random hexadecimal characters last, so the same contributor
      writing twice in one second (two terminals, two agents) cannot collide.

    The time has no colons, because macOS Finder shows a colon in a file name
    as a slash. Every character is lowercase apart from the fixed `T` and `Z`,
    so names cannot clash on a case-insensitive disk.

    Pass `suffix` only in tests, to make the id predictable.
    """
    if suffix is None:
        suffix = secrets.token_hex(4)
    timestamp = _as_utc(moment).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{contributor}-{suffix}"


# ---------------------------------------------------------------------------
# Building records
# ---------------------------------------------------------------------------


def build_journal_entry(
    contributor: str,
    written_at: datetime,
    body: str,
    work_block: str | None = None,
    suffix: str | None = None,
) -> JournalEntry:
    """Build a new journal entry, giving it an id from the time it was written."""
    written_at = _as_utc(written_at)
    return JournalEntry(
        schema_version=SCHEMA_VERSION,
        id=make_record_id(written_at, contributor, suffix),
        contributor=contributor,
        written_at=written_at,
        work_block=work_block,
        body=_tidy_body(body),
    )


def build_intervention(
    contributor: str,
    occurred_at: datetime,
    written_at: datetime,
    category: str,
    decision: str,
    body: str,
    work_block: str | None = None,
    claude_session: str | None = None,
    interrupted: str | None = None,
    question_outcome: str | None = None,
    suffix: str | None = None,
) -> Intervention:
    """Build a new intervention, giving it an id from the time it happened.

    The id uses `occurred_at`, not `written_at`, so an intervention written
    down later still sorts where it happened.
    """
    occurred_at = _as_utc(occurred_at)
    return Intervention(
        schema_version=SCHEMA_VERSION,
        id=make_record_id(occurred_at, contributor, suffix),
        contributor=contributor,
        occurred_at=occurred_at,
        written_at=_as_utc(written_at),
        work_block=work_block,
        claude_session=claude_session,
        category=category,
        decision=decision,
        interrupted=interrupted,
        question_outcome=question_outcome,
        body=_tidy_body(body),
    )


def build_work_block_record(
    contributor: str,
    workstream: str,
    started_at: datetime,
    machine: MachineFacts,
    ended_at: datetime | None = None,
    end_state: str | None = None,
    claude_sessions: Collection[str] = (),
    suffix: str | None = None,
    telemetry: str | None = None,
) -> WorkBlockRecord:
    """Build a work block record, giving it an id from the time it started.

    Leave `ended_at` and `end_state` out for a work block that is still
    running.
    """
    started_at = _as_utc(started_at)
    if ended_at is not None:
        ended_at = _as_utc(ended_at)
    return WorkBlockRecord(
        schema_version=SCHEMA_VERSION,
        id=make_record_id(started_at, contributor, suffix),
        contributor=contributor,
        workstream=workstream,
        started_at=started_at,
        ended_at=ended_at,
        end_state=end_state,
        machine=machine,
        claude_sessions=tuple(claude_sessions),
        telemetry=telemetry,
    )


def build_work_block_fix(
    contributor: str,
    written_at: datetime,
    work_block: str,
    workstream: str | None = None,
    suffix: str | None = None,
) -> WorkBlockFix:
    """Build a new work block fix, giving it an id from the time it was written.

    The id uses `written_at`, not the work block's start, so fixes sort in
    the order they were made, which is the order in which they take effect.
    """
    written_at = _as_utc(written_at)
    return WorkBlockFix(
        schema_version=SCHEMA_VERSION,
        id=make_record_id(written_at, contributor, suffix),
        contributor=contributor,
        written_at=written_at,
        work_block=work_block,
        workstream=workstream,
    )


def build_deleted_work_block(
    work_block: str,
    contributor: str,
    deleted_at: datetime,
    other_work_blocks_lost_transcripts: Collection[str],
) -> DeletedWorkBlock:
    """Build the record that replaces a deleted work block's: its id is the work block's own."""
    return DeletedWorkBlock(
        schema_version=SCHEMA_VERSION,
        id=work_block,
        contributor=contributor,
        deleted_at=_as_utc(deleted_at),
        other_work_blocks_lost_transcripts=tuple(other_work_blocks_lost_transcripts),
    )


# ---------------------------------------------------------------------------
# Rendering records to the text of their files
# ---------------------------------------------------------------------------


def relative_path_for(record: Record) -> PurePosixPath:
    """Return where `record`'s file goes, relative to the record repo's top folder.

    For example `journal/20261116T093000Z-ada-lovelace-7f3a9c2e.md`. The
    path uses forward slashes whatever the machine, because it names a place
    inside a git repository.
    """
    if isinstance(record, WorkBlockRecord | DeletedWorkBlock):
        return PurePosixPath(WORK_BLOCKS_FOLDER) / f"{record.id}.json"
    if isinstance(record, JournalEntry):
        return PurePosixPath(JOURNAL_FOLDER) / f"{record.id}.md"
    if isinstance(record, WorkBlockFix):
        return PurePosixPath(WORK_BLOCK_FIXES_FOLDER) / f"{record.id}.json"
    return PurePosixPath(INTERVENTIONS_FOLDER) / f"{record.id}.md"


def render_record(record: Record) -> str:
    """Return the full text of the file that holds `record`.

    Raises ValueError if the record fails validation, listing every problem.
    This is the one place bad input raises: a record reaching here was built
    by goodall's own code, so an invalid one is a bug, and writing it would
    put a broken or unpublishable file into the record repo. The check uses
    no eval config, so only rules that hold for every eval are applied;
    callers check the roster, workstreams and categories with
    `validate_record` before they get here.
    """
    problems = validate_record(record)
    if problems:
        messages = []
        for problem in problems:
            messages.append(problem.message)
        raise ValueError("cannot render an invalid record: " + " ".join(messages))
    return _render_without_checks(record)


def _render_without_checks(record: Record) -> str:
    """Render a record that has already been validated."""
    if isinstance(record, WorkBlockRecord):
        return _render_work_block_record(record)
    if isinstance(record, WorkBlockFix):
        return _render_work_block_fix(record)
    if isinstance(record, DeletedWorkBlock):
        return _render_deleted_work_block(record)
    if isinstance(record, JournalEntry):
        header_values: list[tuple[str, object]] = [
            ("schema_version", record.schema_version),
            ("record_type", JOURNAL_ENTRY_RECORD_TYPE),
            ("id", record.id),
            ("contributor", record.contributor),
            ("written_at", record.written_at),
            ("work_block", record.work_block),
        ]
        return _render_markdown_record(header_values, record.body)
    header_values = [
        ("schema_version", record.schema_version),
        ("record_type", INTERVENTION_RECORD_TYPE),
        ("id", record.id),
        ("contributor", record.contributor),
        ("occurred_at", record.occurred_at),
        ("written_at", record.written_at),
        ("work_block", record.work_block),
        ("claude_session", record.claude_session),
        ("category", record.category),
        ("decision", record.decision),
        ("interrupted", record.interrupted),
        ("question_outcome", record.question_outcome),
    ]
    return _render_markdown_record(header_values, record.body)


def _render_work_block_record(record: WorkBlockRecord) -> str:
    """Write a work block record as indented JSON, one field per line.

    Unlike TOML, JSON has null, so an empty optional field is written as null
    rather than left out: a reader sees every field the schema has. The one
    exception is `telemetry`, added after records were already being
    written: it is left out when goodall does not say, so a running work
    block's marker, and any record without it, look exactly as before.
    """
    ended_at = None
    if record.ended_at is not None:
        ended_at = _format_timestamp(record.ended_at)
    values = {
        "schema_version": record.schema_version,
        "record_type": WORK_BLOCK_RECORD_TYPE,
        "id": record.id,
        "contributor": record.contributor,
        "workstream": record.workstream,
        "started_at": _format_timestamp(record.started_at),
        "ended_at": ended_at,
        "end_state": record.end_state,
        "machine": {
            "hostname": record.machine.hostname,
            "macos_version": record.machine.macos_version,
            "goodall_version": record.machine.goodall_version,
            "claude_code_version": record.machine.claude_code_version,
        },
        "claude_sessions": list(record.claude_sessions),
    }
    if record.telemetry is not None:
        values["telemetry"] = record.telemetry
    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


def _render_work_block_fix(record: WorkBlockFix) -> str:
    """Write a work block fix as indented JSON; a value the fix leaves alone is null."""
    values = {
        "schema_version": record.schema_version,
        "record_type": WORK_BLOCK_FIX_RECORD_TYPE,
        "id": record.id,
        "contributor": record.contributor,
        "written_at": _format_timestamp(record.written_at),
        "work_block": record.work_block,
        "workstream": record.workstream,
    }
    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


def _render_deleted_work_block(record: DeletedWorkBlock) -> str:
    """Write a deleted work block as indented JSON."""
    values = {
        "schema_version": record.schema_version,
        "record_type": DELETED_WORK_BLOCK_RECORD_TYPE,
        "id": record.id,
        "contributor": record.contributor,
        "deleted_at": _format_timestamp(record.deleted_at),
        "other_work_blocks_lost_transcripts": list(record.other_work_blocks_lost_transcripts),
    }
    return json.dumps(values, indent=2, ensure_ascii=False) + "\n"


def _render_markdown_record(header_values: list[tuple[str, object]], body: str) -> str:
    """Join a TOML header and a Markdown body into the text of one file."""
    header_lines = [HEADER_FENCE]
    for key, value in header_values:
        # TOML has no "null": an optional field with no value is left out.
        if value is None:
            continue
        header_lines.append(f"{key} = {_toml_value(value)}")
    header_lines.append(HEADER_FENCE)
    return "\n".join(header_lines) + "\n\n" + body + "\n"


def _toml_value(value: object) -> str:
    """Write one value in TOML. Covers only the types records use.

    Strings are written as TOML basic strings using JSON's escaping, which
    TOML accepts (\\" \\\\ \\n \\t \\uXXXX and so on). The trap: JSON leaves the
    DEL character (U+007F) unescaped and TOML forbids it raw, so it is escaped
    by hand.
    """
    # bool is a kind of int in Python; records have no booleans, so refuse it
    # rather than write `True`, which is not TOML.
    if isinstance(value, bool):
        raise TypeError("records have no true/false fields")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, datetime):
        return _format_timestamp(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    raise TypeError(f"no TOML form for {type(value).__name__}")


# ---------------------------------------------------------------------------
# Parsing the text of a file back into a record
# ---------------------------------------------------------------------------


def parse_journal_entry(text: str) -> ParseResult[JournalEntry]:
    """Read the text of a journal entry file."""
    problems: list[RecordProblem] = []
    split = _split_markdown_record(text, problems)
    if split is None:
        return ParseResult(record=None, problems=problems)
    header, body = split

    reader = _FieldReader(header, problems)
    _check_record_type(reader, JOURNAL_ENTRY_RECORD_TYPE)
    schema_version = reader.schema_version()
    record_id = reader.text("id")
    contributor = reader.text("contributor")
    written_at = reader.timestamp("written_at")
    work_block = reader.optional_text("work_block")
    reader.report_unknown_keys()

    if schema_version is None or record_id is None or contributor is None or written_at is None:
        return ParseResult(record=None, problems=problems)
    entry = JournalEntry(
        schema_version=schema_version,
        id=record_id,
        contributor=contributor,
        written_at=written_at,
        work_block=work_block,
        body=body,
    )
    return ParseResult(record=entry, problems=problems)


def parse_intervention(text: str) -> ParseResult[Intervention]:
    """Read the text of an intervention file."""
    problems: list[RecordProblem] = []
    split = _split_markdown_record(text, problems)
    if split is None:
        return ParseResult(record=None, problems=problems)
    header, body = split

    reader = _FieldReader(header, problems)
    _check_record_type(reader, INTERVENTION_RECORD_TYPE)
    schema_version = reader.schema_version()
    record_id = reader.text("id")
    contributor = reader.text("contributor")
    occurred_at = reader.timestamp("occurred_at")
    written_at = reader.timestamp("written_at")
    work_block = reader.optional_text("work_block")
    claude_session = reader.optional_text("claude_session")
    category = reader.text("category")
    decision = reader.text("decision")
    interrupted = reader.optional_text("interrupted")
    question_outcome = reader.optional_text("question_outcome")
    reader.report_unknown_keys()

    required_values = [schema_version, record_id, contributor, occurred_at, written_at, category, decision]
    if None in required_values:
        return ParseResult(record=None, problems=problems)
    # The checks above guarantee these; the asserts tell the type checker.
    assert schema_version is not None and record_id is not None and contributor is not None
    assert occurred_at is not None and written_at is not None
    assert category is not None and decision is not None
    intervention = Intervention(
        schema_version=schema_version,
        id=record_id,
        contributor=contributor,
        occurred_at=occurred_at,
        written_at=written_at,
        work_block=work_block,
        claude_session=claude_session,
        category=category,
        decision=decision,
        interrupted=interrupted,
        question_outcome=question_outcome,
        body=body,
    )
    return ParseResult(record=intervention, problems=problems)


def parse_work_block_fix(text: str) -> ParseResult[WorkBlockFix]:
    """Read the text of a work block fix file."""
    problems: list[RecordProblem] = []
    values = _load_json_object(text, problems)
    if values is None:
        return ParseResult(record=None, problems=problems)

    reader = _FieldReader(values, problems)
    _check_record_type(reader, WORK_BLOCK_FIX_RECORD_TYPE)
    schema_version = reader.schema_version()
    record_id = reader.text("id")
    contributor = reader.text("contributor")
    written_at = reader.timestamp("written_at")
    work_block = reader.text("work_block")
    workstream = reader.optional_text("workstream")
    reader.report_unknown_keys()

    required_values = [schema_version, record_id, contributor, written_at, work_block]
    if None in required_values:
        return ParseResult(record=None, problems=problems)
    # The checks above guarantee these; the asserts tell the type checker.
    assert schema_version is not None and record_id is not None and contributor is not None
    assert written_at is not None and work_block is not None
    fix = WorkBlockFix(
        schema_version=schema_version,
        id=record_id,
        contributor=contributor,
        written_at=written_at,
        work_block=work_block,
        workstream=workstream,
    )
    return ParseResult(record=fix, problems=problems)


def parse_deleted_work_block(text: str) -> ParseResult[DeletedWorkBlock]:
    """Read the text of a deleted work block's file."""
    problems: list[RecordProblem] = []
    values = _load_json_object(text, problems)
    if values is None:
        return ParseResult(record=None, problems=problems)

    reader = _FieldReader(values, problems)
    _check_record_type(reader, DELETED_WORK_BLOCK_RECORD_TYPE)
    schema_version = reader.schema_version()
    record_id = reader.text("id")
    contributor = reader.text("contributor")
    deleted_at = reader.timestamp("deleted_at")
    other_work_blocks = reader.text_list("other_work_blocks_lost_transcripts")
    reader.report_unknown_keys()

    if None in [schema_version, record_id, contributor, deleted_at, other_work_blocks]:
        return ParseResult(record=None, problems=problems)
    # The checks above guarantee these; the asserts tell the type checker.
    assert schema_version is not None and record_id is not None and contributor is not None
    assert deleted_at is not None and other_work_blocks is not None
    deleted = DeletedWorkBlock(
        schema_version=schema_version,
        id=record_id,
        contributor=contributor,
        deleted_at=deleted_at,
        other_work_blocks_lost_transcripts=tuple(other_work_blocks),
    )
    return ParseResult(record=deleted, problems=problems)


def json_record_type(text: str) -> str | None:
    """The `record_type` a JSON record's text gives, or None when it is not a JSON object with one.

    The `work-blocks/` folder holds work block records and deleted work
    blocks side by side, so a reader asks this first to know which to parse.
    """
    try:
        values = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(values, dict):
        return None
    record_type = values.get("record_type")
    if not isinstance(record_type, str):
        return None
    return record_type


def _load_json_object(text: str, problems: list[RecordProblem]) -> dict[str, Any] | None:
    """Read a JSON record's text as one object; None, with a problem added, when it is not one."""
    try:
        values = json.loads(text)
    except json.JSONDecodeError as error:
        problems.append(
            RecordProblem(
                "",
                f"The file is not valid JSON: {error.msg} at line {error.lineno}, column {error.colno}; "
                "correct that line.",
            )
        )
        return None
    if not isinstance(values, dict):
        problems.append(RecordProblem("", "The file must hold one JSON object, between { and }; wrap it in braces."))
        return None
    return values


def parse_work_block_record(text: str) -> ParseResult[WorkBlockRecord]:
    """Read the text of a work block record file."""
    problems: list[RecordProblem] = []
    values = _load_json_object(text, problems)
    if values is None:
        return ParseResult(record=None, problems=problems)

    reader = _FieldReader(values, problems)
    _check_record_type(reader, WORK_BLOCK_RECORD_TYPE)
    schema_version = reader.schema_version()
    record_id = reader.text("id")
    contributor = reader.text("contributor")
    workstream = reader.text("workstream")
    started_at = reader.timestamp("started_at")
    ended_at = reader.optional_timestamp("ended_at")
    end_state = reader.optional_text("end_state")
    machine = _read_machine_facts(reader)
    claude_sessions = reader.text_list("claude_sessions")
    telemetry = reader.optional_text("telemetry")
    reader.report_unknown_keys()

    required_values = [schema_version, record_id, contributor, workstream, started_at, machine, claude_sessions]
    if None in required_values:
        return ParseResult(record=None, problems=problems)
    # The checks above guarantee these; the asserts tell the type checker.
    assert schema_version is not None and record_id is not None and contributor is not None
    assert workstream is not None and started_at is not None
    assert machine is not None and claude_sessions is not None
    work_block = WorkBlockRecord(
        schema_version=schema_version,
        id=record_id,
        contributor=contributor,
        workstream=workstream,
        started_at=started_at,
        ended_at=ended_at,
        end_state=end_state,
        machine=machine,
        claude_sessions=tuple(claude_sessions),
        telemetry=telemetry,
    )
    return ParseResult(record=work_block, problems=problems)


def _read_machine_facts(reader: "_FieldReader") -> MachineFacts | None:
    """Read the `machine` object of a work block record."""
    reader.keys_read.add("machine")
    if "machine" not in reader.values:
        reader.problems.append(RecordProblem("machine", "machine is missing; add it."))
        return None
    machine_values = reader.values["machine"]
    if not isinstance(machine_values, dict):
        reader.problems.append(
            RecordProblem("machine", "machine must be an object, between { and }, holding the machine facts.")
        )
        return None
    # Problems inside `machine` are reported as `machine.hostname` and so on.
    machine_reader = _FieldReader(machine_values, reader.problems, prefix="machine.")
    hostname = machine_reader.text("hostname")
    macos_version = machine_reader.optional_text("macos_version")
    goodall_version = machine_reader.text("goodall_version")
    claude_code_version = machine_reader.optional_text("claude_code_version")
    machine_reader.report_unknown_keys()
    if hostname is None or goodall_version is None:
        return None
    return MachineFacts(hostname, macos_version, goodall_version, claude_code_version)


def _split_markdown_record(text: str, problems: list[RecordProblem]) -> tuple[dict[str, Any], str] | None:
    """Split a Markdown record into its parsed header and its body.

    Returns None, with a problem added, if the header is missing or is not
    valid TOML. The body is everything after the closing `+++` line, without
    the blank lines around it.
    """
    # A file saved on Windows has \r\n line endings; treat them as \n.
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != HEADER_FENCE:
        problems.append(
            RecordProblem("", f"The file does not start with a `{HEADER_FENCE}` line; put the metadata header first.")
        )
        return None
    closing_line_number = None
    for line_number in range(1, len(lines)):
        if lines[line_number].strip() == HEADER_FENCE:
            closing_line_number = line_number
            break
    if closing_line_number is None:
        problems.append(
            RecordProblem("", f"The metadata header is never closed; add a `{HEADER_FENCE}` line after it.")
        )
        return None

    header_text = "\n".join(lines[1:closing_line_number])
    try:
        header = tomllib.loads(header_text)
    except tomllib.TOMLDecodeError as error:
        # The trap: tomllib counts lines from the start of the header, which
        # is line 2 of the file, so its line number is one too small for a
        # person looking at the file. Python 3.12 gives the position only
        # inside the message text, so it is read back out of that.
        message = str(error)
        position = re.search(r"\(at line (\d+), column (\d+)\)", message)
        where = "in the metadata header"
        if position is not None:
            file_line = int(position.group(1)) + 1
            where = f"at line {file_line} of the file"
            message = message[: position.start()].strip()
        problems.append(
            RecordProblem("", f"The metadata header is not valid TOML {where}: {message}. Correct that line.")
        )
        return None
    body = "\n".join(lines[closing_line_number + 1 :]).strip("\n")
    return header, body


def _check_record_type(reader: "_FieldReader", expected: str) -> None:
    """Report a file whose `record_type` is not the kind being read."""
    record_type = reader.text("record_type")
    if record_type is not None and record_type != expected:
        reader.problems.append(
            RecordProblem(
                "record_type",
                f"record_type is {record_type!r} but this file is read as {expected!r}; "
                "move the file to the right folder or correct record_type.",
            )
        )


class _FieldReader:
    """Takes typed values out of a parsed header or JSON object, noting problems.

    Each method returns the value, or None after adding a problem. At the end,
    `report_unknown_keys` reports every key no method asked for, so a
    misspelled key is never silently ignored. `prefix` is put before field
    names in problems, so a problem inside `machine` names `machine.hostname`.
    """

    def __init__(self, values: dict[str, Any], problems: list[RecordProblem], prefix: str = "") -> None:
        self.values = values
        self.problems = problems
        self.prefix = prefix
        self.keys_read: set[str] = set()

    def _add(self, key: str, message: str) -> None:
        name = self.prefix + key
        self.problems.append(RecordProblem(name, f"{name} {message}"))

    def schema_version(self) -> int | None:
        self.keys_read.add("schema_version")
        if "schema_version" not in self.values:
            self._add("schema_version", f"is missing; add schema_version = {SCHEMA_VERSION}.")
            return None
        value = self.values["schema_version"]
        # bool is a kind of int in Python, so `true` would otherwise pass.
        if isinstance(value, bool) or not isinstance(value, int):
            self._add("schema_version", f"must be a whole number; write {SCHEMA_VERSION}.")
            return None
        return value

    def text(self, key: str) -> str | None:
        self.keys_read.add(key)
        if key not in self.values:
            self._add(key, "is missing; add it.")
            return None
        return self._as_text(key, self.values[key])

    def optional_text(self, key: str) -> str | None:
        self.keys_read.add(key)
        value = self.values.get(key)
        if value is None:
            return None
        return self._as_text(key, value)

    def _as_text(self, key: str, value: object) -> str | None:
        if not isinstance(value, str):
            self._add(key, "must be text in double quotes; quote it.")
            return None
        return value

    def text_list(self, key: str) -> list[str] | None:
        self.keys_read.add(key)
        if key not in self.values:
            self._add(key, "is missing; add it, as [] if there are none.")
            return None
        value = self.values[key]
        if not isinstance(value, list):
            self._add(key, 'must be a list in square brackets, like ["first", "second"]; correct it.')
            return None
        for item in value:
            if not isinstance(item, str):
                self._add(key, "must hold only text in double quotes; quote each item.")
                return None
        return value

    def timestamp(self, key: str) -> datetime | None:
        self.keys_read.add(key)
        if key not in self.values:
            self._add(key, "is missing; add it.")
            return None
        return self._as_timestamp(key, self.values[key])

    def optional_timestamp(self, key: str) -> datetime | None:
        self.keys_read.add(key)
        value = self.values.get(key)
        if value is None:
            return None
        return self._as_timestamp(key, value)

    def _as_timestamp(self, key: str, value: object) -> datetime | None:
        """Accept a TOML date-time or a text timestamp, as long as it has a time zone.

        TOML gives a datetime object for an unquoted date-time; JSON, and a
        quoted TOML value, give text. Both are accepted so a hand-edited file
        is not rejected over quoting. The result is always in UTC.
        """
        how_to_fix = f"write it in UTC like {EXAMPLE_TIMESTAMP}"
        if isinstance(value, str):
            if not TIMESTAMP_PATTERN.fullmatch(value):
                self._add(key, f"is not a UTC timestamp; {how_to_fix}.")
                return None
            return datetime.fromisoformat(value)
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                self._add(key, f"has no time zone; {how_to_fix}, ending in Z.")
                return None
            return value.astimezone(UTC)
        self._add(key, f"is not a timestamp; {how_to_fix}.")
        return None

    def report_unknown_keys(self) -> None:
        for key in self.values:
            if key not in self.keys_read:
                self._add(key, "is not a field of this record; remove it or correct its spelling.")


# ---------------------------------------------------------------------------
# Validating a record's values
# ---------------------------------------------------------------------------

# A contributor id, workstream or intervention category: lowercase words
# joined by hyphens, like `ada-lovelace` or `go-to-market`. They appear in
# file names, so they are kept to characters every filesystem accepts, and an
# email address can never pass for one.
IDENTIFIER_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
IDENTIFIER_MAXIMUM_LENGTH = 64

# A record id, as made by `make_record_id`.
RECORD_ID_PATTERN = re.compile(r"\d{8}T\d{6}Z-[a-z0-9]+(-[a-z0-9]+)*-[0-9a-f]{8}")
EXAMPLE_RECORD_ID = "20261116T093000Z-ada-lovelace-7f3a9c2e"

# A Claude Code session id, which is a UUID.
CLAUDE_SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Something that looks like an email address. Records are meant to be
# published after redaction, so none may hold one (the roster identifies
# people by contributor id instead).
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

# Shapes of common secret values: model API keys, GitHub, AWS and Slack
# tokens, private keys. This is a safety net for a value pasted by mistake,
# not a guarantee; the real protection is that secrets stay in the secrets
# manager (ADR 0010). A match is reported without repeating the value.
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def validate_record(
    record: Record,
    contributor_ids: Collection[str] | None = None,
    workstreams: Collection[str] | None = None,
    intervention_categories: Collection[str] | None = None,
) -> list[RecordProblem]:
    """Return every problem with `record`'s values; an empty list means it is valid.

    The three optional lists come from the eval config: its roster's ids, its
    workstreams and its intervention categories. Pass them to check the
    record against this eval; leave one out (None) to check only that the
    value is well formed. Never raises.
    """
    checker = _Checker()
    checker.schema_version(record.schema_version)
    checker.record_id("id", record.id)
    checker.identifier("contributor", record.contributor, contributor_ids, "the roster in eval.toml")

    if isinstance(record, WorkBlockRecord):
        _validate_work_block_record(checker, record, workstreams)
    elif isinstance(record, WorkBlockFix):
        _validate_work_block_fix(checker, record, workstreams)
    elif isinstance(record, DeletedWorkBlock):
        checker.timestamp("deleted_at", record.deleted_at)
        for other_work_block in record.other_work_blocks_lost_transcripts:
            checker.record_id("other_work_blocks_lost_transcripts", other_work_block)
    elif isinstance(record, JournalEntry):
        checker.timestamp("written_at", record.written_at)
        checker.optional_record_id("work_block", record.work_block)
        checker.body(record.body)
    else:
        _validate_intervention(checker, record, intervention_categories)
    return checker.problems


def _validate_work_block_record(
    checker: "_Checker", record: WorkBlockRecord, workstreams: Collection[str] | None
) -> None:
    if record.workstream == UNKNOWN_WORKSTREAM:
        if record.end_state != "abnormal":
            checker.add(
                "workstream",
                f"workstream is {UNKNOWN_WORKSTREAM}, which goodall records only for a work block that ended "
                "abnormal; use one of the workstreams in eval.toml.",
            )
    else:
        checker.identifier("workstream", record.workstream, workstreams, "the workstreams in eval.toml")
    started_at_valid = checker.timestamp("started_at", record.started_at)
    if record.ended_at is None and record.end_state is None:
        # A work block that is still running.
        pass
    elif record.ended_at is None or record.end_state is None:
        checker.add(
            "end_state",
            "end_state and ended_at go together: set both or neither (neither while the work block is running).",
        )
    else:
        ended_at_valid = checker.timestamp("ended_at", record.ended_at)
        checker.one_of("end_state", record.end_state, END_STATES)
        if started_at_valid and ended_at_valid and record.ended_at < record.started_at:
            checker.add("ended_at", "ended_at is before started_at; correct whichever time is wrong.")

    machine = record.machine
    checker.required_text("machine.hostname", machine.hostname)
    if machine.macos_version is not None:
        checker.required_text("machine.macos_version", machine.macos_version)
    checker.required_text("machine.goodall_version", machine.goodall_version)
    if machine.claude_code_version is not None:
        checker.required_text("machine.claude_code_version", machine.claude_code_version)
    for claude_session in record.claude_sessions:
        checker.claude_session("claude_sessions", claude_session)
    if record.telemetry is not None and record.telemetry not in TELEMETRY_STATES:
        checker.add(
            "telemetry",
            "telemetry must be captured, incomplete or missing, or left out when goodall does not say; correct it.",
        )


def _validate_work_block_fix(checker: "_Checker", record: WorkBlockFix, workstreams: Collection[str] | None) -> None:
    checker.timestamp("written_at", record.written_at)
    checker.record_id("work_block", record.work_block)
    if record.workstream is None:
        checker.add("workstream", "A work block fix must give at least one corrected value; add workstream.")
    elif record.workstream == UNKNOWN_WORKSTREAM:
        checker.add(
            "workstream",
            f"workstream is {UNKNOWN_WORKSTREAM}, the placeholder goodall records when it cannot tell; a fix "
            "must name one of the workstreams in eval.toml.",
        )
    else:
        checker.identifier("workstream", record.workstream, workstreams, "the workstreams in eval.toml")


def _validate_intervention(
    checker: "_Checker", record: Intervention, intervention_categories: Collection[str] | None
) -> None:
    occurred_at_valid = checker.timestamp("occurred_at", record.occurred_at)
    written_at_valid = checker.timestamp("written_at", record.written_at)
    if occurred_at_valid and written_at_valid and record.written_at < record.occurred_at:
        checker.add("written_at", "written_at is before occurred_at; correct whichever time is wrong.")
    checker.optional_record_id("work_block", record.work_block)
    if record.claude_session is not None:
        checker.claude_session("claude_session", record.claude_session)
    checker.identifier("category", record.category, intervention_categories, "the intervention_categories in eval.toml")
    checker.one_of("decision", record.decision, DECISIONS)
    if record.interrupted is not None:
        checker.one_of("interrupted", record.interrupted, INTERRUPTED_ACTIVITIES)
    if record.question_outcome is not None:
        checker.one_of("question_outcome", record.question_outcome, QUESTION_OUTCOMES)
    checker.body(record.body)


class _Checker:
    """Collects validation problems, one small rule per method.

    Every text value is first checked for an email address or a secret. If
    one is found, that is the only problem reported for the field: it is the
    one that matters, and the format rules would only add noise.
    """

    def __init__(self) -> None:
        self.problems: list[RecordProblem] = []

    def add(self, field: str, message: str) -> None:
        self.problems.append(RecordProblem(field, message))

    def publishable(self, field: str, value: str, where: str = "") -> bool:
        """Report an email address or secret in `value`; True if there is none."""
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                self.add(
                    field,
                    f"{field}{where} looks like it contains a secret (an API key, token or private key). "
                    "Remove it, and if it was real, revoke it in the service that issued it.",
                )
                return False
        if EMAIL_PATTERN.search(value):
            self.add(
                field,
                f"{field}{where} looks like it contains an email address. Records are meant to be published, "
                "so remove it, or name the person by their contributor id.",
            )
            return False
        return True

    def schema_version(self, schema_version: int) -> None:
        if schema_version > SCHEMA_VERSION:
            self.add(
                "schema_version",
                f"schema_version is {schema_version}, so this record was written by a newer goodall "
                f"(this one understands {SCHEMA_VERSION}); upgrade goodall.",
            )
        elif schema_version != SCHEMA_VERSION:
            self.add("schema_version", f"schema_version is {schema_version}; it must be {SCHEMA_VERSION}.")

    def record_id(self, field: str, value: str) -> None:
        if not self.publishable(field, value):
            return
        if not RECORD_ID_PATTERN.fullmatch(value):
            self.add(
                field,
                f"{field} is not a record id; it should look like {EXAMPLE_RECORD_ID} "
                "(UTC time, contributor id, eight random characters).",
            )

    def optional_record_id(self, field: str, value: str | None) -> None:
        if value is not None:
            self.record_id(field, value)

    def identifier(self, field: str, value: str, known_values: Collection[str] | None, where_listed: str) -> None:
        if not self.publishable(field, value):
            return
        if len(value) > IDENTIFIER_MAXIMUM_LENGTH or not IDENTIFIER_PATTERN.fullmatch(value):
            self.add(
                field,
                f"{field} must be lowercase letters, digits and hyphens, at most {IDENTIFIER_MAXIMUM_LENGTH} "
                "characters, like ada-lovelace; use the id from eval.toml.",
            )
            return
        if known_values is not None and value not in known_values:
            self.add(field, f"{field} {value!r} is not in {where_listed}; use one listed there, or add it there first.")

    def one_of(self, field: str, value: str, allowed_values: tuple[str, ...]) -> None:
        if value not in allowed_values:
            self.add(field, f"{field} must be one of {', '.join(allowed_values)}; choose the one that fits.")

    def timestamp(self, field: str, value: datetime) -> bool:
        """Report a timestamp with no time zone or with fractions of a second."""
        if value.utcoffset() is None:
            self.add(field, f"{field} has no time zone; give it in UTC, like {EXAMPLE_TIMESTAMP}.")
            return False
        if value.microsecond != 0:
            self.add(field, f"{field} has fractions of a second; records keep whole seconds, so drop them.")
            return False
        return True

    def claude_session(self, field: str, value: str) -> None:
        if not self.publishable(field, value):
            return
        if not CLAUDE_SESSION_ID_PATTERN.fullmatch(value):
            self.add(
                field,
                f"{field} must hold a Claude Code session id, a lowercase UUID like "
                "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5; copy it from the transcript's file name.",
            )

    def required_text(self, field: str, value: str) -> None:
        if not self.publishable(field, value):
            return
        if value.strip() == "":
            self.add(field, f"{field} is empty; fill it in.")

    def body(self, body: str) -> None:
        if body.strip() == "":
            self.add("body", "body is empty; write what happened below the metadata header.")
            return
        # Check line by line so a problem can say where to look without
        # repeating what it found.
        line_number = 0
        for line in body.split("\n"):
            line_number += 1
            if not self.publishable("body", line, where=f" line {line_number}"):
                return


# ---------------------------------------------------------------------------
# Reading work block records together with their fixes
# ---------------------------------------------------------------------------


def effective_workstreams(
    work_block_records: Iterable[WorkBlockRecord], work_block_fixes: Iterable[WorkBlockFix]
) -> dict[str, str]:
    """Return the workstream each work block counts towards, by work block id.

    The one place to ask: analysis, export and `goodall status` all use it.
    A work block counts towards the workstream in its own record unless a
    work block fix gives another; with several fixes, the one written last
    wins (ties, in the same second, go to the larger id, so the answer
    never depends on the order files were found). Every fix is still kept in
    the record repo. A fix that leaves the workstream alone, or names a work
    block not among `work_block_records`, changes nothing. The result can
    still be `unknown` (UNKNOWN_WORKSTREAM) for a work block nobody has
    fixed yet.
    """
    workstreams: dict[str, str] = {}
    for work_block in work_block_records:
        workstreams[work_block.id] = work_block.workstream
    latest_fix_by_work_block: dict[str, WorkBlockFix] = {}
    for fix in work_block_fixes:
        if fix.workstream is None or fix.work_block not in workstreams:
            continue
        latest = latest_fix_by_work_block.get(fix.work_block)
        if latest is None or (fix.written_at, fix.id) > (latest.written_at, latest.id):
            latest_fix_by_work_block[fix.work_block] = fix
    for work_block_id, fix in latest_fix_by_work_block.items():
        assert fix.workstream is not None
        workstreams[work_block_id] = fix.workstream
    return workstreams


# ---------------------------------------------------------------------------
# Timestamps and text
# ---------------------------------------------------------------------------

# A UTC timestamp in the one form records use: whole seconds and a Z.
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
EXAMPLE_TIMESTAMP = "2026-11-16T09:30:00Z"


def _as_utc(moment: datetime) -> datetime:
    """Convert a time to UTC and drop fractions of a second.

    Whole seconds keep files readable and make render then parse give back an
    equal value. A time with no time zone is left alone so validation can
    report it, because guessing its zone would silently shift it.
    """
    moment = moment.replace(microsecond=0)
    if moment.utcoffset() is None:
        return moment
    return moment.astimezone(UTC)


def _format_timestamp(moment: datetime) -> str:
    """Write a UTC time as `2026-11-16T09:30:00Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tidy_body(body: str) -> str:
    """Drop blank lines at the start and end of a body.

    The file puts one blank line after the header and one newline at the end,
    and parsing strips them again, so a tidy body survives the round trip.
    """
    return body.replace("\r\n", "\n").strip("\n")
