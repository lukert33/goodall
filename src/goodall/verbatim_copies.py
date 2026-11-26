"""Copying files into the log directory, verbatim: the machinery every source application's copies go through.

A source application's own records are raw records (ADR 0003): goodall
copies them byte for byte and never rewrites, redacts or re-encodes them.
This module knows how to copy a file that way and nothing about any one
application. What to copy, and where the copies go, is described by a
Mirror: a folder of the application's and the folder in the log directory
that copies it, path for path, so each application's records keep that
application's own layout (ADR 0019).

Who calls it: an app family's module (goodall.claude_family is the first)
builds the mirrors and lists the files, and goodall.source_applications,
the baseline, decides when. This module is on the hook path, because a hook
uses its lock and its folder-making to remember where Claude Code keeps its
files, so it imports only the standard library (ADR 0005) and starts no
program.

Decisions a reader would otherwise reverse-engineer:

- Inside what is copied, symbolic links are never followed, special files
  are never opened, and both are skipped and noted, as are files that
  cannot be read; the rest is still copied. A link could lead outside the
  folder goodall copies from.
- Files are copied to a temporary file in the destination folder, which is
  then renamed over the copy. A rename is atomic, so a copy interrupted at
  any moment, even by SIGKILL, leaves the old copy or the new one, never a
  file cut short. A killed copy leaves its temporary file behind; they are
  removed once they are STALE_TEMPORARY_AGE old (remove_stale_temporary_files).
- "Unchanged" means the same size and the same modification time, to the
  nanosecond, as the source. The copy takes the source's modification
  time so that this comparison works. It is a cheap stand-in for comparing
  bytes; a tool that rewrote a file and then set its modification time
  back would fool it, and no source application is known to do that.
- A changed file is copied again in full, not appended to, because only a
  whole new file can be renamed into place atomically. The earlier copy is
  then read once more to check that the new one starts with it.
- A copy is never overwritten by something it is not the start of.
  Session files are append-only in normal use, so a source that is shorter
  than the copy, or no longer begins with it, means something rewrote it
  (an edit, a different version of the application). The earlier copy is
  then moved to the mirror's superseded folder, under `<moment in UTC>/`
  at the same relative path, and the new copy takes its place. Kept copies
  sit outside the copies folder, because a program reading that folder as
  the application's own would read a kept copy as a second agent session
  with the same id.
- Copies are readable by this macOS user account only (0600, folders
  0700), as goodall.work_blocks does, because session files hold a
  person's prompts.
"""

import enum
import errno
import fcntl
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The folder in the log directory for goodall's own bookkeeping about
# copying: its locks, the work blocks whose agent sessions are still to be
# copied (goodall.source_applications), and where hooks found Claude Code
# (goodall.claude_family). The name dates from when only Claude Code's
# transcripts were copied (ADR 0015).
#
# Trap: nothing goodall writes here may end in `.jsonl`. Inspect Scout's
# claude_code importer, pointed at a folder, reads every `*.jsonl` file
# directly inside it, or inside each folder one level down, as a Claude
# session, so a `.jsonl` here would be imported as one if someone pointed it
# at the whole log directory.
SYNC_FOLDER_NAME = "transcript-sync"

# Lock files, in `transcript-sync/locks/`, so two goodall commands never
# copy the same files at once.
LOCKS_FOLDER_NAME = "locks"

# How long one goodall waits for another to finish what the lock guards
# before giving up. The next goodall start, stop or status tries again.
LOCK_WAIT = timedelta(seconds=10)
LOCK_POLL_SECONDS = 0.01

# Readable and writable by this macOS user account only.
FILE_PERMISSIONS = 0o600
FOLDER_PERMISSIONS = 0o700

# How much is read and written at a time while copying.
CHUNK_BYTES = 1024 * 1024

# Temporary files end with this, so a leftover one is easy to recognize.
TEMPORARY_SUFFIX = ".goodall-tmp"

