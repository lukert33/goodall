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


def claude_code_markers() -> ClaudeCodeMarkers:
    """Return every fixed string the detection rules depend on, in one place."""
    return ClaudeCodeMarkers(
        interrupted_during_generation="[Request interrupted by user]",
        interrupted_during_tool_call="[Request interrupted by user for tool use]",
        cancelled_tool_result_prefix="The user doesn't want to take this action right now",
        rejected_tool_result_prefix="The user doesn't want to proceed with this tool use",
        rejection_feedback_lead_in="the user said:",
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
    """Read one transcript and return the person's actions, in order."""
    markers = claude_code_markers()
    result = ParseResult(actions=[], unfamiliar_entries=[])
    # Ids of entries that held a rejection, so the interruption marker Claude
    # Code writes straight after one can be recognised as part of it.
    rejection_entry_ids: set[str] = set()
    with transcript_path.open(encoding="utf-8", errors="replace") as transcript_file:
        for line_number, line in enumerate(transcript_file, start=1):
            entry = json.loads(line)
            if entry.get("type") != "user":
                continue
            actions = actions_from_user_entry(entry, line_number, markers, rejection_entry_ids)
            for action in actions:
                if isinstance(action, ToolCallRejection) and action.entry_id is not None:
                    rejection_entry_ids.add(action.entry_id)
            result.actions.extend(actions)
    return result


@dataclass
class UserContent:
    """The parts of a "user" entry's message that the rules look at."""

    text: str
    image_count: int
    tool_results: list[dict[str, Any]]


def read_user_content(entry: dict[str, Any]) -> UserContent:
    """Collect the text, image count and tool results of a "user" entry.

    The message content is either one string or a list of blocks; both forms
    occur for typed prompts.
    """
    content = entry["message"]["content"]
    if isinstance(content, str):
        return UserContent(text=content, image_count=0, tool_results=[])
    texts = []
    image_count = 0
    tool_results = []
    for block in content:
        if block["type"] == "text":
            texts.append(block["text"])
        elif block["type"] == "image":
            image_count += 1
        elif block["type"] == "tool_result":
            tool_results.append(block)
    return UserContent(text="\n".join(texts), image_count=image_count, tool_results=tool_results)


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
    entry: dict[str, Any], line_number: int, markers: ClaudeCodeMarkers, rejection_entry_ids: set[str]
) -> list[HumanAction]:
    """Return the human actions in one "user" entry, by applying each rule."""
    content = read_user_content(entry)

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

    # The marker Claude Code writes after a rejection is the end of that
    # rejection, not a second thing the person did.
    if entry.get("parentUuid") in rejection_entry_ids:
        return []

    interruption = interruption_from_marker(entry, line_number, content.text, markers)
    if interruption is not None:
        return [interruption]

    if is_typed_prompt(entry, content.text, markers):
        prompt = TypedPrompt(
            line_number=line_number,
            entry_id=entry.get("uuid"),
            timestamp=entry.get("timestamp"),
            text=content.text,
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


def is_typed_prompt(entry: dict[str, Any], text: str, markers: ClaudeCodeMarkers) -> bool:
    """Rule: decide whether a "user" entry holding text was typed by the person.

    The trap: Claude Code writes `userType: "external"` on every user entry,
    injected or not, so that field cannot tell them apart. The checks below
    run from the most to the least reliable signal.
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
    for prefix in markers.harness_text_prefixes:
        if text.startswith(prefix):
            return False
    return True
