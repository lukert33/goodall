"""Find what the person did in one Claude Code transcript.

A transcript is Claude Code's own JSON Lines file for one Claude session. Its
"user" entries mix what the person typed with text the harness wrote in the
person's name: tool results, injected reminders, command wrappers. This module
separates them, and returns one record per human action, in transcript order.
A few actions sit outside "user" entries: a prompt queued while the agent was
busy (an "attachment" entry) and a prompt a hook blocked (a "system" entry).

Who calls it: derivation code that builds derived records from raw records
(ADR 0003), `goodall.actor_labels`, and `goodall.human_activity`, which puts
these actions beside those from other sources. It reads the file it is given
and nothing else, imports only the standard library, and never needs Inspect
(AGENTS.md invariants 7 and 8).

The kinds are deliberately split finely, to be merged or renamed after real
results are reviewed. Every record carries `source`, `kind` and `rule` so that
can be done without re-reading transcripts.

Design decisions a reader would otherwise reverse-engineer:

- The detection rules are the part most likely to change, so each one is a
  small function named `is_...` or `..._from_...`, and every fixed string
  Claude Code writes is in `claude_code_markers()`. The rules are described in
  plain words in docs/human-action-rules.md; change both together and raise
  RULES_VERSION (ADR 0004).
- Field values borrow Inspect's vocabulary (InterruptEvent, ApprovalEvent) so
  the same rules could later be offered to Inspect's transcript importer.
- Anything the parser does not recognize is reported in `unfamiliar_entries`,
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
    # Entry types Claude Code is known to write. Human actions are found in
    # "user" entries and in a few "attachment" and "system" entries; the
    # rest are bookkeeping, and any other type is reported as unfamiliar.
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
    # Values of `permissionDecision.source` on a tool result entry. The first
    # three are the person at Claude Code's permission prompt; the others
    # decided without asking them. Any other value is reported as unfamiliar.
    approved_once_source: str
    approved_always_source: str
    dismissed_source: str
    known_permission_sources: tuple[str, ...]
    # The tool the agent presents a plan with, and how its result begins when
    # the person approved the plan.
    plan_tool_name: str
    plan_approved_result_prefix: str
    # The attachment type and command mode of a prompt the person typed while
    # the agent was busy.
    queued_command_attachment_type: str
    queued_prompt_command_mode: str
    # How the warning begins that Claude Code writes when a UserPromptSubmit
    # hook (goodall's prompt gate, for one) blocks a prompt.
    prompt_blocked_by_hook_prefix: str
    # Values of a "user" entry's `turnOrigin`, which newer versions write
    # beside or instead of `origin`. "human" is the person and "sdk" a
    # program; the rest are other senders.
    sdk_turn_origin: str
    known_turn_origins: tuple[str, ...]
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
        approved_once_source="user_temporary",
        approved_always_source="user_permanent",
        dismissed_source="user_abort",
        known_permission_sources=("user_temporary", "user_permanent", "user_abort", "config", "hook"),
        plan_tool_name="ExitPlanMode",
        plan_approved_result_prefix="User has approved your plan",
        queued_command_attachment_type="queued_command",
        queued_prompt_command_mode="prompt",
        prompt_blocked_by_hook_prefix="UserPromptSubmit operation blocked by hook",
        sdk_turn_origin="sdk",
        known_turn_origins=("human", "sdk", "peer", "task_notification", "host_synthetic", "scheduled"),
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


@dataclass(frozen=True, kw_only=True)
class ToolCallApproval:
    """The person allowed a tool call when Claude Code asked them.

    `kind` keeps the two choices apart: "tool_call_approval_once" (allow this
    call) and "tool_call_approval_always" (allow it and add a standing rule,
    so later calls like it are not asked about). `decision` is always
    "approve", Inspect's ApprovalEvent value. `permission_source`,
    `grant_scope` and `rule_source` are Claude Code's own values, kept as
    written so the split can be revisited.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    kind: Literal["tool_call_approval_once", "tool_call_approval_always"]
    tool_call_id: str | None
    tool_name: str | None
    permission_source: str
    grant_scope: str | None
    rule_source: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    decision: Literal["approve"] = field(default="approve", init=False)
    source: str = field(default=SOURCE, init=False)
    rule: str = field(default="approval_from_permission_decision", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class PermissionPromptDismissal:
    """The person dismissed Claude Code's permission prompt without choosing.

    Recorded by Claude Code as a refusal whose source is "user_abort". It is
    kept apart from `ToolCallRejection`, where the person chose to refuse,
    because the transcript does not say what the person meant by it.
    `decision` is "reject", Inspect's ApprovalEvent value, because the call
    did not run.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    tool_call_id: str | None
    tool_name: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    decision: Literal["reject"] = field(default="reject", init=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="permission_prompt_dismissal", init=False)
    rule: str = field(default="dismissal_from_permission_decision", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class QuestionAnswer:
    """The person answered questions the agent asked through its question tool.

    `answers` are the person's choices or typed answers, in the order Claude
    Code recorded them; they are the person's words and are kept like a
    prompt's text. The questions themselves are the model's words and are
    not kept; `tool_call_id` points at the call that asked them.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    tool_call_id: str | None
    question_count: int
    answers: tuple[str, ...]
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="question_answer", init=False)
    rule: str = field(default="question_answer_from_tool_result", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class PlanApproval:
    """The person approved a plan the agent presented before starting work.

    `decision` is always "approve", Inspect's ApprovalEvent value. Rejecting a
    plan is recorded by Claude Code as a refused tool call, and is found as a
    `ToolCallRejection` whose `tool_name` is the plan tool.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    tool_call_id: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    decision: Literal["approve"] = field(default="approve", init=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="plan_approval", init=False)
    rule: str = field(default="plan_approval_from_tool_result", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class QueuedPrompt:
    """A prompt the person typed while the agent was still working.

    Claude Code queues it and hands it to the model part-way through the turn,
    recording it in an attachment entry rather than a user entry, so it is a
    kind of its own: the person redirected a running agent. `timestamp` is
    when the person queued it, when Claude Code recorded that, otherwise when
    it was delivered.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    text: str
    image_count: int
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="queued_prompt", init=False)
    rule: str = field(default="queued_prompt_from_attachment", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class BlockedPrompt:
    """A prompt the person sent that a UserPromptSubmit hook refused.

    goodall's prompt gate is such a hook, so this is how a prompt sent with
    no work block active shows up. Claude Code writes no user entry for it,
    only a warning, so the person's attempt would otherwise go uncounted.
    `text` is the prompt as Claude Code queued it just before the warning,
    or None when no undelivered queued prompt was found. Any hook can block
    a prompt; the hook's message is not kept, because it may quote the
    prompt or other private text.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    text: str | None
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="blocked_prompt", init=False)
    rule: str = field(default="blocked_prompt_from_hook_warning", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class SdkPrompt:
    """A prompt a program sent through Claude Code's SDK, not marked as typed by a person.

    The program may be acting for the person (a script they run) or on its
    own (an automation). Whether this counts as the person is undecided, so it
    is a kind of its own, never a `TypedPrompt`, until real results show how
    often it happens and what it holds.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    text: str
    image_count: int
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="sdk_prompt", init=False)
    rule: str = field(default="is_sdk_prompt", init=False)
    duration_seconds: float | None = field(default=None, init=False)


