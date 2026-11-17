"""Find what the person did outside any agent, in an ActivityWatch export.

ActivityWatch is a separate, optional program that records which application
and window had focus, whether the person was at the keyboard, and, with its
browser and editor watchers, the page or file in front of them. It keeps each
watcher's records in a "bucket" of events. This module reads ActivityWatch's
own export format, a JSON file of buckets as its export button or REST API
produces it, and returns one record per human action, in time order.

Who calls it: derivation code that builds derived records from raw records
(ADR 0003), and `goodall.human_activity`, which puts these actions beside the
ones found in transcripts. Goodall does not yet capture ActivityWatch data;
this is the reading side only. It reads the file it is given and nothing
else, and imports only the standard library.

Design decisions a reader would otherwise reverse-engineer:

- The format was taken from ActivityWatch's source, not from a running copy;
  docs/activitywatch-rules.md says exactly which files and what could not be
  confirmed. Both of its servers' export shapes are accepted.
- Window titles, URLs and file paths are some of the most private text
  goodall handles. Raw records are kept verbatim and redaction is a later,
  separate step, so these values are kept as they are, but they are left out
  of every record's printed form (`repr`), and no message, error or report
  here ever includes one. An unfamiliar record is described by bucket id,
  position and a reason that names no value.
- Every action is done by a human. Which human is not claimed: ActivityWatch
  records what happened at this macOS user account, not who was at it.
- Each watcher's records are their own kinds, finely split, so they can be
  merged or renamed after looking at real results. Raise RULES_VERSION
  whenever a rule changes (ADR 0004).
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Raise this whenever a rule changes, so derived records say which rules
# produced them (ADR 0004).
RULES_VERSION = "1"

# Every action found here says it came from an ActivityWatch export.
SOURCE = "activitywatch_export"

# Every action record below shares these fields:
#
# - `bucket_id`, `hostname`, `event_index` (the event's position in its
#   bucket's list as exported) and `event_id` (the event's own id, which only
#   the Rust server exports): where in the raw record it was found.
# - `timestamp`: when the event started, as ActivityWatch wrote it (UTC).
# - `duration_seconds`: how long it lasted, as ActivityWatch wrote it.
# - `source`, `kind` and `rule`: which kind of raw record, which kind of
#   action, and the rule that found it.
#
# Fields holding private text are marked `repr=False`.


@dataclass(frozen=True, kw_only=True)
class ApplicationFocus:
    """An application's window had focus (aw-watcher-window, bucket type `currentwindow`).

    `title` is the window title and `url` the page address that the macOS
    window watcher adds for some browsers; both are private and may be None.
    ActivityWatch writes "excluded" as the title when its own privacy filter
    hid it.
    """

    bucket_id: str
    hostname: str | None
    event_index: int
    event_id: int | None
    timestamp: str
    duration_seconds: float | None
    app: str
    title: str | None = field(repr=False)
    url: str | None = field(repr=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="application_focus", init=False)
    rule: str = field(default="application_focus_from_window_event", init=False)


@dataclass(frozen=True, kw_only=True)
class BrowserPageFocus:
    """A page was the active tab of a browser (aw-watcher-web, bucket type `web.tab.current`).

    The trap: the browser watcher records the active tab whether or not the
    browser itself had focus; whether the person was looking at it comes from
    lining it up with `ApplicationFocus` for the browser, a later step.
    `url` and `title` are private.
    """

    bucket_id: str
    hostname: str | None
    event_index: int
    event_id: int | None
    timestamp: str
    duration_seconds: float | None
    url: str = field(repr=False)
    title: str | None = field(repr=False)
    audible: bool | None
    incognito: bool | None
    tab_count: int | None
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="browser_page_focus", init=False)
    rule: str = field(default="browser_page_focus_from_web_event", init=False)


@dataclass(frozen=True, kw_only=True)
class EditorFileFocus:
    """A file was open in an editor (an editor watcher, bucket type `app.editor.activity`).

    `file`, `project` and `branch` are private. The VS Code watcher writes
    "unknown" for any it could not read.
    """

    bucket_id: str
    hostname: str | None
    event_index: int
    event_id: int | None
    timestamp: str
    duration_seconds: float | None
    file: str = field(repr=False)
    project: str | None = field(repr=False)
    language: str | None
    branch: str | None = field(repr=False)
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="editor_file_focus", init=False)
    rule: str = field(default="editor_file_focus_from_editor_event", init=False)


@dataclass(frozen=True, kw_only=True)
class AwayFromKeyboard:
    """The person was away from the keyboard (aw-watcher-afk, status "afk").

    The AFK watcher decides from keyboard and mouse input alone, after a
    configurable idle time, so reading or a call without typing also shows
    as away.
    """

    bucket_id: str
    hostname: str | None
    event_index: int
    event_id: int | None
    timestamp: str
    duration_seconds: float | None
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="away_from_keyboard", init=False)
    rule: str = field(default="keyboard_presence_from_afk_event", init=False)


@dataclass(frozen=True, kw_only=True)
class ActiveAtKeyboard:
    """The person was using the keyboard or mouse (aw-watcher-afk, status "not-afk")."""

    bucket_id: str
    hostname: str | None
    event_index: int
    event_id: int | None
    timestamp: str
    duration_seconds: float | None
    source: str = field(default=SOURCE, init=False)
    kind: str = field(default="active_at_keyboard", init=False)
    rule: str = field(default="keyboard_presence_from_afk_event", init=False)


ActivityWatchAction = ApplicationFocus | BrowserPageFocus | EditorFileFocus | AwayFromKeyboard | ActiveAtKeyboard


@dataclass(frozen=True)
class UnfamiliarRecord:
    """A bucket or event the parser did not recognise. The reason names no value.

    `event_index` is None when the whole bucket was not recognised.
    """

    bucket_id: str
    event_index: int | None
    reason: str


@dataclass
class ActivityWatchParseResult:
    """Everything `parse_activitywatch_export` found in one export file."""

    actions: list[ActivityWatchAction]
    unfamiliar_records: list[UnfamiliarRecord]
    rules_version: str = RULES_VERSION


class ActivityWatchExportError(Exception):
    """The file is not an ActivityWatch export at all. The message quotes nothing from it."""


@dataclass(frozen=True)
class EventPlace:
    """Where one event sits in an export, and when it happened; every action from it shares this."""

    bucket_id: str
    hostname: str | None
    event_index: int
    event_id: int | None
    timestamp: str
    duration_seconds: float | None


def parse_activitywatch_export(export_path: Path) -> ActivityWatchParseResult:
    """Read one ActivityWatch export and return the person's actions, in time order.

    This is the module's entry point. An export is `{"buckets": ...}`, where
    the buckets are an object keyed by bucket id (what both ActivityWatch
    servers write) or a list (what ActivityWatch's own JSON schema says). A
    bucket or event the rules do not know goes into `unfamiliar_records` and
    reading carries on. A file that is not an export at all raises
    `ActivityWatchExportError`.
    """
    try:
        export = json.loads(export_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = (
            f"{export_path} could not be read as JSON ({type(error).__name__}). "
            "Export it again from ActivityWatch (the Export button on its Buckets page) and pass that file."
        )
        raise ActivityWatchExportError(message) from None
    buckets = export.get("buckets") if isinstance(export, dict) else None
    if isinstance(buckets, dict):
        bucket_list = list(buckets.values())
    elif isinstance(buckets, list):
        bucket_list = buckets
    else:
        message = (
            f"{export_path} has no 'buckets', so it is not an ActivityWatch export. "
            "Pass a file from ActivityWatch's Export button or its /api/0/export address."
        )
        raise ActivityWatchExportError(message)

    result = ActivityWatchParseResult(actions=[], unfamiliar_records=[])
    timed_actions: list[tuple[datetime, ActivityWatchAction]] = []
    for bucket in bucket_list:
        for start_time, action in actions_from_bucket(bucket, result.unfamiliar_records):
            timed_actions.append((start_time, action))
    # Exports list each bucket's events newest first; actions are returned
    # oldest first across all buckets. Ties keep the order they were read in.
    timed_actions.sort(key=lambda timed_action: timed_action[0])
    for _start_time, action in timed_actions:
        result.actions.append(action)
    return result


def actions_from_bucket(
    bucket: Any, unfamiliar_records: list[UnfamiliarRecord]
) -> list[tuple[datetime, ActivityWatchAction]]:
    """Return every action in one bucket, each with its parsed start time, by applying its type's rule."""
    if not isinstance(bucket, dict):
        unfamiliar_records.append(UnfamiliarRecord("", None, "bucket is not an object"))
        return []
    bucket_id = text_or_none(bucket, "id") or ""
    bucket_type = bucket.get("type")
    rule = RULES_BY_BUCKET_TYPE.get(bucket_type) if isinstance(bucket_type, str) else None
    if rule is None:
        unfamiliar_records.append(UnfamiliarRecord(bucket_id, None, f"unknown bucket type {str(bucket_type)[:60]!r}"))
        return []
    events = bucket.get("events")
    if not isinstance(events, list):
        unfamiliar_records.append(UnfamiliarRecord(bucket_id, None, "bucket has no list of events"))
        return []
    hostname = bucket.get("hostname") if isinstance(bucket.get("hostname"), str) else None

    actions: list[tuple[datetime, ActivityWatchAction]] = []
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            unfamiliar_records.append(UnfamiliarRecord(bucket_id, event_index, "event is not an object"))
            continue
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str):
            unfamiliar_records.append(UnfamiliarRecord(bucket_id, event_index, "event has no timestamp"))
            continue
        start_time = parse_time(timestamp)
        if start_time is None:
            unfamiliar_records.append(UnfamiliarRecord(bucket_id, event_index, "event has an unreadable timestamp"))
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            unfamiliar_records.append(UnfamiliarRecord(bucket_id, event_index, "event has no data"))
            continue
        duration = event.get("duration")
        duration_seconds = None
        if isinstance(duration, int | float) and not isinstance(duration, bool):
            duration_seconds = float(duration)
        event_id = event.get("id")
        place = EventPlace(
            bucket_id=bucket_id,
            hostname=hostname,
            event_index=event_index,
            event_id=event_id if isinstance(event_id, int) and not isinstance(event_id, bool) else None,
            timestamp=timestamp,
            duration_seconds=duration_seconds,
        )
        action = rule(place, data)
        if isinstance(action, str):
            unfamiliar_records.append(UnfamiliarRecord(bucket_id, event_index, f"{bucket_type} event {action}"))
            continue
        actions.append((start_time, action))
    return actions


