"""Unit tests for goodall.timeline_entries: the column rule, and the words that stand for names off the lists."""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from pathlib import PurePosixPath

import pytest

from goodall import timeline_entries
from goodall.timeline_entries import RawRecordPointer, TimelineEntry, entry_problems

INTERVENTION_CATEGORIES = ("infrastructure",)

A_GOOD_ENTRY = TimelineEntry(
    started_at=datetime(2026, 11, 16, 9, 35, tzinfo=UTC),
    duration_seconds=None,
    source="claude",
    kind="tool_call",
    actor="ai",
    raw_record=RawRecordPointer("log_directory", PurePosixPath("transcripts/-Users-ada-work/session.jsonl")),
    position=4,
    source_application="claude_desktop_code_tab",
    entry_type="assistant",
    label_rule="model_wrote_assistant_entry",
    tool="WebSearch",
)


def test_an_entry_whose_every_value_is_on_its_list_has_no_problems() -> None:
    assert entry_problems(A_GOOD_ENTRY, INTERVENTION_CATEGORIES) == []


@pytest.mark.parametrize(
    "column",
    [
        "source",
        "kind",
        "actor",
        "source_application",
        "entry_type",
        "label_rule",
        "tool",
        "application",
        "category",
        "intervention_category",
        "outcome",
    ],
)
def test_text_that_is_not_on_a_columns_list_is_a_problem_that_names_the_column_and_never_the_text(column: str) -> None:
    # Built from the good entry's own values with one replaced, so that the column can be named by the test.
    values = dataclasses.asdict(A_GOOD_ENTRY)
    values["raw_record"] = A_GOOD_ENTRY.raw_record
    values[column] = "SYNTHETIC words a person wrote"
    entry = TimelineEntry(**values)

    assert entry_problems(entry, INTERVENTION_CATEGORIES) == [f"{column} is not on its list"]


def test_an_intervention_category_is_checked_against_the_eval_configs_own_list() -> None:
    entry = dataclasses.replace(A_GOOD_ENTRY, intervention_category="infrastructure")
    other_evals_entry = dataclasses.replace(A_GOOD_ENTRY, intervention_category="policy-mandated")

    assert entry_problems(entry, INTERVENTION_CATEGORIES) == []
    assert entry_problems(other_evals_entry, INTERVENTION_CATEGORIES) == ["intervention_category is not on its list"]


def test_a_time_without_a_zone_a_missing_kind_and_a_pointer_that_leaves_its_folder_are_problems() -> None:
    no_zone = dataclasses.replace(A_GOOD_ENTRY, started_at=datetime(2026, 11, 16, 9, 35))
    leaves_its_folder = RawRecordPointer("log_directory", PurePosixPath("transcripts/../../elsewhere.jsonl"))
    absolute = RawRecordPointer("log_directory", PurePosixPath("/Users/ada/elsewhere.jsonl"))
    kept_elsewhere = RawRecordPointer("home_folder", PurePosixPath("elsewhere.jsonl"))

    assert entry_problems(no_zone, INTERVENTION_CATEGORIES) == ["started_at has no time zone"]
    for raw_record in [leaves_its_folder, absolute]:
        entry = dataclasses.replace(A_GOOD_ENTRY, raw_record=raw_record)
        assert entry_problems(entry, INTERVENTION_CATEGORIES) == ["raw_record.path does not stay inside its folder"]
    assert entry_problems(dataclasses.replace(A_GOOD_ENTRY, raw_record=kept_elsewhere), INTERVENTION_CATEGORIES) == [
        "raw_record.kept_in is not on its list"
    ]
    assert entry_problems(dataclasses.replace(A_GOOD_ENTRY, duration_seconds=-1.0), INTERVENTION_CATEGORIES) == [
        "duration_seconds is negative"
    ]


@pytest.mark.parametrize("duration_seconds", [1e12, float("inf"), float("nan")])
def test_a_duration_that_cannot_be_added_to_the_start_is_a_problem_not_an_error(duration_seconds: float) -> None:
    entry = dataclasses.replace(A_GOOD_ENTRY, duration_seconds=duration_seconds)

    assert entry_problems(entry, INTERVENTION_CATEGORIES) == ["duration_seconds cannot be added to started_at"]


def test_a_time_that_cannot_be_written_in_utc_is_a_problem_not_an_error() -> None:
    # The first moment there is, in a zone ahead of UTC: in UTC it would be before the first date.
    ahead_of_utc = timezone(timedelta(hours=14))
    entry = dataclasses.replace(A_GOOD_ENTRY, started_at=datetime(1, 1, 1, tzinfo=ahead_of_utc))

    assert entry_problems(entry, INTERVENTION_CATEGORIES) == ["started_at cannot be written as a time in UTC"]


def test_a_tool_goodall_lists_keeps_its_name_an_mcp_tool_and_any_other_do_not() -> None:
    assert timeline_entries.listed_tool("Bash") == "Bash"
    assert timeline_entries.listed_tool("mcp__SYNTHETIC-SERVER__search") == "mcp_tool"
    assert timeline_entries.listed_tool("SyntheticToolSomeoneNamed") == "other_tool"
    assert timeline_entries.listed_tool("") == "other_tool"
    assert timeline_entries.listed_tool(None) is None


def test_an_application_goodall_lists_keeps_its_name_and_any_other_does_not() -> None:
    assert timeline_entries.listed_application("Safari") == "Safari"
    assert timeline_entries.listed_application("SYNTHETIC App - Client Name") == "other_application"


def test_an_entrypoint_becomes_the_source_application_it_stands_for() -> None:
    assert timeline_entries.listed_source_application("claude-desktop") == "claude_desktop_code_tab"
    assert timeline_entries.listed_source_application("cli") == "claude_code_terminal"
    assert timeline_entries.listed_source_application("sdk-cli") == "claude_code_sdk"
    assert timeline_entries.listed_source_application("SYNTHETIC-entrypoint") == "other_source_application"
    assert timeline_entries.listed_source_application(None) is None
    assert timeline_entries.listed_source_application(7) is None


def test_every_human_action_kind_goodall_has_is_a_kind_the_timeline_accepts() -> None:
    for kind in ["typed_prompt", "tool_call_approval_once", "application_focus", "away_from_keyboard"]:
        assert kind in timeline_entries.KINDS
    assert len(set(timeline_entries.KINDS)) == len(timeline_entries.KINDS)
