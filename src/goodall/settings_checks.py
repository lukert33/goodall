"""The diagnostic checks for this macOS user account's settings, in the order they run.

`goodall doctor` runs `SETTINGS_CHECKS` first (goodall.doctor), because
every later check needs to know which eval this account belongs to. They
follow the same chain as `goodall status`: local settings, then the folders
they name, then the eval config, then the cross-checks between local
settings and the eval config. The last cross-check, that an eval including
ActivityWatch has its port and identifier recorded, is doctor's alone:
`goodall status` and the commands that record work do not need them.

Each check reuses the reader or folder check that `goodall status` uses, and
reports that reader's own failures, so a problem is worded the same way by
every command. Nothing here writes anything.

One check asks Spotlight, through the runner, whether it can find anything
inside the log directory (ADR 0019 says the folder's name should end in
`.noindex`, so that goodall's copies do not appear among a person's own
files). It reads and writes nothing itself: `mdfind` only queries
Spotlight's existing index.

The ids all start with "settings." and every check sits under the heading
"Settings". Later groups ("Tools", "Eval account", "Secrets") get their own
module and their own prefix.
"""

from pathlib import Path

from goodall.checks import Check, CheckContext, CheckOutcome, could_not_verify, failed, passed, warned
from goodall.eval_config import (
    EvalConfig,
    activitywatch_settings_missing_failure,
    contributor_not_in_roster_failure,
    find_this_contributor,
    main_workstream_is_known,
    main_workstream_unknown_failure,
    missing_activitywatch_settings,
    read_eval_config,
)
from goodall.folders import describe_folder_problem, log_directory_unreachable_failure, record_repo_missing_failure
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.runner import Command
from goodall.terminal import Failure

GROUP = "Settings"

LOCAL_SETTINGS_FOUND = "settings.local_settings_found"
LOCAL_SETTINGS_VALID = "settings.local_settings_valid"
RECORD_REPO_FOUND = "settings.record_repo_found"
LOG_DIRECTORY_REACHABLE = "settings.log_directory_reachable"
LOG_DIRECTORY_NOT_IN_SPOTLIGHT = "settings.log_directory_not_in_spotlight"
EVAL_CONFIG_VALID = "settings.eval_config_valid"
CONTRIBUTOR_IN_ROSTER = "settings.contributor_in_roster"
MAIN_WORKSTREAM_KNOWN = "settings.main_workstream_known"
ACTIVITYWATCH_SETTINGS_RECORDED = "settings.activitywatch_settings_recorded"

# The command that asks Spotlight for the files it knows inside a folder. It
# is named by its full path so a program called `mdfind` placed earlier on
# the PATH cannot answer for it. The query matches every file name, so any
# printed line means Spotlight has indexed something there. The folder is
# added by `spotlight_command`.
MDFIND = "/usr/bin/mdfind"
SPOTLIGHT_QUERY = "kMDItemFSName == '*'"
SPOTLIGHT_TIMEOUT_SECONDS = 10.0
NO_INDEX_SUFFIX = ".noindex"

# The failure kinds from goodall.local_settings that mean goodall could not
# get at the file at all, as opposed to finding something wrong inside it.
# They fail "Local settings are found"; every other kind fails "Local
# settings are valid".
NOT_FOUND_FAILURE_KINDS = ["config_directory_not_absolute", "local_settings_missing", "local_settings_unreadable"]


def check_local_settings_found(context: CheckContext) -> CheckOutcome:
    """Read local settings once, and fail only when goodall could not get at the file.

    The whole reading is kept in the context, so the next check can report
    what is wrong inside the file without reading it a second time.
    """
    reading = read_local_settings()
    context.local_settings_reading = reading
    if isinstance(reading, list) and reading[0].kind in NOT_FOUND_FAILURE_KINDS:
        return failed(reading)
    return passed()


def check_local_settings_valid(context: CheckContext) -> CheckOutcome:
    """Report every problem inside local settings at once, as `goodall status` does."""
    reading = context.local_settings_reading
    if reading is None:
        raise RuntimeError(f"{LOCAL_SETTINGS_VALID} ran before {LOCAL_SETTINGS_FOUND}; check the order of the checks.")
    if isinstance(reading, list):
        return failed(reading)
    context.local_settings = reading
    return passed()


def check_record_repo_found(context: CheckContext) -> CheckOutcome:
    """The record repo folder that local settings name exists and is a folder."""
    local_settings = valid_local_settings(context)
    problem = describe_folder_problem(local_settings.record_repo.path, needs_writing=False)
    if problem is not None:
        return failed([record_repo_missing_failure(local_settings, problem)])
    return passed()


