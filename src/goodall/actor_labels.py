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
ACTOR_LABEL_RULES_VERSION = "2"

ActorLabel = Literal["human", "ai", "harness"]
Initiator = Literal["human", "ai", "harness"] | None

# Values of a user entry's `origin.kind` naming another agent as the sender:
# an agent in another Claude session ("peer") or one coordinating this session
# ("coordinator"). Humans are humans and agents are agents, whichever one; the
# sender's name, when given, goes in the label's identity.
OTHER_AGENT_ORIGIN_KINDS = ("peer", "coordinator")


@dataclass(frozen=True)
class EntryFacts:
    """What the rules may look at in one entry, read out of it in advance.

    The rules test these facts rather than the raw entry, so each rule's test
    fits on a line or two and cannot crash on a missing key. Add a fact here
    when a new rule needs one.
    """

    entry_type: str
    # Kinds of human action `goodall.human_actions` found on this line, such
    # as "typed_prompt", "interruption" or "shell_command".
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
    # The entry's `agentId`, written on a subagent's entries; None when absent.
    agent_id: str | None
    # The name another agent's message gives for its sender (`origin.name`,
    # or `origin.from`); None when absent.
    origin_sender_name: str | None
    # For a compaction summary: what started the compaction, read from the
    # compaction boundary Claude Code writes just before it ("manual" when
    # the person asked with /compact, "auto" when Claude Code did it on its
    # own); None when no boundary came before.
    compaction_trigger: str | None


