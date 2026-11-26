"""The diagnostic checks of the eval account's ActivityWatch, under the heading "ActivityWatch".

`goodall doctor` runs `ACTIVITYWATCH_CHECKS` after the settings checks
(goodall.doctor). Four checks, each waiting for the one before it:

1. "ActivityWatch answers and is this eval account's copy": the server on
   the recorded port answers `/api/0/info`, and the `device_id` it reports
   is the one recorded in local settings.
2. "ActivityWatch has a window bucket and an AFK bucket": `/api/0/buckets`
   lists a bucket of type `currentwindow` and one of type `afkstatus`.
3. "ActivityWatch's AFK bucket was updated in the last 10 minutes": the newest
   event of any AFK bucket (a renamed Mac has an old one too) is recent.
4. "No browser watcher is installed": a warning when a `web.tab.current`
   bucket exists, because ADR 0016 says the browser extension is not
   installed in an eval account.

When the eval does not include ActivityWatch (ADR 0016) all four pass,
the first with a note saying so, and nothing is asked of anything.

How it asks (ADR 0011, amended 2026-11-18): with the `curl` command that
goodall.activitywatch_server builds, which says why each of its flags is
there. Only GET requests are made, so doctor changes nothing in
ActivityWatch (AGENTS.md invariant 5). The answer is read from the runner's
captured output; no file is written.

Why the identifier is checked first: local ports are shared by every macOS
user account on the Mac, and ActivityWatch's launcher silently adopts any
ActivityWatch already answering on its port (ADR 0016). So an answer on
our port might be another account's ActivityWatch. Only the `device_id`,
which differs for every account, says whose it is, and nothing else is
trusted until it matches.

What is never shown: ActivityWatch's answers hold window titles, page
addresses, the Mac's hostname (it is inside every bucket id) and the
identifier. Messages say only the bucket types, a count, and whether
something matched; they do not even say the port, which is a setting
value. Anything unfamiliar is described by what was expected, never by what
came back. The newest AFK event is read for its time only, and the window
bucket's events are never requested at all.

Judgement calls recorded for the owner: a stale AFK bucket is a warning,
not a failure, because a Mac that has just woken from sleep looks the same
and a failing doctor would then block a contributor for nothing; and "recent"
is 10 minutes (`AFK_FRESH_WITHIN`), counted from when the newest event ended
(its start plus its duration, because ActivityWatch lengthens the newest
event with each heartbeat).
"""

import json
from datetime import UTC, datetime, timedelta

from goodall.activitywatch_server import (
    INFO_PATH,
    answer_unfamiliar_failure,
    curl_unavailable_failure,
    device_id_changed_failure,
    events_path,
    local_get,
    not_answering_failure,
)
from goodall.checks import Check, CheckContext, CheckOutcome, failed, passed, warned
from goodall.runner import CommandOutcome
from goodall.settings_checks import ACTIVITYWATCH_SETTINGS_RECORDED, valid_eval_config, valid_local_settings
from goodall.terminal import Failure

GROUP = "ActivityWatch"

SERVER_IS_THIS_ACCOUNTS = "activitywatch.server_is_this_accounts"
WINDOW_AND_AFK_BUCKETS_EXIST = "activitywatch.window_and_afk_buckets_exist"
AFK_BUCKET_IS_RECENT = "activitywatch.afk_bucket_is_recent"
NO_BROWSER_WATCHER = "activitywatch.no_browser_watcher"

# The bucket types that ActivityWatch's watchers write (docs/activitywatch-rules.md).
WINDOW_BUCKET_TYPE = "currentwindow"
AFK_BUCKET_TYPE = "afkstatus"
BROWSER_BUCKET_TYPE = "web.tab.current"

# How long the AFK watcher may have been silent before doctor warns. Its
# watcher reports every few seconds, even while the person is away, so ten
# minutes is long enough for a Mac that just woke and short enough to catch
# a watcher that has died.
AFK_FRESH_WITHIN = timedelta(minutes=10)

