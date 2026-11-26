"""Unit tests for goodall.timeline_activitywatch: entries from the copies of ActivityWatch's data.

Each test writes synthetic copies in the layout goodall.activitywatch_copies
leaves (docs/activitywatch-rules.md) and checks the entries. Every title,
page address, hostname and identifier is made up.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from goodall import timeline_activitywatch
from goodall.timeline_entries import TimelineEntry

DEVICE_ID = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"
OTHER_DEVICE_ID = "11111111-2222-4333-8444-555555555555"
HOSTNAME = "SYNTHETIC-hostname"
WINDOW_BUCKET_ID = f"aw-watcher-window_{HOSTNAME}"
NINE = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

FIRST_COPY = "20261116T100000.000000Z"
SECOND_COPY = "20261116T110000.000000Z"


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    folder.mkdir()
    return folder


def event(event_id: int | None, started: datetime, seconds: float, data: dict[str, object]) -> dict[str, object]:
    """One event as the server answers it."""
    served: dict[str, object] = {
        "timestamp": started.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
        "duration": seconds,
        "data": data,
    }
    if event_id is not None:
        served["id"] = event_id
    return served


def window(event_id: int | None, started: datetime, seconds: float, application: str = "Safari") -> dict[str, object]:
    data: dict[str, object] = {"app": application, "title": "SYNTHETIC title", "url": "https://synthetic.example/"}
    return event(event_id, started, seconds, data)


def write_copy(
    log_directory: Path,
    copy_name: str,
    events_by_bucket: dict[str, list[dict[str, object]]],
    bucket_types: dict[str, str] | None = None,
    device_id: str = DEVICE_ID,
) -> None:
    """Write one copy, each bucket's events as given (the server answers newest first)."""
    copy_folder = log_directory / "activitywatch" / copy_name
    (copy_folder / "events").mkdir(parents=True)
    (copy_folder / "info.json").write_text(json.dumps({"hostname": HOSTNAME, "device_id": device_id}))
    buckets = {}
    for bucket_id, events in events_by_bucket.items():
        bucket_type = "currentwindow"
        if bucket_types is not None:
            bucket_type = bucket_types[bucket_id]
        buckets[bucket_id] = {"id": bucket_id, "type": bucket_type, "hostname": HOSTNAME}
        (copy_folder / "events" / f"{quote(bucket_id, safe='')}.json").write_text(json.dumps(events))
    (copy_folder / "buckets.json").write_text(json.dumps(buckets))


def in_brief(entries: list[TimelineEntry]) -> list[tuple[datetime, float | None, str, str | None]]:
    """Each entry as (start, duration, kind, application), oldest first."""
    brief = []
    for entry in sorted(entries, key=lambda entry: entry.started_at):
        brief.append((entry.started_at, entry.duration_seconds, entry.kind, entry.application))
    return brief


def test_each_event_is_an_entry_the_person_did_with_the_application_only_when_goodall_lists_it(
    log_directory: Path,
) -> None:
    afk_bucket_id = f"aw-watcher-afk_{HOSTNAME}"
    write_copy(
        log_directory,
        FIRST_COPY,
        {
            WINDOW_BUCKET_ID: [
                window(2, NINE + timedelta(minutes=5), 60.0, "SYNTHETIC App - Client Name"),
                window(1, NINE, 300.0),
            ],
            afk_bucket_id: [event(1, NINE, 900.0, {"status": "not-afk"})],
        },
        {WINDOW_BUCKET_ID: "currentwindow", afk_bucket_id: "afkstatus"},
    )

    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert gathered.problems == []
    assert sorted(in_brief(gathered.entries), key=str) == sorted(
        [
            (NINE, 300.0, "application_focus", "Safari"),
            (NINE, 900.0, "active_at_keyboard", None),
            (NINE + timedelta(minutes=5), 60.0, "application_focus", "other_application"),
        ],
        key=str,
    )
    for entry in gathered.entries:
        assert (entry.actor, entry.source) == ("human", "activitywatch")
    # The pointer is the events file and the event's place in its list, counting from 0.
    safari = [entry for entry in gathered.entries if entry.application == "Safari"][0]
    assert str(safari.raw_record.path) == f"activitywatch/{FIRST_COPY}/events/{quote(WINDOW_BUCKET_ID, safe='')}.json"
    assert safari.position == 1
    assert safari.category == "worked_outside_the_agent"


