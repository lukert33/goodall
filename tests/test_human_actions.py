"""Tests for goodall.human_actions: finding what the person did in a transcript.

Each test hands a hand-written transcript from tests/fixtures/ to
`parse_human_actions`, the module's one entry point, and checks the records
that come back. Every fixture is synthetic: invented text, placeholder paths
and ids, shaped like the entries Claude Code writes. Real transcripts hold a
contributor's private prompts and must never be copied into fixtures.
"""

from pathlib import Path

from goodall.human_actions import TypedPrompt, parse_human_actions

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_transcripts"


def test_typed_prompts_are_found_with_their_entry_and_time() -> None:
    result = parse_human_actions(FIXTURES / "typed_prompts.jsonl")

    assert result.actions == [
        TypedPrompt(
            line_number=1,
            entry_id="u-0001",
            timestamp="2026-01-05T09:00:00.000Z",
            text="Please add a greeting function to the example module.",
            image_count=0,
        ),
        TypedPrompt(
            line_number=3,
            entry_id="u-0003",
            timestamp="2026-01-05T09:01:00.000Z",
            text="Here is a sketch of the layout I want.",
            image_count=1,
        ),
    ]
    assert result.unfamiliar_entries == []


def test_text_the_harness_wrote_is_not_a_human_action() -> None:
    # Meta entries, slash-command wrappers, command output, injected
    # reminders, task notifications, tool results, a tool failure, a
    # compaction summary and a subagent's instructions all sit in "user"
    # entries, and none of them was typed by the person.
    result = parse_human_actions(FIXTURES / "harness_lookalikes.jsonl")

    assert result.actions == []
    assert result.unfamiliar_entries == []
