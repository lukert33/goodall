"""The baseline: when a work block ends, copy every agent session that has a record inside it.

This is the one way agent sessions reach the log directory, for every
source application (ADR 0019). It needs nothing from the application but
the files it already writes, which is why it carries to the next one.

How it works:

1. When a work block ends, whether by `goodall stop` or because goodall
   closed one nobody stopped, goodall.work_block_commands leaves a note
   here saying the work block's agent sessions are still to be copied
   (note_work_block_to_copy): `transcript-sync/work-blocks-to-copy/<work
   block id>.json`, holding when it started and ended.
2. `goodall start`, `stop` and `status` then copy for every note
   (copy_ended_work_blocks). For each app family in APP_FAMILIES, every
   agent session in the eval account is looked at, whatever folder it ran
   in, because the eval account is the boundary of the eval; those with a
   record inside the work block are copied, byte for byte, in the
   application's own layout (goodall.verbatim_copies). An agent session
   with no record inside the work block is not copied.
3. A note is removed once everything was copied. If something went wrong
   (the log directory is full, a file cannot be read), the note stays and
   the next goodall start, stop or status tries again, so a failed copy is
   never forgotten.

Who calls it: goodall.work_block_commands. Not on the hook path: hooks
copy nothing.

Decisions a reader would otherwise reverse-engineer:

- The note is written before the work block's record is saved and its
  marker removed, so a goodall killed at any moment in between still
  copies later. A note for a work block that then ends again, at another
  moment, is simply replaced.
- An agent session is copied whole, including records from before or
  after the work block: a raw record is never cut (ADR 0003). Which
  records fall inside which work block is worked out later, from the
  copies.
- Copying happens once, when the work block ends. An agent still writing
  after `goodall stop` leaves records the copy does not hold; they are
  copied when a later work block that the agent session also belongs to
  ends, and not otherwise (docs/source-applications.md).
- A file goodall had to skip among an agent session's files (one it
  cannot read, a symbolic link, a special file) is a problem like any
  other: the rest is copied, the note stays, and every goodall start,
  stop and status says so until someone has looked. In an eval, a record
  with a hole nobody noticed is the worse failure.
"""

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from goodall import claude_family, work_blocks
from goodall.source_applications import AppFamily
from goodall.verbatim_copies import (
    FILE_PERMISSIONS,
    SYNC_FOLDER_NAME,
    TEMPORARY_SUFFIX,
    describe_error,
    describe_skipped,
    held_lock,
    make_folders,
    remove_stale_temporary_files,
)

# Every app family goodall captures. To add one, write its module as
# goodall.claude_family is written and add its APP_FAMILY here; nothing
# else in goodall needs to change for its agent sessions to be copied.
APP_FAMILIES: list[AppFamily] = [claude_family.APP_FAMILY]

# The folder, inside `transcript-sync/` in the log directory, holding one
# note for each ended work block whose agent sessions are still to be
# copied. The notes end in `.json`, never `.jsonl` (the trap is described
# at SYNC_FOLDER_NAME).
TO_COPY_FOLDER_NAME = "work-blocks-to-copy"

# The lock held while copying, so two goodall commands run at the same
# moment (a `goodall status` in one terminal, a `goodall stop` in another)
# never copy the same files at once.
COPYING_LOCK_NAME = "copying-agent-sessions"


@dataclass
class CopyResult:
    """What copying for one or more ended work blocks did.

    `agent_sessions_copied` counts the agent sessions that had a record
    inside a work block and whose files are now in the log directory,
    whether or not they had changed since an earlier copy. `problems`
    holds a plain sentence for each thing that went wrong; while there is
    one, the work block's note stays and copying is tried again.
    """

    agent_sessions_copied: int = 0
    earlier_copies_kept: int = 0
    problems: list[str] = field(default_factory=list)

    def add(self, other: "CopyResult") -> None:
        """Take in what copying for another work block did."""
        self.agent_sessions_copied += other.agent_sessions_copied
        self.earlier_copies_kept += other.earlier_copies_kept
        self.problems.extend(other.problems)


def note_file(log_directory: Path, work_block_id: str) -> Path:
    """Where the note for one ended work block is kept."""
    return log_directory / SYNC_FOLDER_NAME / TO_COPY_FOLDER_NAME / f"{work_block_id}.json"


def note_work_block_to_copy(log_directory: Path, work_block_id: str, started_at: datetime, ended_at: datetime) -> None:
    """Leave a note that an ended work block's agent sessions are still to be copied.

    Written whole and then renamed into place, so a reader never sees half
    a note, and a note already there for the same work block is replaced.
    Raises OSError when the log directory cannot be written.
    """
    note_folder = make_folders(log_directory, [SYNC_FOLDER_NAME, TO_COPY_FOLDER_NAME])
    note = {
        "work_block": work_block_id,
        "started_at": work_blocks.format_timestamp(started_at),
        "ended_at": work_blocks.format_timestamp(ended_at),
    }
    temporary_file = note_folder / f".{work_block_id}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
    try:
        descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(note, temporary, indent=2)
            temporary.write("\n")
        os.replace(temporary_file, note_file(log_directory, work_block_id))
    finally:
        temporary_file.unlink(missing_ok=True)


