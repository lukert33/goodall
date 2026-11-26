"""Unit tests for goodall.source_applications: the one rule that says which records belong to a work block."""

from datetime import UTC, datetime, timedelta, timezone

from goodall.source_applications import is_inside_work_block

STARTED_AT = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)


def test_a_moment_between_the_start_and_the_end_is_inside_the_work_block() -> None:
    assert is_inside_work_block(STARTED_AT + timedelta(minutes=20), STARTED_AT, ENDED_AT)


def test_the_moment_the_work_block_started_is_inside_it_and_the_moment_before_is_not() -> None:
    assert is_inside_work_block(STARTED_AT, STARTED_AT, ENDED_AT)
    assert not is_inside_work_block(STARTED_AT - timedelta(milliseconds=1), STARTED_AT, ENDED_AT)


def test_the_whole_of_the_work_blocks_last_second_is_inside_it_because_its_end_is_rounded_down() -> None:
    assert is_inside_work_block(ENDED_AT + timedelta(milliseconds=999), STARTED_AT, ENDED_AT)
    assert not is_inside_work_block(ENDED_AT + timedelta(seconds=1), STARTED_AT, ENDED_AT)


def test_a_moment_written_in_another_time_zone_is_compared_as_the_same_moment() -> None:
    five_hours_behind = timezone(timedelta(hours=-5))
    moment = datetime(2026, 11, 16, 5, 0, tzinfo=five_hours_behind)

    assert is_inside_work_block(moment, STARTED_AT, ENDED_AT)