def parse_time(timestamp: str) -> datetime | None:
    """Read an ActivityWatch timestamp; one with no time zone is taken as UTC, as ActivityWatch stores."""
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def text_or_none(data: dict[str, Any], key: str) -> str | None:
    """Return `data[key]` when it is a string, otherwise None."""
    value = data.get(key)
    return value if isinstance(value, str) else None


# Each rule below takes one event's place and data and returns the action, or
# a short reason (naming no value) why the event was not understood.


def application_focus_from_window_event(place: EventPlace, data: dict[str, Any]) -> ApplicationFocus | str:
    """Rule: a `currentwindow` event with an `app` is that application having focus."""
    app = text_or_none(data, "app")
    if app is None:
        return "has no app"
    return ApplicationFocus(
        bucket_id=place.bucket_id,
        hostname=place.hostname,
        event_index=place.event_index,
        event_id=place.event_id,
        timestamp=place.timestamp,
        duration_seconds=place.duration_seconds,
        app=app,
        title=text_or_none(data, "title"),
        url=text_or_none(data, "url"),
    )


def browser_page_focus_from_web_event(place: EventPlace, data: dict[str, Any]) -> BrowserPageFocus | str:
    """Rule: a `web.tab.current` event with a `url` is that page being the active tab."""
    url = text_or_none(data, "url")
    if url is None:
        return "has no url"
    audible = data.get("audible")
    incognito = data.get("incognito")
    tab_count = data.get("tabCount")
    return BrowserPageFocus(
        bucket_id=place.bucket_id,
        hostname=place.hostname,
        event_index=place.event_index,
        event_id=place.event_id,
        timestamp=place.timestamp,
        duration_seconds=place.duration_seconds,
        url=url,
        title=text_or_none(data, "title"),
        audible=audible if isinstance(audible, bool) else None,
        incognito=incognito if isinstance(incognito, bool) else None,
        tab_count=tab_count if isinstance(tab_count, int) and not isinstance(tab_count, bool) else None,
    )


