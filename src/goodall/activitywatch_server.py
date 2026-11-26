"""How goodall asks the eval account's ActivityWatch server a question, and what it says when it cannot.

Two callers ask: `goodall doctor`'s checks (goodall.activitywatch_checks),
which read the answer, and the copy taken when a work block ends
(goodall.activitywatch_copies), which has `curl` write the answer straight
to a file. Both build their request here, so there is one place that
decides how goodall talks to a local server.

How it asks (ADR 0011, amended 2026-11-18): `curl` through the runner, to
`http://127.0.0.1:<port>/api/0/...`, with

- `--disable`, first, so curl does not read `~/.curlrc`, where a line could
  add credentials or an output file;
- `--noproxy '*'`, so a proxy setting cannot send a request for this Mac's
  own server elsewhere;
- `--fail`, so an error page is not mistaken for an answer;
- `--max-time`, so a server that has hung cannot hang goodall;
- no credentials, no request body and no other method: every request is a
  GET, which changes nothing in ActivityWatch.

The address is always `127.0.0.1`, never `localhost`: the server listens on
IPv4 only, and `localhost` is tried as IPv6 first.

The paths are the ones a running ActivityWatch 0.14.3 answered in the hand
test of 2026-11-26 (Beads issue goodall-fs9.19), which differ from what
reading its source suggested:

- The list of buckets needs its trailing slash. Without it the server
  answers with a redirect, which goodall does not follow, and no list.
- A bucket's events must NOT have one: with it the server answers 404.
- A time in `start` or `end` is written in UTC ending in `Z`. A raw `+`,
  as in `+00:00`, is read as a space and refused.

Trap for every caller: local ports are shared by every macOS user account
on the Mac, and ActivityWatch's launcher silently adopts any ActivityWatch
already answering on its port (ADR 0016). So whatever answers on our port
might be another account's ActivityWatch, and bucket ids are the same in
every account. Ask `INFO_PATH` first and compare the `device_id` in the
answer with the one in local settings before trusting anything else.

What is never shown: ActivityWatch's answers hold window titles, page
addresses, the Mac's hostname (it is inside every bucket id) and the
identifier. The failures here say what was expected, never what came back,
and do not say the port, which is a setting value.
"""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from goodall.local_settings import ACTIVITYWATCH_INFO_PATH
from goodall.runner import Command, OutputMode
from goodall.terminal import Failure

# What the server says about itself, including its `device_id`.
INFO_PATH = ACTIVITYWATCH_INFO_PATH

# The list of buckets. The trailing slash is needed (see the module docstring).
BUCKETS_PATH = "/api/0/buckets/"

# How long `curl` may take over a short answer, and how much longer than
# `curl`'s own limit the runner waits for `curl` itself.
CURL_MAX_SECONDS = 3
RUNNER_EXTRA_SECONDS = 2.0