NOT_INCLUDED_NOTE = "This eval does not include ActivityWatch, so there is nothing to check."


def ask_activitywatch(context: CheckContext, port: int, path: str) -> object | Failure:
    """Read `path` from ActivityWatch and return the JSON it answered, or a Failure saying why not.

    The failure never repeats anything the answer held.
    """
    result = context.runner.run(local_get(port, path))
    if result.outcome == CommandOutcome.NOT_FOUND or result.outcome == CommandOutcome.COULD_NOT_START:
        return curl_unavailable_failure()
    if not result.succeeded:
        return not_answering_failure(
            timed_out=result.outcome == CommandOutcome.TIMED_OUT,
            settings_file=valid_local_settings(context).settings_file,
        )
    try:
        answer: object = json.loads(result.standard_output)
        return answer
    except json.JSONDecodeError:
        return answer_unfamiliar_failure()


def check_server_is_this_accounts(context: CheckContext) -> CheckOutcome:
    """The server on the recorded port answers, and reports the identifier recorded in local settings."""
    eval_config = valid_eval_config(context)
    if not eval_config.activitywatch:
        return passed(note=NOT_INCLUDED_NOTE)
    local_settings = valid_local_settings(context)
    port = local_settings.activitywatch_port
    recorded_device_id = local_settings.activitywatch_device_id
    if port is None or recorded_device_id is None:
        raise RuntimeError(f"{SERVER_IS_THIS_ACCOUNTS} must list {ACTIVITYWATCH_SETTINGS_RECORDED} as a prerequisite.")

    answer = ask_activitywatch(context, port, INFO_PATH)
    if isinstance(answer, Failure):
        return failed([answer])
    if not isinstance(answer, dict) or not isinstance(answer.get("device_id"), str):
        return failed([answer_unfamiliar_failure()])
    # Local settings record the identifier in lowercase (goodall.local_settings).
    if answer["device_id"].lower() != recorded_device_id:
        return failed([device_id_changed_failure(local_settings.settings_file)])
    return passed()


def check_window_and_afk_buckets_exist(context: CheckContext) -> CheckOutcome:
    """ActivityWatch lists a window bucket and an AFK bucket; remembers every bucket's id and type."""
    if not valid_eval_config(context).activitywatch:
        return passed()
    port = recorded_port(context)

    answer = ask_activitywatch(context, port, "/api/0/buckets")
    if isinstance(answer, Failure):
        return failed([answer])
    bucket_types = read_bucket_types(answer)
    if bucket_types is None:
        return failed([answer_unfamiliar_failure()])
    context.activitywatch_buckets = bucket_types

    missing: list[str] = []
    if WINDOW_BUCKET_TYPE not in bucket_types.values():
        missing.append("a window bucket (type currentwindow, from aw-watcher-window)")
    if AFK_BUCKET_TYPE not in bucket_types.values():
        missing.append("an AFK bucket (type afkstatus, from aw-watcher-afk)")
    if missing:
        return failed([buckets_missing_failure(missing)])
    return passed()


