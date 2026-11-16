"""`goodall status`: tells a contributor whether a work block is active.

Called by `cli.main` when someone runs `goodall status` or
`goodall status --json`. Agents use the exit code and the JSON document to
decide whether to proceed, so both are part of goodall's stable contract.

What is built so far: goodall finds where local settings should be and, if
there are none, fails with what happened, why, and what to do next. Reading
the local settings and the eval config comes next; until then, finding the
file is also reported as a failure, so that "found a file" is never mistaken
for "everything is fine".

Each failure is built once as a `Failure` record and then shown either as
text on standard error or as one JSON document on standard output, so the two
renderings cannot drift apart.
"""

from goodall import exit_codes
from goodall.local_settings import (
    CONFIG_DIRECTORY_VARIABLE,
    LocalSettingsLocation,
    RelativeConfigDirectoryError,
    find_local_settings_location,
)
from goodall.terminal import Failure, Terminal


def run_status(terminal: Terminal, as_json: bool) -> int:
    """Report the state of this macOS user account's eval and return the exit code."""
    try:
        location = find_local_settings_location()
    except RelativeConfigDirectoryError as relative_directory_error:
        return report_failure(terminal, relative_config_directory_failure(relative_directory_error.value), as_json)

    # Opening the file is how goodall learns whether it exists. Trap: asking
    # first with Path.exists() raises PermissionError when the folder cannot
    # be searched, so the question "is it there?" would crash goodall.
    try:
        location.settings_file.read_bytes()
    except FileNotFoundError:
        return report_failure(terminal, local_settings_missing_failure(location), as_json)
    except OSError as read_error:
        return report_failure(terminal, local_settings_unreadable_failure(location, read_error), as_json)

    return report_failure(terminal, local_settings_not_read_yet_failure(location), as_json)


def report_failure(terminal: Terminal, failure: Failure, as_json: bool) -> int:
    """Show `failure` as text or as JSON, and return the error exit code.

    The JSON document has the same keys whatever went wrong. No work block
    can be active while goodall is failing, so `work_block_active` is false.
    """
    if as_json:
        terminal.write_json({"work_block_active": False, "failures": [failure.as_json()]})
    else:
        terminal.show_failure(failure)
    return exit_codes.ERROR


def local_settings_missing_failure(location: LocalSettingsLocation) -> Failure:
    """The failure for a macOS user account where goodall has never been set up.

    When GOODALL_CONFIG_DIR chose the folder, the message says so and offers
    unsetting it, because a variable left over from testing is the likeliest
    reason settings that exist in the home folder were not found. Otherwise it
    does not mention the variable: it is for tests and goodall's own
    development, and ADR 0008 rejected it as the way contributors find their
    eval.
    """
    if location.chosen_by_override:
        where_goodall_looked = f"in the folder that {CONFIG_DIRECTORY_VARIABLE} names"
        what_to_do_next = (
            "Run goodall onboard to set up this eval account, "
            f"or unset {CONFIG_DIRECTORY_VARIABLE} if your local settings are in your home folder."
        )
    else:
        where_goodall_looked = "the usual place in this macOS user account's home folder"
        what_to_do_next = "Run goodall onboard to set up this eval account."

    return Failure(
        kind="local_settings_missing",
        what_happened=(
            f"No local settings were found. Goodall looked for {location.settings_file}, {where_goodall_looked}."
        ),
        why=(
            "Local settings tell goodall which eval this macOS user account belongs to: where its record repo "
            "and log directory are and which contributor you are. Without them goodall cannot find the eval, "
            "so nothing can be recorded."
        ),
        what_to_do_next=what_to_do_next,
    )


def local_settings_unreadable_failure(location: LocalSettingsLocation, read_error: OSError) -> Failure:
    """The failure for local settings that exist, or may exist, but cannot be opened.

    The operating system's own reason (for example "Permission denied") is
    quoted, because it is the most precise clue there is.
    """
    return Failure(
        kind="local_settings_unreadable",
        what_happened=(f"Local settings at {location.settings_file} could not be read: {read_error.strerror}."),
        why=(
            "Goodall needs to read local settings to find this macOS user account's eval, "
            "so it cannot tell whether a work block is active."
        ),
        what_to_do_next=(
            f"Check the permissions of {location.settings_file} and the folders above it: "
            "this macOS user account must be able to read them. Then run goodall status again."
        ),
    )


def relative_config_directory_failure(value: str) -> Failure:
    """The failure for a GOODALL_CONFIG_DIR that is not an absolute path."""
    return Failure(
        kind="config_directory_not_absolute",
        what_happened=f"{CONFIG_DIRECTORY_VARIABLE} is set to {value}, which is not an absolute path.",
        why=(
            "Goodall would look for local settings relative to whichever folder a command started in, "
            "so two commands could find two different evals."
        ),
        what_to_do_next=(
            f"Set {CONFIG_DIRECTORY_VARIABLE} to a path starting with / or ~, "
            "or unset it to use local settings in your home folder."
        ),
    )


def local_settings_not_read_yet_failure(location: LocalSettingsLocation) -> Failure:
    """The placeholder failure for when local settings exist.

    Temporary: replaced when goodall learns to read local settings
    (goodall-yjt.9.2). It is a failure, not a success, because goodall has
    not checked anything yet and must not suggest the setup is fine.
    """
    return Failure(
        kind="local_settings_not_read",
        what_happened=(
            f"Local settings were found at {location.settings_file}, but this version of goodall cannot read them yet."
        ),
        why="Reading local settings is not built yet, so goodall cannot tell whether a work block is active.",
        what_to_do_next="Check bd ready in the goodall repo for progress.",
    )
