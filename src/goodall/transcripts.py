"""Copying Claude Code's transcripts into the log directory, verbatim and in Claude Code's own layout.

A transcript is a raw record (ADR 0003): goodall copies it byte for byte and
never rewrites, redacts or re-encodes it. The copy keeps the layout of
Claude Code's projects folder, so the copied folder can be read by anything
that reads that folder, Inspect Scout's claude_code importer included:

    <Claude projects folder>/<project folder>/<session id>.jsonl
    <Claude projects folder>/<project folder>/<session id>/subagents/agent-<id>.jsonl
                                                         (and .meta.json, .prefix.json)
      are copied to
    <log directory>/transcripts/<project folder>/<session id>.jsonl
    <log directory>/transcripts/<project folder>/<session id>/subagents/...

    and goodall keeps, beside them:
    <log directory>/superseded-transcripts/<moment>/...   earlier copies that were replaced
    <log directory>/transcript-sync/failed-copies.jsonl   copies hooks could not make
    <log directory>/transcript-sync/reported/...          those, once goodall status said so
    <log directory>/transcript-sync/locks/<session id>.lock

Who calls it: `goodall hook` (goodall.hook) copies one Claude session's
files after PostToolUse, Stop and SessionEnd, with `copy_from_hook`; and
`goodall start`, `stop` and `status` run `sweep`, which copies every Claude
session the hooks missed (a Claude Code killed with SIGKILL fires no hook).
This module is on the hook path, so it imports only the standard library
(ADR 0005) and starts no program.

Where the Claude projects folder is: `projects` inside Claude Code's own
folder, which is `~/.claude` unless the environment variable
CLAUDE_CONFIG_DIR names another. CLAUDE_CONFIG_DIR is Claude Code's own
setting, not goodall's; goodall honours it so it looks where Claude Code
writes, and tests point it at tmp_path so the real home folder, which holds
a person's private prompts, stays out of reach. Trap: a terminal where
CLAUDE_CONFIG_DIR is not set while Claude Code runs with it would make
`goodall status` sweep the wrong folder; the hooks are unaffected, since
they inherit Claude Code's environment.

Decisions a reader would otherwise reverse-engineer (PROVISIONAL):

- Goodall copies only from inside the Claude projects folder. The path a
  hook names comes from outside goodall, so a path that resolves anywhere
  else, through `..` or a symbolic link, is refused; so are symbolic links
  among a Claude session's subagent files.
- Files are copied to a temporary file in the destination folder, which is
  then renamed over the copy. A rename is atomic, so a copy interrupted at
  any moment, even by SIGKILL, leaves the old copy or the new one, never a
  file cut short. A killed copy leaves its temporary file behind; the
  sweep removes those once they are STALE_TEMPORARY_AGE old.
- "Unchanged" means the same size and the same modification time, to the
  nanosecond, as the source. The copy takes the source's modification
  time so that this comparison works. It is a cheap stand-in for comparing
  bytes; a tool that rewrote a transcript and then set its modification
  time back would fool it, and Claude Code is not known to do that.
- A changed file is copied again in full, not appended to, because only a
  whole new file can be renamed into place atomically. The earlier copy is
  then read once more to check that the new one starts with it.
- A copy is never overwritten by something it is not the start of.
  Transcripts are append-only in normal use, so a source that is shorter
  than the copy, or no longer begins with it, means something rewrote it
  (compaction, an edit, a different Claude Code version). The earlier copy
  is then moved to `superseded-transcripts/<moment in UTC>/` at the same
  relative path, and the new copy takes its place. Kept copies sit outside
  `transcripts/`, because an importer walking `transcripts/` would read a
  kept copy as a second Claude session with the same id. Trap: an importer
  pointed at the whole log directory would still find them.
- Copies are readable by this macOS user account only (0600, folders
  0700), as goodall.work_blocks does, because transcripts hold a person's
  prompts.
"""

