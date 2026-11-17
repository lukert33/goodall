"""Find what the person did in one Claude Code transcript.

A transcript is Claude Code's own JSON Lines file for one Claude session. Its
"user" entries mix what the person typed with text the harness wrote in the
person's name: tool results, injected reminders, command wrappers. This module
separates them, and returns one record per human action, in transcript order.

Who calls it: derivation code that builds derived records from raw records
(ADR 0003). It reads the file it is given and nothing else, imports only the
standard library, and never needs Inspect (AGENTS.md invariants 7 and 8).

Design decisions a reader would otherwise reverse-engineer:

- The detection rules are the part most likely to change, so each one is a
  small function named `is_...` or `..._from_...`, and every fixed string
  Claude Code writes is in `claude_code_markers()`. The rules are described in
  plain words in docs/human-action-rules.md; change both together and raise
  RULES_VERSION (ADR 0004).
- Field values borrow Inspect's vocabulary (InterruptEvent, ApprovalEvent) so
  the same rules could later be offered to Inspect's transcript importer.
- Anything the parser does not recognise is reported in `unfamiliar_entries`,
  never dropped silently: Claude Code's transcript format is internal and
  changes between versions, and a silent gap would undercount the person.
"""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

# Raise this whenever a detection rule changes, so derived records say which
# rules produced them (ADR 0004).
RULES_VERSION = "2"


@dataclass(frozen=True)
class ClaudeCodeMarkers:
    """The fixed strings Claude Code writes, which the detection rules match.

    Claude Code may change any of these in a new version. When it does, a rule
    stops matching and the action goes uncounted, so these are the first thing
    to check when counts look wrong for a new Claude Code version.
    """

    # Text prefixes that mark a "user" entry as written by the harness rather
    # than typed by the person.
    harness_text_prefixes: tuple[str, ...]
    # The tags around context the harness puts in front of a typed prompt,
    # such as project instructions at the start of a Claude session.
    prepended_context_opening_tag: str
    prepended_context_closing_tag: str
    # The whole text of the entry Claude Code writes when the person presses
    # Escape while the model is replying.
    interrupted_during_generation: str
    # The whole text of the entry Claude Code writes when the person stops the
    # agent during a tool call.
    interrupted_during_tool_call: str
    # How a tool result begins when the person stopped that tool while it ran.
    cancelled_tool_result_prefix: str
    # How a tool result begins when the person refused permission for the call.
    rejected_tool_result_prefix: str
    # The words in a rejected tool result that come just before the feedback
    # the person typed with the refusal, if they typed any.
    rejection_feedback_lead_in: str
    # Entry types Claude Code is known to write. Only "user" entries can hold
    # a human action; the rest are known not to, and any other type is
    # reported as unfamiliar.
    known_entry_types: tuple[str, ...]
    # The tags around a shell command the person ran with `!`, and the tags
    # that begin the command's output.
    shell_command_opening_tag: str
    shell_command_closing_tag: str
    shell_output_opening_tags: tuple[str, ...]
    # The tags Claude Code writes around a slash command. Which of the first
    # two opens the entry says what kind of command it was: the name tag
    # first for a command Claude Code runs itself, the message tag first for
    # one that expands into a prompt.
    slash_command_name_tags: tuple[str, str]
    slash_command_message_tags: tuple[str, str]
    slash_command_arguments_tags: tuple[str, str]
    # Block types known to appear in a "user" entry's message content.
    known_content_block_types: tuple[str, ...]
    # Values of a "user" entry's `origin.kind`; only "human" is the person.
    known_origin_kinds: tuple[str, ...]


