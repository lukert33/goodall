"""Copy ActivityWatch's data into the log directory when a work block has ended (ADR 0016).

ActivityWatch is how goodall sees what a person did outside the agents:
which application and window were in front, and whether they were at the
keyboard. It keeps that in its own database and answers questions about it
on a local port. This module copies its answers, byte for byte, into the
log directory, where they are raw records (ADR 0003).

Who calls it: goodall.work_block_commands, and only for an eval whose
config includes ActivityWatch. Never a hook.

How it works:

1. When a work block ends, by `goodall stop` or because goodall closed one
   nobody stopped, the caller leaves a note that a copy is wanted
   (note_copy_wanted): `activitywatch-sync/copy-wanted.json`.
2. `goodall stop`, and `goodall start` and `goodall status` after it, take
   the copy while that note is there (copy_if_wanted). With no note,
   nothing is asked of ActivityWatch, so a `goodall status` in the middle
   of a work block costs one look for a file.
3. A copy is built in `activitywatch-sync/copy-in-progress/` and moved, in
   one step and only when all of it arrived, to
   `activitywatch/<moment of the copy>/`:

       info.json                 the server's answer about itself
       buckets.json              its list of buckets
       events/<bucket id>.json   each bucket's events

   The bucket id in a file name is quoted as in a web address
   (`urllib.parse.quote`), because it holds the Mac's hostname and so may
   hold any character.
4. Then goodall writes what it asked for, as its own bookkeeping, to
   `activitywatch-sync/copies/<moment of the copy>.json`, and removes the
   note from step 1. If anything went wrong the note stays, nothing is
   left in `activitywatch/`, and the next goodall start, stop or status
   tries again. ActivityWatch keeps its own history, so a copy taken late
   holds the same events.

Decisions a reader would otherwise reverse-engineer:

- The identifier is checked before anything else is asked or kept. Local
  ports are shared by every macOS user account on the Mac, ActivityWatch
  silently adopts any copy of itself already on its port, and bucket ids
  are the same in every account. Only the `device_id` in the server's
  answer about itself tells this account's ActivityWatch from another's.
  When it is not the one in local settings, the copy stops and the answer
  is deleted: it describes another account.
- `curl` writes each answer straight to its file (ADR 0011), so the window
  titles and page addresses in the events never pass through goodall.
  Goodall reads the server's answer about itself (for the identifier) and
  the list of buckets (for their ids), and of each events file only the
  first and last bytes, to see that it is a JSON list.
- Each file is created by goodall, readable only by this account, before
  curl writes into it: curl keeps the permissions of a file that exists,
  and would otherwise make one that other accounts could read.
- A bucket's first copy asks for everything it holds, with no range. A
  later copy asks from where the bucket's last copy ended, less OVERLAP,
  to the moment of this copy. The overlap is there because ActivityWatch
  lengthens its newest event while the same window stays in front, and
  because a watcher that could not reach the server sends its events
  late. So the same event can be in two copies, and the one that crosses
  the start of a range is cut to fit while keeping its id: a reader
  dropping the overlap must prefer the longer of two events with one id,
  not the first it meets.
- "Where the last copy ended" is worked out from the notes in
  `activitywatch-sync/copies/`, and only from those written for the
  identifier in local settings. A new identifier (a reinstalled
  ActivityWatch) therefore starts from the beginning, and losing the notes
  only means the next copy holds everything again.
- Nothing of goodall's own goes inside `activitywatch/`, and no file name
  here ends in `.jsonl`, which Scout's importer would take for a Claude
  session (ADR 0015).

What is never shown: nothing from ActivityWatch's answers, not a bucket id
(it holds the hostname), not the identifier, not the port. Problems say
what was expected, never what came back.
"""

import json
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from goodall import work_blocks
from goodall.activitywatch_server import (
    BUCKETS_PATH,
    CURL_MAX_SECONDS,
    INFO_PATH,
    answer_unfamiliar_failure,
    curl_unavailable_failure,
    device_id_changed_failure,
    events_path,
    local_get,
    not_answering_failure,
)
from goodall.runner import CommandOutcome, Runner
from goodall.terminal import Failure
from goodall.verbatim_copies import (
    FILE_PERMISSIONS,
    TEMPORARY_SUFFIX,
    describe_error,
    format_moment,
    held_lock,
    make_folders,
)

# The folder in the log directory that holds the copies, one folder for
# each, named after the moment it was taken. Raw records only.
COPIES_FOLDER_NAME = "activitywatch"