import enum
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from goodall import records

# The folder in the log directory that mirrors Claude Code's projects folder.
TRANSCRIPTS_FOLDER_NAME = "transcripts"

# The folder in the log directory where an earlier copy is kept when the
# source no longer starts with it.
SUPERSEDED_FOLDER_NAME = "superseded-transcripts"

# The folder in the log directory for goodall's own notes about copying:
# `failed-copies.jsonl`, one line per copy a hook could not make, and
# `reported/`, where those lines go once `goodall status` has said so.
SYNC_FOLDER_NAME = "transcript-sync"
FAILED_COPIES_FILE_NAME = "failed-copies.jsonl"
REPORTED_FOLDER_NAME = "reported"

# One lock file per Claude session, in `transcript-sync/locks/`, so two
# copies of one Claude session never run at once.
LOCKS_FOLDER_NAME = "locks"

# How long a copy waits for another copy of the same Claude session to
# finish before giving up. Copies take well under a second; a hook must
# not hold Claude Code up for long, and the next hook or sweep tries again.
LOCK_WAIT = timedelta(seconds=10)
LOCK_POLL_SECONDS = 0.01

# Where Claude Code keeps a Claude session's subagent transcripts, inside a
# folder named after the session.
SUBAGENTS_FOLDER_NAME = "subagents"

# Readable and writable by this macOS user account only.
FILE_PERMISSIONS = 0o600
FOLDER_PERMISSIONS = 0o700

# How much is read and written at a time while copying.
CHUNK_BYTES = 1024 * 1024

# Temporary files end with this, so a leftover one is easy to recognise.
TEMPORARY_SUFFIX = ".goodall-tmp"

# A temporary file older than this was left by a copy that was killed, and
# the sweep removes it. Far longer than any copy takes, so a copy still
# running in another process never loses its temporary file.
STALE_TEMPORARY_AGE = timedelta(minutes=10)


class FileOutcome(enum.Enum):
    """What copying one file did."""

    UNCHANGED = "unchanged"
    COPIED = "copied"
    # The earlier copy was not the start of the source, so it was kept aside.
    COPIED_AND_EARLIER_COPY_KEPT = "copied_and_earlier_copy_kept"


def claude_projects_folder() -> Path:
    """Where Claude Code keeps its transcripts: `projects` in CLAUDE_CONFIG_DIR, or in `~/.claude`.

    Read from the environment each time, so a test can change it. An empty
    value counts as unset, as goodall treats its own GOODALL_CONFIG_DIR.
    """
    claude_config_directory = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if claude_config_directory == "":
        return Path.home() / ".claude" / "projects"
    return Path(claude_config_directory).expanduser() / "projects"


def copy_from_hook(
    projects_folder: Path, log_directory: Path, claude_session: str, transcript_path: Path, now: datetime
) -> None:
    """Copy the Claude session a hook named, from `transcript_path`, into the log directory.

    `transcript_path` comes from the hook's input, so it is checked before
    anything is read: it must be `<projects folder>/<project folder>/<claude
    session>.jsonl` once every symbolic link and `..` is resolved. Anything
    else raises TranscriptPathRefused, and nothing is copied: goodall copies
    only from inside the Claude projects folder. A transcript that does not
    exist yet (Claude Code creates it after the first prompt) is not an
    error; there is simply nothing to copy.
    """
    if not transcript_path.is_absolute() or transcript_path.name != f"{claude_session}.jsonl":
        raise TranscriptPathRefused("the transcript path is not a Claude session's transcript")
    try:
        real_projects_folder = projects_folder.resolve(strict=True)
        real_transcript_path = transcript_path.resolve(strict=True)
    except FileNotFoundError:
        return
    if not real_transcript_path.is_relative_to(real_projects_folder):
        raise TranscriptPathRefused("the transcript path leads outside the Claude projects folder")
    relative_parts = real_transcript_path.relative_to(real_projects_folder).parts
    if len(relative_parts) != 2 or relative_parts[1] != transcript_path.name:
        raise TranscriptPathRefused("the transcript path is not directly inside a project folder")
    copy_claude_session(real_projects_folder, log_directory, relative_parts[0], claude_session, now)


