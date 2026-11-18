"""Build the Inspect Scout database: Scout's own import, plus what only goodall knows.

The one goodall module that imports inspect-ai and inspect-scout, the
optional `scout` extra (AGENTS.md invariant 8). goodall.scout_export imports
it only when `goodall export scout` runs, and tells the person how to install
the extra when the import fails, so nothing else in goodall needs the extra.

Design decisions a reader would otherwise reverse-engineer:

- Scout's `claude_code` importer is called through its Python API, in this
  process, not by running the `scout import` command through the runner.
  Scout's database never overwrites a transcript it already holds, so
  goodall's additions have to be in each transcript before it is written;
  calling the importer directly hands goodall each transcript on its way to
  the database. It also starts no program, so the runner gateway (ADR 0011)
  has nothing to run. goodall does not convert transcripts itself.
- The importer is always given `<log directory>/transcripts`. Given no path
  it would read the person's own Claude Code folder in their home folder.
  `superseded-transcripts/` sits beside it and is never read (ADR 0015).
- Every Scout transcript is held in memory until all are read, because a
  human action is placed in the part of its Claude session it happened in,
  and a session split by /clear arrives as several transcripts. An eval's
  sessions are expected to fit; a very large eval would need this to stream.
- Added events go where they happened: an approval just before the tool
  event it decided, in the same span; anything else before the first event
  that happened later. Each takes `working_start` from its neighbour,
  because Scout's own values for imported transcripts count from no fixed
  moment.
"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import inspect_ai
import inspect_scout
from inspect_ai.event import ApprovalEvent, Event, InfoEvent, InputEvent, InterruptEvent, ToolEvent
from inspect_ai.tool import ToolCall
from inspect_scout import Transcript, transcripts_db
from inspect_scout.sources import claude_code

from goodall.scout_additions import (
    WORK_BLOCK_TRANSCRIPT_PREFIX,
    AddedEvent,
    ClaudeSessionAdditions,
    GoodallTranscript,
    ScoutAdditions,
    in_time_order,
)

# The source type of goodall's own transcripts, beside Scout's "claude_code".
GOODALL_SOURCE_TYPE = "goodall"

# The time given to an event when neither it nor its transcript has one.
NO_TIME = datetime.fromtimestamp(0, UTC)


@dataclass
class DatabaseSummary:
    """What went into the database, counted for the person who asked for it."""

    claude_code_transcripts: int = 0
    work_block_transcripts: int = 0
    outside_work_blocks_transcripts: int = 0
    events_added: dict[str, int] = field(default_factory=lambda: {"approval": 0, "info": 0, "input": 0, "interrupt": 0})


def package_versions() -> dict[str, str]:
    """The versions of the Inspect packages that built the database."""
    return {"inspect_ai": inspect_ai.__version__, "inspect_scout": inspect_scout.__version__}


def build_database(transcripts_folder: Path, database_folder: Path, additions: ScoutAdditions) -> DatabaseSummary:
    """Write a new Scout database in `database_folder` and return what went into it.

    `database_folder` should be new and empty: Scout keeps any transcript a
    folder already holds, so writing twice would keep stale copies.
    """
    return asyncio.run(build_database_async(transcripts_folder, database_folder, additions))


async def build_database_async(
    transcripts_folder: Path, database_folder: Path, additions: ScoutAdditions
) -> DatabaseSummary:
    """The asynchronous body of `build_database`; Scout's importer and database are asynchronous."""
    summary = DatabaseSummary()
    scout_transcripts = []
    if transcripts_folder.is_dir():
        async for transcript in claude_code(path=str(transcripts_folder)):
            scout_transcripts.append(transcript)

    placed_events = place_session_events(scout_transcripts, additions.claude_sessions.values())
    finished: list[Transcript] = []
    for transcript in scout_transcripts:
        events_to_add = placed_events.get(transcript.transcript_id, [])
        finished.append(with_goodall_additions(transcript, additions, events_to_add, summary))
        summary.claude_code_transcripts += 1

    unplaced_by_work_block, unplaced_outside = unplaced_session_events(scout_transcripts, additions)
    for work_block_transcript in additions.work_block_transcripts:
        extra_events = unplaced_by_work_block.get(work_block_transcript.transcript_id, [])
        finished.append(goodall_transcript(work_block_transcript, extra_events, summary))
        summary.work_block_transcripts += 1
    outside = additions.outside_work_blocks
    if outside.events or unplaced_outside:
        finished.append(goodall_transcript(outside, unplaced_outside, summary))
        summary.outside_work_blocks_transcripts += 1

    database_folder.mkdir(parents=True, exist_ok=True)
    async with transcripts_db(str(database_folder)) as database:
        await database.insert(as_async(finished))
    return summary


