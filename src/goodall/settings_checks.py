"""The diagnostic checks for this macOS user account's settings, in the order they run.

`goodall doctor` runs `SETTINGS_CHECKS` first (goodall.doctor), because
every later check needs to know which eval this account belongs to. They
follow the same chain as `goodall status`: local settings, then the folders
they name, then the eval config, then the two cross-checks between local
settings and the eval config.

Each check reuses the reader or folder check that `goodall status` uses, and
reports that reader's own failures, so a problem is worded the same way by
every command. Nothing here writes anything.

The ids all start with "settings." and every check sits under the heading
"Settings". Later groups ("Tools", "Eval account", "Secrets") get their own
module and their own prefix.
"""

from goodall.checks import Check, CheckContext, CheckOutcome, failed, passed, warned
from goodall.eval_config import (
    EvalConfig,
    contributor_not_in_roster_failure,
    find_this_contributor,
    main_workstream_is_known,
    main_workstream_unknown_failure,
    read_eval_config,
)
from goodall.folders import describe_folder_problem, log_directory_unreachable_failure, record_repo_missing_failure
from goodall.local_settings import LocalSettings, read_local_settings

GROUP = "Settings"

LOCAL_SETTINGS_FOUND = "settings.local_settings_found"
LOCAL_SETTINGS_VALID = "settings.local_settings_valid"
RECORD_REPO_FOUND = "settings.record_repo_found"
LOG_DIRECTORY_REACHABLE = "settings.log_directory_reachable"
EVAL_CONFIG_VALID = "settings.eval_config_valid"
CONTRIBUTOR_IN_ROSTER = "settings.contributor_in_roster"
MAIN_WORKSTREAM_KNOWN = "settings.main_workstream_known"

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
]
