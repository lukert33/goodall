"""What only goodall knows, gathered for the Scout export, as plain values.

`goodall export scout` builds an Inspect Scout database in two halves. Scout's
own `claude_code` importer turns the copied Claude Code transcripts into Scout
transcripts (goodall.scout_database). This module gathers the other half:
goodall's own records from the record repo (work blocks, journal entries,
interventions), the human actions and actor labels goodall derives from the
same transcripts, and the prompts the prompt gate refused, which Scout's
importer drops. It describes each addition in Inspect's words (an approval
event, an info event, transcript metadata) but only as plain Python values.

Who calls it: goodall.scout_export, which hands the result to
goodall.scout_database. It imports only the standard library and goodall's
own standard-library modules, so it can be read, tested and imported without
the optional `scout` extra (AGENTS.md invariant 8); only
goodall.scout_database turns these values into Inspect objects.

Design decisions a reader would otherwise reverse-engineer:

- Approvals and refusals of a tool call become approval events; the call
  itself is filled in later from Scout's own tool event with the same id.
  Journal entries and interventions have no tool call, so they are info
  events, never approval events (owner decision 2026-11-17, Q11).
- An answer to the agent's question tool becomes an input event and an
  interruption an interrupt event, because those are Inspect's events for
  exactly those moments. Every other kind of human action is an info event
  whose `source` is "goodall.human_action".
- Every added event carries goodall's metadata keys (all starting
  `goodall_`), including `goodall_actor_label`, so events can be filtered by
  who produced them. Scout's own events are left without labels: the
  importer does not keep which transcript line an event came from, so a
  label could not be attached without guessing.
- goodall's records also get transcripts of their own: one per work block
  (`goodall-work-block-<id>`), holding the work block, its journal entries
  and its interventions, and one (`goodall-outside-work-blocks`) for records
  with no recorded work block and for prompts refused while no work block
  was active. A journal entry belongs to a work block, not to any one
  Claude session, so it would otherwise have nowhere to go. An intervention
  that names a Claude session is also added to that session's transcript.
- Event ids are made from where the thing was found, never at random, so
  exporting again gives the same ids: the database is derived data and
  must be regenerable (ADR 0003).
- Values that are the same for every transcript of the eval, such as the
  rule versions, are repeated on each one, because Scout filters
  transcripts by their own metadata columns.
"""

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any, Literal

from goodall import records
from goodall.actor_labels import ACTOR_LABEL_RULES_VERSION, label_transcript
from goodall.claude_family import TRANSCRIPTS_FOLDER_NAME
from goodall.human_actions import (
    RULES_VERSION as HUMAN_ACTION_RULES_VERSION,
)
from goodall.human_actions import (
    Interruption,
    PermissionPromptDismissal,
    PlanApproval,
    QuestionAnswer,
    ToolCallApproval,
    ToolCallRejection,
    parse_human_actions,
)
from goodall.human_activity import HumanAction, MergedInterruption, category_of, merge_repeated_interruptions
from goodall.terminal import Failure

# Raise this whenever the mapping below changes (which record becomes which
# event, or which metadata key means what), so a database says which mapping
# made it.
EXPORT_RULES_VERSION = "1"

# The transcript that holds goodall's records with no recorded work block.
OUTSIDE_WORK_BLOCKS_TRANSCRIPT_ID = "goodall-outside-work-blocks"
WORK_BLOCK_TRANSCRIPT_PREFIX = "goodall-work-block-"

# Inspect's name for whoever made an approval decision. Inspect's own human
# approver is called "human" too.
HUMAN_APPROVER = "human"

# Inspect's InterruptEvent has no value for "the person pressed Escape in
# Claude Code". `user_cancel`, a client cancelling a running turn, is the
# nearest one.
PERSON_INTERRUPTED = "user_cancel"

EventType = Literal["approval", "info", "input", "interrupt"]