@dataclass(frozen=True, kw_only=True)
class PermissionModeChange:
    """The permission mode differed from the one on the person's previous prompt.

    Claude Code writes the permission mode (such as "default", "plan",
    "acceptEdits", "auto") on each prompt the person sends, and nowhere at the
    moment it changes. So the change is seen on the next prompt, and takes
    that prompt's place and time; it happened some time after the previous
    prompt. The person usually changes the mode themselves, but approving a
    plan also leaves plan mode, and an agent can ask to enter plan mode.
    """

    line_number: int
    entry_id: str | None
    session_id: str | None
    timestamp: str | None
    from_mode: str
    to_mode: str
    transcript_path: Path | None = field(default=None, compare=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="permission_mode_change", init=False)
    rule: str = field(default="permission_mode_change_between_prompts", init=False)
    duration_seconds: float | None = field(default=None, init=False)


TranscriptAction = (
    TypedPrompt
    | PermissionModeChange
    | SdkPrompt
    | QueuedPrompt
    | BlockedPrompt
    | Interruption
    | ToolCallRejection
    | ShellCommand
    | SlashCommand
    | ToolCallApproval
    | PermissionPromptDismissal
    | QuestionAnswer
    | PlanApproval
)


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
    """An entry whose type or shape the parser does not recognize."""

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
            if entry_type == "queue-operation":
                remember_queued_prompt(entry, memory)
                continue
            if entry_type == "system":
                blocked_prompt = blocked_prompt_from_hook_warning(entry, place, markers, memory)
                if blocked_prompt is not None:
                    result.actions.append(blocked_prompt)
                continue
            if entry_type == "attachment":
                queued_prompt = queued_prompt_from_attachment(entry, place, markers)
                if queued_prompt is not None:
                    result.actions.append(queued_prompt)
                continue
            if entry_type != "user":
                continue
            # A user entry means the queued prompt, if any, was delivered.
            memory.undelivered_prompt = None
            user_content = read_user_content(entry, markers)
            for reason in user_content.unfamiliar_reasons:
                result.unfamiliar_entries.append(UnfamiliarEntry(line_number, entry_id, reason))
            link_shell_command_output(entry, place, user_content.text, result.actions, memory, markers)
            actions = actions_from_user_entry(entry, place, user_content, markers, memory)
            for action in actions:
                if isinstance(action, TypedPrompt):
                    mode_change = permission_mode_change_between_prompts(entry, place, memory)
                    if mode_change is not None:
                        result.actions.append(mode_change)
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
    # Code writes straight after one can be recognized as part of it.
    rejection_entry_ids: set[str] = field(default_factory=set)
    # The tool each tool call asked for, by call id, read from the assistant
    # entries that made the calls. Only names are kept, never tool inputs.
    tool_names_by_call_id: dict[str, str] = field(default_factory=dict)
    # Where in the action list each shell command with no output yet sits, by
    # its entry id, so older versions' separate output entry can be linked.
    shell_command_positions: dict[str, int] = field(default_factory=dict)
    # The text of the last prompt Claude Code queued and has not yet written
    # as a user entry, so a blocked prompt can say what was blocked.
    undelivered_prompt: str | None = None
    # The permission mode written on the person's last prompt that had one.
    last_permission_mode: str | None = None