def claude_code_markers() -> ClaudeCodeMarkers:
    """Return every fixed string the detection rules depend on, in one place."""
    return ClaudeCodeMarkers(
        interrupted_during_generation="[Request interrupted by user]",
        interrupted_during_tool_call="[Request interrupted by user for tool use]",
        cancelled_tool_result_prefix="The user doesn't want to take this action right now",
        rejected_tool_result_prefix="The user doesn't want to proceed with this tool use",
        rejection_feedback_lead_in="the user said:",
        known_entry_types=(
            "user",
            "assistant",
            "system",
            "attachment",
            "summary",
            "queue-operation",
            "file-history-snapshot",
            "file-history-delta",
            "last-prompt",
            "custom-title",
            "ai-title",
            "agent-name",
            "mode",
            "permission-mode",
            "pr-link",
            "frame-link",
            "worktree-state",
            "relocated",
            "cost-state",
            "tools-used",
            "session-owner",
            "bridge-session",
            "api-request",
            "api-request-blob",
            "api-request-shape",
            "atis-latch",
            "artifact-autoreact-ledger",
            "artifact-comment-monitor",
        ),
        shell_command_opening_tag="<bash-input>",
        shell_command_closing_tag="</bash-input>",
        shell_output_opening_tags=("<bash-stdout>", "<bash-stderr>"),
        slash_command_name_tags=("<command-name>", "</command-name>"),
        slash_command_message_tags=("<command-message>", "</command-message>"),
        slash_command_arguments_tags=("<command-args>", "</command-args>"),
        known_content_block_types=("text", "image", "document", "tool_result"),
        known_origin_kinds=("human", "task-notification", "peer", "coordinator"),
        prepended_context_opening_tag="<system-reminder>",
        prepended_context_closing_tag="</system-reminder>",
        harness_text_prefixes=(
            "<system-reminder>",
            "<command-name>",
            "<command-message>",
            "<local-command-stdout>",
            "<local-command-stderr>",
            "<local-command-caveat>",
            "<task-notification>",
            "<bash-input>",
            "<bash-stdout>",
            "<bash-stderr>",
        ),
    )


# Every action found here says it came from a Claude Code transcript, so it
# can sit in one list with actions from other sources (goodall.human_activity).
SOURCE = "claude_code_transcript"

# Every action record below shares these fields, in this order:
#
# - `line_number`, `entry_id` (the entry's `uuid`), `session_id` (the entry's
#   `sessionId`) and `transcript_path`: where in which raw record it was found.
#   `transcript_path` is left out of equality, because it says where an action
#   was read from, not what the person did.
# - `timestamp`: the entry's own timestamp, as Claude Code wrote it.
# - `source`, `kind` and `rule`: which kind of raw record, which kind of
#   action, and the name of the rule function that found it, so that actions
#   can be counted, merged or renamed later without re-reading transcripts.
# - `duration_seconds`: always None here; a transcript entry is a moment.
#
# The records are keyword-only, so each one reads as a list of named values.


