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
from typing import Any

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


def claude_code_markers() -> ClaudeCodeMarkers:
    """Return every fixed string the detection rules depend on, in one place."""
    return ClaudeCodeMarkers(
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


HumanAction = TypedPrompt


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
    result = ParseResult(actions=[], unfamiliar_entries=[])
    with transcript_path.open(encoding="utf-8", errors="replace") as transcript_file:
        for line_number, line in enumerate(transcript_file, start=1):
            entry = json.loads(line)
            if entry.get("type") == "user":
                result.actions.extend(actions_from_user_entry(entry, line_number))
    return result


def actions_from_user_entry(entry: dict[str, Any], line_number: int) -> list[HumanAction]:
    """Return the human actions in one "user" entry."""
    markers = claude_code_markers()
    content = entry["message"]["content"]
    if isinstance(content, str):
        text = content
        image_count = 0
        has_tool_result = False
    else:
        texts = []
        image_count = 0
        has_tool_result = False
        for block in content:
            if block["type"] == "text":
                texts.append(block["text"])
            elif block["type"] == "image":
                image_count += 1
            elif block["type"] == "tool_result":
                has_tool_result = True
        text = "\n".join(texts)
    if has_tool_result or not is_typed_prompt(entry, text, markers):
        return []
    prompt = TypedPrompt(
        line_number=line_number,
        entry_id=entry.get("uuid"),
        timestamp=entry.get("timestamp"),
        text=text,
        image_count=image_count,
    )
    return [prompt]


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