@dataclass(frozen=True)
class AddedEvent:
    """One event goodall adds to the database, as plain values in Inspect's words.

    `fields` holds the Inspect event's own fields (for an approval:
    `decision`, `approver`, `explanation`; for an info event: `source` and
    `data`; and so on). An approval's `call` is not here: goodall.scout_database
    fills it from Scout's tool event with id `tool_call_id`, and falls back to
    `tool_name` with no arguments when there is none. `timestamp` is None when
    the raw record gave no readable time; the event then goes at the start.
    """

    event_type: EventType
    uuid: str
    timestamp: datetime | None
    fields: dict[str, Any]
    metadata: dict[str, Any]
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass
class GoodallTranscript:
    """A transcript of goodall's own records: one work block, or the records outside any."""

    transcript_id: str
    date: datetime | None
    metadata: dict[str, Any]
    events: list[AddedEvent]


@dataclass
class ClaudeSessionAdditions:
    """What goodall adds to the Scout transcript of one Claude session.

    `human_action_events` come from the session's own transcript files.
    `intervention_events` are copies of interventions that name the session;
    each one is also in its work block's transcript, or in the one for records
    outside work blocks, so a session Scout made no transcript of loses none.
    `work_blocks` are the ids of the work blocks whose records list the session.
    """

    session_id: str
    metadata: dict[str, Any]
    human_action_events: list[AddedEvent]
    intervention_events: list[AddedEvent]
    work_blocks: list[str]


@dataclass
class ScoutAdditions:
    """Everything this module gathered, ready for goodall.scout_database.

    `record_counts` counts the records that could be read, by kind.
    `warnings` name every record or transcript that could not be read; the
    export goes on without it and says so.
    """

    claude_sessions: dict[str, ClaudeSessionAdditions]
    work_block_transcripts: list[GoodallTranscript]
    outside_work_blocks: GoodallTranscript
    record_counts: dict[str, int]
    rules_versions: dict[str, str]
    warnings: list[Failure] = field(default_factory=list)


def rules_versions() -> dict[str, str]:
    """The version of every set of rules that decides what goes into the database."""
    return {
        "actor_labels": ACTOR_LABEL_RULES_VERSION,
        "human_actions": HUMAN_ACTION_RULES_VERSION,
        "export": EXPORT_RULES_VERSION,
    }