def test_one_event_seen_in_two_overlapping_copies_is_one_entry_from_its_earliest_start_to_its_latest_end(
    log_directory: Path,
) -> None:
    # The first copy holds event 7 as far as it had run. The second copy's
    # range began later, so the server cut the event to fit, and by then
    # the same window had stayed in front for longer.
    write_copy(log_directory, FIRST_COPY, {WINDOW_BUCKET_ID: [window(7, NINE + timedelta(minutes=50), 600.0)]})
    write_copy(
        log_directory,
        SECOND_COPY,
        {
            WINDOW_BUCKET_ID: [
                window(8, NINE + timedelta(minutes=70), 30.0),
                window(7, NINE + timedelta(minutes=55), 900.0),
            ]
        },
    )

    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert in_brief(gathered.entries) == [
        (NINE + timedelta(minutes=50), 1200.0, "application_focus", "Safari"),
        (NINE + timedelta(minutes=70), 30.0, "application_focus", "Safari"),
    ]
    # The pointer leads to the longer sighting, in the second copy.
    merged_entry = sorted(gathered.entries, key=lambda entry: entry.started_at)[0]
    assert merged_entry.raw_record.path.parts[1] == SECOND_COPY
    assert merged_entry.position == 1


def test_the_same_id_from_another_activitywatch_is_another_event(log_directory: Path) -> None:
    write_copy(log_directory, FIRST_COPY, {WINDOW_BUCKET_ID: [window(7, NINE, 60.0)]})
    write_copy(
        log_directory,
        SECOND_COPY,
        {WINDOW_BUCKET_ID: [window(7, NINE + timedelta(hours=1), 60.0)]},
        device_id=OTHER_DEVICE_ID,
    )

    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert len(gathered.entries) == 2


def test_events_without_an_id_are_never_merged(log_directory: Path) -> None:
    write_copy(log_directory, FIRST_COPY, {WINDOW_BUCKET_ID: [window(None, NINE, 60.0), window(None, NINE, 60.0)]})

    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert len(gathered.entries) == 2


def test_a_bucket_of_a_type_no_rule_knows_and_an_event_without_a_time_are_counted_not_listed(
    log_directory: Path,
) -> None:
    unknown_bucket_id = f"aw-watcher-SYNTHETIC_{HOSTNAME}"
    no_time = {"id": 3, "duration": 5.0, "data": {"app": "Safari", "title": "SYNTHETIC"}}
    write_copy(
        log_directory,
        FIRST_COPY,
        {
            WINDOW_BUCKET_ID: [no_time, window(1, NINE, 60.0)],
            unknown_bucket_id: [event(1, NINE, 5.0, {"x": "SYNTHETIC"})],
        },
        {WINDOW_BUCKET_ID: "currentwindow", unknown_bucket_id: "SYNTHETIC.type"},
    )

    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert len(gathered.entries) == 1
    assert gathered.without_a_time == 2
    assert gathered.problems == []


def test_a_copy_that_cannot_be_read_is_a_problem_naming_only_its_moment_and_the_others_are_still_read(
    log_directory: Path,
) -> None:
    write_copy(log_directory, FIRST_COPY, {WINDOW_BUCKET_ID: [window(1, NINE, 60.0)]})
    write_copy(log_directory, SECOND_COPY, {WINDOW_BUCKET_ID: [window(2, NINE + timedelta(hours=1), 60.0)]})
    events_file = log_directory / "activitywatch" / SECOND_COPY / "events" / f"{quote(WINDOW_BUCKET_ID, safe='')}.json"
    events_file.write_text('[{"id": 2, "data": {"title": "SYNTHETIC cut sh')

    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert len(gathered.entries) == 1
    assert gathered.problems == [
        f"The copy of ActivityWatch's data taken at {SECOND_COPY} is not in the shape goodall copies."
    ]


def test_with_no_copies_there_are_no_entries_and_no_problems(log_directory: Path) -> None:
    gathered = timeline_activitywatch.entries_from_activitywatch_copies(log_directory)

    assert (gathered.entries, gathered.problems, gathered.without_a_time) == ([], [], 0)
