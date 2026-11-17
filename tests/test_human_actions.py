"""Tests for goodall.human_actions: finding what the person did in a transcript.

Each test hands a hand-written transcript from tests/fixtures/ to
`parse_human_actions`, the module's one entry point, and checks the records
that come back. Every fixture is synthetic: invented text, placeholder paths
and ids, shaped like the entries Claude Code writes. Real transcripts hold a
contributor's private prompts and must never be copied into fixtures.
"""

from pathlib import Path

from goodall.human_actions import (
    RULES_VERSION,
    Interruption,
    ShellCommand,
    SlashCommand,
    ToolCallRejection,
    TypedPrompt,
    UnfamiliarEntry,
    parse_human_actions,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_transcripts"


def test_typed_prompts_are_found_with_their_entry_and_time() -> None:
    result = parse_human_actions(FIXTURES / "typed_prompts.jsonl")

    assert result.actions == [
        TypedPrompt(
            line_number=1,
            entry_id="u-0001",
            session_id="session-a",
            timestamp="2026-01-05T09:00:00.000Z",
            text="Please add a greeting function to the example module.",
            image_count=0,
        ),
        TypedPrompt(
            line_number=3,
            entry_id="u-0003",
            session_id="session-a",
            timestamp="2026-01-05T09:01:00.000Z",
            text="Here is a sketch of the layout I want.",
            image_count=1,
        ),
        # Lines 4 and 5: the harness put reminders in front of the typed text.
        # The prompt is kept, and its text is only what the person typed.
        TypedPrompt(
            line_number=4,
            entry_id="u-0004",
            session_id="session-a",
            timestamp="2026-01-05T09:02:00.000Z",
            text="Now add a farewell function too.",
            image_count=0,
        ),
        TypedPrompt(
            line_number=5,
            entry_id="u-0005",
            session_id="session-a",
            timestamp="2026-01-05T09:03:00.000Z",
            text="And a test for each.",
            image_count=0,
        ),
    ]
    assert result.unfamiliar_entries == []


def test_every_action_says_its_source_the_rule_that_found_it_and_its_file() -> None:
    # These fields let actions from different sources sit in one list and be
    # regrouped later: which raw record, which rule, which file.
    transcript_path = FIXTURES / "interruptions.jsonl"
    result = parse_human_actions(transcript_path)

    rules = [action.rule for action in result.actions]
    assert rules == [
        "is_typed_prompt",
        "interruption_from_marker",
        "is_typed_prompt",
        "interruption_from_tool_result",
        "is_typed_prompt",
        "interruption_from_marker",
    ]
    for action in result.actions:
        assert action.source == "claude_code_transcript"
        assert action.transcript_path == transcript_path
        # A transcript entry is a moment, not a stretch of time.
        assert action.duration_seconds is None
    assert result.rules_version == RULES_VERSION == "2"


def test_interruptions_say_what_was_interrupted() -> None:
    result = parse_human_actions(FIXTURES / "interruptions.jsonl")

    interruptions = [action for action in result.actions if isinstance(action, Interruption)]
    assert interruptions == [
        # Stopped while the model was writing its reply.
        Interruption(
            line_number=3,
            entry_id="u-0003",
            session_id="session-c",
            timestamp="2026-01-07T11:00:04.000Z",
            rule="interruption_from_marker",
            interrupted="generate",
            interrupted_tool_call_id=None,
        ),
        # Stopped a running tool; the cancelled tool result names the call.
        Interruption(
            line_number=6,
            entry_id="u-0006",
            session_id="session-c",
            timestamp="2026-01-07T11:01:30.000Z",
            rule="interruption_from_tool_result",
            interrupted="tool_call",
            interrupted_tool_call_id="toolu_example_03",
        ),
        # Stopped during a tool call, recorded only by the marker text, which
        # does not name the call.
        Interruption(
            line_number=9,
            entry_id="u-0009",
            session_id="session-c",
            timestamp="2026-01-07T11:02:10.000Z",
            rule="interruption_from_marker",
            interrupted="tool_call",
            interrupted_tool_call_id=None,
        ),
    ]
    # The three prompts are still found, and the markers are not prompts.
    prompt_lines = [action.line_number for action in result.actions if isinstance(action, TypedPrompt)]
    assert prompt_lines == [1, 4, 7]
    assert result.unfamiliar_entries == []


def test_rejected_tool_calls_carry_the_call_id_and_any_feedback() -> None:
    result = parse_human_actions(FIXTURES / "rejections.jsonl")

    assert result.actions == [
        TypedPrompt(
            line_number=1,
            entry_id="u-0001",
            session_id="session-d",
            timestamp="2026-01-08T12:00:00.000Z",
            text="Tidy up the example configuration file.",
            image_count=0,
        ),
        ToolCallRejection(
            line_number=3,
            entry_id="u-0003",
            session_id="session-d",
            timestamp="2026-01-08T12:00:20.000Z",
            tool_call_id="toolu_example_05",
            tool_name="Write",
            feedback=None,
        ),
        # Line 4 is the interruption marker Claude Code writes after a
        # rejection without feedback. It is part of the rejection, not a
        # second action, so it is not counted.
        TypedPrompt(
            line_number=5,
            entry_id="u-0005",
            session_id="session-d",
            timestamp="2026-01-08T12:01:00.000Z",
            text="Only change the indentation.",
            image_count=0,
        ),
        ToolCallRejection(
            line_number=7,
            entry_id="u-0007",
            session_id="session-d",
            timestamp="2026-01-08T12:01:30.000Z",
            tool_call_id="toolu_example_06",
            tool_name="Edit",
            feedback="Use two spaces, not four.",
        ),
        # Line 9 was denied by a permission rule, not by the person.
    ]
    assert result.unfamiliar_entries == []


def test_a_prompt_typed_straight_after_a_rejection_is_still_counted() -> None:
    # Only the interruption marker is folded into a rejection; anything else
    # whose parent is the rejection entry is judged on its own.
    result = parse_human_actions(FIXTURES / "prompt_after_rejection.jsonl")

    kinds = [action.kind for action in result.actions]
    assert kinds == ["tool_call_rejection", "typed_prompt"]


def test_a_rejection_uses_inspects_decision_value() -> None:
    result = parse_human_actions(FIXTURES / "rejections.jsonl")

    rejection = result.actions[1]
    assert isinstance(rejection, ToolCallRejection)
    assert rejection.decision == "reject"
    assert rejection.kind == "tool_call_rejection"


def test_unfamiliar_and_malformed_entries_are_reported_and_parsing_continues() -> None:
    result = parse_human_actions(FIXTURES / "unfamiliar_and_malformed.jsonl")

    assert result.unfamiliar_entries == [
        UnfamiliarEntry(line_number=2, entry_id=None, reason="line is not valid JSON"),
        UnfamiliarEntry(line_number=4, entry_id=None, reason="line is JSON but not an object"),
        UnfamiliarEntry(line_number=5, entry_id="x-0005", reason="unknown entry type 'example-future-entry'"),
        UnfamiliarEntry(line_number=6, entry_id="x-0006", reason="entry has no type"),
        UnfamiliarEntry(line_number=7, entry_id="u-0007", reason="user entry has no message content"),
        UnfamiliarEntry(line_number=8, entry_id="u-0008", reason="unknown content block type 'example-future-block'"),
        UnfamiliarEntry(line_number=9, entry_id="u-0009", reason="unknown origin kind 'example-future-origin'"),
    ]
    # The blank line 3 is not an entry and is skipped. The prompt on line 8 is
    # still found despite its unfamiliar block; line 9 is not counted, because
    # only an origin of "human" counts as the person.
    prompt_lines = [action.line_number for action in result.actions]
    assert prompt_lines == [1, 8, 10]


def test_shell_commands_the_person_ran_are_found_with_a_link_to_their_output() -> None:
    result = parse_human_actions(FIXTURES / "shell_commands.jsonl")

    assert result.actions == [
        # Newer Claude Code writes the command and its output in one entry.
        ShellCommand(
            line_number=1,
            entry_id="u-0001",
            session_id="session-g",
            timestamp="2026-03-02T10:00:00.000Z",
            text="ls example-folder",
            output_line_number=1,
            output_entry_id="u-0001",
        ),
        # Older Claude Code writes the output as the next entry. The link
        # names that entry; the output itself is not copied.
        ShellCommand(
            line_number=3,
            entry_id="u-0003",
            session_id="session-g",
            timestamp="2026-03-02T10:01:00.000Z",
            text="make example",
            output_line_number=4,
            output_entry_id="u-0004",
        ),
        # Near miss: a prompt that mentions the tag part-way through is a prompt.
        TypedPrompt(
            line_number=5,
            entry_id="u-0005",
            session_id="session-g",
            timestamp="2026-03-02T10:02:00.000Z",
            text="Why does the text <bash-input> appear in the example log?",
            image_count=0,
        ),
        # Line 6 is marked meta, so the harness wrote it. Line 7 is output
        # with no command before it. Neither is the person.
        # A command with no output yet has no link.
        ShellCommand(
            line_number=8,
            entry_id="u-0008",
            session_id="session-g",
            timestamp="2026-03-02T10:05:00.000Z",
            text="git status",
            output_line_number=None,
            output_entry_id=None,
        ),
    ]
    assert result.actions[0].kind == "shell_command"
    assert result.actions[0].rule == "shell_command_from_bash_input"
    assert result.unfamiliar_entries == []


def test_slash_commands_keep_the_name_and_arguments_as_typed() -> None:
    result = parse_human_actions(FIXTURES / "slash_commands.jsonl")

    assert result.actions == [
        # A command Claude Code runs itself writes its name tag first, and
        # its output follows as harness text (line 2).
        SlashCommand(
            line_number=1,
            entry_id="u-0001",
            session_id="session-h",
            timestamp="2026-03-03T09:00:00.000Z",
            command_name="/model",
            arguments="example-model",
            command_type="local",
        ),
        # A command that expands into a prompt (a skill or custom command)
        # writes its message tag first. The arguments are kept as typed; the
        # prompt it expanded into (line 4) is not the person's words.
        SlashCommand(
            line_number=3,
            entry_id="u-0003",
            session_id="session-h",
            timestamp="2026-03-03T09:01:00.000Z",
            command_name="/example-skill",
            arguments="write the example  twice",
            command_type="prompt",
        ),
        # No arguments tag at all: no arguments, as opposed to empty ones.
        SlashCommand(
            line_number=5,
            entry_id="u-0005",
            session_id="session-h",
            timestamp="2026-03-03T09:02:00.000Z",
            command_name="/example-plugin:review",
            arguments=None,
            command_type="prompt",
        ),
        # Near miss: a prompt that mentions the tag part-way through.
        TypedPrompt(
            line_number=6,
            entry_id="u-0006",
            session_id="session-h",
            timestamp="2026-03-03T09:03:00.000Z",
            text="What does the <command-name> tag in the example log mean?",
            image_count=0,
        ),
        # Line 7 is on a subagent's sidechain, so the person did not type it.
        SlashCommand(
            line_number=8,
            entry_id="u-0008",
            session_id="session-h",
            timestamp="2026-03-03T09:05:00.000Z",
            command_name="/compact",
            arguments="",
            command_type="local",
        ),
    ]
    assert result.actions[0].kind == "slash_command"
    assert result.actions[0].rule == "slash_command_from_command_tags"
    assert result.unfamiliar_entries == []


def test_text_the_harness_wrote_is_not_a_human_action() -> None:
    # Meta entries, command output, injected reminders, task notifications,
    # tool results, a tool failure, a compaction summary and a subagent's
    # instructions all sit in "user" entries, and none of them was typed by
    # the person.
    result = parse_human_actions(FIXTURES / "harness_lookalikes.jsonl")

    assert result.actions == []
    assert result.unfamiliar_entries == []
