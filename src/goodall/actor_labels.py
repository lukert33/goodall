"""Give every entry of one Claude Code transcript an actor label.

An actor label answers, for one entry, who produced it: `human` (the person),
`ai` (a model) or `harness` (the software around the model: tool results,
injected reminders, bookkeeping entries). Labels are a derived record: they are
computed from the raw transcript by the versioned rules below and never written
back into it (ADR 0003, ADR 0004).

Who calls it: derivation code that builds derived records from raw records. It
reads the file it is given and nothing else, and imports only the standard
library (AGENTS.md invariants 7 and 8).

Design decisions a reader would otherwise reverse-engineer:

- `ACTOR_LABEL_RULES` is the part of this file meant to be read and edited. It
  is an ordered list; the first rule whose test is true decides the label. Each
  rule is described in plain words in docs/actor-label-rules.md; change both
  together and raise `ACTOR_LABEL_RULES_VERSION`.
- What the person did is decided by `goodall.human_actions`, not here. This
  module asks that parser which lines hold a human action, and reuses its
  fixed strings (`claude_code_markers()`), so the two can never disagree about
  whether a line is the person's.
- One entry, one label. When an entry holds more than one actor's words (a
  typed prompt with a reminder in front, a refusal the harness wrote up around
  the person's decision), it is labelled by its primary producer and marked
  `mixed`. The rule decides both, so a mixed entry is never a surprise.
- Nothing is guessed. An entry no rule matches, and a line that cannot be read,
  goes into `unmatched` with a reason that names no content, and labelling
  carries on with the next line.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from goodall.human_actions import (
    RULES_VERSION as HUMAN_ACTION_RULES_VERSION,
)
from goodall.human_actions import (
    claude_code_markers,
    parse_human_actions,
    read_user_content,
    strip_prepended_context,
)

# Raise this whenever a rule below changes, or when the human-action rules it
# relies on change, so labels made by different rules can be told apart and
# regenerated (ADR 0004).
ACTOR_LABEL_RULES_VERSION = "1"

ActorLabel = Literal["human", "ai", "harness"]


@dataclass(frozen=True)
class EntryFacts:
    """What the rules may look at in one entry, read out of it in advance.

    The rules test these facts rather than the raw entry, so each rule's test
    fits on a line or two and cannot crash on a missing key. Add a fact here
    when a new rule needs one.
    """

    entry_type: str
    # Kinds of human action `goodall.human_actions` found on this line:
    # "typed_prompt", "interruption", "tool_call_rejection".
    human_action_kinds: frozenset[str]
    # The entry's text began with reminder blocks the harness put in front.
    has_prepended_context: bool
    # The entry's text with any leading reminder blocks removed.
    text_after_context: str
    image_count: int
    has_tool_results: bool
    has_message_content: bool
    is_sidechain: bool
    is_meta: bool
    is_compact_summary: bool
    is_api_error_message: bool
    # `origin.kind`, present on newer "user" entries; None when absent.
    origin_kind: str | None
    # The model named on an assistant message; None when absent.
    model: str | None


@dataclass(frozen=True)
class Rule:
    """One actor labelling rule: if `test` is true, the entry gets `label`."""

    name: str
    description: str
    label: ActorLabel
    mixed: bool
    test: Callable[[EntryFacts], bool]


def starts_with_harness_wrapper(text: str) -> bool:
    """True when text begins with a tag Claude Code wraps its own text in."""
    for prefix in claude_code_markers().harness_text_prefixes:
        if text.startswith(prefix):
            return True
    return False


def is_interruption_marker(text: str) -> bool:
    """True when the whole text is one of Claude Code's interruption markers."""
    markers = claude_code_markers()
    return text in (markers.interrupted_during_generation, markers.interrupted_during_tool_call)


def is_harness_injected_user_entry(entry: EntryFacts) -> bool:
    """True for a "user" entry the harness wrote in the person's place.

    The trap: an entry that is only reminder blocks has empty text once they
    are removed, so emptiness (with no image) is itself the sign of injection.
    """
    if entry.entry_type != "user" or entry.has_tool_results:
        return False
    if entry.is_meta or entry.is_compact_summary or entry.origin_kind == "task-notification":
        return True
    if starts_with_harness_wrapper(entry.text_after_context):
        return True
    return entry.has_message_content and entry.text_after_context == "" and entry.image_count == 0


