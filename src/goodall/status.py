"""`goodall status`: tells a contributor whether a work block is active.

Called by `cli.main` when someone runs `goodall status` or
`goodall status --json`. Agents use the exit code and the JSON document to
decide whether to proceed, so both are part of goodall's stable contract.

What is built so far, in the order goodall checks:

1. Local settings are found, read and checked (goodall.local_settings).
   Everything wrong with the file is reported at once.
2. The record repo folder exists, and the log directory can be written to.
   An unreachable log directory is reported as "not recording" (ADR 0008):
   when goodall cannot write raw records, no work block can be active, so
   "cannot write records" and "not recording" are the same observable state.
3. The eval config is not read yet (the next ticket). Until it is, valid
   settings still end in a failure, so that "the settings are fine" is never
   mistaken for "a work block is active".

Each failure is built once as a `Failure` record and then shown either as
text on standard error or as one JSON document on standard output, so the two
renderings cannot drift apart. The JSON document always has the keys
`work_block_active` and `failures`.

The folder checks live here, not in goodall.local_settings, because they ask
about the machine rather than the file; move them when a second command
(goodall doctor, or the prompt gate) needs them.
"""

import os
import stat
from pathlib import Path

from goodall import exit_codes
from goodall.local_settings import FolderSetting, LocalSettings, read_local_settings
from goodall.terminal import Failure, Terminal


def run_status(terminal: Terminal, as_json: bool) -> int:
    """Report the state of this macOS user account's eval and return the exit code."""
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        return report_failures(terminal, settings_reading, as_json)
    settings = settings_reading

    # The two folders do not depend on each other, so both are checked and
    # both problems shown at once.
    folder_failures: list[Failure] = []
    record_repo_problem = describe_folder_problem(settings.record_repo.path, needs_writing=False)
    if record_repo_problem is not None:
        folder_failures.append(record_repo_missing_failure(settings, record_repo_problem))
    log_directory_problem = describe_folder_problem(settings.log_directory.path, needs_writing=True)
    if log_directory_problem is not None:
        folder_failures.append(log_directory_unreachable_failure(settings, log_directory_problem))
    if folder_failures:
        return report_failures(terminal, folder_failures, as_json)

    return report_failures(terminal, [eval_config_not_read_yet_failure(settings)], as_json)


def report_failures(terminal: Terminal, failures: list[Failure], as_json: bool) -> int:
    """Show `failures` as text or as JSON, and return the error exit code.

    The JSON document has the same keys whatever went wrong. No work block
    can be active while goodall is failing, so `work_block_active` is false.
    """
    if as_json:
        failure_documents = []
        for failure in failures:
            failure_documents.append(failure.as_json())
        terminal.write_json({"work_block_active": False, "failures": failure_documents})
    else:
        terminal.show_failures(failures)
    return exit_codes.ERROR


def describe_folder_problem(folder: Path, needs_writing: bool) -> str | None:
    """Say what is wrong with `folder`, as the end of a sentence, or return None if nothing is.

    Only looks; nothing is created or changed. Trap: Path.exists() and
    Path.is_dir() raise PermissionError when a folder above cannot be
    searched, so this asks with stat() and turns every error into words.
    """
    try:
        folder_status = folder.stat()
    except (FileNotFoundError, NotADirectoryError):
        return "does not exist"
    except OSError as stat_error:
        return f"cannot be reached: {stat_error.strerror}"

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
            f"If it is not on this Mac yet, clone it to {settings.record_repo.path}. Then run goodall status again."
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
            "Then run goodall status again."
        ),
    )


def eval_config_not_read_yet_failure(settings: LocalSettings) -> Failure:
    """The failure for valid local settings, until goodall can read the eval config.

    Temporary: replaced when goodall learns to read the eval config
    (goodall-yjt.9.3). It is a failure, not a success, because without the
    eval config goodall cannot tell whether a work block is active, and must
    not suggest the setup is complete.
    """
    return Failure(
        kind="eval_config_not_read_yet",
        what_happened=(
            "Local settings are valid, but this version of goodall cannot read the eval config yet, "
            "so it cannot tell whether a work block is active."
        ),
        why=(
            f"The eval config, in the record repo at {settings.record_repo.path}, names the eval's contributors "
            "and workstreams. Goodall must read it before it can record anything, and reading it is not built yet."
        ),
        what_to_do_next=(
            "Nothing in your local settings needs fixing. Check bd ready in the goodall repo for progress."
        ),
    )