# A temporary file older than this was left by a copy that was killed, and
# goodall removes it. Far longer than any copy takes, so a copy still
# running in another process never loses its temporary file.
STALE_TEMPORARY_AGE = timedelta(minutes=10)


class FileOutcome(enum.Enum):
    """What copying one file did."""

    UNCHANGED = "unchanged"
    COPIED = "copied"
    # The earlier copy was not the start of the source, so it was kept aside.
    COPIED_AND_EARLIER_COPY_KEPT = "copied_and_earlier_copy_kept"


# How many skipped files a note names before it only counts the rest, so a
# folder of a thousand unreadable files makes one readable line, not a page.
SKIPPED_NAMED_LIMIT = 5


def describe_skipped(skipped: list[str]) -> str:
    """One sentence about what was not copied: the first few notes in full, then how many more."""
    named = skipped[:SKIPPED_NAMED_LIMIT]
    sentence = f"{len(skipped)} thing(s) were not copied: " + "; ".join(named)
    if len(skipped) > len(named):
        sentence += f"; and {len(skipped) - len(named)} more"
    return sentence


def format_moment(moment: datetime) -> str:
    """A moment in UTC to the microsecond, fit for a file or folder name: `20261117T101500.123456Z`."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


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


@dataclass
class Copying:
    """What copying some files did: an outcome for each file copied or left alone, and what was skipped.

    `skipped` holds one short note per thing that was not copied, naming it
    by its path inside the folder it was to be copied from and saying why:
    `<project>/<id>/tool-results/x.txt: Permission denied`. Never anything
    read from inside a file.
    """

    outcomes: list[FileOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, other: "Copying") -> None:
        """Take in what another copying did."""
        self.outcomes.extend(other.outcomes)
        self.skipped.extend(other.skipped)

    def copied_anything(self) -> bool:
        """True when at least one file was copied, rather than left alone as unchanged."""
        return FileOutcome.COPIED in self.outcomes or FileOutcome.COPIED_AND_EARLIER_COPY_KEPT in self.outcomes

    def earlier_copies_kept(self) -> int:
        """How many earlier copies were kept aside because their source no longer started with them."""
        return self.outcomes.count(FileOutcome.COPIED_AND_EARLIER_COPY_KEPT)


@dataclass(frozen=True)
class Mirror:
    """A folder of a source application's and the folder in the log directory that copies it, path for path.

    `copies_folder_names` is where the copies go, inside the log directory;
    an earlier copy that is replaced goes to `<superseded_folder_name>/
    <moment>/`, followed by the rest of those names and the same relative
    path, so kept copies have the same layout as the copies themselves.
    """

    source_folder: Path
    copies_folder_names: tuple[str, ...]
    superseded_folder_name: str

    def copies_folder(self, log_directory: Path) -> Path:
        """The folder in the log directory that holds the copies."""
        return log_directory.joinpath(*self.copies_folder_names)


def copy_files(mirror: Mirror, log_directory: Path, relative_paths: list[Path], now: datetime) -> Copying:
    """Copy each of `relative_paths` from the mirror's source folder to its copies, skipping what cannot be read.

    A file that is not there (yet, or any more) is passed over without a
    note, and its earlier copy stays. A source goodall cannot read, or that
    has turned into something other than a regular file, is skipped and
    noted, and the rest is still copied. A copy goodall cannot write is a
    problem with the log directory, so it raises OSError.
    """
    copying = Copying()
    for relative_path in relative_paths:
        source = mirror.source_folder / relative_path
        try:
            copying.outcomes.append(copy_file(mirror, log_directory, relative_path, now))
        except FileNotFoundError as error:
            if error.filename != str(source):
                raise
        except OSError as error:
            if error.filename != str(source):
                raise
            copying.skipped.append(f"{relative_path}: {describe_error(error)}")
    return copying


def copy_places(mirror: Mirror, log_directory: Path, places: list[Path], now: datetime) -> Copying:
    """Copy each of `places` in the mirror's source folder: a file, or a folder with everything under it.

    A place with nothing there is passed over. What had to be skipped while
    listing a folder (list_files_at) or while copying (copy_files) is noted
    in the result. Raises OSError when the log directory cannot be written.
    """
    relative_paths = []
    skipped = []
    for place in places:
        found_files, found_skipped = list_files_at(mirror.source_folder, place)
        relative_paths.extend(found_files)
        skipped.extend(found_skipped)
    copying = copy_files(mirror, log_directory, relative_paths, now)
    copying.skipped = skipped + copying.skipped
    return copying


def list_files_at(source_folder: Path, relative_path: Path) -> tuple[list[Path], list[str]]:
    """The file at `relative_path`, or every regular file under it at any depth, by its path inside `source_folder`.

    Returns the files, sorted, and a note for each thing passed over: a
    symbolic link (to a file or a folder: it could lead outside the folder
    goodall copies from, so it is never followed), a special file, or a
    folder that cannot be read. Nothing at `relative_path` means no files
    and no notes. Walks with a list of folders still to visit rather than by
    recursion, so however deep an application nests folders, Python's recursion
    limit is never reached.
    """
    files: list[Path] = []
    skipped: list[str] = []
    top_status = status_or_none(source_folder / relative_path)
    if top_status is None:
        return files, skipped
    if stat.S_ISREG(top_status.st_mode):
        return [relative_path], skipped
    if not stat.S_ISDIR(top_status.st_mode):
        return files, [f"{relative_path}: {describe_file_kind(top_status)}, which goodall does not copy"]
    folders_to_visit = [relative_path]
    while folders_to_visit:
        folder = folders_to_visit.pop()
        try:
            with os.scandir(source_folder / folder) as entries:
                found = list(entries)
        except FileNotFoundError:
            continue
        except OSError as error:
            skipped.append(f"{folder}: {describe_error(error)}")
            continue
        for entry in found:
            entry_path = folder / entry.name
            if entry.is_symlink():
                skipped.append(f"{entry_path}: a symbolic link, which goodall does not follow")
            elif entry.is_dir(follow_symlinks=False):
                folders_to_visit.append(entry_path)
            elif entry.is_file(follow_symlinks=False):
                files.append(entry_path)
            else:
                skipped.append(f"{entry_path}: a special file, which goodall does not copy")
    return sorted(files), sorted(skipped)


def describe_file_kind(file_status: os.stat_result) -> str:
    """What sort of thing a path is, for a note about why it was not copied."""
    if stat.S_ISLNK(file_status.st_mode):
        return "a symbolic link"
    return "a special file"


@contextmanager
def held_lock(log_directory: Path, lock_name: str, what_waits: str) -> Iterator[None]:
    """Hold `transcript-sync/locks/<lock_name>.lock`, waiting up to LOCK_WAIT for whoever holds it.

    The lock is an advisory `flock`, which the system releases when the
    process ends, so a killed goodall never leaves it held. Raises TimeoutError,
    naming `what_waits`, after LOCK_WAIT.
    """
    lock_folder = make_folders(log_directory, [SYNC_FOLDER_NAME, LOCKS_FOLDER_NAME])
    descriptor = os.open(lock_folder / f"{lock_name}.lock", os.O_WRONLY | os.O_CREAT, FILE_PERMISSIONS)
    try:
        give_up_at = time.monotonic() + LOCK_WAIT.total_seconds()
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > give_up_at:
                    raise TimeoutError(f"{what_waits} took too long") from None
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(descriptor)


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


def copy_file(mirror: Mirror, log_directory: Path, relative_path: Path, now: datetime) -> FileOutcome:
    """Copy one file from the mirror's source folder to its copies in the log directory, at the same path.

    Byte for byte and atomically, keeping the source's modification time.

    - A copy whose size and modification time (to the nanosecond) match
      the source's is taken to be identical and left alone, without reading
      either file: that is what keeps copying fast when nothing changed.
    - Otherwise the source is copied in full. Session files only grow in
      normal use, so the earlier copy is normally the start of the new one.
    - When it is not (the source is shorter, or what was copied before has
      changed), the earlier copy is kept, never overwritten: it is moved to
      the mirror's superseded folder (keep_earlier_copy), then the new copy
      takes its place.
    """
    source = mirror.source_folder / relative_path
    destination = mirror.copies_folder(log_directory) / relative_path
    source_status = os.stat(source, follow_symlinks=False)
    if not stat.S_ISREG(source_status.st_mode):
        raise NotARegularFile(errno.EINVAL, "not a regular file", str(source))
    copy_status = status_or_none(destination)
    if is_same_size_and_time(copy_status, source_status):
        return FileOutcome.UNCHANGED

    # O_NOFOLLOW refuses a symbolic link put in the file's place since the
    # stat above; O_NONBLOCK keeps a named pipe put there from waiting
    # forever for a writer. The fstat then checks what was really opened.
    source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        source_status = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_status.st_mode):
            raise NotARegularFile(errno.EINVAL, "not a regular file", str(source))
        outcome = FileOutcome.COPIED
        make_folders(log_directory, [*mirror.copies_folder_names, *relative_path.parent.parts])
        temporary_file = destination.parent / f".{destination.name}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
        try:
            write_copy(source_descriptor, source_status.st_size, temporary_file)
            os.utime(temporary_file, ns=(source_status.st_atime_ns, source_status.st_mtime_ns))
            if copy_status is not None and not is_start_of(destination, temporary_file):
                keep_earlier_copy(mirror, log_directory, relative_path, now)
                outcome = FileOutcome.COPIED_AND_EARLIER_COPY_KEPT
            os.replace(temporary_file, destination)
        finally:
            temporary_file.unlink(missing_ok=True)
    finally:
        os.close(source_descriptor)
    return outcome


class NotARegularFile(OSError):
    """A path goodall was to copy is not a regular file: a named pipe, a device, or a link put in its place."""


def is_same_size_and_time(copy_status: os.stat_result | None, source_status: os.stat_result) -> bool:
    """True when a copy exists with the source's size and modification time, to the nanosecond."""
    return (
        copy_status is not None
        and copy_status.st_size == source_status.st_size
        and copy_status.st_mtime_ns == source_status.st_mtime_ns
    )


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