def permission_mode_change_between_prompts(
    entry: dict[str, Any], place: EntryPlace, memory: TranscriptMemory
) -> PermissionModeChange | None:
    """Rule: a typed prompt whose permission mode differs from the last one is a mode change.

    Only the person's typed prompts are compared, so a mode written on a
    program's prompt or a tool result cannot produce a change. A prompt with
    no mode is skipped, and the next one is compared with the last known mode.
    """
    permission_mode = entry.get("permissionMode")
    if not isinstance(permission_mode, str):
        return None
    previous_mode = memory.last_permission_mode
    memory.last_permission_mode = permission_mode
    if previous_mode is None or previous_mode == permission_mode:
        return None
    return PermissionModeChange(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        from_mode=previous_mode,
        to_mode=permission_mode,
    )


def remember_queued_prompt(entry: dict[str, Any], memory: TranscriptMemory) -> None:
    """Note the text of a prompt as Claude Code queues it.

    Claude Code writes a `queue-operation` entry with `operation: enqueue` and
    the prompt as `content` before deciding what to do with it. These entries
    are bookkeeping, not actions: a delivered prompt is counted from its user
    entry, a blocked one from the hook's warning.
    """
    if entry.get("operation") == "enqueue" and isinstance(entry.get("content"), str):
        memory.undelivered_prompt = entry["content"]