@dataclass(frozen=True, kw_only=True)
class TypedPrompt:
    """A prompt the person typed and sent to the agent."""

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    text: str
    image_count: int
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="typed_prompt", init=False)
    rule: str = field(default="is_typed_prompt", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class Interruption:
    """The person stopped the agent part-way through.

    `interrupted` uses Inspect's InterruptEvent values: "generate" (the model
    was replying) or "tool_call" (a tool was running or about to run).
    Inspect's third value, "between_turns", is never produced: a transcript
    records nothing when the person stops an agent that is not working. Two
    rules find interruptions, so `rule` says which one did.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    rule: Literal["interruption_from_marker", "interruption_from_tool_result"]
    interrupted: Literal["generate", "tool_call", "between_turns"]
    interrupted_tool_call_id: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="interruption", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class ToolCallRejection:
    """The person refused permission for a tool call the agent asked to make.

    `decision` is always "reject", Inspect's ApprovalEvent value. `feedback`
    is what the person typed with the refusal, or None if they typed nothing.
    `tool_name` is the tool the agent asked for, when the call is in the same
    transcript.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    tool_call_id: str | None
    tool_name: str | None
    feedback: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    decision: Literal["reject"] = field(default="reject", init=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="tool_call_rejection", init=False)
    rule: str = field(default="rejection_from_tool_result", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class ShellCommand:
    """A shell command the person ran themselves inside Claude Code, with `!`.

    `text` is the command as typed: it is the person's own work, kept like a
    prompt's text. The command's output is not copied; `output_line_number`
    and `output_entry_id` point at the entry holding it, which is the same
    entry in newer Claude Code versions and the next one in older versions,
    or are None when no output was recorded.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    text: str
    output_line_number: int | None
    output_entry_id: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="shell_command", init=False)
    rule: str = field(default="shell_command_from_bash_input", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class SlashCommand:
    """A slash command the person typed, such as `/model example-model`.

    `command_name` and `arguments` are as typed; `arguments` is None when the
    entry has no arguments tag at all. The prompt a command may expand into is
    not kept: those words are the command author's, not the person's.

    `command_type` is "local" for a command Claude Code runs itself (such as
    /model, /compact, /fast) and "prompt" for one that expands into a prompt
    for the model (a skill, a plugin or custom command, and some built-in
    commands). Claude Code does not record which commands are built in.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    command_name: str
    arguments: str | None
    command_type: Literal["local", "prompt"]
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="slash_command", init=False)
    rule: str = field(default="slash_command_from_command_tags", init=False)
    duration_seconds: float | None = field(default=None, init=False)


TranscriptAction = TypedPrompt | Interruption | ToolCallRejection | ShellCommand | SlashCommand


@dataclass(frozen=True)
class EntryPlace:
    """Where one entry sits in one transcript; every action found in it shares this."""

    transcript_path: Path
    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None


def place_of_entry(entry: dict[str, Any], line_number: int, transcript_path: Path) -> EntryPlace:
    """Read an entry's place, keeping only values of the expected type."""
    entry_id = entry.get("uuid")
    session_id = entry.get("sessionId")
    timestamp = entry.get("timestamp")
    return EntryPlace(
        transcript_path=transcript_path,
        line_number=line_number,
        entry_id=entry_id if isinstance(entry_id, str) else None,
        session_id=session_id if isinstance(session_id, str) else None,
        timestamp=timestamp if isinstance(timestamp, str) else None,
    )


@dataclass(frozen=True)
class UnfamiliarEntry:
    """An entry whose type or shape the parser does not recognise."""

    line_number: int
    entry_id: str | None
    reason: str


@dataclass
class ParseResult:
    """Everything `parse_human_actions` found in one transcript."""

    actions: list[TranscriptAction]
    unfamiliar_entries: list[UnfamiliarEntry]
    rules_version: str = RULES_VERSION


def parse_human_actions(transcript_path: Path) -> ParseResult:
    """Read one transcript and return the person's actions, in order.

    This is the module's entry point. A line that cannot be read, or an entry
    of a type or shape the rules do not know, goes into `unfamiliar_entries`
    and parsing carries on with the next line. Blank lines hold no entry and
    are skipped.
    """
    markers = claude_code_markers()
    result = ParseResult(actions=[], unfamiliar_entries=[])
    memory = TranscriptMemory()
    with transcript_path.open(encoding="utf-8", errors="replace") as transcript_file:
        for line_number, line in enumerate(transcript_file, start=1):
            if line.strip() == "":
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, None, "line is not valid JSON"))
                continue
            if not isinstance(entry, dict):
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, None, "line is JSON but not an object"))
                continue
            entry_id = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
            entry_type = entry.get("type")
            if not isinstance(entry_type, str):
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, entry_id, "entry has no type"))
                continue
            if entry_type not in markers.known_entry_types:
                reason = f"unknown entry type {entry_type[:60]!r}"
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, entry_id, reason))
                continue
            place = place_of_entry(entry, line_number, transcript_path)
            if entry_type == "assistant":
                remember_tool_calls(entry, memory)
                continue
            if entry_type != "user":
                continue
            user_content = read_user_content(entry, markers)
            for reason in user_content.unfamiliar_reasons:
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, entry_id, reason))
            link_shell_command_output(entry, place, user_content.text, result.actions, memory, markers)
            actions = actions_from_user_entry(entry, place, user_content, markers, memory)
            for action in actions:
                if isinstance(action, ToolCallRejection) and action.entry_id is not None:
                    memory.rejection_entry_ids.add(action.entry_id)
                if isinstance(action, ShellCommand) and action.entry_id is not None and action.output_entry_id is None:
                    memory.shell_command_positions[action.entry_id] = len(result.actions)
                result.actions.append(action)
    return result


@dataclass
class TranscriptMemory:
    """What the parser remembers from earlier lines while reading later ones.

    Most rules look at one entry alone. These are the exceptions, each kept
    for one stated reason.
    """

    # Ids of entries that held a rejection, so the interruption marker Claude
    # Code writes straight after one can be recognised as part of it.
    rejection_entry_ids: set[str] = field(default_factory=set)
    # The tool each tool call asked for, by call id, read from the assistant
    # entries that made the calls. Only names are kept, never tool inputs.
    tool_names_by_call_id: dict[str, str] = field(default_factory=dict)
    # Where in the action list each shell command with no output yet sits, by
    # its entry id, so older versions' separate output entry can be linked.
    shell_command_positions: dict[str, int] = field(default_factory=dict)