# The folder in the log directory for goodall's own bookkeeping about
# those copies. Trap: nothing written here may end in `.jsonl` (ADR 0015).
SYNC_FOLDER_NAME = "activitywatch-sync"

# In `activitywatch-sync/`: the note that a work block has ended since the
# last copy; the folder a copy is built in; and the folder of notes, one
# for each copy, saying what goodall asked for.
WANTED_FILE_NAME = "copy-wanted.json"
IN_PROGRESS_FOLDER_NAME = "copy-in-progress"
NOTES_FOLDER_NAME = "copies"

# Inside one copy.
INFO_FILE_NAME = "info.json"
BUCKETS_FILE_NAME = "buckets.json"
EVENTS_FOLDER_NAME = "events"

NOTE_SCHEMA_VERSION = 1

# How far before the end of a bucket's last copy the next one starts (see
# the module docstring). Ten minutes costs a few repeated events and
# covers a watcher that was briefly unable to reach the server.
OVERLAP = timedelta(minutes=10)

# How long `curl` may take to fetch one bucket's events. A first copy
# holds everything the bucket has, which can be months of events.
EVENTS_MAX_SECONDS = 120

# The lock held while copying, so two goodall commands run at the same
# moment never build the same copy at once.
COPYING_LOCK_NAME = "copying-activitywatch"


@dataclass(frozen=True)
class CopyResult:
    """What one attempt to copy ActivityWatch's data did.

    `copied` is True when a copy is now in the log directory, holding
    `bucket_count` buckets. `problem` says why not, with a next step; the
    copy is then still wanted and is tried again. Both are empty when no
    copy was wanted.
    """

    copied: bool = False
    bucket_count: int = 0
    problem: Failure | None = None


class CopyStopped(Exception):
    """Raised inside this module to stop a copy; carries the failure to report."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.what_happened)
        self.failure = failure


def wanted_file(log_directory: Path) -> Path:
    """Where the note that a copy is wanted is kept."""
    return log_directory / SYNC_FOLDER_NAME / WANTED_FILE_NAME


def note_copy_wanted(log_directory: Path, ended_at: datetime) -> None:
    """Leave a note that a work block has ended, so ActivityWatch's data is to be copied.

    A note already there is left as it is: one copy covers every work block
    that has ended since the last. `ended_at` is kept for a person looking
    at the file; goodall reads only whether the file exists. Raises OSError
    when the log directory cannot be written.
    """
    make_folders(log_directory, [SYNC_FOLDER_NAME])
    note = {"wanted_since": work_blocks.format_timestamp(ended_at)}
    try:
        descriptor = os.open(wanted_file(log_directory), os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as wanted:
        json.dump(note, wanted, indent=2)
        wanted.write("\n")


def copy_is_wanted(log_directory: Path) -> bool:
    """True while a work block has ended and ActivityWatch's data has not been copied since."""
    return wanted_file(log_directory).exists()


def copy_if_wanted(
    runner: Runner, log_directory: Path, port: int, recorded_device_id: str, settings_file: Path, now: datetime
) -> CopyResult:
    """Copy ActivityWatch's data into the log directory if a work block has ended since the last copy.

    `port` and `recorded_device_id` are the ones in local settings, and
    `settings_file` is named in a next step that tells the person to look
    there. Never raises for anything about the server or the disk: what
    went wrong becomes the result's `problem`, and the copy stays wanted.
    """
    if not copy_is_wanted(log_directory):
        return CopyResult()
    try:
        with held_lock(log_directory, COPYING_LOCK_NAME, "another goodall command copying ActivityWatch's data"):
            if not copy_is_wanted(log_directory):
                # Another goodall took the copy while this one waited.
                return CopyResult()
            try:
                bucket_count = take_copy(runner, log_directory, port, recorded_device_id, settings_file, now)
            finally:
                # Whatever a copy that stopped part-way had fetched goes;
                # after a copy that worked, this folder is already gone.
                remove_copy_in_progress(log_directory)
            wanted_file(log_directory).unlink(missing_ok=True)
            return CopyResult(copied=True, bucket_count=bucket_count)
    except CopyStopped as stopped:
        return CopyResult(problem=stopped.failure)
    except TimeoutError:
        return CopyResult(problem=another_copy_running_failure())
    except OSError as error:
        return CopyResult(problem=log_directory_failure(describe_error(error)))