def blocked_prompt_from_hook_warning(
    entry: dict[str, Any], place: EntryPlace, markers: ClaudeCodeMarkers, memory: TranscriptMemory
) -> BlockedPrompt | None:
    """Rule: a system warning saying a UserPromptSubmit hook blocked the prompt is a blocked prompt.

    Seen as a `system` entry with `subtype: informational` whose `content`
    begins with fixed text. The prompt's text is taken from the last queued
    prompt not yet delivered, and is then used up, so one queued prompt is
    never given to two blocked prompts.
    """
    if entry.get("subtype") != "informational":
        return None
    content = entry.get("content")
    if not isinstance(content, str) or not content.startswith(markers.prompt_blocked_by_hook_prefix):
        return None
    text = memory.undelivered_prompt
    memory.undelivered_prompt = None
    return BlockedPrompt(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        text=text,
    )


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
    recognize. The recognized parts are still used, so a new kind of block
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

    turn_origin = entry.get("turnOrigin")
    if turn_origin is not None and (not isinstance(turn_origin, str) or turn_origin not in markers.known_turn_origins):
        user_content.unfamiliar_reasons.append(f"unknown turn origin {str(turn_origin)[:60]!r}")

    permission_decision = entry.get("permissionDecision")
    if isinstance(permission_decision, dict):
        permission_source = permission_decision.get("source")
        if not isinstance(permission_source, str) or permission_source not in markers.known_permission_sources:
            reason = f"unknown permission decision source {str(permission_source)[:60]!r}"
            user_content.unfamiliar_reasons.append(reason)
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
            answer = question_answer_from_tool_result(entry, place, tool_result)
            if answer is not None:
                actions.append(answer)
            plan_approval = plan_approval_from_tool_result(place, tool_result, markers, memory)
            if plan_approval is not None:
                actions.append(plan_approval)
        # The permission decision is written on the entry, not on a result,
        # so it is read only when the entry answers exactly one call. When the
        # person already acted on that call in another way (answering the
        # question tool writes a one-off approval too), the decision is part
        # of that action and is not counted again.
        if len(content.tool_results) == 1 and not actions:
            decision = permission_decision_from_entry(entry, place, content.tool_results[0], markers, memory)
            if decision is not None:
                actions.append(decision)
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

    if is_sdk_prompt(entry, typed_text, content.image_count, markers):
        sdk_prompt = SdkPrompt(
            line_number=place.line_number,
            entry_id=place.entry_id,
            session_id=place.session_id,
            timestamp=place.timestamp,
            transcript_path=place.transcript_path,
            text=typed_text,
            image_count=content.image_count,
        )
        return [sdk_prompt]
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