def remember_tool_calls(entry: dict[str, Any], memory: TranscriptMemory) -> None:
    """Note the name of every tool an assistant entry called, by call id."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_call_id = block.get("id")
        tool_name = block.get("name")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            memory.tool_names_by_call_id[tool_call_id] = tool_name


def tool_name_for(tool_result: dict[str, Any], memory: TranscriptMemory) -> str | None:
    """Return the name of the tool a tool result answers, if its call was seen."""
    tool_call_id = tool_result.get("tool_use_id")
    if not isinstance(tool_call_id, str):
        return None
    return memory.tool_names_by_call_id.get(tool_call_id)


@dataclass
class UserContent:
    """The parts of a "user" entry that the rules look at.

    `unfamiliar_reasons` lists anything in the entry the parser did not
    recognise. The recognised parts are still used, so a new kind of block
    beside a typed prompt does not hide the prompt.
    """

    text: str
    image_count: int
    tool_results: list[dict[str, Any]]
    unfamiliar_reasons: list[str]


def read_user_content(entry: dict[str, Any], markers: ClaudeCodeMarkers) -> UserContent:
    """Collect the text, image count and tool results of a "user" entry.

    The message content is either one string or a list of blocks; both forms
    occur for typed prompts. Values are checked before use, because a
    transcript is another program's internal file and may hold anything.
    """
    user_content = UserContent(text="", image_count=0, tool_results=[], unfamiliar_reasons=[])
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        user_content.text = content
    elif isinstance(content, list):
        texts = []
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block_type == "image":
                user_content.image_count += 1
            elif block_type == "tool_result":
                user_content.tool_results.append(block)
            elif isinstance(block_type, str) and block_type in markers.known_content_block_types:
                continue
            elif isinstance(block_type, str):
                user_content.unfamiliar_reasons.append(f"unknown content block type {block_type[:60]!r}")
            else:
                user_content.unfamiliar_reasons.append("content block has no type")
        user_content.text = "\n".join(texts)
    else:
        user_content.unfamiliar_reasons.append("user entry has no message content")

    origin = entry.get("origin")
    if isinstance(origin, dict):
        origin_kind = origin.get("kind")
        if not isinstance(origin_kind, str) or origin_kind not in markers.known_origin_kinds:
            user_content.unfamiliar_reasons.append(f"unknown origin kind {str(origin_kind)[:60]!r}")
    return user_content


def tool_result_text(tool_result: dict[str, Any]) -> str:
    """Return a tool result's text, whether stored as a string or as blocks."""
    content = tool_result.get("content")
    if isinstance(content, str):
        return content
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return "\n".join(texts)


def actions_from_user_entry(
    entry: dict[str, Any],
    place: EntryPlace,
    content: UserContent,
    markers: ClaudeCodeMarkers,
    memory: TranscriptMemory,
) -> list[TranscriptAction]:
    """Return the human actions in one "user" entry, by applying each rule."""

    # An entry carrying tool results is the harness reporting back to the
    # model. It is a human action only when the result shows the person
    # stepped in.
    if content.tool_results:
        actions: list[TranscriptAction] = []
        for tool_result in content.tool_results:
            rejection = rejection_from_tool_result(place, tool_result, markers, memory)
            if rejection is not None:
                actions.append(rejection)
            interruption = interruption_from_tool_result(place, tool_result, markers)
            if interruption is not None:
                actions.append(interruption)
        return actions

    interruption = interruption_from_marker(place, content.text, markers)
    if interruption is not None:
        # The marker Claude Code writes straight after a rejection is the end
        # of that rejection, not a second thing the person did.
        if entry.get("parentUuid") in memory.rejection_entry_ids:
            return []
        return [interruption]

    typed_text = strip_prepended_context(content.text, markers)
    shell_command = shell_command_from_bash_input(entry, place, typed_text, markers)
    if shell_command is not None:
        return [shell_command]

    slash_command = slash_command_from_command_tags(entry, place, typed_text, markers)
    if slash_command is not None:
        return [slash_command]

    if is_typed_prompt(entry, typed_text, content.image_count, markers):
        prompt = TypedPrompt(
            line_number=place.line_number,
            entry_id=place.entry_id,
            session_id=place.session_id,
            timestamp=place.timestamp,
            transcript_path=place.transcript_path,
            text=typed_text,
            image_count=content.image_count,
        )
        return [prompt]
    return []