def take_copy(
    runner: Runner, log_directory: Path, port: int, recorded_device_id: str, settings_file: Path, now: datetime
) -> int:
    """Fetch one whole copy, move it into `activitywatch/`, write its note, and return how many buckets it holds.

    Raises CopyStopped, with the failure to report, when the server does
    not answer, is not this account's, or answers something unfamiliar;
    and OSError when the log directory cannot be written.
    """
    remove_copy_in_progress(log_directory)
    in_progress = make_folders(log_directory, [SYNC_FOLDER_NAME, IN_PROGRESS_FOLDER_NAME])
    make_folders(in_progress, [EVENTS_FOLDER_NAME])
    copied_at = now.astimezone(UTC).replace(microsecond=0)

    # 1. Whose ActivityWatch is this? Nothing else is asked until it is ours.
    info_file = in_progress / INFO_FILE_NAME
    fetch(runner, port, INFO_PATH, info_file, settings_file)
    info = read_json(info_file)
    if not isinstance(info, dict) or not isinstance(info.get("device_id"), str):
        raise CopyStopped(answer_unfamiliar_failure())
    # Local settings record the identifier in lowercase (goodall.local_settings).
    if info["device_id"].lower() != recorded_device_id:
        raise CopyStopped(device_id_changed_failure(settings_file))

    # 2. Which buckets does it have?
    buckets_file = in_progress / BUCKETS_FILE_NAME
    fetch(runner, port, BUCKETS_PATH, buckets_file, settings_file)
    buckets = read_json(buckets_file)
    if not isinstance(buckets, dict):
        raise CopyStopped(answer_unfamiliar_failure())

    # 3. Each bucket's events, from where its last copy ended.
    last_copied_at = read_last_copies(log_directory, recorded_device_id)
    bucket_notes: list[dict[str, object]] = []
    for bucket_id in buckets:
        start = None
        end = None
        if bucket_id in last_copied_at:
            start = last_copied_at[bucket_id] - OVERLAP
            end = copied_at
        relative_file = f"{EVENTS_FOLDER_NAME}/{quote(bucket_id, safe='')}.json"
        events_file = in_progress / relative_file
        fetch(runner, port, events_path(bucket_id, start, end), events_file, settings_file, EVENTS_MAX_SECONDS)
        if not looks_like_a_json_list(events_file):
            raise CopyStopped(answer_unfamiliar_failure())
        bucket_notes.append(
            {"id": bucket_id, "file": relative_file, "start": time_or_none(start), "end": time_or_none(end)}
        )

    # 4. All of it arrived: it becomes a copy, in one step.
    copy_name = format_moment(now)
    copies_folder = make_folders(log_directory, [COPIES_FOLDER_NAME])
    copy_folder = copies_folder / copy_name
    if copy_folder.exists():
        # Two copies in the same microsecond. A raw record is never replaced.
        raise CopyStopped(copy_already_there_failure())
    os.rename(in_progress, copy_folder)

    # 5. What was asked, for the next copy and for whoever reads this one.
    # Written last: a goodall killed just before it copies the same range
    # again next time, which repeats events and loses none.
    note = {
        "schema_version": NOTE_SCHEMA_VERSION,
        "copied_at": work_blocks.format_timestamp(copied_at),
        "device_id": recorded_device_id,
        "folder": f"{COPIES_FOLDER_NAME}/{copy_name}",
        "overlap_seconds": int(OVERLAP.total_seconds()),
        "buckets": bucket_notes,
    }
    write_note(log_directory, copy_name, note)
    return len(bucket_notes)


def time_or_none(moment: datetime | None) -> str | None:
    """A moment as a note writes it, or None when no moment was asked for."""
    if moment is None:
        return None
    return work_blocks.format_timestamp(moment)


def fetch(
    runner: Runner,
    port: int,
    path: str,
    output_file: Path,
    settings_file: Path,
    max_seconds: int = CURL_MAX_SECONDS,
) -> None:
    """Have `curl` write ActivityWatch's answer for `path` into `output_file`; raise CopyStopped when it does not.

    The file is created here first, readable only by this account: curl
    keeps the permissions of a file that exists.
    """
    os.close(os.open(output_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS))
    result = runner.run(local_get(port, path, output_file, max_seconds))
    if result.outcome == CommandOutcome.NOT_FOUND or result.outcome == CommandOutcome.COULD_NOT_START:
        raise CopyStopped(curl_unavailable_failure())
    if result.outcome == CommandOutcome.TIMED_OUT:
        raise CopyStopped(not_answering_failure(timed_out=True, settings_file=settings_file, max_seconds=max_seconds))
    if not result.succeeded:
        raise CopyStopped(not_answering_failure(timed_out=False, settings_file=settings_file))