def check_afk_bucket_is_recent(context: CheckContext) -> CheckOutcome:
    """The newest event of any AFK bucket ended within `AFK_FRESH_WITHIN` of now.

    Every AFK bucket is asked, not only the first: a bucket id carries the
    Mac's hostname, so a renamed Mac has an old bucket beside the live one,
    and the old one must not cause a warning. It passes when any bucket's
    newest event is recent. Otherwise it warns: with the first problem met
    (an answer that could not be read, say) or, when every answer was
    readable, that the data is stale or empty. Only the newest event is
    asked for, and only its time is read from it.
    """
    if not valid_eval_config(context).activitywatch:
        return passed()
    port = recorded_port(context)
    if context.now is None:
        raise RuntimeError("Doctor must give the checks the time now, so they can tell whether data is recent.")
    if context.activitywatch_buckets is None:
        raise RuntimeError(f"{AFK_BUCKET_IS_RECENT} must list {WINDOW_AND_AFK_BUCKETS_EXIST} as a prerequisite.")
    afk_bucket_ids = bucket_ids_of_type(context.activitywatch_buckets, AFK_BUCKET_TYPE)
    if len(afk_bucket_ids) == 0:
        raise RuntimeError(f"{AFK_BUCKET_IS_RECENT} ran although {WINDOW_AND_AFK_BUCKETS_EXIST} found no AFK bucket.")

    first_problem: Failure | None = None
    found_an_event = False
    for afk_bucket_id in afk_bucket_ids:
        answer = ask_activitywatch(context, port, events_path(afk_bucket_id, limit=1))
        if isinstance(answer, Failure):
            problem = answer
        elif not isinstance(answer, list):
            problem = answer_unfamiliar_failure()
        elif len(answer) == 0:
            continue
        else:
            ended = read_event_end(answer[0])
            if ended is None:
                problem = answer_unfamiliar_failure()
            elif context.now - ended <= AFK_FRESH_WITHIN:
                return passed()
            else:
                found_an_event = True
                continue
        if first_problem is None:
            first_problem = problem
    if first_problem is not None:
        return warned([first_problem])
    return warned([afk_bucket_stale_failure(empty=not found_an_event)])


def check_no_browser_watcher(context: CheckContext) -> CheckOutcome:
    """No bucket of type `web.tab.current` exists: the browser extension is not installed (ADR 0016)."""
    if not valid_eval_config(context).activitywatch:
        return passed()
    if context.activitywatch_buckets is None:
        raise RuntimeError(f"{NO_BROWSER_WATCHER} must list {WINDOW_AND_AFK_BUCKETS_EXIST} as a prerequisite.")
    browser_bucket_count = 0
    for bucket_type in context.activitywatch_buckets.values():
        if bucket_type == BROWSER_BUCKET_TYPE:
            browser_bucket_count += 1
    if browser_bucket_count > 0:
        return warned([browser_watcher_found_failure(browser_bucket_count)])
    return passed()


def recorded_port(context: CheckContext) -> int:
    """The port local settings record for ActivityWatch; a check that needs it waits for the first check."""
    port = valid_local_settings(context).activitywatch_port
    if port is None:
        raise RuntimeError(f"A check that asks ActivityWatch must list {SERVER_IS_THIS_ACCOUNTS} as a prerequisite.")
    return port


def read_bucket_types(answer: object) -> dict[str, str] | None:
    """Map each bucket id in ActivityWatch's list of buckets to its type, or None when the list is not that shape.

    Both of ActivityWatch's servers answer with an object keyed by bucket
    id, each value holding a `type` text.
    """
    if not isinstance(answer, dict):
        return None
    bucket_types: dict[str, str] = {}
    for bucket_id, bucket in answer.items():
        if not isinstance(bucket, dict) or not isinstance(bucket.get("type"), str):
            return None
        bucket_types[bucket_id] = bucket["type"]
    return bucket_types


def bucket_ids_of_type(bucket_types: dict[str, str], wanted_type: str) -> list[str]:
    """The ids of every bucket of `wanted_type`, in the order ActivityWatch listed them."""
    bucket_ids: list[str] = []
    for bucket_id, bucket_type in bucket_types.items():
        if bucket_type == wanted_type:
            bucket_ids.append(bucket_id)
    return bucket_ids


def read_event_end(event: object) -> datetime | None:
    """When an event ended, which is its start plus its duration; None when it has no readable time.

    A start with no time zone is taken as UTC, as in goodall.activitywatch.
    """
    if not isinstance(event, dict):
        return None
    timestamp = event.get("timestamp")
    duration = event.get("duration", 0)
    if not isinstance(timestamp, str) or isinstance(duration, bool) or not isinstance(duration, int | float):
        return None
    try:
        started = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started + timedelta(seconds=duration)