@dataclass(frozen=True)
class Rule:
    """One actor labelling rule: if `test` is true, the entry gets `label`.

    `initiated_by` says who asked for the entry to be written, when that is
    clear from the rule: "human", "ai" or "harness", or None when the raw
    record does not make it clear. It is kept apart from the label because
    the two can differ: a session title is the model's words (`ai`), asked
    for by the harness, unprompted by the person or the task.
    """

    name: str
    description: str
    label: ActorLabel
    mixed: bool
    initiated_by: Initiator
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
        initiated_by="human",
        test=lambda entry: "typed_prompt" in entry.human_action_kinds and not entry.has_prepended_context,
    ),
    Rule(
        name="person_typed_prompt_behind_harness_context",
        description="The person typed this prompt, and the harness put reminder blocks in front of it.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "typed_prompt" in entry.human_action_kinds and entry.has_prepended_context,
    ),
    Rule(
        name="person_rejected_tool_call",
        description="The person refused a tool call; the harness wrote the refusal up as a tool result.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "tool_call_rejection" in entry.human_action_kinds,
    ),
    Rule(
        name="person_interrupted",
        description="The person stopped the agent; the harness wrote a fixed marker or a cancelled tool result.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "interruption" in entry.human_action_kinds,
    ),
    Rule(
        name="person_ran_shell_command",
        description="The person ran a shell command with !; the harness wrapped it in tags, often with its output.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "shell_command" in entry.human_action_kinds,
    ),
    Rule(
        name="person_typed_slash_command",
        description="The person typed a slash command; the harness recorded its name and arguments in tags.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "slash_command" in entry.human_action_kinds,
    ),
    Rule(
        name="person_approved_tool_call",
        description="The person allowed a tool call, once or always; the entry also holds the tool's result.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: bool(entry.human_action_kinds & {"tool_call_approval_once", "tool_call_approval_always"}),
    ),
    Rule(
        name="person_dismissed_permission_prompt",
        description="The person dismissed a permission prompt; the harness wrote the refusal up as a tool result.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "permission_prompt_dismissal" in entry.human_action_kinds,
    ),
    Rule(
        name="person_answered_question",
        description="The person answered the agent's questions; the entry also holds the questions and a report.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "question_answer" in entry.human_action_kinds,
    ),
    Rule(
        name="person_approved_plan",
        description="The person approved the agent's plan; the harness wrote the approval up as a tool result.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "plan_approval" in entry.human_action_kinds,
    ),
    Rule(
        name="person_queued_prompt",
        description="The person typed this prompt while the agent was busy, and the harness queued it.",
        label="human",
        mixed=False,
        initiated_by="human",
        test=lambda entry: "queued_prompt" in entry.human_action_kinds,
    ),
    Rule(
        name="person_prompt_blocked_by_hook",
        description="The person sent a prompt that a hook blocked; the harness wrote the hook's warning.",
        label="human",
        mixed=True,
        initiated_by="human",
        test=lambda entry: "blocked_prompt" in entry.human_action_kinds,
    ),
    Rule(
        name="another_agent_sent_message",
        description="Another agent sent this message: an agent in another session, or a coordinating agent.",
        label="ai",
        mixed=False,
        initiated_by="ai",
        test=lambda entry: entry.entry_type == "user" and entry.origin_kind in OTHER_AGENT_ORIGIN_KINDS,
    ),
    # Chosen direction, open to revision: the label says who chose the words,
    # so titles and compaction summaries are the model's, and `initiated_by`
    # says who asked for them. These come before the harness rules below,
    # which would otherwise claim a summary (it is marked isCompactSummary).
    Rule(
        name="model_summarised_conversation_when_asked",
        description="A model summarised the conversation because the person asked with /compact.",
        label="ai",
        mixed=False,
        initiated_by="human",
        test=lambda entry: entry.is_compact_summary and entry.compaction_trigger == "manual",
    ),
    Rule(
        name="model_summarised_conversation_unprompted",
        description="A model summarised the conversation because Claude Code compacted it on its own.",
        label="ai",
        mixed=False,
        initiated_by="harness",
        test=lambda entry: entry.is_compact_summary and entry.compaction_trigger == "auto",
    ),
    Rule(
        name="model_summarised_conversation",
        description="A model summarised the conversation; nothing in the record says what started it.",
        label="ai",
        mixed=False,
        initiated_by=None,
        test=lambda entry: entry.is_compact_summary,
    ),
    Rule(
        name="model_titled_session",
        description="A model wrote a title for the Claude session when Claude Code asked it to.",
        label="ai",
        mixed=False,
        initiated_by="harness",
        test=lambda entry: entry.entry_type == "ai-title",
    ),
    Rule(
        name="session_title_recorded",
        description="Claude Code recorded a title for the Claude session; who chose it is not recorded.",
        label="ai",
        mixed=False,
        initiated_by=None,
        test=lambda entry: entry.entry_type == "custom-title",
    ),
    Rule(
        name="harness_wrote_assistant_entry",
        description="Claude Code wrote this assistant entry itself, such as an API error, without calling a model.",
        label="harness",
        mixed=False,
        initiated_by="harness",
        test=lambda entry: (
            entry.entry_type == "assistant" and (entry.model == "<synthetic>" or entry.is_api_error_message)
        ),
    ),
    Rule(
        name="model_wrote_assistant_entry",
        description="A model wrote this assistant entry: its words, its thinking, or the tool calls it chose.",
        label="ai",
        mixed=False,
        initiated_by=None,
        test=lambda entry: entry.entry_type == "assistant" and entry.has_message_content,
    ),
    Rule(
        name="harness_returned_tool_results",
        description="The harness ran tools the model called and reported their results back to it.",
        label="harness",
        mixed=False,
        initiated_by="ai",
        test=lambda entry: entry.entry_type == "user" and entry.has_tool_results,
    ),
    Rule(
        name="interruption_marker_closing_a_rejection",
        description="The marker Claude Code writes straight after a refusal, already counted with the refusal.",
        label="harness",
        mixed=False,
        initiated_by="human",
        test=lambda entry: entry.entry_type == "user" and is_interruption_marker(entry.text_after_context),
    ),
    Rule(
        name="harness_injected_user_entry",
        description="The harness wrote this user entry: a reminder, a command wrapper, a notification or a summary.",
        label="harness",
        mixed=False,
        initiated_by="harness",
        test=is_harness_injected_user_entry,
    ),
    Rule(
        name="agent_instructed_subagent",
        description="The main agent's model wrote these instructions to start a subagent.",
        label="ai",
        mixed=False,
        initiated_by="ai",
        test=is_subagent_instructions,
    ),
    Rule(
        name="bookkeeping_entry",
        description="Claude Code wrote this entry to keep track of the Claude session; no one said anything in it.",
        label="harness",
        mixed=False,
        initiated_by="harness",
        test=lambda entry: entry.entry_type not in ("user", "assistant"),
    ),
)


@dataclass(frozen=True)
class EntryLabel:
    """The actor label one rule gave one entry.

    `entry_id` is the entry's `uuid`, or None for entries that have none.
    `mixed` is true when the entry also holds words from an actor other than
    `actor_label`. `identity` says which agent within the label, when the
    entry says so (see `identity_of`), and is empty otherwise.
    `initiated_by` says who asked for the entry, as the rule decides (see
    `Rule`), or None when the record does not make it clear.
    """

    line_number: int
    entry_id: str | None
    entry_type: str
    actor_label: ActorLabel
    rule_name: str
    mixed: bool
    identity: tuple[str, ...] = ()
    initiated_by: Initiator = None


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
    # What started the last compaction, until the summary after it is read.
    compaction_trigger: str | None = None
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
            # Whether a program sending a prompt counts as the person is
            # undecided, so no rule may label it, and it is reported instead.
            if "sdk_prompt" in human_action_kinds:
                reason = "prompt sent through the SDK: whether that is the person is undecided"
                result.unmatched.append(UnmatchedEntry(line_number, entry_id, entry_type, reason))
                continue
            facts = facts_from_entry(entry, entry_type, human_action_kinds, compaction_trigger)
            # A compaction boundary's trigger belongs to the summary written
            # straight after it, and to nothing later.
            if facts.is_compact_summary:
                compaction_trigger = None
            if entry_type == "system" and entry.get("subtype") == "compact_boundary":
                compaction_trigger = compaction_trigger_of(entry)
            rule = first_matching_rule(facts)
            if rule is None:
                reason = f"no rule matched this {entry_type} entry"
                if facts.origin_kind is not None:
                    reason += f" (origin kind {facts.origin_kind[:60]!r})"
                result.unmatched.append(UnmatchedEntry(line_number, entry_id, entry_type, reason))
                continue
            label = EntryLabel(
                line_number,
                entry_id,
                entry_type,
                rule.label,
                rule.name,
                rule.mixed,
                identity=identity_of(facts, rule.label),
                initiated_by=rule.initiated_by,
            )
            result.labels.append(label)
    return result