def is_subagent_instructions(entry: EntryFacts) -> bool:
    """True for text the main agent wrote to start a subagent.

    A subagent's conversation is stored as a sidechain, and its first "user"
    entry is the instructions the main agent's model wrote, not the person.
    """
    if entry.entry_type != "user" or not entry.is_sidechain or entry.has_tool_results:
        return False
    if entry.origin_kind is not None and entry.origin_kind != "human":
        return False
    return entry.text_after_context != ""


# The rules, in order. The first rule whose test is true decides the label.
# Read docs/actor-label-rules.md alongside this list; change both together and
# raise ACTOR_LABEL_RULES_VERSION.
ACTOR_LABEL_RULES: tuple[Rule, ...] = (
    Rule(
        name="person_typed_prompt",
        description="The person typed this prompt, and the harness added nothing to it.",
        label="human",
        mixed=False,
        test=lambda entry: "typed_prompt" in entry.human_action_kinds and not entry.has_prepended_context,
    ),
    Rule(
        name="person_typed_prompt_behind_harness_context",
        description="The person typed this prompt, and the harness put reminder blocks in front of it.",
        label="human",
        mixed=True,
        test=lambda entry: "typed_prompt" in entry.human_action_kinds and entry.has_prepended_context,
    ),
    Rule(
        name="person_rejected_tool_call",
        description="The person refused a tool call; the harness wrote the refusal up as a tool result.",
        label="human",
        mixed=True,
        test=lambda entry: "tool_call_rejection" in entry.human_action_kinds,
    ),
    Rule(
        name="person_interrupted",
        description="The person stopped the agent; the harness wrote a fixed marker or a cancelled tool result.",
        label="human",
        mixed=True,
        test=lambda entry: "interruption" in entry.human_action_kinds,
    ),
    Rule(
        name="harness_wrote_assistant_entry",
        description="Claude Code wrote this assistant entry itself, such as an API error, without calling a model.",
        label="harness",
        mixed=False,
        test=lambda entry: (
            entry.entry_type == "assistant" and (entry.model == "<synthetic>" or entry.is_api_error_message)
        ),
    ),
    Rule(
        name="model_wrote_assistant_entry",
        description="A model wrote this assistant entry: its words, its thinking, or the tool calls it chose.",
        label="ai",
        mixed=False,
        test=lambda entry: entry.entry_type == "assistant" and entry.has_message_content,
    ),
    Rule(
        name="harness_returned_tool_results",
        description="The harness ran tools the model called and reported their results back to it.",
        label="harness",
        mixed=False,
        test=lambda entry: entry.entry_type == "user" and entry.has_tool_results,
    ),
    Rule(
        name="interruption_marker_closing_a_rejection",
        description="The marker Claude Code writes straight after a refusal, already counted with the refusal.",
        label="harness",
        mixed=False,
        test=lambda entry: entry.entry_type == "user" and is_interruption_marker(entry.text_after_context),
    ),
    Rule(
        name="harness_injected_user_entry",
        description="The harness wrote this user entry: a reminder, a command wrapper, a notification or a summary.",
        label="harness",
        mixed=False,
        test=is_harness_injected_user_entry,
    ),
    Rule(
        name="agent_instructed_subagent",
        description="The main agent's model wrote these instructions to start a subagent.",
        label="ai",
        mixed=False,
        test=is_subagent_instructions,
    ),
    Rule(
        name="bookkeeping_entry",
        description="Claude Code wrote this entry to keep track of the Claude session; no one said anything in it.",
        label="harness",
        mixed=False,
        test=lambda entry: entry.entry_type not in ("user", "assistant"),
    ),
)


@dataclass(frozen=True)
class EntryLabel:
    """The actor label one rule gave one entry.

    `entry_id` is the entry's `uuid`, or None for entries that have none.
    `mixed` is true when the entry also holds words from an actor other than
    `actor_label`.
    """

    line_number: int
    entry_id: str | None
    entry_type: str
    actor_label: ActorLabel
    rule_name: str
    mixed: bool


@dataclass(frozen=True)
class UnmatchedEntry:
    """A line no rule could label, and why. The reason names no content."""

    line_number: int
    entry_id: str | None
    entry_type: str | None
    reason: str