async def as_async(transcripts: Iterable[Transcript]) -> AsyncIterator[Transcript]:
    """Hand a list to Scout's database, which reads transcripts as they arrive."""
    for transcript in transcripts:
        yield transcript


# ---------------------------------------------------------------------------
# Which Scout transcript each added event belongs in
# ---------------------------------------------------------------------------


def sessions_of(transcript: Transcript) -> list[str]:
    """The Claude session ids a Scout transcript was made from.

    Scout's importer merges the sessions of a plan and its execution into one
    transcript and lists them in `session_ids`; otherwise `source_id` is the
    one session.
    """
    session_ids = transcript.metadata.get("session_ids")
    if isinstance(session_ids, list) and session_ids:
        found = []
        for session_id in session_ids:
            found.append(str(session_id))
        return found
    if transcript.source_id is None:
        return []
    return [transcript.source_id]


def start_of(transcript: Transcript) -> datetime | None:
    """When a transcript's first event happened, or None for one with no events."""
    if not transcript.events:
        return None
    return transcript.events[0].timestamp


def place_session_events(
    scout_transcripts: list[Transcript], sessions: Iterable[ClaudeSessionAdditions]
) -> dict[str, list[AddedEvent]]:
    """Decide which Scout transcript each session's added events go in, by transcript id.

    A Claude session split by /clear becomes several Scout transcripts. Each
    event goes in the last part that started at or before it, or the first
    part when it came before them all (a prompt refused just before the first
    accepted one) or has no time.
    """
    parts_by_session: dict[str, list[Transcript]] = {}
    for transcript in scout_transcripts:
        for session_id in sessions_of(transcript):
            parts_by_session.setdefault(session_id, []).append(transcript)

    placed: dict[str, list[AddedEvent]] = {}
    for session in sessions:
        parts = parts_by_session.get(session.session_id, [])
        if not parts:
            continue
        timed_parts = []
        for part in parts:
            timed_parts.append((start_of(part), part))
        for event in [*session.human_action_events, *session.intervention_events]:
            chosen = parts[0]
            for part_start, part in timed_parts:
                if event.timestamp is not None and part_start is not None and part_start <= event.timestamp:
                    chosen = part
            placed.setdefault(chosen.transcript_id, []).append(event)
    return placed


def unplaced_session_events(
    scout_transcripts: list[Transcript], additions: ScoutAdditions
) -> tuple[dict[str, list[AddedEvent]], list[AddedEvent]]:
    """Find the human actions of Claude sessions Scout made no transcript of, and where they go instead.

    That is, nearly always, a session whose only prompt the prompt gate
    refused: Scout's importer skips a session with no messages. Its actions
    go in the transcript of the session's work block when its record lists
    one, and otherwise in the one for records outside work blocks. Copies of
    interventions are dropped, because the intervention itself is already in
    one of those two.
    """
    imported_sessions = set()
    for transcript in scout_transcripts:
        imported_sessions.update(sessions_of(transcript))
    by_work_block: dict[str, list[AddedEvent]] = {}
    outside: list[AddedEvent] = []
    for session in additions.claude_sessions.values():
        if session.session_id in imported_sessions or not session.human_action_events:
            continue
        if session.work_blocks:
            transcript_id = WORK_BLOCK_TRANSCRIPT_PREFIX + session.work_blocks[0]
            by_work_block.setdefault(transcript_id, []).extend(session.human_action_events)
        else:
            outside.extend(session.human_action_events)
    return by_work_block, outside


# ---------------------------------------------------------------------------
# Turning goodall's plain values into Inspect events and transcripts
# ---------------------------------------------------------------------------


def with_goodall_additions(
    transcript: Transcript, additions: ScoutAdditions, events_to_add: list[AddedEvent], summary: DatabaseSummary
) -> Transcript:
    """Return a Scout transcript with goodall's events merged in and its metadata added.

    goodall's metadata describes the whole Claude session, so every part of
    a session split by /clear carries the same values; with several merged
    sessions, the first one's.
    """
    metadata = dict(transcript.metadata)
    for session_id in sessions_of(transcript):
        session = additions.claude_sessions.get(session_id)
        if session is not None:
            metadata.update(session.metadata)
            break
    events = merge_events(transcript.events, events_to_add, summary)
    return transcript.model_copy(update={"metadata": metadata, "events": events})