def check_log_directory_reachable(context: CheckContext) -> CheckOutcome:
    """The log directory can be written to.

    A warning, not a failure: an unreachable log directory means "not
    recording" (ADR 0008), which is an everyday state such as an unplugged
    drive, and every other check can still run. Only whether it could be
    written to is asked; nothing is written to find out.
    """
    local_settings = valid_local_settings(context)
    problem = describe_folder_problem(local_settings.log_directory.path, needs_writing=True)
    if problem is not None:
        return warned([log_directory_unreachable_failure(local_settings, problem)])
    return passed()


def spotlight_command(folder: Path) -> Command:
    """The command that lists what Spotlight has indexed inside `folder`."""
    return Command([MDFIND, "-onlyin", str(folder), SPOTLIGHT_QUERY], timeout_seconds=SPOTLIGHT_TIMEOUT_SECONDS)


def check_log_directory_not_in_spotlight(context: CheckContext) -> CheckOutcome:
    """Spotlight finds nothing inside the log directory, so goodall's copies stay out of a person's searches.

    A warning, not a failure: Spotlight listing the copies breaks nothing
    in goodall. It only puts raw records, which may hold anything the
    person did, among the results of their own searches.

    What each answer means, and the traps:

    - `mdfind` prints nothing: passes. That is also what it prints when
      Spotlight indexing is turned off, or the folder was created so
      recently that Spotlight has not indexed it yet. In both cases the
      copies are not listed now, which is all this check promises; run
      doctor again a few minutes after the first work block to be sure.
    - `mdfind` is missing, fails or times out: unverified. Not knowing
      must never read as "fine", and the person can look in Spotlight
      themselves (see `spotlight_unanswered_failure`).
    - The log directory does not exist: passes with a note, because the
      reachability check has already said so, and there is nothing to list.
    """
    local_settings = valid_local_settings(context)
    log_directory = local_settings.log_directory.path
    if not log_directory.is_dir():
        return passed(note="The log directory does not exist yet, so Spotlight has nothing to list from it.")
    result = context.runner.run(spotlight_command(log_directory))
    if not result.succeeded:
        problem = result.problem or "`mdfind` did not succeed"
        return could_not_verify([spotlight_unanswered_failure(local_settings, problem)])
    if result.standard_output.strip() == "":
        return passed()
    return warned([log_directory_in_spotlight_warning(local_settings)])


def log_directory_in_spotlight_warning(local_settings: LocalSettings) -> Failure:
    """The warning for a log directory whose files Spotlight lists.

    The next step depends on the folder's name. When it does not end in
    `.noindex`, the fix is to rename it. When it already does and Spotlight
    still lists files, the name did not work on this macOS, so the next step
    is Spotlight's own privacy list, which every macOS has.
    """
    log_directory = local_settings.log_directory
    what_happened = (
        f"Spotlight can find files inside the log directory {log_directory.as_written}, so goodall's copies of "
        "your agents' sessions and window titles can appear among the results when you search your Mac."
    )
    why = (
        "The log directory holds raw records, which may include anything you typed or viewed during a work "
        "block. Spotlight should not list them with your own files."
    )
    if log_directory.path.name.endswith(NO_INDEX_SUFFIX):
        what_to_do_next = (
            f"The folder's name already ends in {NO_INDEX_SUFFIX}, which Spotlight did not honor here. Open System "
            "Settings, then Spotlight, then Search Privacy, and add the log directory to the list. Then run goodall "
            "doctor again."
        )
    else:
        new_path = log_directory.path.with_name(log_directory.path.name + NO_INDEX_SUFFIX)
        what_to_do_next = (
            f"Rename the folder so its name ends in {NO_INDEX_SUFFIX}, which makes Spotlight skip it: for example "
            f"{new_path}. Then set log_directory in {local_settings.settings_file} to the new path. Do this while no "
            "work block is active, then run goodall doctor again."
        )
    return Failure(
        kind="log_directory_in_spotlight", what_happened=what_happened, why=why, what_to_do_next=what_to_do_next
    )


def spotlight_unanswered_failure(local_settings: LocalSettings, problem: str) -> Failure:
    """The unverified result for when Spotlight could not be asked about the log directory."""
    log_directory = local_settings.log_directory
    return Failure(
        kind="spotlight_unanswered",
        what_happened=(
            f"Goodall could not ask Spotlight whether it lists files inside the log directory "
            f"{log_directory.as_written}: {problem}."
        ),
        why="Goodall asks Spotlight so that its copies of your agents' sessions do not appear in your searches.",
        what_to_do_next=(
            "Open Spotlight, and search for a word you know is in a file in the log directory. If that file appears, "
            f"rename the folder so its name ends in {NO_INDEX_SUFFIX} and update log_directory in "
            f"{local_settings.settings_file}."
        ),
    )