def editor_file_focus_from_editor_event(place: EventPlace, data: dict[str, Any]) -> EditorFileFocus | str:
    """Rule: an `app.editor.activity` event with a `file` is that file being open in the editor."""
    file = text_or_none(data, "file")
    if file is None:
        return "has no file"
    return EditorFileFocus(
        bucket_id=place.bucket_id,
        hostname=place.hostname,
        event_index=place.event_index,
        event_id=place.event_id,
        timestamp=place.timestamp,
        duration_seconds=place.duration_seconds,
        file=file,
        project=text_or_none(data, "project"),
        language=text_or_none(data, "language"),
        branch=text_or_none(data, "branch"),
    )


def keyboard_presence_from_afk_event(
    place: EventPlace, data: dict[str, Any]
) -> AwayFromKeyboard | ActiveAtKeyboard | str:
    """Rule: an `afkstatus` event is away ("afk") or active ("not-afk"); any other status is not understood."""
    status = data.get("status")
    if status == "afk":
        return AwayFromKeyboard(
            bucket_id=place.bucket_id,
            hostname=place.hostname,
            event_index=place.event_index,
            event_id=place.event_id,
            timestamp=place.timestamp,
            duration_seconds=place.duration_seconds,
        )
    if status == "not-afk":
        return ActiveAtKeyboard(
            bucket_id=place.bucket_id,
            hostname=place.hostname,
            event_index=place.event_index,
            event_id=place.event_id,
            timestamp=place.timestamp,
            duration_seconds=place.duration_seconds,
        )
    return "has an unknown status"


# Which rule reads which bucket type. The types are the ones ActivityWatch's
# watchers write; any other bucket is reported as unfamiliar.
RULES_BY_BUCKET_TYPE = {
    "currentwindow": application_focus_from_window_event,
    "web.tab.current": browser_page_focus_from_web_event,
    "app.editor.activity": editor_file_focus_from_editor_event,
    "afkstatus": keyboard_presence_from_afk_event,
}
