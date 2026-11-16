"""Whether the folders named in local settings are really there on this Mac.

Local settings name two folders: the record repo and the log directory.
goodall.local_settings checks only that each is written as a full path; it
never looks at the disk, because that is a question about the machine, not
the file. This module asks the machine. `goodall status` and
`goodall doctor` both call it, so a missing folder is worded the same way
wherever it is reported.

Everything here only looks: nothing is created or changed, because
`goodall doctor` promises to change nothing on the machine (ADR 0012).
"""

import os
import stat
from pathlib import Path

from goodall.local_settings import FolderSetting, LocalSettings
from goodall.terminal import Failure


def describe_folder_problem(folder: Path, needs_writing: bool) -> str | None:
    """Say what is wrong with `folder`, as the end of a sentence, or return None if nothing is.

    Only looks; nothing is created or changed. Trap: Path.exists() and
    Path.is_dir() raise PermissionError when a folder above cannot be
    searched, so this asks with stat() and turns every error into words.
    A path holding a null character makes stat() raise ValueError, not
    OSError; local settings refuse such a path first, and this catches it
    too so no caller can crash on one.
    """
    try:
        folder_status = folder.stat()
    except (FileNotFoundError, NotADirectoryError):
        return "does not exist"
    except OSError as stat_error:
        return f"cannot be reached: {stat_error.strerror}"
    except ValueError:
        return "cannot be reached: its path contains a character no path can contain"

    if not stat.S_ISDIR(folder_status.st_mode):
        return "is a file, not a folder"
    if needs_writing and not os.access(folder, os.W_OK | os.X_OK):
        return "cannot be written to by this macOS user account"
    return None


def where_goodall_looked(folder_setting: FolderSetting) -> str:
    """A sentence naming the full path goodall used, or nothing when it is exactly what was written.

    Messages name a folder as written first, because that is what the
    contributor will search for in the file; this adds the expanded path
    when a leading ~ made the two differ.
    """
    if folder_setting.as_written == str(folder_setting.path):
        return ""
    return f" Goodall looked for {folder_setting.path}."


def record_repo_missing_failure(settings: LocalSettings, problem: str) -> Failure:
    """The failure for a record repo folder that is not there."""
    return Failure(
        kind="record_repo_missing",
        what_happened=(
            f"The record repo folder {settings.record_repo.as_written} {problem}."
            + where_goodall_looked(settings.record_repo)
        ),
        why=(
            "The record repo holds this eval's eval config and its records, "
            "so goodall cannot tell whether a work block is active without it."
        ),
        what_to_do_next=(
            f"If the record repo is somewhere else on this Mac, correct record_repo in {settings.settings_file}. "
            f"If it is not on this Mac yet, clone it to {settings.record_repo.path}. "
            "Then run the same goodall command again."
        ),
    )


def log_directory_unreachable_failure(settings: LocalSettings, problem: str) -> Failure:
    """The failure for a log directory goodall cannot write raw records to.

    Worded as "not recording", not as an error in goodall, because an
    unplugged drive is an everyday event (ADR 0008).
    """
    return Failure(
        kind="log_directory_unreachable",
        what_happened=(
            f"Not recording: the log directory {settings.log_directory.as_written} {problem}, "
            "so no work block can be active." + where_goodall_looked(settings.log_directory)
        ),
        why=(
            "Raw records are written to the log directory. When goodall cannot write there it records "
            "nothing, and says so rather than lose records without telling you."
        ),
        what_to_do_next=(
            "If the log directory is on an external drive, connect it. Otherwise create the folder, make sure "
            f"this macOS user account can write to it, or correct log_directory in {settings.settings_file}. "
            "Then run the same goodall command again."
        ),
    )
