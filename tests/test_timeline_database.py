"""Unit tests for goodall.timeline: writing the database whole, reading it back, and knowing when to build again."""

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest

from goodall import timeline, timeline_claude
from goodall.timeline_entries import RawRecordPointer, SourceEntries, TimelineEntry, TimelineWorkBlock

NINE = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
TRANSCRIPT = RawRecordPointer("log_directory", PurePosixPath("transcripts/-Users-ada-work/session.jsonl"))
EVENTS = RawRecordPointer("log_directory", PurePosixPath("activitywatch/20261116T100000.000000Z/events/window.json"))
WORK_BLOCK = TimelineWorkBlock(
    NINE, NINE + timedelta(hours=1), "clean", RawRecordPointer("record_repo", PurePosixPath("work-blocks/one.json"))
)


def prompt(started_at: datetime, line: int) -> TimelineEntry:
    return TimelineEntry(started_at, None, "claude", "typed_prompt", "human", TRANSCRIPT, line)


def focus(started_at: datetime, seconds: float, position: int) -> TimelineEntry:
    return TimelineEntry(started_at, seconds, "activitywatch", "application_focus", "human", EVENTS, position)


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    folder.mkdir()
    return folder


def write(log_directory: Path, entries: list[TimelineEntry], information: dict[str, str] | None = None) -> Path:
    timeline.write_timeline(log_directory, information or dict(timeline.VERSIONS), [WORK_BLOCK], entries)
    return timeline.timeline_file_in(log_directory)


def test_what_is_written_is_read_back_the_same(log_directory: Path) -> None:
    entries = [focus(NINE, 600.5, 3), dataclasses.replace(prompt(NINE + timedelta(minutes=5), 2), in_subagent=True)]

    timeline_file = write(log_directory, entries)

    assert timeline.read_entries_overlapping(timeline_file, NINE, NINE + timedelta(hours=1)) == entries
    assert timeline.read_work_blocks(timeline_file) == [WORK_BLOCK]
    assert timeline.read_information(timeline_file) == timeline.VERSIONS


def test_times_are_written_so_that_sorting_the_text_sorts_the_times_whatever_zone_they_came_in() -> None:
    five_hours_behind = timezone(timedelta(hours=-5))
    earlier = datetime(2026, 11, 16, 4, 59, 59, 999999, tzinfo=five_hours_behind)
    later = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)

    assert timeline.format_time(earlier) == "2026-11-16T09:59:59.999999Z"
    assert timeline.format_time(later) == "2026-11-16T10:00:00.000000Z"
    assert timeline.format_time(earlier) < timeline.format_time(later)
    assert timeline.parse_time(timeline.format_time(earlier)) == earlier


def test_an_entry_overlaps_a_work_block_when_any_part_of_it_is_inside_and_the_last_second_counts(
    log_directory: Path,
) -> None:
    entries = [
        # Ended a second before the work block began.
        focus(NINE - timedelta(minutes=10), 599.0, 0),
        # Began before it and ran into it.
        focus(NINE - timedelta(minutes=5), 600.0, 1),
        prompt(NINE, 1),
        # Half a second after the end, which is rounded down to the second.
        prompt(NINE + timedelta(hours=1, milliseconds=500), 2),
        prompt(NINE + timedelta(hours=1, seconds=1), 3),
    ]

    timeline_file = write(log_directory, entries)

    assert timeline.read_entries_overlapping(timeline_file, NINE, NINE + timedelta(hours=1)) == entries[1:4]


def test_a_file_from_other_versions_or_no_file_or_a_file_that_is_not_a_timeline_is_not_current(
    log_directory: Path,
) -> None:
    timeline_file = timeline.timeline_file_in(log_directory)
    assert not timeline.is_current(timeline_file)

    write(log_directory, [])
    assert timeline.is_current(timeline_file)

    for name in timeline.VERSIONS:
        write(log_directory, [], {**timeline.VERSIONS, name: "0"})
        assert not timeline.is_current(timeline_file), name

    timeline_file.write_text("SYNTHETIC not a database")
    assert not timeline.is_current(timeline_file)


def test_a_build_that_fails_leaves_the_last_good_timeline_and_nothing_half_written(
    log_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    timeline_file = write(log_directory, [prompt(NINE, 1)])
    good_bytes = timeline_file.read_bytes()

    def fail(*arguments: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(timeline, "insert_everything", fail)

    build = timeline.build_timeline(log_directory, record_repo, (), NINE)

    assert not build.built
    assert build.problems == ["The timeline could not be written: database or disk is full."]
    assert timeline_file.read_bytes() == good_bytes
    assert [path.name for path in timeline_file.parent.iterdir()] == ["timeline.sqlite3"]
    assert timeline.build_problems_failure(build).kind == "timeline_not_built"


def test_an_entry_that_breaks_the_column_rule_is_left_out_and_the_build_says_which_column_never_the_value(
    log_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    off_its_list = dataclasses.replace(prompt(NINE, 2), tool="SYNTHETIC name someone gave a tool")

    def entries_with_a_fault(log_directory: Path) -> SourceEntries:
        return SourceEntries(entries=[prompt(NINE, 1), off_its_list])

    monkeypatch.setattr(timeline_claude, "entries_from_transcripts", entries_with_a_fault)

    build = timeline.build_timeline(log_directory, record_repo, (), NINE)

    assert build.built
    assert build.entry_count == 1
    [problem] = build.problems
    assert "1 entries were left out" in problem and "tool is not on its list" in problem
    assert "SYNTHETIC" not in problem
    assert b"SYNTHETIC" not in build.timeline_file.read_bytes()
    assert timeline.build_problems_failure(build).kind == "timeline_incomplete"


def test_entries_at_one_moment_come_out_in_the_same_order_on_every_build(log_directory: Path, tmp_path: Path) -> None:
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    unsorted = [prompt(NINE, 9), focus(NINE, 5.0, 1), prompt(NINE, 2), focus(NINE - timedelta(seconds=1), 5.0, 0)]

    in_time_order = sorted(unsorted, key=timeline.time_order)

    assert in_time_order == [unsorted[3], unsorted[2], unsorted[0], unsorted[1]]


def test_an_error_nobody_expected_while_building_names_its_kind_never_its_message_and_keeps_the_last_good_file(
    log_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    timeline_file = write(log_directory, [prompt(NINE, 1)])
    good_bytes = timeline_file.read_bytes()

    def raise_unexpected(log_directory: Path) -> SourceEntries:
        raise KeyError("SYNTHETIC text quoted from a raw record")

    monkeypatch.setattr(timeline_claude, "entries_from_transcripts", raise_unexpected)

    build = timeline.build_timeline(log_directory, record_repo, (), NINE)

    assert not build.built
    assert build.problems == ["Goodall met an error it did not expect while building it (KeyError)."]
    assert timeline_file.read_bytes() == good_bytes
    assert [path.name for path in timeline_file.parent.iterdir()] == ["timeline.sqlite3"]