def gather_additions(record_repo: Path, log_directory: Path, eval_name: str) -> ScoutAdditions:
    """Read goodall's records and derive human actions and labels, for the Scout export.

    This is the module's entry point. It reads the record repo's record
    folders and the transcript files under `<log directory>/transcripts`;
    nothing else in the log directory, so earlier copies in
    `superseded-transcripts/` never count twice. It writes nothing.
    """
    warnings: list[Failure] = []
    work_blocks = read_records(record_repo, records.WORK_BLOCKS_FOLDER, "*.json", records.parse_work_block_record)
    fixes = read_records(record_repo, records.WORK_BLOCK_FIXES_FOLDER, "*.json", records.parse_work_block_fix)
    journal_entries = read_records(record_repo, records.JOURNAL_FOLDER, "*.md", records.parse_journal_entry)
    interventions = read_records(record_repo, records.INTERVENTIONS_FOLDER, "*.md", records.parse_intervention)
    warnings.extend(work_blocks.warnings)
    warnings.extend(fixes.warnings)
    warnings.extend(journal_entries.warnings)
    warnings.extend(interventions.warnings)
    workstreams = records.effective_workstreams(work_blocks.records, fixes.records)
    versions = rules_versions()

    work_blocks_by_session: dict[str, list[records.WorkBlockRecord]] = {}
    for work_block in work_blocks.records:
        for session_id in work_block.claude_sessions:
            work_blocks_by_session.setdefault(session_id, []).append(work_block)

    claude_sessions: dict[str, ClaudeSessionAdditions] = {}
    transcripts_folder = log_directory / TRANSCRIPTS_FOLDER_NAME
    for session_file in main_session_files(transcripts_folder):
        session_id = session_file.stem
        sessions_work_blocks = work_blocks_by_session.get(session_id, [])
        claude_sessions[session_id] = session_additions(
            session_file,
            log_directory,
            eval_name,
            sessions_work_blocks,
            workstreams,
            interventions.records,
            versions,
        )

    work_block_ids = set()
    for work_block in work_blocks.records:
        work_block_ids.add(work_block.id)
    work_block_transcripts = []
    for work_block in sorted(work_blocks.records, key=lambda record: (record.started_at, record.id)):
        block_journal_entries = []
        for journal_entry in journal_entries.records:
            if journal_entry.work_block == work_block.id:
                block_journal_entries.append(journal_entry)
        block_interventions = []
        for intervention in interventions.records:
            if intervention.work_block == work_block.id:
                block_interventions.append(intervention)
        work_block_transcripts.append(
            work_block_transcript(
                work_block,
                workstreams[work_block.id],
                block_journal_entries,
                block_interventions,
                eval_name,
                versions,
            )
        )

    outside_journal_entries = []
    for journal_entry in journal_entries.records:
        if journal_entry.work_block not in work_block_ids:
            outside_journal_entries.append(journal_entry)
    outside_interventions = []
    for intervention in interventions.records:
        if intervention.work_block not in work_block_ids:
            outside_interventions.append(intervention)
    outside = outside_work_blocks_transcript(outside_journal_entries, outside_interventions, eval_name, versions)

    return ScoutAdditions(
        claude_sessions=claude_sessions,
        work_block_transcripts=work_block_transcripts,
        outside_work_blocks=outside,
        record_counts={
            "work_blocks": len(work_blocks.records),
            "journal_entries": len(journal_entries.records),
            "interventions": len(interventions.records),
        },
        rules_versions=versions,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Reading goodall's records
# ---------------------------------------------------------------------------


@dataclass
class RecordReading[RecordType]:
    """The records of one kind that could be read, and a warning for each file that could not."""

    records: list[RecordType]
    warnings: list[Failure]


def read_records[RecordType](
    record_repo: Path,
    folder_name: str,
    pattern: str,
    parse: Callable[[str], records.ParseResult[RecordType]],
) -> RecordReading[RecordType]:
    """Read every record file in one folder of the record repo, oldest name first.

    `parse` is one of the `records.parse_...` functions. A file that cannot
    be read, or that does not parse into a record, is left out with a
    warning naming it; problems that still let a record be built (an unknown
    key, say) are not the export's business, and `goodall doctor` reports them.
    """
    reading: RecordReading[RecordType] = RecordReading(records=[], warnings=[])
    folder = record_repo / folder_name
    if not folder.is_dir():
        return reading
    for record_file in sorted(folder.glob(pattern)):
        try:
            text = record_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            reading.warnings.append(record_unreadable_warning(record_repo, record_file))
            continue
        parse_result = parse(text)
        if parse_result.record is None:
            reading.warnings.append(record_unreadable_warning(record_repo, record_file))
            continue
        reading.records.append(parse_result.record)
    return reading


def record_unreadable_warning(record_repo: Path, record_file: Path) -> Failure:
    """The warning for a record file the export had to leave out. It names the file, never its content."""
    relative_path = record_file.relative_to(record_repo)
    return Failure(
        kind="record_unreadable",
        what_happened=f"The record {relative_path} in the record repo could not be read, so it is not in the export.",
        why="The file is not in the shape goodall writes, or this macOS user account cannot read it.",
        what_to_do_next=(
            "Run goodall doctor to see what is wrong with it. Once it is fixed, run goodall export scout again: "
            "the database is rebuilt from the records each time."
        ),
    )


# ---------------------------------------------------------------------------
# Claude sessions: human actions, actor labels and metadata
# ---------------------------------------------------------------------------


def main_session_files(transcripts_folder: Path) -> list[Path]:
    """Return every Claude session's own transcript, `<project folder>/<session id>.jsonl`, in name order.

    Subagent transcripts sit inside the session's own folder and are read
    with their session (see `subagent_files`), not as sessions of their own.
    """
    if not transcripts_folder.is_dir():
        return []
    return sorted(transcripts_folder.glob("*/*.jsonl"))


def subagent_files(session_file: Path) -> list[Path]:
    """Return the transcripts of a Claude session's subagents, from `<session id>/subagents/`."""
    subagents_folder = session_file.parent / session_file.stem / "subagents"
    if not subagents_folder.is_dir():
        return []
    return sorted(subagents_folder.glob("*.jsonl"))


def session_additions(
    session_file: Path,
    log_directory: Path,
    eval_name: str,
    work_blocks: list[records.WorkBlockRecord],
    workstreams: dict[str, str],
    interventions: list[records.Intervention],
    versions: dict[str, str],
) -> ClaudeSessionAdditions:
    """Gather what goodall adds to one Claude session's Scout transcript.

    Human actions are read from the session's transcript and its subagents'
    together, so an interruption written in both counts once
    (`merge_repeated_interruptions`). Actor labels are counted for the
    session's own transcript only, which is what Scout's transcript shows
    as its messages.
    """
    session_id = session_file.stem
    actions: list[HumanAction] = []
    unfamiliar_entries = 0
    for transcript_file in [session_file, *subagent_files(session_file)]:
        parse_result = parse_human_actions(transcript_file)
        actions.extend(parse_result.actions)
        unfamiliar_entries += len(parse_result.unfamiliar_entries)
    actions = merge_repeated_interruptions(actions)

    human_action_events = []
    actions_by_kind: dict[str, int] = {}
    refused_prompts = 0
    for action in actions:
        human_action_events.append(event_for_human_action(action, log_directory))
        actions_by_kind[action.kind] = actions_by_kind.get(action.kind, 0) + 1
        if action.kind == "blocked_prompt":
            refused_prompts += 1

    labelled = label_transcript(session_file)
    entries_by_label = {"human": 0, "ai": 0, "harness": 0}
    for label in labelled.labels:
        entries_by_label[label.actor_label] += 1

    session_interventions = []
    for intervention in interventions:
        if intervention.claude_session == session_id:
            session_interventions.append(intervention)
    intervention_events = []
    for intervention in session_interventions:
        intervention_events.append(event_for_intervention(intervention))

    work_block_ids = []
    session_workstreams: list[str] = []
    contributors: list[str] = []
    for work_block in work_blocks:
        work_block_ids.append(work_block.id)
        add_once(session_workstreams, workstreams[work_block.id])
        add_once(contributors, work_block.contributor)

    metadata: dict[str, Any] = {
        "goodall_eval": eval_name,
        "goodall_transcript_kind": "claude_code",
        "goodall_claude_session": session_id,
    }
    metadata.update(work_block_metadata(work_block_ids, session_workstreams, contributors))
    metadata.update(intervention_metadata(session_interventions))
    metadata.update(
        {
            "goodall_human_entries": entries_by_label["human"],
            "goodall_ai_entries": entries_by_label["ai"],
            "goodall_harness_entries": entries_by_label["harness"],
            "goodall_unlabelled_entries": len(labelled.unmatched),
            "goodall_human_actions": len(actions),
            "goodall_human_actions_by_kind": actions_by_kind,
            "goodall_refused_prompts": refused_prompts,
            "goodall_unfamiliar_entries": unfamiliar_entries,
        }
    )
    metadata.update(version_metadata(versions))
    return ClaudeSessionAdditions(
        session_id=session_id,
        metadata=metadata,
        human_action_events=human_action_events,
        intervention_events=intervention_events,
        work_blocks=work_block_ids,
    )


def work_block_metadata(work_block_ids: list[str], workstreams: list[str], contributors: list[str]) -> dict[str, Any]:
    """The metadata saying which work blocks a transcript belongs to.

    `goodall_work_block` is the one work block when there is exactly one, and
    None otherwise, so that the everyday filter is a plain comparison. The
    lists hold every one, for a Claude session resumed in a later work block.
    Workstream and contributor follow the same pattern.
    """
    return {
        "goodall_work_block": only_value(work_block_ids),
        "goodall_work_blocks": work_block_ids,
        "goodall_workstream": only_value(workstreams),
        "goodall_workstreams": workstreams,
        "goodall_contributor": only_value(contributors),
        "goodall_contributors": contributors,
    }


def intervention_metadata(interventions: Sequence[records.Intervention]) -> dict[str, Any]:
    """The metadata for filtering by intervention: how many, and which categories and decisions, each once."""
    categories: list[str] = []
    decisions: list[str] = []
    for intervention in interventions:
        add_once(categories, intervention.category)
        add_once(decisions, intervention.decision)
    return {
        "goodall_interventions": len(interventions),
        "goodall_intervention_categories": sorted(categories),
        "goodall_intervention_decisions": sorted(decisions),
    }


def version_metadata(versions: dict[str, str]) -> dict[str, Any]:
    """The rule versions, as metadata, so a database says which rules made it."""
    return {
        "goodall_actor_label_rules_version": versions["actor_labels"],
        "goodall_human_action_rules_version": versions["human_actions"],
        "goodall_export_rules_version": versions["export"],
    }


def only_value(values: list[str]) -> str | None:
    """The single value in `values`, or None when there are none or several."""
    if len(values) == 1:
        return values[0]
    return None


def add_once(values: list[str], value: str) -> None:
    """Append `value` unless it is already there, keeping first-seen order."""
    if value not in values:
        values.append(value)


# ---------------------------------------------------------------------------
# Human actions as events
# ---------------------------------------------------------------------------


def event_for_human_action(action: HumanAction, log_directory: Path) -> AddedEvent:
    """Describe one human action as the Inspect event it most nearly is.

    Approvals and refusals of a tool call are approval events, an answer to
    the agent's question is an input event, an interruption is an interrupt
    event, and every other kind is an info event carrying the action's own
    fields.
    """
    place = place_of(action, log_directory)
    metadata = {
        "goodall_actor_label": "human",
        "goodall_kind": action.kind,
        "goodall_category": category_of(action.kind),
        "goodall_rule": action.rule,
        "goodall_source": action.source,
        "goodall_transcript_file": place.transcript_file,
        "goodall_line_number": place.line_number,
    }
    uuid = stable_id("human-action", place.transcript_file, str(place.line_number), action.kind)
    timestamp = parse_timestamp(action.timestamp)

    if isinstance(action, ToolCallApproval | PlanApproval | ToolCallRejection | PermissionPromptDismissal):
        explanation = None
        if isinstance(action, ToolCallRejection):
            explanation = action.feedback
        tool_name = None
        if not isinstance(action, PlanApproval):
            tool_name = action.tool_name
        return AddedEvent(
            event_type="approval",
            uuid=uuid,
            timestamp=timestamp,
            fields={"decision": action.decision, "approver": HUMAN_APPROVER, "explanation": explanation},
            metadata=metadata,
            tool_call_id=action.tool_call_id,
            tool_name=tool_name,
        )
    if isinstance(action, QuestionAnswer):
        answer_text = "\n".join(action.answers)
        return AddedEvent(
            event_type="input",
            uuid=uuid,
            timestamp=timestamp,
            fields={"input": answer_text, "input_ansi": answer_text, "outcome": "accepted"},
            metadata=metadata,
        )
    if isinstance(action, Interruption | MergedInterruption):
        interrupted = action.stands_for[0].interrupted if isinstance(action, MergedInterruption) else action.interrupted
        return AddedEvent(
            event_type="interrupt",
            uuid=uuid,
            timestamp=timestamp,
            fields={
                "source": PERSON_INTERRUPTED,
                "interrupted": interrupted,
                "interrupted_tool_call_id": action.interrupted_tool_call_id,
            },
            metadata=metadata,
        )
    return AddedEvent(
        event_type="info",
        uuid=uuid,
        timestamp=timestamp,
        fields={"source": "goodall.human_action", "data": plain_fields(action)},
        metadata=metadata,
    )


@dataclass(frozen=True)
class ActionPlace:
    """Where an action was found: its transcript, relative to the log directory, and line."""

    transcript_file: str | None
    line_number: int | None


def place_of(action: HumanAction, log_directory: Path) -> ActionPlace:
    """Say where an action was found. A merged interruption is placed where its first original was."""
    if isinstance(action, MergedInterruption):
        action = action.stands_for[0]
    transcript_path = getattr(action, "transcript_path", None)
    line_number = getattr(action, "line_number", None)
    transcript_file = None
    if isinstance(transcript_path, Path):
        try:
            transcript_file = transcript_path.relative_to(log_directory).as_posix()
        except ValueError:
            transcript_file = transcript_path.name
    return ActionPlace(transcript_file=transcript_file, line_number=line_number)


# ---------------------------------------------------------------------------
# goodall's records as events and transcripts
# ---------------------------------------------------------------------------


def event_for_work_block(work_block: records.WorkBlockRecord, workstream: str) -> AddedEvent:
    """Describe a work block record as an info event at its start. goodall wrote it, so its label is harness."""
    data = plain_fields(work_block)
    data["effective_workstream"] = workstream
    return AddedEvent(
        event_type="info",
        uuid=stable_id("record", work_block.id),
        timestamp=work_block.started_at,
        fields={"source": "goodall.work_block", "data": data},
        metadata={
            "goodall_actor_label": "harness",
            "goodall_record_type": records.WORK_BLOCK_RECORD_TYPE,
            "goodall_record_id": work_block.id,
        },
    )


def event_for_journal_entry(journal_entry: records.JournalEntry) -> AddedEvent:
    """Describe a journal entry as an info event at the time it was written. A person wrote it."""
    return AddedEvent(
        event_type="info",
        uuid=stable_id("record", journal_entry.id),
        timestamp=journal_entry.written_at,
        fields={"source": "goodall.journal_entry", "data": plain_fields(journal_entry)},
        metadata={
            "goodall_actor_label": "human",
            "goodall_record_type": records.JOURNAL_ENTRY_RECORD_TYPE,
            "goodall_record_id": journal_entry.id,
        },
    )


def event_for_intervention(intervention: records.Intervention) -> AddedEvent:
    """Describe an intervention as an info event at the time it happened. A person wrote it.

    Not an approval event, although an intervention has a `decision`: an
    approval event needs the tool call that was decided, and a hand-logged
    intervention names none.
    """
    return AddedEvent(
        event_type="info",
        uuid=stable_id("record", intervention.id),
        timestamp=intervention.occurred_at,
        fields={"source": "goodall.intervention", "data": plain_fields(intervention)},
        metadata={
            "goodall_actor_label": "human",
            "goodall_record_type": records.INTERVENTION_RECORD_TYPE,
            "goodall_record_id": intervention.id,
            "goodall_intervention_category": intervention.category,
            "goodall_decision": intervention.decision,
        },
    )


def work_block_transcript(
    work_block: records.WorkBlockRecord,
    workstream: str,
    journal_entries: list[records.JournalEntry],
    interventions: list[records.Intervention],
    eval_name: str,
    versions: dict[str, str],
) -> GoodallTranscript:
    """Build the transcript of one work block's own records, in time order."""
    events = [event_for_work_block(work_block, workstream)]
    for journal_entry in journal_entries:
        events.append(event_for_journal_entry(journal_entry))
    for intervention in interventions:
        events.append(event_for_intervention(intervention))
    metadata: dict[str, Any] = {
        "goodall_eval": eval_name,
        "goodall_transcript_kind": "work_block",
        "goodall_claude_session": None,
    }
    metadata.update(work_block_metadata([work_block.id], [workstream], [work_block.contributor]))
    metadata.update(intervention_metadata(interventions))
    metadata.update(
        {
            "goodall_started_at": format_timestamp(work_block.started_at),
            "goodall_ended_at": format_timestamp(work_block.ended_at),
            "goodall_end_state": work_block.end_state,
            "goodall_claude_sessions": list(work_block.claude_sessions),
            "goodall_journal_entries": len(journal_entries),
        }
    )
    metadata.update(version_metadata(versions))
    return GoodallTranscript(
        transcript_id=WORK_BLOCK_TRANSCRIPT_PREFIX + work_block.id,
        date=work_block.started_at,
        metadata=metadata,
        events=in_time_order(events),
    )


def outside_work_blocks_transcript(
    journal_entries: list[records.JournalEntry],
    interventions: list[records.Intervention],
    eval_name: str,
    versions: dict[str, str],
) -> GoodallTranscript:
    """Build the transcript for records with no recorded work block.

    That is a journal entry or intervention written with no work block, or
    naming one whose record is not in the record repo yet (a work block still
    running). goodall.scout_database adds prompts refused while no work block
    was active, from Claude sessions Scout made no transcript of.
    """
    events = []
    for journal_entry in journal_entries:
        events.append(event_for_journal_entry(journal_entry))
    for intervention in interventions:
        events.append(event_for_intervention(intervention))
    contributors: list[str] = []
    for journal_entry in journal_entries:
        add_once(contributors, journal_entry.contributor)
    for intervention in interventions:
        add_once(contributors, intervention.contributor)
    metadata: dict[str, Any] = {
        "goodall_eval": eval_name,
        "goodall_transcript_kind": "outside_work_blocks",
        "goodall_claude_session": None,
    }
    metadata.update(work_block_metadata([], [], contributors))
    metadata.update(intervention_metadata(interventions))
    metadata["goodall_journal_entries"] = len(journal_entries)
    metadata.update(version_metadata(versions))
    return GoodallTranscript(
        transcript_id=OUTSIDE_WORK_BLOCKS_TRANSCRIPT_ID,
        date=None,
        metadata=metadata,
        events=in_time_order(events),
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def in_time_order(events: Iterable[AddedEvent]) -> list[AddedEvent]:
    """Sort events oldest first; an event with no time goes first, and ties keep their order."""
    oldest_possible = datetime.min.replace(tzinfo=UTC)
    return sorted(events, key=lambda event: event.timestamp or oldest_possible)


def stable_id(*parts: str | None) -> str:
    """An event id made from where the thing was found, so exporting again gives the same id."""
    joined = "\x1f".join(part or "" for part in parts)
    return "goodall-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def parse_timestamp(text: str | None) -> datetime | None:
    """Read a timestamp as Claude Code writes it, in UTC; None when it is missing or unreadable."""
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def format_timestamp(moment: datetime | None) -> str | None:
    """Write a record's timestamp as records do: UTC, whole seconds, with a Z."""
    if moment is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plain_fields(record: Any) -> dict[str, Any]:
    """Turn a record or action into a dictionary of JSON values, for an info event's `data`.

    A transcript path is left out, because it says where a copy was read
    from on this Mac, and `goodall_transcript_file` in the metadata already
    says where in the log directory it was found.
    """
    data: dict[str, Any] = {}
    for record_field in fields(record):
        if record_field.name == "transcript_path":
            continue
        data[record_field.name] = plain_value(getattr(record, record_field.name))
    return data


def plain_value(value: Any) -> Any:
    """Turn one field's value into a JSON value: times as record timestamps, records as dictionaries."""
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, PurePath):
        return value.as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        return plain_fields(value)
    if isinstance(value, tuple | list):
        items = []
        for item in value:
            items.append(plain_value(item))
        return items
    if isinstance(value, dict):
        plain_dictionary = {}
        for key, item in value.items():
            plain_dictionary[str(key)] = plain_value(item)
        return plain_dictionary
    return value