def merge_events(scout_events: list[Event], events_to_add: list[AddedEvent], summary: DatabaseSummary) -> list[Event]:
    """Put each added event where it happened among Scout's events, keeping Scout's own order.

    An approval goes just before the tool event it decided, in its span; an
    approval whose tool event Scout did not keep, and every other event, goes
    before the first event that happened after it.
    """
    tool_event_positions: dict[str, int] = {}
    for position, scout_event in enumerate(scout_events):
        if isinstance(scout_event, ToolEvent):
            tool_event_positions.setdefault(scout_event.id, position)

    start = start_of_events(scout_events)
    added_before: dict[int, list[Event]] = {}
    for added in events_to_add:
        if added.event_type == "approval" and added.tool_call_id in tool_event_positions:
            position = tool_event_positions[added.tool_call_id]
        else:
            position = position_by_time(scout_events, added.timestamp)
        neighbour: Event | None = None
        if position < len(scout_events):
            neighbour = scout_events[position]
        elif scout_events:
            neighbour = scout_events[-1]
        inspect_event = to_inspect_event(added, neighbour, start)
        added_before.setdefault(position, []).append(inspect_event)
        summary.events_added[added.event_type] += 1

    merged: list[Event] = []
    for position, scout_event in enumerate(scout_events):
        merged.extend(added_before.get(position, []))
        merged.append(scout_event)
    merged.extend(added_before.get(len(scout_events), []))
    return merged


def start_of_events(events: list[Event]) -> datetime | None:
    """When the first of `events` happened, or None when there are none."""
    if not events:
        return None
    return events[0].timestamp


def position_by_time(scout_events: list[Event], moment: datetime | None) -> int:
    """The position of the first event that happened after `moment`; 0 for no time, the end for the latest."""
    if moment is None:
        return 0
    for position, scout_event in enumerate(scout_events):
        if scout_event.timestamp > moment:
            return position
    return len(scout_events)


def to_inspect_event(added: AddedEvent, neighbour: Event | None, start: datetime | None) -> Event:
    """Build the Inspect event an `AddedEvent` describes.

    An approval's tool call comes from the neighbouring tool event when it is
    the one decided, which is how Inspect itself records an approval: the
    call as the model made it. When Scout kept no tool event for the call,
    the call has the tool's name and no arguments, and
    `goodall_tool_call_found` in its metadata says so.
    """
    # An event with no time of its own takes the transcript's start, or a
    # fixed moment, never the time now: exporting again must give the same.
    timestamp = added.timestamp
    if timestamp is None:
        timestamp = start if start is not None else NO_TIME
    working_start = 0.0
    span_id = None
    if neighbour is not None:
        working_start = neighbour.working_start
    common: dict[str, Any] = {
        "uuid": added.uuid,
        "timestamp": timestamp,
        "working_start": working_start,
        "metadata": dict(added.metadata),
    }
    if added.event_type == "approval":
        tool_event = None
        if isinstance(neighbour, ToolEvent) and neighbour.id == added.tool_call_id:
            tool_event = neighbour
        if tool_event is not None:
            call = ToolCall(id=tool_event.id, function=tool_event.function, arguments=dict(tool_event.arguments))
            span_id = tool_event.span_id
        else:
            call = ToolCall(id=added.tool_call_id or "", function=added.tool_name or "", arguments={})
        common["metadata"]["goodall_tool_call_found"] = tool_event is not None
        return ApprovalEvent(span_id=span_id, message="", call=call, **common, **added.fields)
    if added.event_type == "input":
        return InputEvent(**common, **added.fields)
    if added.event_type == "interrupt":
        return InterruptEvent(**common, **added.fields)
    return InfoEvent(**common, **added.fields)


def goodall_transcript(
    transcript: GoodallTranscript, extra_events: list[AddedEvent], summary: DatabaseSummary
) -> Transcript:
    """Build a Scout transcript of goodall's own records. It has events and no messages."""
    added = in_time_order([*transcript.events, *extra_events])
    start = transcript.date
    if start is None and added:
        start = added[0].timestamp
    events: list[Event] = []
    for added_event in added:
        inspect_event = to_inspect_event(added_event, None, start)
        if start is not None:
            inspect_event.working_start = max(0.0, (inspect_event.timestamp - start).total_seconds())
        events.append(inspect_event)
        summary.events_added[added_event.event_type] += 1
    date = None
    if start is not None:
        date = start.isoformat()
    eval_name = transcript.metadata.get("goodall_eval")
    return Transcript(
        transcript_id=transcript.transcript_id,
        source_type=GOODALL_SOURCE_TYPE,
        source_id=eval_name,
        date=date,
        task_set=eval_name,
        task_id=transcript.transcript_id,
        task_repeat=1,
        message_count=0,
        messages=[],
        events=events,
        metadata=dict(transcript.metadata),
    )