class TranscriptPathRefused(ValueError):
    """A hook named a transcript path goodall will not copy from; the message says why, without the path."""


@dataclass
class SweepResult:
    """What a sweep did: how many Claude sessions it copied, earlier copies it kept aside, and what failed.

    `problems` holds one plain sentence for each Claude session that could
    not be copied, naming it and saying what went wrong.
    """

    claude_sessions_copied: int = 0
    earlier_copies_kept: int = 0
    problems: list[str] = field(default_factory=list)
    # How many copies hooks failed to make since the last sweep that
    # succeeded, and where the reasons are kept.
    failed_hook_copies: int = 0
    failed_hook_copies_file: Path | None = None


def record_failed_copy(log_directory: Path, now: datetime, event_name: str, claude_session: str, problem: str) -> None:
    """Note, in the log directory, that a hook could not copy a transcript, so `goodall status` can say so later.

    The hook must stay silent (anything it printed or any non-zero exit
    would reach Claude Code), so this file is how a failure stays visible.
    One JSON line per failure, appended in a single write. Never raises:
    when even this cannot be written (the log directory is unplugged, say),
    the failure goes unrecorded, and the next sweep's own attempt shows it.
    """
    line = json.dumps(
        {"at": format_moment(now), "event": event_name, "claude_session": claude_session, "problem": problem}
    )
    try:
        sync_folder = make_folders(log_directory, [SYNC_FOLDER_NAME])
        descriptor = os.open(
            sync_folder / FAILED_COPIES_FILE_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_PERMISSIONS
        )
        try:
            os.write(descriptor, (line + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError:
        pass


def take_failed_hook_copies(log_directory: Path, now: datetime, result: SweepResult) -> None:
    """Count the copies hooks failed to make, and once a sweep has copied everything, file their record away.

    While the sweep itself still fails, the record stays where it is, to be
    reported again. Once a sweep succeeds, every transcript is copied, so
    the record is moved to `transcript-sync/reported/` (kept, not deleted)
    and is reported only this once.
    """
    failed_copies_file = log_directory / SYNC_FOLDER_NAME / FAILED_COPIES_FILE_NAME
    try:
        failed_copies_text = failed_copies_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    result.failed_hook_copies = len(failed_copies_text.splitlines())
    result.failed_hook_copies_file = failed_copies_file
    if result.problems:
        return
    reported_folder = make_folders(log_directory, [SYNC_FOLDER_NAME, REPORTED_FOLDER_NAME])
    reported_file = reported_folder / f"failed-copies-{format_moment(now)}-{secrets.token_hex(2)}.jsonl"
    os.rename(failed_copies_file, reported_file)
    result.failed_hook_copies_file = reported_file


def format_moment(moment: datetime) -> str:
    """A moment in UTC to the microsecond, fit for a file or folder name: `20261117T101500.123456Z`."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def sweep(projects_folder: Path, log_directory: Path, now: datetime) -> SweepResult:
    """Copy every Claude session in every project folder that has changed since it was last copied.

    `goodall start`, `stop` and `status` run this, because a Claude Code
    killed with SIGKILL, or a Mac that lost power, fires no hook, and the
    sweep is then the only thing that copies what the hooks missed.

    It covers every Claude session in the Claude projects folder, not only
    those a work block's heartbeats named: an eval account is used for the
    eval and nothing else (ADR 0008), so copying too much is the safer
    mistake. A Claude session is a `<session id>.jsonl` file, or a
    `<session id>/` folder, directly inside a project folder; anything else
    is left alone. Unchanged files cost two stats each and are not read.

    Never raises for anything on the disk: whatever cannot be read or
    written becomes one of `problems`, and the rest is still copied.
    """
    result = SweepResult()
    try:
        project_folder_names = list_folders(projects_folder)
    except OSError as error:
        result.problems.append(f"The Claude projects folder {projects_folder}: {describe_error(error)}.")
        project_folder_names = []
    for project_folder_name in project_folder_names:
        try:
            claude_sessions = list_claude_sessions(projects_folder / project_folder_name)
        except OSError as error:
            result.problems.append(f"The project folder {project_folder_name}: {describe_error(error)}.")
            continue
        for claude_session in claude_sessions:
            try:
                outcomes = copy_claude_session(projects_folder, log_directory, project_folder_name, claude_session, now)
            except OSError as error:
                result.problems.append(f"Claude session {claude_session}: {describe_error(error)}.")
                continue
            if FileOutcome.COPIED in outcomes or FileOutcome.COPIED_AND_EARLIER_COPY_KEPT in outcomes:
                result.claude_sessions_copied += 1
            result.earlier_copies_kept += outcomes.count(FileOutcome.COPIED_AND_EARLIER_COPY_KEPT)
    try:
        take_failed_hook_copies(log_directory, now, result)
        remove_stale_temporary_files(log_directory / TRANSCRIPTS_FOLDER_NAME, now)
    except OSError as error:
        result.problems.append(f"Tidying {SYNC_FOLDER_NAME} in the log directory: {describe_error(error)}.")
    return result


def describe_error(error: OSError) -> str:
    """What went wrong, in the system's words: `Permission denied`, `No space left on device`."""
    if error.strerror:
        return error.strerror
    return str(error)


def remove_stale_temporary_files(folder: Path, now: datetime) -> None:
    """Remove the temporary files that killed copies left anywhere under `folder`.

    A copy killed by SIGKILL cannot clean up after itself, so its temporary
    file stays beside the copy. Only files ending in TEMPORARY_SUFFIX and
    last changed more than STALE_TEMPORARY_AGE before `now` are removed.
    """
    oldest_kept = (now - STALE_TEMPORARY_AGE).timestamp()
    for folder_path, _, file_names in os.walk(folder):
        for file_name in file_names:
            if not file_name.endswith(TEMPORARY_SUFFIX):
                continue
            temporary_file = Path(folder_path) / file_name
            try:
                if temporary_file.stat(follow_symlinks=False).st_mtime < oldest_kept:
                    temporary_file.unlink()
            except FileNotFoundError:
                pass


def list_folders(folder: Path) -> list[str]:
    """The names of the real folders (not symbolic links) directly inside `folder`, sorted; none when it is missing."""
    folder_status = status_or_none(folder)
    if folder_status is None or not stat.S_ISDIR(folder_status.st_mode):
        return []
    folder_names = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                folder_names.append(entry.name)
    return sorted(folder_names)


def list_claude_sessions(project_folder: Path) -> list[str]:
    """The Claude session ids in one project folder: from `<id>.jsonl` files and `<id>/` folders, sorted."""
    claude_sessions = set()
    with os.scandir(project_folder) as entries:
        for entry in entries:
            name = entry.name
            if entry.is_file(follow_symlinks=False) and name.endswith(".jsonl"):
                name = name.removesuffix(".jsonl")
            elif not entry.is_dir(follow_symlinks=False):
                continue
            if records.CLAUDE_SESSION_ID_PATTERN.fullmatch(name):
                claude_sessions.add(name)
    return sorted(claude_sessions)


def copy_claude_session(
    projects_folder: Path, log_directory: Path, project_folder_name: str, claude_session: str, now: datetime
) -> list[FileOutcome]:
    """Copy one Claude session's files into the same places under the log directory's `transcripts` folder.

    The files are the transcript, `<session id>.jsonl`, and every file in
    `<session id>/subagents/` (each subagent's transcript, with its
    `.meta.json` and `.prefix.json`). Anything else in `<session id>/`,
    and any folder inside `subagents/`, is not copied. A file that is not
    there (yet, or any more) is skipped. Returns what happened to each file
    copied or left alone.
    """
    relative_paths = [Path(project_folder_name) / f"{claude_session}.jsonl"]
    subagents_folder = Path(project_folder_name) / claude_session / SUBAGENTS_FOLDER_NAME
    for subagent_file_name in list_regular_files(projects_folder / subagents_folder):
        relative_paths.append(subagents_folder / subagent_file_name)
    outcomes = []
    with claude_session_lock(log_directory, claude_session):
        for relative_path in relative_paths:
            try:
                outcomes.append(copy_file(projects_folder, log_directory, relative_path, now))
            except FileNotFoundError as error:
                if error.filename != str(projects_folder / relative_path):
                    raise
    return outcomes


@contextmanager
def claude_session_lock(log_directory: Path, claude_session: str) -> Iterator[None]:
    """Hold the lock on one Claude session's copies, waiting up to LOCK_WAIT for another copy to finish.

    Why: Claude Code runs the PostToolUse hooks of parallel tool calls at
    the same moment. Two copies racing could rename their copies in the
    wrong order, and the later one would then find a copy it is not the
    start of and keep it aside for nothing. The lock is an advisory
    `flock`, which the system releases when the process ends, so a killed
    copy never leaves it held. Raises TimeoutError after LOCK_WAIT.
    """
    lock_folder = make_folders(log_directory, [SYNC_FOLDER_NAME, LOCKS_FOLDER_NAME])
    descriptor = os.open(lock_folder / f"{claude_session}.lock", os.O_WRONLY | os.O_CREAT, FILE_PERMISSIONS)
    try:
        give_up_at = time.monotonic() + LOCK_WAIT.total_seconds()
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > give_up_at:
                    raise TimeoutError(f"another copy of Claude session {claude_session} took too long") from None
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(descriptor)


def list_regular_files(folder: Path) -> list[str]:
    """The names of the regular files directly inside `folder`, sorted; none when it is missing or not a real folder.

    Symbolic links are skipped, and so is a `folder` reached through one
    (checked for `folder` and its parent), so nothing outside the Claude
    projects folder is copied by way of a link.
    """
    for folder_to_check in [folder.parent, folder]:
        folder_status = status_or_none(folder_to_check)
        if folder_status is None or not stat.S_ISDIR(folder_status.st_mode):
            return []
    file_names = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                file_names.append(entry.name)
    return sorted(file_names)


def make_folders(log_directory: Path, folder_names: list[str]) -> Path:
    """Create each of `folder_names` inside the one before, starting in the log directory; return the last.

    One at a time, owner-only, because `mkdir(parents=True)` would give the
    folders it creates on the way the default permissions instead. The log
    directory itself is never created: if it is missing (an external drive
    unplugged), this raises rather than make a stray folder in its place.
    """
    folder = log_directory
    for folder_name in folder_names:
        folder = folder / folder_name
        folder.mkdir(mode=FOLDER_PERMISSIONS, exist_ok=True)
    return folder


def copy_file(projects_folder: Path, log_directory: Path, relative_path: Path, now: datetime) -> FileOutcome:
    """Copy one file from the Claude projects folder into `transcripts` in the log directory, at the same path.

    Byte for byte and atomically, keeping the source's modification time.

    - A copy whose size and modification time (to the nanosecond) match
      the source's is taken to be identical and left alone, without reading
      either file: that is what keeps a hook fast when nothing changed.
    - Otherwise the source is copied in full. Transcripts only grow in
      normal use, so the earlier copy is normally the start of the new one.
    - When it is not (the source is shorter, or what was copied before has
      changed), the earlier copy is kept, never overwritten: it is moved to
      `superseded-transcripts/<moment>/` at the same relative path, then
      the new copy takes its place.
    """
    source = projects_folder / relative_path
    destination = log_directory / TRANSCRIPTS_FOLDER_NAME / relative_path
    source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        source_status = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_status.st_mode):
            raise OSError(f"{source} is not a regular file")
        copy_status = status_or_none(destination)
        if (
            copy_status is not None
            and copy_status.st_size == source_status.st_size
            and copy_status.st_mtime_ns == source_status.st_mtime_ns
        ):
            return FileOutcome.UNCHANGED

        outcome = FileOutcome.COPIED
        make_folders(log_directory, [TRANSCRIPTS_FOLDER_NAME, *relative_path.parent.parts])
        temporary_file = destination.parent / f".{destination.name}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
        try:
            write_copy(source_descriptor, source_status.st_size, temporary_file)
            os.utime(temporary_file, ns=(source_status.st_atime_ns, source_status.st_mtime_ns))
            if copy_status is not None and not is_start_of(destination, temporary_file):
                keep_earlier_copy(log_directory, relative_path, now)
                outcome = FileOutcome.COPIED_AND_EARLIER_COPY_KEPT
            os.replace(temporary_file, destination)
        finally:
            temporary_file.unlink(missing_ok=True)
    finally:
        os.close(source_descriptor)
    return outcome


def is_start_of(earlier_copy: Path, new_copy: Path) -> bool:
    """True when every byte of `earlier_copy` is at the same place at the start of `new_copy`.

    Reads the whole of the earlier copy, and as much of the new one: an
    honest check that nothing copied before would be lost, at the price of
    reading the file twice. The new copy is compared, not the source, since
    the new copy is what will replace the earlier one.
    """
    if earlier_copy.stat().st_size > new_copy.stat().st_size:
        return False
    with earlier_copy.open("rb") as earlier, new_copy.open("rb") as new:
        while True:
            earlier_chunk = earlier.read(CHUNK_BYTES)
            if earlier_chunk == b"":
                return True
            if new.read(len(earlier_chunk)) != earlier_chunk:
                return False


def keep_earlier_copy(log_directory: Path, relative_path: Path, now: datetime) -> None:
    """Move the copy at `relative_path` to `superseded-transcripts/<moment>/<relative_path>`, keeping it.

    The moment, in UTC to the microsecond, names the folder so a person can
    see when each earlier copy was replaced. The kept copy sits outside
    `transcripts/` on purpose, so a program reading `transcripts/` as a
    Claude projects folder never sees two versions of one Claude session.
    Raises FileExistsError rather than overwrite anything already kept there.
    """
    kept_folder = make_folders(log_directory, [SUPERSEDED_FOLDER_NAME, format_moment(now), *relative_path.parent.parts])
    kept_copy = kept_folder / relative_path.name
    if kept_copy.exists():
        raise FileExistsError(f"{kept_copy} is already there")
    os.rename(log_directory / TRANSCRIPTS_FOLDER_NAME / relative_path, kept_copy)


def status_or_none(path: Path) -> os.stat_result | None:
    """The file's own status (a symlink is not followed), or None when nothing is there."""
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None


def write_copy(source_descriptor: int, size: int, temporary_file: Path) -> None:
    """Write the first `size` bytes of the open source into a new owner-only `temporary_file`.

    Exactly `size` bytes are copied, the size when the copy began: Claude
    Code may append while goodall copies, and what it appends is copied next
    time, because the sizes will then differ.
    """
    temporary_descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS)
    with os.fdopen(temporary_descriptor, "wb") as temporary:
        copied = 0
        while copied < size:
            chunk = os.pread(source_descriptor, min(CHUNK_BYTES, size - copied), copied)
            if chunk == b"":
                break
            temporary.write(chunk)
            copied += len(chunk)
        temporary.flush()
        os.fsync(temporary.fileno())
