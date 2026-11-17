"""Tests for goodall.human_activity: every human action in a time range, from every source.

The tests drive `human_actions_in_time_range`, the one place to ask what the
person did, with the same synthetic fixtures the per-source parsers use.
"""

from datetime import UTC, datetime
from pathlib import Path

from goodall.activitywatch import parse_activitywatch_export
from goodall.human_actions import parse_human_actions
from goodall.human_activity import (
    PROVISIONAL_CATEGORIES,
    HumanActionSummary,
    category_of,
    human_actions_in_time_range,
    summarise_human_actions,
)

FIXTURES = Path(__file__).parent / "fixtures"
TRANSCRIPTS = FIXTURES / "claude_code_transcripts"
ACTIVITYWATCH_EXPORTS = FIXTURES / "activitywatch"


def at(text: str) -> datetime:
    """A moment in UTC, written as in the fixtures."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_actions_from_every_source_come_back_together_in_time_order() -> None:
    actions = human_actions_in_time_range(
        at("2026-01-05T09:01:00"),
        at("2026-04-01T09:05:00"),
        transcript_paths=[TRANSCRIPTS / "typed_prompts.jsonl"],
        activitywatch_export_paths=[ACTIVITYWATCH_EXPORTS / "all_buckets_rust_server.json"],
    )

    summary = []
    for action in actions:
        summary.append((action.source, action.kind, action.timestamp))
    assert summary == [
        # The prompt at 09:00 is before the range starts.
        ("claude_code_transcript", "typed_prompt", "2026-01-05T09:01:00.000Z"),
        ("claude_code_transcript", "typed_prompt", "2026-01-05T09:02:00.000Z"),
        ("claude_code_transcript", "typed_prompt", "2026-01-05T09:03:00.000Z"),
        ("activitywatch_export", "application_focus", "2026-04-01T09:01:00Z"),
        ("activitywatch_export", "editor_file_focus", "2026-04-01T09:01:00Z"),
        ("activitywatch_export", "application_focus", "2026-04-01T09:04:00Z"),
        ("activitywatch_export", "browser_page_focus", "2026-04-01T09:04:00Z"),
        # The range's end is not part of it: the 09:05 action is left out.
    ]


def test_every_kind_any_source_produces_has_a_provisional_category() -> None:
    kinds = set()
    for transcript_path in sorted(TRANSCRIPTS.glob("*.jsonl")):
        for transcript_action in parse_human_actions(transcript_path).actions:
            kinds.add(transcript_action.kind)
    for export_path in sorted(ACTIVITYWATCH_EXPORTS.glob("*.json")):
        for activitywatch_action in parse_activitywatch_export(export_path).actions:
            kinds.add(activitywatch_action.kind)

    missing = kinds - set(PROVISIONAL_CATEGORIES)
    assert missing == set()
    # A kind nobody has placed yet still has an answer, not an error.
    assert category_of("example_future_kind") == "uncategorised"
    assert category_of("typed_prompt") == "directed_the_agent"


def test_the_summary_counts_by_kind_category_and_source_listing_zeros() -> None:
    actions = human_actions_in_time_range(
        at("2026-01-01T00:00:00"),
        at("2027-01-01T00:00:00"),
        transcript_paths=[TRANSCRIPTS / "rejections.jsonl", TRANSCRIPTS / "sdk_prompts.jsonl"],
        activitywatch_export_paths=[ACTIVITYWATCH_EXPORTS / "one_bucket_python_server.json"],
    )

    summary = summarise_human_actions(actions)

    assert isinstance(summary, HumanActionSummary)
    assert summary.total == 10
    assert summary.actions_by_kind["typed_prompt"] == 4
    assert summary.actions_by_kind["tool_call_rejection"] == 2
    # A prompt a program sent is counted on its own, not as typed.
    assert summary.actions_by_kind["sdk_prompt"] == 2
    assert summary.actions_by_kind["application_focus"] == 2
    # A kind with nothing in this range is listed with zero, not left out.
    assert summary.actions_by_kind["shell_command"] == 0
    assert set(summary.actions_by_kind) == set(PROVISIONAL_CATEGORIES)
    assert summary.actions_by_category["directed_the_agent"] == 4
    assert summary.actions_by_category["approved_or_refused"] == 2
    assert summary.actions_by_category["undecided"] == 2
    assert summary.actions_by_category["worked_outside_the_agent"] == 2
    assert summary.actions_by_category["was_elsewhere"] == 0
    assert summary.actions_by_source == {"claude_code_transcript": 8, "activitywatch_export": 2}
