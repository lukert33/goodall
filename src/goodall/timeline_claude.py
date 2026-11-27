"""Timeline entries from the Claude family's transcripts, as copied into the log directory.

Reads `<log directory>/transcripts/`, the copies goodall.claude_family
makes: each Claude session's transcript and its subagents' transcripts.
Nothing else there is read, so earlier copies kept in
`superseded-transcripts/` never count twice.

Who calls it: goodall.timeline, when it builds the timeline. The rules
below are numbered in docs/timeline.md; change both together and raise
TIMELINE_RULES_VERSION (goodall.timeline_entries).

How a transcript line becomes entries:

1. A line with no time (its top-level `timestamp`, read by the same rule
   that decides whether an agent session is copied) is in no entry. Titles,
   modes and other bookkeeping lines carry none.
2. Each human action goodall.human_actions finds on the line is an entry:
   the person did it. Its kind is the action's kind. A prompt sent through
   the SDK is the one exception to "the person": whether that is the
   person is undecided, so its actor is left empty.
3. Unless the line's actor label is `human`, the line itself also gives
   entries, labeled with its actor: one for each tool the model asked for
   (`tool_call`), each tool result (`tool_result`, with `ok` or `error`),
   each piece of the model's words (`message_text`) and of its reasoning
   (`thinking`). A line with none of these is one `entry`, whose
   `entry_type` and `label_rule` say what sort of line it was.
4. A line the person produced (rule 2 found an action and the label is
   `human`) gives no entry of its own: the action entries stand for it.
   So a tool result that carries the person's approval or refusal appears
   as that approval or refusal, not as a tool result as well.

What is read from a line and what is kept: the time, the type, the
`entrypoint`, the types of its content blocks, a tool's name (kept only
when goodall lists it), and whether a result was an error. No text of a
prompt, a reply, a tool's input or its output is kept, or shown in a
problem.
"""

import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from goodall import claude_family
from goodall.actor_labels import EntryLabel, label_transcript
from goodall.human_actions import parse_human_actions
from goodall.human_activity import HumanAction, category_of
from goodall.timeline_entries import (
    ENTRY_TYPES,
    IN_LOG_DIRECTORY,
    KIND_ENTRY,
    KIND_MESSAGE_TEXT,
    KIND_THINKING,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_UNFAMILIAR_ENTRY,
    OUTCOME_ERROR,
    OUTCOME_OK,
    SOURCE_CLAUDE,
    RawRecordPointer,
    SourceEntries,
    TimelineEntry,
    listed_source_application,
    listed_tool,
)
from goodall.verbatim_copies import describe_error, list_files_at, list_folders

# The human action whose actor is left empty (rule 2).
UNDECIDED_ACTION_KIND = "sdk_prompt"

# The folder, inside a Claude session's own folder, that holds its subagents' transcripts.
SUBAGENTS_FOLDER_NAME = "subagents"


def entries_from_transcripts(log_directory: Path) -> SourceEntries:
    """Every entry from every Claude session copied into the log directory, in no particular order.

    Never raises for anything on the disk: a transcript that cannot be read
    becomes a problem naming the file, and the rest are still read.
    """
    gathered = SourceEntries()
    transcripts_folder = log_directory / claude_family.TRANSCRIPTS_FOLDER_NAME
    try:
        project_folder_names = list_folders(transcripts_folder)
    except OSError as error:
        gathered.problems.append(f"The copied transcripts in the log directory: {describe_error(error)}.")
        return gathered
    for project_folder_name in project_folder_names:
        for relative_path, in_subagent in transcript_files(transcripts_folder, project_folder_name, gathered):
            try:
                gathered.add(entries_from_transcript(log_directory, relative_path, in_subagent))
            except OSError as error:
                gathered.problems.append(f"The copied transcript {relative_path}: {describe_error(error)}.")
    return gathered


def transcript_files(
    transcripts_folder: Path, project_folder_name: str, gathered: SourceEntries
) -> list[tuple[Path, bool]]:
    """The transcripts in one copied project folder, by path inside the log directory, each with whether a subagent's.

    A Claude session's own transcript is `<session id>.jsonl`; its
    subagents' are the `.jsonl` files under `<session id>/subagents/`, at
    any depth. Other `.jsonl` files, such as a tool's saved output, are not
    transcripts.
    """
    found: list[tuple[Path, bool]] = []
    copies_folder_name = Path(claude_family.TRANSCRIPTS_FOLDER_NAME)
    try:
        claude_sessions = claude_family.list_claude_sessions(transcripts_folder / project_folder_name)
    except OSError as error:
        gathered.problems.append(f"The copied project folder {project_folder_name}: {describe_error(error)}.")
        return found
    for claude_session in claude_sessions:
        transcript = Path(project_folder_name) / f"{claude_session}.jsonl"
        if (transcripts_folder / transcript).is_file():
            found.append((copies_folder_name / transcript, False))
        subagents_folder = Path(project_folder_name) / claude_session / SUBAGENTS_FOLDER_NAME
        subagent_files, _ = list_files_at(transcripts_folder, subagents_folder)
        for subagent_file in subagent_files:
            if subagent_file.suffix == ".jsonl":
                found.append((copies_folder_name / subagent_file, True))
    return found