def work_blocks_to_copy(log_directory: Path) -> list[str]:
    """The ids of the ended work blocks whose agent sessions are still to be copied, oldest first.

    A work block's id begins with the moment it started, so sorting the
    ids puts the oldest first. None when the folder is missing.
    """
    work_block_ids = []
    try:
        with os.scandir(log_directory / SYNC_FOLDER_NAME / TO_COPY_FOLDER_NAME) as entries:
            for entry in entries:
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    work_block_ids.append(entry.name.removesuffix(".json"))
    except FileNotFoundError:
        return []
    return sorted(work_block_ids)


def read_note(log_directory: Path, work_block_id: str) -> tuple[datetime, datetime] | None:
    """When the work block of one note started and ended; None when the note cannot be read as one.

    Raises FileNotFoundError when there is no note.
    """
    note_text = note_file(log_directory, work_block_id).read_text(encoding="utf-8")
    try:
        note = json.loads(note_text)
        started_at = datetime.fromisoformat(note["started_at"])
        ended_at = datetime.fromisoformat(note["ended_at"])
    except (ValueError, KeyError, TypeError):
        return None
    if started_at.tzinfo is None or ended_at.tzinfo is None:
        return None
    return started_at, ended_at


def copy_ended_work_blocks(log_directory: Path, now: datetime) -> CopyResult:
    """Copy the agent sessions of every ended work block that still has a note, and say what happened.

    Never raises for anything on the disk: whatever cannot be read or
    written becomes one of the result's problems, and the rest is still
    copied. With no note there is nothing to look at, so a `goodall status`
    run while nothing is waiting costs one folder listing.
    """
    result = CopyResult()
    try:
        work_block_ids = work_blocks_to_copy(log_directory)
    except OSError as error:
        result.problems.append(f"The work blocks still to copy, in the log directory: {describe_error(error)}.")
        return result
    for work_block_id in work_block_ids:
        result.add(copy_work_block(log_directory, work_block_id, now))
    return result


def copy_work_block(log_directory: Path, work_block_id: str, now: datetime) -> CopyResult:
    """Copy the agent sessions of one ended work block, from every app family, and remove its note if all went well.

    With no note for the work block, nothing is done: another goodall has
    already copied for it. Never raises for anything on the disk.
    """
    result = CopyResult()
    try:
        times = read_note(log_directory, work_block_id)
    except FileNotFoundError:
        return result
    except (OSError, UnicodeDecodeError) as error:
        result.problems.append(f"The note for work block {work_block_id}: {describe_note_error(error)}.")
        return result
    if times is None:
        result.problems.append(
            f"The note for work block {work_block_id}, {note_file(log_directory, work_block_id)}, does not say when "
            "the work block started and ended, so goodall cannot tell which agent sessions belong to it."
        )
        return result
    started_at, ended_at = times
    try:
        with held_lock(log_directory, COPYING_LOCK_NAME, "another goodall command copying agent sessions"):
            for app_family in APP_FAMILIES:
                copy_agent_sessions_inside(app_family, log_directory, started_at, ended_at, now, result)
                for copies_folder_name in app_family.copies_folder_names:
                    remove_stale_temporary_files(log_directory / copies_folder_name, now)
            if not result.problems:
                note_file(log_directory, work_block_id).unlink(missing_ok=True)
    except TimeoutError as error:
        result.problems.append(f"Copying agent sessions did not start: {error}.")
    except OSError as error:
        result.problems.append(f"The log directory: {describe_error(error)}.")
    return result


def describe_note_error(error: OSError | UnicodeDecodeError) -> str:
    """What went wrong reading a note, in a few words."""
    if isinstance(error, OSError):
        return describe_error(error)
    return "it is not text goodall can read"


def copy_agent_sessions_inside(
    app_family: AppFamily,
    log_directory: Path,
    started_at: datetime,
    ended_at: datetime,
    now: datetime,
    result: CopyResult,
) -> None:
    """Copy each of one app family's agent sessions that has a record inside the work block, adding to `result`.

    One agent session that cannot be read or copied becomes a problem, and
    the others are still copied. So does one with files that had to be
    skipped, though it counts as copied: the rest of it is.
    """
    found = app_family.find_agent_sessions(log_directory)
    result.problems.extend(found.problems)
    for agent_session in found.agent_sessions:
        described = f"{app_family.name} agent session {agent_session.id}"
        try:
            if not app_family.has_record_inside(agent_session, started_at, ended_at):
                continue
            copying = app_family.copy_agent_session(agent_session, log_directory, now)
        except OSError as error:
            result.problems.append(f"{described}: {describe_error(error)}.")
            continue
        if copying.skipped:
            result.problems.append(f"{described}: {describe_skipped(copying.skipped)}.")
        result.agent_sessions_copied += 1
        result.earlier_copies_kept += copying.earlier_copies_kept()