def keep_earlier_copy(mirror: Mirror, log_directory: Path, relative_path: Path, now: datetime) -> None:
    """Move the copy at `relative_path` into the mirror's superseded folder, under `<moment>/`, keeping it.

    For the Claude family's `transcripts/` that is
    `superseded-transcripts/<moment>/<relative path>`; for a Claude config
    folder's copies it is `superseded-claude-config/<moment>/<name for that
    folder>/<relative path>`. The moment, in UTC to the microsecond, names
    the folder so a person can see when each earlier copy was replaced.
    Kept copies sit outside the copies folder on purpose, so a program
    reading it as the application's own folder never sees two versions of
    one agent session. Raises FileExistsError rather than overwrite anything
    already kept there.
    """
    kept_folder = make_folders(
        log_directory,
        [
            mirror.superseded_folder_name,
            format_moment(now),
            *mirror.copies_folder_names[1:],
            *relative_path.parent.parts,
        ],
    )
    kept_copy = kept_folder / relative_path.name
    if kept_copy.exists():
        raise FileExistsError(f"{kept_copy} is already there")
    os.rename(mirror.copies_folder(log_directory) / relative_path, kept_copy)


def status_or_none(path: Path) -> os.stat_result | None:
    """The file's own status (a symlink is not followed), or None when nothing is there."""
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None


def write_copy(source_descriptor: int, size: int, temporary_file: Path) -> None:
    """Write the first `size` bytes of the open source into a new owner-only `temporary_file`.

    Exactly `size` bytes are copied, the size when the copy began: the
    application may append while goodall copies, and what it appends is
    copied next time, because the sizes will then differ.
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