def entries_from_transcript(log_directory: Path, relative_path: Path, in_subagent: bool) -> SourceEntries:
    """The entries from one copied transcript. Raises OSError when it cannot be read.

    The file is read as goodall.actor_labels reads it, so that the line
    numbers here, in the labels and in the human actions agree.
    """
    transcript = log_directory / relative_path
    raw_record = RawRecordPointer(IN_LOG_DIRECTORY, PurePosixPath(relative_path.as_posix()))
    labels_by_line: dict[int, EntryLabel] = {}
    for label in label_transcript(transcript).labels:
        labels_by_line[label.line_number] = label
    actions_by_line: dict[int, list[HumanAction]] = {}
    for action in parse_human_actions(transcript).actions:
        actions_by_line.setdefault(action.line_number, []).append(action)

    gathered = SourceEntries()
    # The name of each tool the model asked for, by the id its result names.
    tool_names_by_call_id: dict[str, str] = {}
    with transcript.open(encoding="utf-8", errors="replace") as lines:
        for line_number, line in enumerate(lines, start=1):
            if line.strip() == "":
                continue
            try:
                record = json.loads(line)
            except ValueError:
                gathered.without_a_time += 1
                continue
            started_at = claude_family.time_in_record(record)
            if not isinstance(record, dict) or started_at is None:
                gathered.without_a_time += 1
                continue
            content_blocks = content_blocks_of(record)
            remember_tool_calls(content_blocks, tool_names_by_call_id)
            gathered.entries.extend(
                entries_from_line(
                    record,
                    content_blocks,
                    started_at,
                    labels_by_line.get(line_number),
                    actions_by_line.get(line_number, []),
                    tool_names_by_call_id,
                    raw_record,
                    line_number,
                    in_subagent,
                )
            )
    return gathered


def content_blocks_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The content blocks of a transcript line's message; none when it has no message or its content is plain text."""
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    blocks = []
    for block in content:
        if isinstance(block, dict):
            blocks.append(block)
    return blocks


def remember_tool_calls(content_blocks: list[dict[str, Any]], tool_names_by_call_id: dict[str, str]) -> None:
    """Note the name of each tool a line asks for, so the line holding its result can be given the tool."""
    for block in content_blocks:
        tool_call_id = block.get("id")
        tool_name = block.get("name")
        if block.get("type") == "tool_use" and isinstance(tool_call_id, str) and isinstance(tool_name, str):
            tool_names_by_call_id[tool_call_id] = tool_name


def entries_from_line(
    record: dict[str, Any],
    content_blocks: list[dict[str, Any]],
    started_at: datetime,
    label: EntryLabel | None,
    actions: list[HumanAction],
    tool_names_by_call_id: dict[str, str],
    raw_record: RawRecordPointer,
    line_number: int,
    in_subagent: bool,
) -> list[TimelineEntry]:
    """The entries one timed transcript line gives, by rules 2 to 4 in the module docstring."""
    record_type = record.get("type")
    entry_type = record_type if record_type in ENTRY_TYPES else None
    actor = None
    label_rule = None
    if label is not None:
        actor = label.actor_label
        label_rule = label.rule_name

    def entry(
        kind: str,
        entry_actor: str | None,
        tool: str | None = None,
        category: str | None = None,
        outcome: str | None = None,
    ) -> TimelineEntry:
        """One entry for this line: everything but its kind and what the kind adds is the same for each."""
        return TimelineEntry(
            started_at=started_at,
            duration_seconds=None,
            source=SOURCE_CLAUDE,
            kind=kind,
            actor=entry_actor,
            raw_record=raw_record,
            position=line_number,
            source_application=listed_source_application(record.get("entrypoint")),
            entry_type=entry_type,
            label_rule=label_rule,
            tool=tool,
            category=category,
            outcome=outcome,
            in_subagent=in_subagent,
        )

    entries = []
    for action in actions:
        action_actor: str | None = "human"
        if action.kind == UNDECIDED_ACTION_KIND:
            action_actor = None
        tool_name = getattr(action, "tool_name", None)
        if not isinstance(tool_name, str):
            tool_name = None
        entries.append(entry(action.kind, action_actor, tool=listed_tool(tool_name), category=category_of(action.kind)))
    if actions and (label is None or label.actor_label == "human"):
        return entries

    line_entries = []
    for block in content_blocks:
        block_type = block.get("type")
        if block_type == "tool_use":
            tool_name = block.get("name")
            line_entries.append(
                entry(KIND_TOOL_CALL, actor, tool=listed_tool(tool_name if isinstance(tool_name, str) else ""))
            )
        elif block_type == "tool_result":
            tool_call_id = block.get("tool_use_id")
            tool_name = tool_names_by_call_id.get(tool_call_id) if isinstance(tool_call_id, str) else None
            outcome = OUTCOME_ERROR if block.get("is_error") is True else OUTCOME_OK
            line_entries.append(entry(KIND_TOOL_RESULT, actor, tool=listed_tool(tool_name), outcome=outcome))
        elif block_type == "text" and record_type == "assistant":
            line_entries.append(entry(KIND_MESSAGE_TEXT, actor))
        elif block_type in ("thinking", "redacted_thinking"):
            line_entries.append(entry(KIND_THINKING, actor))
    if not line_entries:
        line_entries.append(entry(KIND_ENTRY if entry_type is not None else KIND_UNFAMILIAR_ENTRY, actor))
    return entries + line_entries