def format_request_time(moment: datetime) -> str:
    """A moment as `start` and `end` take it: UTC, whole seconds, ending in `Z` (`2026-11-16T10:20:00Z`)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def events_path(
    bucket_id: str, start: datetime | None = None, end: datetime | None = None, limit: int | None = None
) -> str:
    """The path that answers one bucket's events, newest first.

    With neither `start` nor `end` the answer is every event the bucket
    holds. With them it is the events in that range; one that crosses
    `start` or `end` is included and cut to fit, keeping its id. `limit`
    keeps only that many of the newest.

    The bucket id holds the Mac's hostname and may hold any character, so
    it is quoted, slashes included.
    """
    path = f"/api/0/buckets/{quote(bucket_id, safe='')}/events"
    parameters = []
    if start is not None:
        parameters.append(f"start={format_request_time(start)}")
    if end is not None:
        parameters.append(f"end={format_request_time(end)}")
    if limit is not None:
        parameters.append(f"limit={limit}")
    if parameters:
        path += "?" + "&".join(parameters)
    return path


def local_get(port: int, path: str, output_file: Path | None = None, max_seconds: int = CURL_MAX_SECONDS) -> Command:
    """The `curl` that reads `path` from the ActivityWatch server on this Mac's `port`, with no credentials.

    With `output_file`, curl writes the answer straight into that file and
    goodall is handed none of it (ADR 0011): the caller creates the file
    first, readable only by this account, and curl keeps those permissions.
    Without it the answer comes back as the command's captured output.
    """
    arguments = [
        "curl",
        # Must be first: curl then skips its settings file, ~/.curlrc,
        # which could add credentials or an output file.
        "--disable",
        "--silent",
        "--fail",
        "--noproxy",
        "*",
        "--max-time",
        str(max_seconds),
    ]
    output = OutputMode.CAPTURE
    if output_file is not None:
        arguments.extend(["--output", str(output_file)])
        output = OutputMode.DISCARD
    arguments.append(f"http://127.0.0.1:{port}{path}")
    return Command(arguments=arguments, output=output, timeout_seconds=max_seconds + RUNNER_EXTRA_SECONDS)


def curl_unavailable_failure() -> Failure:
    """The failure for a Mac where `curl` cannot be run."""
    return Failure(
        kind="activitywatch_curl_unavailable",
        what_happened="Goodall could not run curl, the program it uses to ask ActivityWatch questions.",
        why=(
            "curl ships with macOS, so it has probably been removed or blocked on this Mac. Without it goodall "
            "cannot check ActivityWatch."
        ),
        what_to_do_next=(
            "Check that curl runs in a terminal (curl --version). Ask whoever administers this Mac to restore it."
        ),
        commands_to_try=("curl --version",),
    )


def not_answering_failure(timed_out: bool, settings_file: Path, max_seconds: int = CURL_MAX_SECONDS) -> Failure:
    """The failure for an ActivityWatch that does not answer on the port in local settings."""
    if timed_out:
        what_happened = (
            f"ActivityWatch did not answer within {max_seconds} seconds on the port recorded in local settings."
        )
    else:
        what_happened = "Nothing answered as ActivityWatch on the port recorded in local settings."
    return Failure(
        kind="activitywatch_not_answering",
        what_happened=what_happened,
        why=(
            "This eval includes ActivityWatch (ADR 0016), which goodall asks for what you did outside the agents. "
            "If it is not running, nothing is recording that part of your work."
        ),
        what_to_do_next=(
            "Start ActivityWatch in this eval account and run goodall doctor again. If it is running, check that "
            f"the port in its own settings is the one in activitywatch_port in {settings_file}."
        ),
    )


def answer_unfamiliar_failure() -> Failure:
    """The failure for an answer that is not shaped as ActivityWatch's: another program may be on the port."""
    return Failure(
        kind="activitywatch_answer_unfamiliar",
        what_happened="What answered on ActivityWatch's port was not shaped as ActivityWatch's answers are.",
        why=(
            "Another program may be using the port, or this is a version of ActivityWatch whose answers goodall "
            "does not know. Goodall will not trust an answer it cannot read."
        ),
        what_to_do_next=(
            "Check that the port in activitywatch_port in your local settings is the one ActivityWatch's own "
            "settings use, and that no other program uses it. If ActivityWatch was just updated, tell whoever runs "
            "the eval."
        ),
    )


def device_id_changed_failure(settings_file: Path) -> Failure:
    """The failure for a server whose identifier is not the one recorded: a new copy, or another account's."""
    return Failure(
        kind="activitywatch_device_id_changed",
        what_happened=(
            "The ActivityWatch on the recorded port reports a different device_id from the one in local settings."
        ),
        why=(
            "The identifier tells this account's ActivityWatch from every other account's on this Mac (ADR 0016). "
            "It changes when ActivityWatch is switched between its Python and Rust servers, or when its data folder "
            "is deleted. It also differs when the port is answered by another macOS account's ActivityWatch, whose "
            "data must never be recorded as yours."
        ),
        what_to_do_next=(
            "Switching ActivityWatch between its Python and Rust servers, or deleting its data folder, changes the "
            "identifier. If you did either, record the new one: run goodall onboard, or "
            f"set activitywatch_device_id in {settings_file} to the device_id that ActivityWatch shows at "
            f"{INFO_PATH} on your port. If you did neither, another account may be using your port: do "
            "not record its identifier; give your own ActivityWatch a different port, in both of its settings files, "
            "and record that port."
        ),
    )