def check_eval_config_valid(context: CheckContext) -> CheckOutcome:
    """The eval config is at the top of the record repo and passes every check of its own."""
    local_settings = valid_local_settings(context)
    reading = read_eval_config(local_settings.record_repo.path, local_settings.settings_file)
    if isinstance(reading, list):
        return failed(reading)
    context.eval_config = reading
    return passed()


def check_contributor_in_roster(context: CheckContext) -> CheckOutcome:
    """The contributor named in local settings is in the eval's roster."""
    local_settings = valid_local_settings(context)
    eval_config = valid_eval_config(context)
    if find_this_contributor(local_settings, eval_config) is None:
        return failed([contributor_not_in_roster_failure(local_settings, eval_config)])
    return passed()


def check_main_workstream_known(context: CheckContext) -> CheckOutcome:
    """The main workstream named in local settings is one of the eval's workstreams."""
    local_settings = valid_local_settings(context)
    eval_config = valid_eval_config(context)
    if not main_workstream_is_known(local_settings, eval_config):
        return failed([main_workstream_unknown_failure(local_settings, eval_config)])
    return passed()


def check_activitywatch_settings_recorded(context: CheckContext) -> CheckOutcome:
    """When the eval includes ActivityWatch, local settings hold its port and identifier.

    Passes when the eval does not include it, whatever local settings hold.
    Reads the two files only; whether ActivityWatch answers on that port
    with that identifier is a later check of its own.
    """
    local_settings = valid_local_settings(context)
    eval_config = valid_eval_config(context)
    missing_settings = missing_activitywatch_settings(local_settings, eval_config)
    if missing_settings:
        return failed([activitywatch_settings_missing_failure(local_settings, eval_config, missing_settings)])
    return passed()


def valid_local_settings(context: CheckContext) -> LocalSettings:
    """The valid local settings an earlier check put in the context.

    Trap: they are missing only when a check lists the wrong
    prerequisites, which is a mistake in goodall, so this raises rather
    than reporting it to the contributor.
    """
    if context.local_settings is None:
        raise RuntimeError(f"A check that needs local settings must list {LOCAL_SETTINGS_VALID} as a prerequisite.")
    return context.local_settings


def valid_eval_config(context: CheckContext) -> EvalConfig:
    """The valid eval config an earlier check put in the context; see `valid_local_settings`."""
    if context.eval_config is None:
        raise RuntimeError(f"A check that needs the eval config must list {EVAL_CONFIG_VALID} as a prerequisite.")
    return context.eval_config


SETTINGS_CHECKS = [
    Check(
        id=LOCAL_SETTINGS_FOUND,
        group=GROUP,
        summary="Local settings are found",
        prerequisites=[],
        run=check_local_settings_found,
    ),
    Check(
        id=LOCAL_SETTINGS_VALID,
        group=GROUP,
        summary="Local settings are valid",
        prerequisites=[LOCAL_SETTINGS_FOUND],
        run=check_local_settings_valid,
    ),
    Check(
        id=RECORD_REPO_FOUND,
        group=GROUP,
        summary="The record repo folder exists",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_record_repo_found,
    ),
    Check(
        id=LOG_DIRECTORY_REACHABLE,
        group=GROUP,
        summary="The log directory can be written to",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_log_directory_reachable,
    ),
    Check(
        id=LOG_DIRECTORY_NOT_IN_SPOTLIGHT,
        group=GROUP,
        summary="Spotlight does not list files in the log directory",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_log_directory_not_in_spotlight,
    ),
    Check(
        id=EVAL_CONFIG_VALID,
        group=GROUP,
        summary="The eval config is found and valid",
        prerequisites=[RECORD_REPO_FOUND],
        run=check_eval_config_valid,
    ),
    Check(
        id=CONTRIBUTOR_IN_ROSTER,
        group=GROUP,
        summary="This eval account's contributor is in the roster",
        prerequisites=[EVAL_CONFIG_VALID],
        run=check_contributor_in_roster,
    ),
    Check(
        id=MAIN_WORKSTREAM_KNOWN,
        group=GROUP,
        summary="The main workstream is one of the eval's workstreams",
        prerequisites=[EVAL_CONFIG_VALID],
        run=check_main_workstream_known,
    ),
    Check(
        id=ACTIVITYWATCH_SETTINGS_RECORDED,
        group=GROUP,
        summary="ActivityWatch's port and identifier are recorded, if the eval includes it",
        prerequisites=[EVAL_CONFIG_VALID],
        run=check_activitywatch_settings_recorded,
    ),
]
