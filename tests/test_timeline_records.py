"""Unit tests for goodall.timeline_records: entries from journal entries and interventions, and the work blocks."""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import records, timeline_records, work_blocks

NINE = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
MACHINE = records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None)


@pytest.fixture
def record_repo(tmp_path: Path) -> Path:
    folder = tmp_path / "records"
    folder.mkdir()
    return folder


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    folder.mkdir()
    return folder


def write_record(record_repo: Path, record: records.Record) -> str:
    """Write a record where goodall keeps it in the record repo; return its path there."""
    relative_path = records.relative_path_for(record)
    (record_repo / relative_path).parent.mkdir(parents=True, exist_ok=True)
    (record_repo / relative_path).write_text(records.render_record(record))
    return str(relative_path)


def ended_work_block(started_at: datetime, minutes: int, end_state: str = "clean") -> records.WorkBlockRecord:
    running = records.build_work_block_record("ada-lovelace", "research", started_at, MACHINE)
    return dataclasses.replace(running, ended_at=started_at + timedelta(minutes=minutes), end_state=end_state)


def test_a_journal_entry_is_an_entry_at_the_moment_it_was_written_with_none_of_its_words(record_repo: Path) -> None:
    path = write_record(record_repo, records.build_journal_entry("ada-lovelace", NINE, "SYNTHETIC decided."))

    gathered = timeline_records.entries_from_records(record_repo, ("infrastructure",))

    [entry] = gathered.entries
    assert (entry.started_at, entry.kind, entry.actor, entry.source) == (NINE, "journal_entry", "human", "record_repo")
    assert (entry.raw_record.kept_in, str(entry.raw_record.path), entry.position) == ("record_repo", path, None)
    assert "SYNTHETIC" not in repr(entry)


def test_an_intervention_is_an_entry_at_the_moment_it_happened_with_its_category_and_decision(
    record_repo: Path,
) -> None:
    listed = records.build_intervention(
        "ada-lovelace", NINE, NINE + timedelta(hours=3), "infrastructure", "reject", "SYNTHETIC stopped it."
    )
    unlisted = records.build_intervention(
        "ada-lovelace",
        NINE + timedelta(minutes=1),
        NINE + timedelta(hours=3),
        "a-category-another-eval-lists",
        "modify",
        "x",
    )
    write_record(record_repo, listed)
    write_record(record_repo, unlisted)

    gathered = timeline_records.entries_from_records(record_repo, ("infrastructure",))

    assert [
        (entry.started_at, entry.kind, entry.intervention_category, entry.outcome) for entry in gathered.entries
    ] == [
        (NINE, "intervention", "infrastructure", "reject"),
        (NINE + timedelta(minutes=1), "intervention", "other_category", "modify"),
    ]


def test_a_record_that_cannot_be_read_is_a_problem_naming_the_file_and_the_others_are_still_read(
    record_repo: Path,
) -> None:
    write_record(record_repo, records.build_journal_entry("ada-lovelace", NINE, "A note."))
    (record_repo / "journal" / "20261116T100000Z-ada-lovelace-00000000.md").write_text("SYNTHETIC not a record")

    gathered = timeline_records.entries_from_records(record_repo, ())

    assert len(gathered.entries) == 1
    assert gathered.problems == [
        "The record journal/20261116T100000Z-ada-lovelace-00000000.md in the record repo could not be read."
    ]


def test_ended_work_blocks_come_from_the_record_repo_and_from_those_still_waiting_oldest_first(
    record_repo: Path, log_directory: Path
) -> None:
    recorded = ended_work_block(NINE + timedelta(hours=2), 30, "inferred")
    waiting = ended_work_block(NINE, 45)
    recorded_path = write_record(record_repo, recorded)
    assert work_blocks.save_unrecorded(log_directory, waiting)
    # Committed a moment ago and its waiting file not removed yet: taken once, from the record repo.
    assert work_blocks.save_unrecorded(log_directory, recorded)
    problems: list[str] = []

    found = timeline_records.ended_work_blocks(record_repo, log_directory, problems)

    assert problems == []
    assert [(block.started_at, block.ended_at, block.end_state, block.raw_record.kept_in) for block in found] == [
        (NINE, NINE + timedelta(minutes=45), "clean", "log_directory"),
        (NINE + timedelta(hours=2), NINE + timedelta(hours=2, minutes=30), "inferred", "record_repo"),
    ]
    assert str(found[0].raw_record.path) == f"work-blocks/unrecorded/{waiting.id}.json"
    assert str(found[1].raw_record.path) == recorded_path


def test_with_an_empty_record_repo_there_is_nothing_and_no_problem(record_repo: Path, log_directory: Path) -> None:
    problems: list[str] = []

    assert timeline_records.entries_from_records(record_repo, ()).entries == []
    assert timeline_records.ended_work_blocks(record_repo, log_directory, problems) == []
    assert problems == []


def test_a_deleted_work_block_is_passed_over_without_a_problem(record_repo: Path, log_directory: Path) -> None:
    kept = ended_work_block(NINE, 30)
    write_record(record_repo, kept)
    write_record(
        record_repo,
        records.build_deleted_work_block(
            "20261116T100000Z-ada-lovelace-00000000", "ada-lovelace", NINE + timedelta(hours=5), []
        ),
    )
    problems: list[str] = []

    found = timeline_records.ended_work_blocks(record_repo, log_directory, problems)

    assert [work_block.raw_record.path.stem for work_block in found] == [kept.id]
    assert problems == []


def test_a_work_block_file_that_is_no_record_at_all_is_still_a_problem(record_repo: Path, log_directory: Path) -> None:
    (record_repo / "work-blocks").mkdir()
    (record_repo / "work-blocks" / "20261116T100000Z-ada-lovelace-00000000.json").write_text("{}")
    problems: list[str] = []

    timeline_records.ended_work_blocks(record_repo, log_directory, problems)

    assert problems == [
        "The record work-blocks/20261116T100000Z-ada-lovelace-00000000.json in the record repo could not be read."
    ]