def shell_command_from_bash_input(
    entry: dict[str, Any], place: EntryPlace, typed_text: str, markers: ClaudeCodeMarkers
) -> ShellCommand | None:
    """Rule: an entry from the person that begins with a shell-command tag is a shell command.

    Claude Code records a `!command` as the command inside `<bash-input>`
    tags, followed by its output in `<bash-stdout>` and `<bash-stderr>` tags
    in the same entry (newer versions) or in the next entry (older versions,
    linked by `link_shell_command_output`). The tag must open the text: a
    prompt that mentions it part-way through is a prompt.
    """
    if not is_from_the_person(entry):
        return None
    if not typed_text.startswith(markers.shell_command_opening_tag):
        return None
    closing_position = typed_text.find(markers.shell_command_closing_tag)
    if closing_position == -1:
        return None
    command = typed_text[len(markers.shell_command_opening_tag) : closing_position]
    after_command = typed_text[closing_position + len(markers.shell_command_closing_tag) :].lstrip()
    output_in_same_entry = after_command.startswith(markers.shell_output_opening_tags)
    return ShellCommand(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        text=command,
        output_line_number=place.line_number if output_in_same_entry else None,
        output_entry_id=place.entry_id if output_in_same_entry else None,
    )


def text_between(text: str, tags: tuple[str, str]) -> str | None:
    """Return the text inside the first pair of these tags, or None if either is missing."""
    opening_tag, closing_tag = tags
    opening_position = text.find(opening_tag)
    if opening_position == -1:
        return None
    start = opening_position + len(opening_tag)
    closing_position = text.find(closing_tag, start)
    if closing_position == -1:
        return None
    return text[start:closing_position]


def slash_command_from_command_tags(
    entry: dict[str, Any], place: EntryPlace, typed_text: str, markers: ClaudeCodeMarkers
) -> SlashCommand | None:
    """Rule: an entry from the person that opens with slash-command tags is a slash command.

    The trap: older transcripts carry no origin on these entries, and newer
    ones carry an origin of "human" only for some commands, so the origin
    cannot decide; the tags do, together with `is_from_the_person`. The tag
    that opens the entry gives `command_type` (see `SlashCommand`); this was
    observed to separate the two kinds in every transcript scanned, across
    Claude Code 2.1.205 to 2.1.326, and is the first thing to recheck if a
    later version changes it.
    """
    if not is_from_the_person(entry):
        return None
    if typed_text.startswith(markers.slash_command_name_tags[0]):
        command_type: Literal["local", "prompt"] = "local"
    elif typed_text.startswith(markers.slash_command_message_tags[0]):
        command_type = "prompt"
    else:
        return None
    command_name = text_between(typed_text, markers.slash_command_name_tags)
    if command_name is None:
        return None
    return SlashCommand(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        command_name=command_name,
        arguments=text_between(typed_text, markers.slash_command_arguments_tags),
        command_type=command_type,
    )


def link_shell_command_output(
    entry: dict[str, Any],
    place: EntryPlace,
    text: str,
    actions: list[TranscriptAction],
    memory: TranscriptMemory,
    markers: ClaudeCodeMarkers,
) -> None:
    """Rule: output whose parent is a shell command with no output yet is that command's output.

    Older Claude Code versions write a `!command`'s output as a separate entry
    whose parent is the command's entry. The command's record is replaced by
    one that points at the output entry. The output is not copied.
    """
    if not text.lstrip().startswith(markers.shell_output_opening_tags):
        return
    parent_id = entry.get("parentUuid")
    if not isinstance(parent_id, str) or parent_id not in memory.shell_command_positions:
        return
    position = memory.shell_command_positions.pop(parent_id)
    command = actions[position]
    if isinstance(command, ShellCommand) and command.output_line_number is None:
        actions[position] = replace(command, output_line_number=place.line_number, output_entry_id=place.entry_id)


def interruption_from_marker(place: EntryPlace, text: str, markers: ClaudeCodeMarkers) -> Interruption | None:
    """Rule: an entry whose whole text is an interruption marker is an interruption.

    The marker does not name the tool call that was stopped, so the id is left
    empty. The whole text must match: a prompt that merely quotes the marker
    is still a prompt.
    """
    if text == markers.interrupted_during_generation:
        interrupted: Literal["generate", "tool_call"] = "generate"
    elif text == markers.interrupted_during_tool_call:
        interrupted = "tool_call"
    else:
        return None
    return Interruption(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        rule="interruption_from_marker",
        interrupted=interrupted,
        interrupted_tool_call_id=None,
    )