def buckets_missing_failure(missing: list[str]) -> Failure:
    """The failure for an ActivityWatch with no window bucket or no AFK bucket."""
    return Failure(
        kind="activitywatch_buckets_missing",
        what_happened=f"ActivityWatch has no {' and no '.join(missing)}.",
        why=(
            "The window watcher records which application and window had focus, and the AFK watcher records when "
            "you were at the keyboard. Without them there is nothing to say what you did outside the agents."
        ),
        what_to_do_next=(
            "Check that ActivityWatch's window and AFK watchers are running (its menu-bar icon lists them), and "
            "that macOS has given ActivityWatch the permissions the window watcher needs. A watcher creates its "
            "bucket a few seconds after it starts, so run goodall doctor again."
        ),
    )


def afk_bucket_stale_failure(empty: bool) -> Failure:
    """The warning for an AFK bucket that has stopped updating, has no events, or has an unreadable newest event."""
    if empty:
        what_happened = "ActivityWatch's AFK bucket has no events."
    else:
        what_happened = "ActivityWatch's AFK bucket has not been updated in the last 10 minutes."
    return Failure(
        kind="activitywatch_afk_bucket_stale",
        what_happened=what_happened,
        why=(
            "The AFK watcher reports every few seconds, even while you are away, so it has probably stopped, and "
            "the time since is missing from the record. A Mac that has only just woken from sleep looks the same."
        ),
        what_to_do_next=(
            "Check that ActivityWatch's AFK watcher is running (its menu-bar icon lists it), start it if not, then "
            "run goodall doctor again in a minute."
        ),
    )


def browser_watcher_found_failure(bucket_count: int) -> Failure:
    """The warning for an ActivityWatch that has a browser-watcher bucket."""
    if bucket_count == 1:
        what_happened = "ActivityWatch has a browser-watcher bucket (type web.tab.current)."
    else:
        what_happened = f"ActivityWatch has {bucket_count} browser-watcher buckets (type web.tab.current)."
    return Failure(
        kind="activitywatch_browser_watcher_found",
        what_happened=what_happened,
        why=(
            "ADR 0016 says the browser extension is not installed in an eval account: it always sends to "
            "ActivityWatch's default port, so it cannot reach this account's copy and would send to any other "
            "account's. The window watcher already records the page in front for the main browsers."
        ),
        what_to_do_next=(
            "ADR 0016 says not to install the browser extension in an eval account, because it cannot be given this "
            "account's port. Remove the ActivityWatch extension from every browser in this account. Its old bucket "
            "stays in ActivityWatch until deleted, so this warning remains until then; tell whoever runs the eval "
            "if that matters to them."
        ),
    )


ACTIVITYWATCH_CHECKS = [
    Check(
        id=SERVER_IS_THIS_ACCOUNTS,
        group=GROUP,
        summary="ActivityWatch answers and is this eval account's copy",
        prerequisites=[ACTIVITYWATCH_SETTINGS_RECORDED],
        run=check_server_is_this_accounts,
    ),
    Check(
        id=WINDOW_AND_AFK_BUCKETS_EXIST,
        group=GROUP,
        summary="ActivityWatch has a window bucket and an AFK bucket",
        prerequisites=[SERVER_IS_THIS_ACCOUNTS],
        run=check_window_and_afk_buckets_exist,
    ),
    Check(
        id=AFK_BUCKET_IS_RECENT,
        group=GROUP,
        summary="ActivityWatch's AFK bucket was updated in the last 10 minutes",
        prerequisites=[WINDOW_AND_AFK_BUCKETS_EXIST],
        run=check_afk_bucket_is_recent,
    ),
    Check(
        id=NO_BROWSER_WATCHER,
        group=GROUP,
        summary="No ActivityWatch browser watcher is installed",
        prerequisites=[WINDOW_AND_AFK_BUCKETS_EXIST],
        run=check_no_browser_watcher,
    ),
]
