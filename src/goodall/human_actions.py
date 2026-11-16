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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Raise this whenever a detection rule changes, so derived records say which
# rules produced them (ADR 0004).
RULES_VERSION = "1"


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


@dataclass(frozen=True)
class TypedPrompt:
    """A prompt the person typed and sent to the agent."""

    line_number: int
    entry_id: str | None
    timestamp: str | None
    text: str
    image_count: int
    kind: str = field(default="typed_prompt", init=False)


@dataclass(frozen=True)
class Interruption:
    """The person stopped the agent part-way through.

    `interrupted` uses Inspect's InterruptEvent values: "generate" (the model
    was replying) or "tool_call" (a tool was running or about to run).
    Inspect's third value, "between_turns", is never produced: a transcript
    records nothing when the person stops an agent that is not working.
    """

    line_number: int
    entry_id: str | None
    timestamp: str | None
    interrupted: Literal["generate", "tool_call", "between_turns"]
    interrupted_tool_call_id: str | None
    kind: str = field(default="interruption", init=False)


@dataclass(frozen=True)
class ToolCallRejection:
    """The person refused permission for a tool call the agent asked to make.

    `decision` is always "reject", Inspect's ApprovalEvent value. `feedback`
    is what the person typed with the refusal, or None if they typed nothing.
    """

    line_number: int
    entry_id: str | None
    timestamp: str | None
    tool_call_id: str | None
    feedback: str | None
    decision: Literal["reject"] = field(default="reject", init=False)
    kind: str = field(default="tool_call_rejection", init=False)


HumanAction = TypedPrompt | Interruption | ToolCallRejection


@dataclass(frozen=True)
class UnfamiliarEntry:
    """An entry whose type or shape the parser does not recognise."""

    line_number: int
    entry_id: str | None
    reason: str


@dataclass
class ParseResult:
    """Everything `parse_human_actions` found in one transcript."""

    actions: list[HumanAction]
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
    # Ids of entries that held a rejection, so the interruption marker Claude
    # Code writes straight after one can be recognised as part of it.
    rejection_entry_ids: set[str] = set()
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
            if entry_type != "user":
                continue
            user_content = read_user_content(entry, markers)
            for reason in user_content.unfamiliar_reasons:
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, entry_id, reason))
            actions = actions_from_user_entry(entry, line_number, user_content, markers, rejection_entry_ids)
            for action in actions:
                if isinstance(action, ToolCallRejection) and action.entry_id is not None:
                    rejection_entry_ids.add(action.entry_id)
            result.actions.extend(actions)
    return result


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
    line_number: int,
    content: UserContent,
    markers: ClaudeCodeMarkers,
    rejection_entry_ids: set[str],
) -> list[HumanAction]:
    """Return the human actions in one "user" entry, by applying each rule."""

    # An entry carrying tool results is the harness reporting back to the
    # model. It is a human action only when the result shows the person
    # stepped in.
    if content.tool_results:
        actions: list[HumanAction] = []
        for tool_result in content.tool_results:
            rejection = rejection_from_tool_result(entry, line_number, tool_result, markers)
            if rejection is not None:
                actions.append(rejection)
            interruption = interruption_from_tool_result(entry, line_number, tool_result, markers)
            if interruption is not None:
                actions.append(interruption)
        return actions

    interruption = interruption_from_marker(entry, line_number, content.text, markers)
    if interruption is not None:
        # The marker Claude Code writes straight after a rejection is the end
        # of that rejection, not a second thing the person did.
        if entry.get("parentUuid") in rejection_entry_ids:
            return []
        return [interruption]

    typed_text = strip_prepended_context(content.text, markers)
    if is_typed_prompt(entry, typed_text, content.image_count, markers):
        prompt = TypedPrompt(
            line_number=line_number,
            entry_id=entry.get("uuid"),
            timestamp=entry.get("timestamp"),
            text=typed_text,
            image_count=content.image_count,
        )
        return [prompt]
    return []


def interruption_from_marker(
    entry: dict[str, Any], line_number: int, text: str, markers: ClaudeCodeMarkers
) -> Interruption | None:
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
        line_number=line_number,
        entry_id=entry.get("uuid"),
        timestamp=entry.get("timestamp"),
        interrupted=interrupted,
        interrupted_tool_call_id=None,
    )


def interruption_from_tool_result(
    entry: dict[str, Any], line_number: int, tool_result: dict[str, Any], markers: ClaudeCodeMarkers
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
    return Interruption(
        line_number=line_number,
        entry_id=entry.get("uuid"),
        timestamp=entry.get("timestamp"),
        interrupted="tool_call",
        interrupted_tool_call_id=tool_result.get("tool_use_id"),
    )


def rejection_from_tool_result(
    entry: dict[str, Any], line_number: int, tool_result: dict[str, Any], markers: ClaudeCodeMarkers
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
    return ToolCallRejection(
        line_number=line_number,
        entry_id=entry.get("uuid"),
        timestamp=entry.get("timestamp"),
        tool_call_id=tool_result.get("tool_use_id"),
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


def is_typed_prompt(entry: dict[str, Any], typed_text: str, image_count: int, markers: ClaudeCodeMarkers) -> bool:
    """Rule: decide whether a "user" entry holding text was typed by the person.

    `typed_text` is the entry's text with any prepended context already
    removed. The trap: Claude Code writes `userType: "external"` on every user
    entry, injected or not, so that field cannot tell them apart. The checks
    below run from the most to the least reliable signal.
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
    # Wrappers the harness writes: slash-command records, command output,
    # task notifications. These are checked even when origin says "human",
    # because a slash command the person typed is recorded in a wrapper and
    # version 1 does not count slash commands as typed prompts.
    for prefix in markers.harness_text_prefixes:
        if typed_text.startswith(prefix):
            return False
    # A prompt has something in it. An entry with neither text nor an image is
    # either all harness context or a shape the parser did not understand.
    if typed_text == "" and image_count == 0:
        return False
    return True