def interruption_from_tool_result(
    place: EntryPlace, tool_result: dict[str, Any], markers: ClaudeCodeMarkers
) -> Interruption | None:
    """Rule: an error tool result with the cancellation text is an interruption.

    When the person stops a running tool, Claude Code writes the tool's result
    as an error beginning with fixed text, and writes no separate marker. The
    result names the call it answers, so the interrupted call is known.
    """
    if tool_result.get("is_error") is not True:
        return None
    if not tool_result_text(tool_result).startswith(markers.cancelled_tool_result_prefix):
        return None
    tool_call_id = tool_result.get("tool_use_id")
    return Interruption(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        rule="interruption_from_tool_result",
        interrupted="tool_call",
        interrupted_tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
    )


def rejection_from_tool_result(
    place: EntryPlace, tool_result: dict[str, Any], markers: ClaudeCodeMarkers, memory: TranscriptMemory
) -> ToolCallRejection | None:
    """Rule: an error tool result with the rejection text is a rejection.

    The trap: Claude Code's `toolDenialKind` field is not reliable here. It
    says "user-rejected" for a plain refusal but "permission-rule" when the
    person typed feedback, which is also what it says when a configured rule
    refused the call. The fixed text is what separates the person from a rule.
    Any feedback follows the lead-in words and is kept as typed, trimmed.
    """
    if tool_result.get("is_error") is not True:
        return None
    text = tool_result_text(tool_result)
    if not text.startswith(markers.rejected_tool_result_prefix):
        return None
    feedback = None
    lead_in_position = text.find(markers.rejection_feedback_lead_in)
    if lead_in_position != -1:
        feedback = text[lead_in_position + len(markers.rejection_feedback_lead_in) :].strip() or None
    tool_call_id = tool_result.get("tool_use_id")
    return ToolCallRejection(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        tool_name=tool_name_for(tool_result, memory),
        feedback=feedback,
    )


def strip_prepended_context(text: str, markers: ClaudeCodeMarkers) -> str:
    """Rule: remove the reminder blocks the harness puts in front of a typed prompt.

    The trap: an entry that begins with a reminder is not necessarily
    injected. Claude Code puts context (project instructions, for example) in
    front of the person's own text in the same entry, so throwing away every
    entry that starts with a reminder loses real prompts, typically the first
    one of each Claude session. What is left after the leading blocks is what
    the person typed; if nothing is left, the entry was all harness. A block
    that never closes is left in place, and the prefix check then treats the
    entry as harness text.
    """
    remainder = text.lstrip()
    while remainder.startswith(markers.prepended_context_opening_tag):
        closing_position = remainder.find(markers.prepended_context_closing_tag)
        if closing_position == -1:
            return remainder
        remainder = remainder[closing_position + len(markers.prepended_context_closing_tag) :].lstrip()
    return remainder


def is_from_the_person(entry: dict[str, Any]) -> bool:
    """Rule: rule out "user" entries that the entry itself says the person did not write.

    Shared by every rule that reads what the person typed into a user entry.
    It says only who did not write an entry; each rule then checks the text.
    """
    # Claude Code marks some injected entries outright.
    if entry.get("isMeta") is True or entry.get("isCompactSummary") is True:
        return False
    # Entries on a sidechain belong to a subagent; its "prompt" was written by
    # the main agent, not the person.
    if entry.get("isSidechain") is True:
        return False
    # Newer Claude Code versions say where a user entry came from. Older ones
    # do not, so a missing origin falls through to the text checks.
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return False
    return True


def is_typed_prompt(entry: dict[str, Any], typed_text: str, image_count: int, markers: ClaudeCodeMarkers) -> bool:
    """Rule: decide whether a "user" entry holding text was typed by the person.

    `typed_text` is the entry's text with any prepended context already
    removed. The trap: Claude Code writes `userType: "external"` on every user
    entry, injected or not, so that field cannot tell them apart. The checks
    below run from the most to the least reliable signal.
    """
    if not is_from_the_person(entry):
        return False
    # Wrappers the harness writes: slash-command records, shell commands and
    # their output, task notifications. These are checked even when origin
    # says "human", because what the person typed inside a wrapper is a
    # different kind of action, found by its own rule.
    for prefix in markers.harness_text_prefixes:
        if typed_text.startswith(prefix):
            return False
    # A prompt has something in it. An entry with neither text nor an image is
    # either all harness context or a shape the parser did not understand.
    if typed_text == "" and image_count == 0:
        return False
    return True
