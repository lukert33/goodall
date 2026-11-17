"""Copying Claude Code's transcripts into the log directory, verbatim and in Claude Code's own layout.

A transcript is a raw record (ADR 0003): goodall copies it byte for byte and
never rewrites, redacts or re-encodes it. The copy keeps the layout of
Claude Code's projects folder, so the copied folder can be read by anything
that reads that folder, Inspect Scout's claude_code importer included:

    <Claude projects folder>/<project folder>/<session id>.jsonl
      is copied to
    <log directory>/transcripts/<project folder>/<session id>.jsonl

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

- Files are copied to a temporary file in the destination folder, which is
  then renamed over the copy. A rename is atomic, so a copy interrupted at
  any moment, even by SIGKILL, leaves the old copy or the new one, never a
  file cut short.
- The copy takes the source's modification time, so it can be compared
  with the source later. Copies are readable by this macOS user account
  only (0600, folders 0700), as goodall.work_blocks does, because
  transcripts hold a person's prompts.
"""

import enum
import os
import secrets
import stat
from pathlib import Path

# The folder in the log directory that mirrors Claude Code's projects folder.
TRANSCRIPTS_FOLDER_NAME = "transcripts"

# Readable and writable by this macOS user account only.
FILE_PERMISSIONS = 0o600
FOLDER_PERMISSIONS = 0o700

# How much is read and written at a time while copying.
CHUNK_BYTES = 1024 * 1024

# Temporary files end with this, so a leftover one is easy to recognise.
TEMPORARY_SUFFIX = ".goodall-tmp"


def claude_projects_folder() -> Path:
    """Where Claude Code keeps its transcripts: `projects` in CLAUDE_CONFIG_DIR, or in `~/.claude`.

    Read from the environment each time, so a test can change it. An empty
    value counts as unset, as goodall treats its own GOODALL_CONFIG_DIR.
    """
    claude_config_directory = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if claude_config_directory == "":
        return Path.home() / ".claude" / "projects"
    return Path(claude_config_directory).expanduser() / "projects"


def copy_from_hook(projects_folder: Path, log_directory: Path, claude_session: str, transcript_path: Path) -> None:
    """Copy the Claude session a hook named, from `transcript_path`, into the log directory."""
    project_folder_name = transcript_path.parent.name
    copy_claude_session(projects_folder, log_directory, project_folder_name, claude_session)


def copy_claude_session(
    projects_folder: Path, log_directory: Path, project_folder_name: str, claude_session: str
) -> None:
    """Copy one Claude session's transcript into the same place under the log directory's `transcripts` folder."""
    source = projects_folder / project_folder_name / f"{claude_session}.jsonl"
    copied_project_folder = make_folders(log_directory, [TRANSCRIPTS_FOLDER_NAME, project_folder_name])
    copy_file(source, copied_project_folder / source.name, log_directory / "superseded" / source.name)


def make_folders(log_directory: Path, folder_names: list[str]) -> Path:
    """Create each of `folder_names` inside the one before, starting in the log directory; return the last.

    One at a time, owner-only, because `mkdir(parents=True)` would give the
    folders it creates on the way the default permissions instead.
    """
    folder = log_directory
    for folder_name in folder_names:
        folder = folder / folder_name
        folder.mkdir(mode=FOLDER_PERMISSIONS, exist_ok=True)
    return folder


class FileOutcome(enum.Enum):
    """What copying one file did."""

    COPIED = "copied"


def copy_file(source: Path, destination: Path, superseded_destination: Path) -> FileOutcome:
    """Copy `source` to `destination` byte for byte, atomically, keeping the source's modification time.

    `superseded_destination` is where an existing copy would be kept if it
    had to be replaced. The destination folder must exist.
    """
    source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        source_status = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_status.st_mode):
            raise OSError(f"{source} is not a regular file")
        temporary_file = destination.parent / f".{destination.name}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
        try:
            write_copy(source_descriptor, source_status.st_size, temporary_file)
            os.utime(temporary_file, ns=(source_status.st_atime_ns, source_status.st_mtime_ns))
            os.replace(temporary_file, destination)
        finally:
            temporary_file.unlink(missing_ok=True)
    finally:
        os.close(source_descriptor)
    return FileOutcome.COPIED


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