def read_json(answer_file: Path) -> object:
    """What a small answer holds, read back from its file; raises CopyStopped when it is not JSON."""
    try:
        answer: object = json.loads(answer_file.read_bytes())
    except ValueError:
        raise CopyStopped(answer_unfamiliar_failure()) from None
    return answer


def looks_like_a_json_list(events_file: Path) -> bool:
    """True when the file starts with `[` and ends with `]`, ignoring blank space around them.

    Only the first and last few bytes are read, so the events themselves,
    which hold window titles and page addresses, never enter goodall. It
    is enough to tell a list of events from an error page or a redirect's
    empty answer.
    """
    edge_bytes = 64
    with events_file.open("rb") as events:
        first_bytes = events.read(edge_bytes)
        events.seek(0, os.SEEK_END)
        size = events.tell()
        events.seek(max(0, size - edge_bytes))
        last_bytes = events.read(edge_bytes)
    return first_bytes.lstrip().startswith(b"[") and last_bytes.rstrip().endswith(b"]")


def remove_copy_in_progress(log_directory: Path) -> None:
    """Remove the folder a copy is built in, with whatever a stopped or killed copy left there."""
    in_progress = log_directory / SYNC_FOLDER_NAME / IN_PROGRESS_FOLDER_NAME
    if in_progress.exists():
        shutil.rmtree(in_progress)


def write_note(log_directory: Path, copy_name: str, note: dict[str, object]) -> None:
    """Write the note for one copy, whole and then renamed into place, so a reader never sees half a note."""
    notes_folder = make_folders(log_directory, [SYNC_FOLDER_NAME, NOTES_FOLDER_NAME])
    temporary_file = notes_folder / f".{copy_name}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
    try:
        descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(note, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_file, notes_folder / f"{copy_name}.json")
    finally:
        temporary_file.unlink(missing_ok=True)


def read_last_copies(log_directory: Path, device_id: str) -> dict[str, datetime]:
    """For each bucket id, when its last copy from the ActivityWatch with this identifier was taken.

    Read from the notes in `activitywatch-sync/copies/`. A note for another
    identifier is passed over: bucket ids are the same in every account's
    ActivityWatch, so only the identifier says whose a copy was. A note
    goodall cannot read is passed over too, which at worst makes the next
    copy of a bucket hold everything again.
    """
    last_copied_at: dict[str, datetime] = {}
    notes_folder = log_directory / SYNC_FOLDER_NAME / NOTES_FOLDER_NAME
    if not notes_folder.is_dir():
        return last_copied_at
    for note_file in sorted(notes_folder.iterdir()):
        if not note_file.name.endswith(".json"):
            continue
        try:
            note = json.loads(note_file.read_text(encoding="utf-8"))
            if note["device_id"] != device_id:
                continue
            copied_at = datetime.fromisoformat(note["copied_at"])
            bucket_ids = []
            for bucket_note in note["buckets"]:
                bucket_ids.append(str(bucket_note["id"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if copied_at.tzinfo is None:
            continue
        for bucket_id in bucket_ids:
            if bucket_id not in last_copied_at or copied_at > last_copied_at[bucket_id]:
                last_copied_at[bucket_id] = copied_at
    return last_copied_at


def another_copy_running_failure() -> Failure:
    """The failure for a copy that waited too long for another goodall command's copy to finish."""
    return Failure(
        kind="activitywatch_copy_busy",
        what_happened="Another goodall command was still copying ActivityWatch's data, and did not finish in time.",
        why="Goodall takes one copy at a time, so two commands never write the same copy.",
        what_to_do_next="Wait a minute, then run goodall status.",
        commands_to_try=("goodall status",),
    )


def log_directory_failure(reason: str) -> Failure:
    """The failure for a copy that could not be written into the log directory."""
    return Failure(
        kind="activitywatch_copy_not_written",
        what_happened=f"Goodall could not write the copy into the log directory: {reason}.",
        why="Copies of ActivityWatch's data are kept in the activitywatch folder of the log directory.",
        what_to_do_next=(
            "Check that the log directory has free space and that this account can write to it, then run goodall "
            "status."
        ),
        commands_to_try=("goodall status",),
    )


def copy_already_there_failure() -> Failure:
    """The failure for a copy whose folder, named after this very moment, already exists."""
    return Failure(
        kind="activitywatch_copy_already_there",
        what_happened="A copy taken at this very moment is already in the log directory, and goodall replaces none.",
        why="Each copy is kept in a folder named after the moment it was taken, and a raw record is never replaced.",
        what_to_do_next="Run goodall status, which takes the copy at a new moment.",
        commands_to_try=("goodall status",),
    )