def compaction_trigger_of(boundary_entry: dict[str, Any]) -> str | None:
    """Return a compaction boundary's `compactMetadata.trigger`, such as "manual" or "auto"."""
    metadata = boundary_entry.get("compactMetadata")
    trigger = metadata.get("trigger") if isinstance(metadata, dict) else None
    return trigger if isinstance(trigger, str) else None


@dataclass(frozen=True)
class LabelSummary:
    """How one labelled transcript divides, for a researcher to see at a glance.

    Every label and every rule appears, with zero when unused, so a rule that
    stopped matching after a Claude Code update shows up as a zero rather than
    as a missing line. Rules are listed in the order they are applied.
    """

    rules_version: str
    entries_by_label: dict[str, int]
    entries_by_rule: dict[str, int]
    mixed_entries: int
    unmatched_entries: int


def summarise_labels(labelled_transcript: LabelledTranscript) -> LabelSummary:
    """Count the labels, the rules that decided them, mixed and unmatched entries."""
    entries_by_label = {"human": 0, "ai": 0, "harness": 0}
    entries_by_rule: dict[str, int] = {}
    for rule in ACTOR_LABEL_RULES:
        entries_by_rule[rule.name] = 0
    mixed_entries = 0
    for label in labelled_transcript.labels:
        entries_by_label[label.actor_label] += 1
        entries_by_rule[label.rule_name] += 1
        if label.mixed:
            mixed_entries += 1
    return LabelSummary(
        rules_version=labelled_transcript.rules_version,
        entries_by_label=entries_by_label,
        entries_by_rule=entries_by_rule,
        mixed_entries=mixed_entries,
        unmatched_entries=len(labelled_transcript.unmatched),
    )


def first_matching_rule(facts: EntryFacts) -> Rule | None:
    """Return the first rule in `ACTOR_LABEL_RULES` whose test is true, if any."""
    for rule in ACTOR_LABEL_RULES:
        if rule.test(facts):
            return rule
    return None


def facts_from_entry(
    entry: dict[str, Any], entry_type: str, human_action_kinds: frozenset[str], compaction_trigger: str | None
) -> EntryFacts:
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
    origin_kind = None
    origin_sender_name = None
    if isinstance(origin, dict):
        origin_kind = origin.get("kind")
        for sender_field in ("name", "from"):
            if isinstance(origin.get(sender_field), str) and origin_sender_name is None:
                origin_sender_name = origin[sender_field]
    model = message.get("model") if isinstance(message, dict) else None
    agent_id = entry.get("agentId")
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
        agent_id=agent_id if isinstance(agent_id, str) else None,
        origin_sender_name=origin_sender_name,
        compaction_trigger=compaction_trigger if entry.get("isCompactSummary") is True else None,
    )


def identity_of(facts: EntryFacts, actor_label: ActorLabel) -> tuple[str, ...]:
    """Say which agent produced an `ai` entry, when the entry itself says so.

    The actor label stays one of three kinds; this says which one inside the
    kind, as "what:value" parts: `model:<model>`, `subagent:<agentId>`,
    `peer:<name>` or `coordinator:<name>`. Nothing is inferred. A `human`
    entry gets no identity, because a transcript does not say which person
    typed; a `harness` entry gets none either.
    """
    if actor_label != "ai":
        return ()
    parts = []
    if facts.entry_type == "assistant" and facts.model is not None:
        parts.append(f"model:{facts.model}")
    if facts.entry_type == "assistant" and facts.is_sidechain and facts.agent_id is not None:
        parts.append(f"subagent:{facts.agent_id}")
    if facts.origin_kind in OTHER_AGENT_ORIGIN_KINDS and facts.origin_sender_name is not None:
        parts.append(f"{facts.origin_kind}:{facts.origin_sender_name}")
    return tuple(parts)