def queued_prompt_from_attachment(
    entry: dict[str, Any], place: EntryPlace, markers: ClaudeCodeMarkers
) -> QueuedPrompt | None:
    """Rule: a queued-command attachment holding a prompt from the person is a queued prompt.

    The attachment has `type: queued_command`, `commandMode: prompt` and an
    `origin.kind` of "human". The same attachment type also carries task
    notifications and messages from other sessions, told apart by
    `commandMode` and `origin`. A queued prompt with no origin is not
    counted: in the structure scan those were all injected reminders.
    """
    attachment = entry.get("attachment")
    if not isinstance(attachment, dict):
        return None
    if attachment.get("type") != markers.queued_command_attachment_type:
        return None
    if attachment.get("commandMode") != markers.queued_prompt_command_mode:
        return None
    if attachment.get("isMeta") is True:
        return None
    origin = attachment.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    # The prompt is stored like a user entry's message content, so it is read
    # the same way.
    prompt_content = read_user_content({"message": {"content": attachment.get("prompt")}}, markers)
    typed_text = strip_prepended_context(prompt_content.text, markers)
    if typed_text == "" and prompt_content.image_count == 0:
        return None
    queued_at = attachment.get("timestamp")
    return QueuedPrompt(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=queued_at if isinstance(queued_at, str) else place.timestamp,
        transcript_path=place.transcript_path,
        text=typed_text,
        image_count=prompt_content.image_count,
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


def question_answer_from_tool_result(
    entry: dict[str, Any], place: EntryPlace, tool_result: dict[str, Any]
) -> QuestionAnswer | None:
    """Rule: a successful tool result recording questions and answers is the person answering.

    The agent's question tool (AskUserQuestion) records its result in the
    entry's `toolUseResult` as `questions` (a list) and `answers` (question
    text to answer). The structure, not the wording, is matched, because the
    text reported to the model is not fixed. Declining to answer is a refused
    tool call, found by `rejection_from_tool_result`.
    """
    if tool_result.get("is_error") is True:
        return None
    tool_use_result = entry.get("toolUseResult")
    if not isinstance(tool_use_result, dict):
        return None
    questions = tool_use_result.get("questions")
    answers_by_question = tool_use_result.get("answers")
    if not isinstance(questions, list) or not isinstance(answers_by_question, dict):
        return None
    answers = []
    for answer in answers_by_question.values():
        if isinstance(answer, str):
            answers.append(answer)
    tool_call_id = tool_result.get("tool_use_id")
    return QuestionAnswer(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        question_count=len(questions),
        answers=tuple(answers),
    )


def plan_approval_from_tool_result(
    place: EntryPlace, tool_result: dict[str, Any], markers: ClaudeCodeMarkers, memory: TranscriptMemory
) -> PlanApproval | None:
    """Rule: a successful plan-tool result beginning with the approval text is a plan approval.

    The trap: the approval text alone is not enough, because any command's
    output could begin with the same words. The result must answer a call to
    the plan tool; when the call is not in this transcript, the text decides.
    """
    if tool_result.get("is_error") is True:
        return None
    if not tool_result_text(tool_result).startswith(markers.plan_approved_result_prefix):
        return None
    tool_name = tool_name_for(tool_result, memory)
    if tool_name is not None and tool_name != markers.plan_tool_name:
        return None
    tool_call_id = tool_result.get("tool_use_id")
    return PlanApproval(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
    )


def permission_decision_from_entry(
    entry: dict[str, Any],
    place: EntryPlace,
    tool_result: dict[str, Any],
    markers: ClaudeCodeMarkers,
    memory: TranscriptMemory,
) -> ToolCallApproval | PermissionPromptDismissal | None:
    """Apply the two rules that read the person's permission decision on a tool result entry.

    Newer Claude Code versions (seen from 2.1.289) write `permissionDecision`
    on every tool result entry, saying who allowed or refused the call. Only
    the sources naming the person count; "config" (settings, the permission
    mode or the auto-mode classifier) and "hook" decided without asking them.
    A refusal the person chose leaves no decision here and is found by
    `rejection_from_tool_result` instead.
    """
    permission_decision = entry.get("permissionDecision")
    if not isinstance(permission_decision, dict):
        return None
    dismissal = dismissal_from_permission_decision(permission_decision, place, tool_result, markers, memory)
    if dismissal is not None:
        return dismissal
    return approval_from_permission_decision(permission_decision, place, tool_result, markers, memory)


def dismissal_from_permission_decision(
    permission_decision: dict[str, Any],
    place: EntryPlace,
    tool_result: dict[str, Any],
    markers: ClaudeCodeMarkers,
    memory: TranscriptMemory,
) -> PermissionPromptDismissal | None:
    """Rule: a refusal whose source is "user_abort" is the person dismissing the permission prompt."""
    if permission_decision.get("source") != markers.dismissed_source:
        return None
    if permission_decision.get("decision") != "reject":
        return None
    tool_call_id = tool_result.get("tool_use_id")
    return PermissionPromptDismissal(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
        tool_name=tool_name_for(tool_result, memory),
    )


def approval_from_permission_decision(
    permission_decision: dict[str, Any],
    place: EntryPlace,
    tool_result: dict[str, Any],
    markers: ClaudeCodeMarkers,
    memory: TranscriptMemory,
) -> ToolCallApproval | None:
    """Rule: an acceptance whose source is "user_temporary" or "user_permanent" is the person approving.

    "user_temporary" is allowing this call once; "user_permanent" is "always
    allow", which also adds a standing rule.
    """
    if permission_decision.get("decision") != "accept":
        return None
    permission_source = permission_decision.get("source")
    tool_call_id = tool_result.get("tool_use_id")
    if not isinstance(tool_call_id, str):
        tool_call_id = None
    if permission_source == markers.approved_once_source:
        kind: Literal["tool_call_approval_once", "tool_call_approval_always"] = "tool_call_approval_once"
    elif permission_source == markers.approved_always_source:
        kind = "tool_call_approval_always"
    else:
        return None
    grant_scope = permission_decision.get("grantScope")
    rule_source = permission_decision.get("ruleSource")
    return ToolCallApproval(
        line_number=place.line_number,
        entry_id=place.entry_id,
        session_id=place.session_id,
        timestamp=place.timestamp,
        transcript_path=place.transcript_path,
        kind=kind,
        tool_call_id=tool_call_id,
        tool_name=tool_name_for(tool_result, memory),
        permission_source=permission_source,
        grant_scope=grant_scope if isinstance(grant_scope, str) else None,
        rule_source=rule_source if isinstance(rule_source, str) else None,
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
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    # Some entries carry `turnOrigin` and no origin. Any sender other than
    # "human" there is not the person; "sdk" is found by its own rule. The
    # trap: `promptSource: "sdk"` is not this field. An app that drives Claude
    # Code through the SDK writes it on prompts the person typed.
    turn_origin = entry.get("turnOrigin")
    if turn_origin is not None and turn_origin != "human":
        return False
    return True


def has_prompt_text(typed_text: str, image_count: int, markers: ClaudeCodeMarkers) -> bool:
    """True when the text left after prepended context is a prompt, not a harness wrapper or nothing."""
    # Wrappers the harness writes: slash-command records, shell commands and
    # their output, task notifications. These are checked even when origin
    # says "human", because what the person typed inside a wrapper is a
    # different kind of action, found by its own rule.
    for prefix in markers.harness_text_prefixes:
        if typed_text.startswith(prefix):
            return False
    # A prompt has something in it. An entry with neither text nor an image is
    # either all harness context or a shape the parser did not understand.
    return not (typed_text == "" and image_count == 0)


def is_sdk_prompt(entry: dict[str, Any], typed_text: str, image_count: int, markers: ClaudeCodeMarkers) -> bool:
    """Rule: a prompt with no origin and a `turnOrigin` of "sdk" was sent by a program.

    Apart from who sent it, it is judged as a typed prompt is: not meta, not
    a compaction summary, not on a sidechain, and holding prompt text.
    """
    if entry.get("isMeta") is True or entry.get("isCompactSummary") is True or entry.get("isSidechain") is True:
        return False
    if isinstance(entry.get("origin"), dict) or entry.get("turnOrigin") != markers.sdk_turn_origin:
        return False
    return has_prompt_text(typed_text, image_count, markers)


def is_typed_prompt(entry: dict[str, Any], typed_text: str, image_count: int, markers: ClaudeCodeMarkers) -> bool:
    """Rule: decide whether a "user" entry holding text was typed by the person.

    `typed_text` is the entry's text with any prepended context already
    removed. The trap: Claude Code writes `userType: "external"` on every user
    entry, injected or not, so that field cannot tell them apart. The checks
    below run from the most to the least reliable signal.
    """
    if not is_from_the_person(entry):
        return False
    return has_prompt_text(typed_text, image_count, markers)
