"""Timeline entries from the copies of ActivityWatch's data in the log directory.

Reads `<log directory>/activitywatch/<moment of a copy>/`, which
goodall.activitywatch_copies fills when a work block ends: `info.json`,
`buckets.json` and one events file for each bucket
(docs/activitywatch-rules.md, "Copies in the log directory").

Who calls it: goodall.timeline, when it builds the timeline. The rules
below are numbered in docs/timeline.md; change both together and raise
TIMELINE_RULES_VERSION (goodall.timeline_entries).

How events become entries:

1. Each event is read by the rule for its bucket's type, the same rules
   goodall.activitywatch applies to an export. The entry's kind is the
   action's kind (`application_focus`, `active_at_keyboard` and so on) and
   its actor is `human`: ActivityWatch records what happened at this macOS
   user account.
2. Copies overlap, and the server cuts an event that crosses the start of
   a range while keeping its `id`. So events with one `id`, in one bucket,
   from one ActivityWatch (one `device_id`) are one entry, running from
   the earliest start seen to the latest end seen. Its pointer leads to
   the longest of them, the later copy's when two are as long.
3. An event without an `id` is an entry of its own.
4. An event no rule can read (an unknown bucket type, no time) is in no
   entry and is counted.

What is kept: the time, the duration, the kind, and the name of the
application in front when goodall lists it. A window's title, a page's
address and a file's path are read into memory with the rest of the event
and are never kept, and no problem names a bucket, because a bucket's id
holds the Mac's hostname.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from goodall.activitywatch import ActivityWatchAction, ApplicationFocus, UnfamiliarRecord, actions_from_bucket
from goodall.activitywatch_copies import BUCKETS_FILE_NAME, COPIES_FOLDER_NAME, EVENTS_FOLDER_NAME, INFO_FILE_NAME
from goodall.human_activity import category_of
from goodall.timeline_entries import (
    IN_LOG_DIRECTORY,
    SOURCE_ACTIVITYWATCH,
    RawRecordPointer,
    SourceEntries,
    TimelineEntry,
    listed_application,
)
from goodall.verbatim_copies import describe_error, list_folders


@dataclass
class SeenEvent:
    """One event as seen so far across the copies: its widest reach, and the pointer to its longest sighting."""

    started_at: datetime
    ended_at: datetime
    has_duration: bool
    longest_seconds: float
    action: ActivityWatchAction
    raw_record: RawRecordPointer
    position: int


def entries_from_activitywatch_copies(log_directory: Path) -> SourceEntries:
    """Every entry from every copy of ActivityWatch's data in the log directory, in no particular order.

    Never raises for anything on the disk or in a file's shape: a copy
    that cannot be read becomes a problem naming the copy by its moment,
    and the rest are still read.
    """
    gathered = SourceEntries()
    copies_folder = log_directory / COPIES_FOLDER_NAME
    try:
        copy_names = list_folders(copies_folder)
    except OSError as error:
        gathered.problems.append(f"The copies of ActivityWatch's data in the log directory: {describe_error(error)}.")
        return gathered
    # Events that carry an id, by (device id, bucket id, event id): rule 2.
    seen_by_identity: dict[tuple[str, str, int], SeenEvent] = {}
    # Events that carry none: rule 3.
    seen_without_id: list[SeenEvent] = []
    # Oldest copy first, so that of two sightings as long as each other the later copy's is kept.
    for copy_name in copy_names:
        try:
            read_copy(log_directory, copy_name, seen_by_identity, seen_without_id, gathered)
        except OSError as error:
            gathered.problems.append(f"The copy of ActivityWatch's data taken at {copy_name}: {describe_error(error)}.")
        except ValueError:
            gathered.problems.append(
                f"The copy of ActivityWatch's data taken at {copy_name} is not in the shape goodall copies."
            )
    for seen in [*seen_by_identity.values(), *seen_without_id]:
        gathered.entries.append(entry_from_seen_event(seen))
    return gathered


def read_copy(
    log_directory: Path,
    copy_name: str,
    seen_by_identity: dict[tuple[str, str, int], SeenEvent],
    seen_without_id: list[SeenEvent],
    gathered: SourceEntries,
) -> None:
    """Read one copy's events into what has been seen so far.

    Raises OSError when a file cannot be read and ValueError when one is
    not the JSON goodall copies. A bucket whose events file is missing is
    passed over: the copy asked for none.
    """
    copy_folder = log_directory / COPIES_FOLDER_NAME / copy_name
    info = read_json(copy_folder / INFO_FILE_NAME)
    buckets = read_json(copy_folder / BUCKETS_FILE_NAME)
    if not isinstance(info, dict) or not isinstance(buckets, dict):
        raise ValueError("the copy's files are not JSON objects")
    device_id = info.get("device_id")
    if not isinstance(device_id, str):
        raise ValueError("the copy does not say which ActivityWatch it came from")
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            gathered.without_a_time += 1
            continue
        events_path = PurePosixPath(
            COPIES_FOLDER_NAME, copy_name, EVENTS_FOLDER_NAME, f"{quote(bucket_id, safe='')}.json"
        )
        try:
            events = read_json(log_directory / events_path)
        except FileNotFoundError:
            continue
        unfamiliar_records: list[UnfamiliarRecord] = []
        bucket_as_exported = {
            "id": bucket_id,
            "type": bucket.get("type"),
            "hostname": bucket.get("hostname"),
            "events": events,
        }
        actions = actions_from_bucket(bucket_as_exported, unfamiliar_records)
        gathered.without_a_time += len(unfamiliar_records)
        raw_record = RawRecordPointer(IN_LOG_DIRECTORY, events_path)
        for started_at, action in actions:
            sighting = sighting_of(started_at, action, raw_record)
            if action.event_id is None:
                seen_without_id.append(sighting)
                continue
            identity = (device_id.lower(), bucket_id, action.event_id)
            earlier = seen_by_identity.get(identity)
            if earlier is None:
                seen_by_identity[identity] = sighting
            else:
                seen_by_identity[identity] = merged(earlier, sighting)


def read_json(path: Path) -> Any:
    """Read one JSON file. Raises OSError when it cannot be read and ValueError when it is not JSON."""
    with path.open(encoding="utf-8") as json_file:
        return json.load(json_file)


def sighting_of(started_at: datetime, action: ActivityWatchAction, raw_record: RawRecordPointer) -> SeenEvent:
    """One event as one copy holds it."""
    seconds = 0.0
    if action.duration_seconds is not None and action.duration_seconds > 0:
        seconds = action.duration_seconds
    return SeenEvent(
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=seconds),
        has_duration=action.duration_seconds is not None,
        longest_seconds=seconds,
        action=action,
        raw_record=raw_record,
        position=action.event_index,
    )


def merged(earlier: SeenEvent, later: SeenEvent) -> SeenEvent:
    """Two sightings of one event as one: from the earliest start to the latest end (rule 2).

    The pointer, and the action read from it, stay with the longer
    sighting, or with the later copy's when both are as long.
    """
    longest = earlier
    if later.longest_seconds >= earlier.longest_seconds:
        longest = later
    return SeenEvent(
        started_at=min(earlier.started_at, later.started_at),
        ended_at=max(earlier.ended_at, later.ended_at),
        has_duration=earlier.has_duration or later.has_duration,
        longest_seconds=longest.longest_seconds,
        action=longest.action,
        raw_record=longest.raw_record,
        position=longest.position,
    )


def entry_from_seen_event(seen: SeenEvent) -> TimelineEntry:
    """The timeline entry for one event, once every copy has been read."""
    duration_seconds = None
    if seen.has_duration:
        duration_seconds = (seen.ended_at - seen.started_at).total_seconds()
    application = None
    if isinstance(seen.action, ApplicationFocus):
        application = listed_application(seen.action.app)
    return TimelineEntry(
        started_at=seen.started_at,
        duration_seconds=duration_seconds,
        source=SOURCE_ACTIVITYWATCH,
        kind=seen.action.kind,
        actor="human",
        raw_record=seen.raw_record,
        position=seen.position,
        application=application,
        category=category_of(seen.action.kind),
    )
