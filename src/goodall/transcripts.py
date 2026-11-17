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

Who calls it: `goodall hook` (goodall.hook) copies one Claude session's
files after PostToolUse, Stop and SessionEnd. This module is on the hook
path, so it imports only the standard library (ADR 0005) and starts no
program.

Where the Claude projects folder is: `projects` inside Claude Code's own
folder, which is `~/.claude` unless the environment variable
CLAUDE_CONFIG_DIR names another. CLAUDE_CONFIG_DIR is Claude Code's own
setting, not goodall's; goodall honours it so it looks where Claude Code
writes, and tests point it at tmp_path so the real home folder, which holds
a person's private prompts, stays out of reach.

Decisions a reader would otherwise reverse-engineer (PROVISIONAL):

- Goodall copies only from inside the Claude projects folder. The path a
  hook names comes from outside goodall, so a path that resolves anywhere
  else, through `..` or a symbolic link, is refused; so are symbolic links
  among a Claude session's subagent files.

- Files are copied to a temporary file in the destination folder, which is
  then renamed over the copy. A rename is atomic, so a copy interrupted at
  any moment, even by SIGKILL, leaves the old copy or the new one, never a
  file cut short.
- "Unchanged" means the same size and the same modification time, to the
  nanosecond, as the source. The copy takes the source's modification
  time so that this comparison works. It is a cheap stand-in for comparing
  bytes; a tool that rewrote a transcript and then set its modification
  time back would fool it, and Claude Code is not known to do that.
- A copy is never overwritten by something it is not the start of.
  Transcripts are append-only in normal use, so a source that is shorter
  than the copy, or no longer begins with it, means something rewrote it
  (compaction, an edit, a different Claude Code version). The earlier copy
  is then moved to `superseded-transcripts/<moment in UTC>/` at the same
  relative path, and the new copy takes its place. Kept copies sit outside
  `transcripts/`, because an importer walking `transcripts/` would read a
  kept copy as a second Claude session with the same id. Trap: an importer
  pointed at the whole log directory would still find them. Copies are readable by this macOS user account
  only (0600, folders 0700), as goodall.work_blocks does, because
  transcripts hold a person's prompts.
"""

import enum
import os
import secrets
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from goodall import records

# The folder in the log directory that mirrors Claude Code's projects folder.
TRANSCRIPTS_FOLDER_NAME = "transcripts"

# The folder in the log directory where an earlier copy is kept when the
# source no longer starts with it.
SUPERSEDED_FOLDER_NAME = "superseded-transcripts"

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
    """
    result = SweepResult()
    for project_folder_name in list_folders(projects_folder):
        for claude_session in list_claude_sessions(projects_folder / project_folder_name):
            try:
                outcomes = copy_claude_session(projects_folder, log_directory, project_folder_name, claude_session, now)
            except OSError as error:
                result.problems.append(f"Claude session {claude_session}: {error.strerror or error}.")
                continue
            if FileOutcome.COPIED in outcomes or FileOutcome.COPIED_AND_EARLIER_COPY_KEPT in outcomes:
                result.claude_sessions_copied += 1
            result.earlier_copies_kept += outcomes.count(FileOutcome.COPIED_AND_EARLIER_COPY_KEPT)
    return result


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
    for relative_path in relative_paths:
        try:
            outcomes.append(copy_file(projects_folder, log_directory, relative_path, now))
        except FileNotFoundError as error:
            if error.filename != str(projects_folder / relative_path):
                raise
    return outcomes


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
    moment = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    kept_folder = make_folders(log_directory, [SUPERSEDED_FOLDER_NAME, moment, *relative_path.parent.parts])
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