@dataclass
class LabelledTranscript:
    """Everything `label_transcript` produced for one transcript.

    Every non-blank line is in exactly one of `labels` and `unmatched`, in
    transcript order. Both versions are recorded, because a label depends on
    the human-action rules as well as on the actor labelling rules.
    """

    labels: list[EntryLabel]
    unmatched: list[UnmatchedEntry]
    rules_version: str = ACTOR_LABEL_RULES_VERSION
    human_action_rules_version: str = HUMAN_ACTION_RULES_VERSION


def label_transcript(transcript_path: Path) -> LabelledTranscript:
    """Read one transcript and label every entry in it.

    This is the module's entry point. Blank lines hold no entry and are
    skipped, as `goodall.human_actions` skips them, so line numbers agree
    between the two.
    """
    human_action_kinds_by_line: dict[int, set[str]] = {}
    for action in parse_human_actions(transcript_path).actions:
        human_action_kinds_by_line.setdefault(action.line_number, set()).add(action.kind)

    known_entry_types = claude_code_markers().known_entry_types
    result = LabelledTranscript(labels=[], unmatched=[])
    with transcript_path.open(encoding="utf-8", errors="replace") as transcript_file:
        for line_number, line in enumerate(transcript_file, start=1):
            if line.strip() == "":
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                result.unmatched.append(UnmatchedEntry(line_number, None, None, "line is not valid JSON"))
                continue
            if not isinstance(entry, dict):
                result.unmatched.append(UnmatchedEntry(line_number, None, None, "line is JSON but not an object"))
                continue
            entry_id = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
            entry_type = entry.get("type")
            if not isinstance(entry_type, str):
                result.unmatched.append(UnmatchedEntry(line_number, entry_id, None, "entry has no type"))
                continue
            if entry_type not in known_entry_types:
                reason = f"unknown entry type {entry_type[:60]!r}"
                result.unmatched.append(UnmatchedEntry(line_number, entry_id, entry_type[:60], reason))
                continue

            human_action_kinds = frozenset(human_action_kinds_by_line.get(line_number, set()))
            facts = facts_from_entry(entry, entry_type, human_action_kinds)
            rule = first_matching_rule(facts)
            if rule is None:
                reason = f"no rule matched this {entry_type} entry"
                if facts.origin_kind is not None:
                    reason += f" (origin kind {facts.origin_kind[:60]!r})"
                result.unmatched.append(UnmatchedEntry(line_number, entry_id, entry_type, reason))
                continue
            label = EntryLabel(line_number, entry_id, entry_type, rule.label, rule.name, rule.mixed)
            result.labels.append(label)
    return result


def first_matching_rule(facts: EntryFacts) -> Rule | None:
    """Return the first rule in `ACTOR_LABEL_RULES` whose test is true, if any."""
    for rule in ACTOR_LABEL_RULES:
        if rule.test(facts):
            return rule
    return None


def facts_from_entry(entry: dict[str, Any], entry_type: str, human_action_kinds: frozenset[str]) -> EntryFacts:
    """Read the facts the rules use out of one entry.

    Text, images and tool results are read only from "user" entries, through
    the human-action parser's own reader, so both modules see a "user" entry
    the same way. Values are checked before use, because a transcript is
    another program's internal file and may hold anything.
    """
    markers = claude_code_markers()
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    has_message_content = isinstance(content, str) or (isinstance(content, list) and len(content) > 0)

    text = ""
    text_after_context = ""
    image_count = 0
    has_tool_results = False
    if entry_type == "user":
        user_content = read_user_content(entry, markers)
        text = user_content.text.lstrip()
        text_after_context = strip_prepended_context(user_content.text, markers)
        image_count = user_content.image_count
        has_tool_results = len(user_content.tool_results) > 0

    origin = entry.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    model = message.get("model") if isinstance(message, dict) else None
    return EntryFacts(
        entry_type=entry_type,
        human_action_kinds=human_action_kinds,
        has_prepended_context=text_after_context != text,
        text_after_context=text_after_context,
        image_count=image_count,
        has_tool_results=has_tool_results,
        has_message_content=has_message_content,
        is_sidechain=entry.get("isSidechain") is True,
        is_meta=entry.get("isMeta") is True,
        is_compact_summary=entry.get("isCompactSummary") is True,
        is_api_error_message=entry.get("isApiErrorMessage") is True,
        origin_kind=origin_kind if isinstance(origin_kind, str) else None,
        model=model if isinstance(model, str) else None,
    )
