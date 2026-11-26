"""Tests for copying ActivityWatch's data into the log directory when a work block ends (ADR 0016).

The first half runs goodall commands the way a person would, in this
process, with a captured terminal, a clock the test sets, and the fake
runner. The fake runner hands every `curl` to `SimulatedActivityWatch`
below, which answers as the real server did in the hand test of 2026-11-26
(Beads issue goodall-fs9.19): the bucket list needs its trailing slash, a
bucket's events must not have one, a raw `+` in a time is refused, and a
range clips the event that crosses its start. The second half pins the
pieces of goodall.activitywatch_copies one at a time.

Nothing here contacts a real ActivityWatch, and every title, page address,
hostname and identifier is made up.
"""

import io
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from goodall import activitywatch_copies, cli
from goodall.fake_runner import FakeRunner, PretendProgram, ScriptedAnswer
from goodall.runner import Command, CommandOutcome, OutputMode
from goodall.terminal import Terminal

PORT = 5610
DEVICE_ID = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"
OTHER_ACCOUNTS_DEVICE_ID = "11111111-2222-4333-8444-555555555555"

# Text that must never reach goodall's terminal output or the record repo.
SYNTHETIC_HOSTNAME = "synthetic-hostname-7"
SYNTHETIC_WINDOW_TITLE = "SYNTHETIC-TITLE-domain-shortlist.txt"
SYNTHETIC_PAGE_ADDRESS = "https://synthetic.example/private/SYNTHETIC-PAGE"
PRIVATE_TEXTS = [
    SYNTHETIC_HOSTNAME,
    SYNTHETIC_WINDOW_TITLE,
    SYNTHETIC_PAGE_ADDRESS,
    "SYNTHETIC",
    DEVICE_ID,
    OTHER_ACCOUNTS_DEVICE_ID,
]

# Bucket ids are a watcher's name and the Mac's hostname, as ActivityWatch names them.
WINDOW_BUCKET_ID = f"aw-watcher-window_{SYNTHETIC_HOSTNAME}"
AFK_BUCKET_ID = f"aw-watcher-afk_{SYNTHETIC_HOSTNAME}"

BLOCK_START = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
BLOCK_END = BLOCK_START + timedelta(hours=1)

# The flags that would add credentials, change the request from a GET, or follow a redirect elsewhere.
FORBIDDEN_CURL_FLAGS = [
    "-u",
    "--user",
    "-H",
    "--header",
    "-X",
    "--request",
    "-d",
    "--data",
    "-b",
    "--cookie",
    "-n",
    "--netrc",
    "-L",
    "--location",
    "-K",
    "--config",
]


# ---------------------------------------------------------------------------
# A simulated ActivityWatch server, reached only through the fake runner's `curl`


def event(event_id: int, started: datetime, seconds: float, data: dict[str, object]) -> dict[str, object]:
    """One event as ActivityWatch's server answers it: an id, a start ending in +00:00, a duration and its data."""
    return {
        "id": event_id,
        "timestamp": started.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
        "duration": seconds,
        "data": data,
    }


def window_event(event_id: int, started: datetime, seconds: float) -> dict[str, object]:
    """A window event with a title and a page address that goodall must copy and never show."""
    return event(
        event_id, started, seconds, {"app": "Safari", "title": SYNTHETIC_WINDOW_TITLE, "url": SYNTHETIC_PAGE_ADDRESS}
    )


def afk_event(event_id: int, started: datetime, seconds: float) -> dict[str, object]:
    return event(event_id, started, seconds, {"status": "not-afk"})


def as_served_json(answer: object) -> bytes:
    """An answer's bytes, spaced unlike Python's own JSON and with text that is not ASCII left as it is.

    A copy that parsed the answer and wrote it out again would not match
    these bytes, so a test comparing them proves the copy is byte for byte.
    """
    return (json.dumps(answer, indent=3, separators=(" ,", " : "), ensure_ascii=False) + "\n").encode("utf-8")


def read_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass
class SimulatedActivityWatch:
    """What the fake runner's `curl` reaches: one account's ActivityWatch server on one port of this Mac.

    `buckets` maps each bucket id to its type; `events` maps it to its
    events, in any order. `requests` keeps the path and query of every
    request, in order. Set `answering` to False for a server that is not
    running, and `failing_events_of` to a bucket id whose events it fails
    to send.
    """

    port: int = PORT
    device_id: str = DEVICE_ID
    buckets: dict[str, str] = field(default_factory=dict)
    events: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    answering: bool = True
    failing_events_of: str | None = None
    # A decoration the server's info answer carries, so its bytes are not what goodall would write itself.
    info_extra: str = "Café ✓"

    def add_bucket(self, bucket_id: str, bucket_type: str, events: list[dict[str, object]]) -> None:
        self.buckets[bucket_id] = bucket_type
        self.events[bucket_id] = list(events)

    def info_bytes(self) -> bytes:
        info = {
            "hostname": SYNTHETIC_HOSTNAME,
            "version": "v0.14.3",
            "testing": False,
            "device_id": self.device_id,
            "profile": self.info_extra,
        }
        return as_served_json(info)

    def buckets_bytes(self) -> bytes:
        listed: dict[str, object] = {}
        for bucket_id, bucket_type in self.buckets.items():
            listed[bucket_id] = {
                "id": bucket_id,
                "created": "2026-11-01T08:00:00.000000+00:00",
                "name": None,
                "type": bucket_type,
                "client": bucket_id.split("_")[0],
                "hostname": SYNTHETIC_HOSTNAME,
                "data": {},
                "last_updated": "2026-11-16T10:29:58.000000+00:00",
            }
        return as_served_json(listed)

    def events_in_range(self, bucket_id: str, start: datetime | None, end: datetime | None) -> list[dict[str, object]]:
        """The bucket's events, newest first; one that crosses the range is clipped and keeps its id."""
        answered = []
        for stored in self.events[bucket_id]:
            started = read_time(str(stored["timestamp"]))
            duration = stored["duration"]
            assert isinstance(duration, int | float)
            ended = started + timedelta(seconds=duration)
            if start is not None and ended < start:
                continue
            if end is not None and started > end:
                continue
            clipped = dict(stored)
            if start is not None and started < start:
                started = start
            if end is not None and ended > end:
                ended = end
            clipped["timestamp"] = started.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
            clipped["duration"] = (ended - started).total_seconds()
            answered.append(clipped)
        answered.sort(key=lambda answered_event: str(answered_event["timestamp"]), reverse=True)
        return answered

    def answer_for(self, path_and_query: str) -> bytes | int:
        """The body the server answers with, or the HTTP status of an answer that is not a 200."""
        split = urlsplit(path_and_query)
        if split.path == "/api/0/info":
            return self.info_bytes()
        if split.path == "/api/0/buckets":
            # The real server redirects to the form with a trailing slash.
            return 308
        if split.path == "/api/0/buckets/":
            return self.buckets_bytes()
        parts = split.path.split("/")
        if len(parts) == 6 and parts[:4] == ["", "api", "0", "buckets"] and parts[5] == "events":
            bucket_id = unquote(parts[4])
            if bucket_id not in self.buckets:
                return 404
            if bucket_id == self.failing_events_of:
                return 500
            times: dict[str, datetime | None] = {"start": None, "end": None}
            # Split by hand: the real server reads a raw `+` as a space and then cannot read the time.
            for name, value in parse_qsl(split.query, keep_blank_values=True):
                if name not in times:
                    return 400
                if " " in value:
                    return 400
                times[name] = read_time(value)
            return as_served_json(self.events_in_range(bucket_id, times["start"], times["end"]))
        # Among others: a bucket's events asked for with a trailing slash.
        return 404

    def curl(self, command: Command) -> ScriptedAnswer:
        """What `curl` does with `command`: check it is a request goodall may make, then answer as the server would."""
        arguments = command.arguments
        assert arguments[:2] == ["curl", "--disable"], "--disable must come first, so curl reads no settings file"
        assert "--fail" in arguments, "without --fail an error page would be taken for an answer"
        assert arguments[arguments.index("--noproxy") + 1] == "*"
        for forbidden_flag in FORBIDDEN_CURL_FLAGS:
            assert forbidden_flag not in arguments, f"goodall must not pass {forbidden_flag} to curl"
        url = arguments[-1]
        origin = f"http://127.0.0.1:{self.port}"
        if not url.startswith("http://127.0.0.1:"):
            raise AssertionError("goodall asked for an address that is not on this Mac")
        if not url.startswith(origin + "/") or not self.answering:
            # Nothing is listening: curl's "could not connect".
            return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=7)
        path_and_query = url.removeprefix(origin)
        self.requests.append(path_and_query)
        answer = self.answer_for(path_and_query)
        if isinstance(answer, int):
            if answer >= 400:
                # What --fail makes of an error status.
                return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=22)
            answer = b""
        if "--output" in arguments:
            assert command.output == OutputMode.DISCARD, "a copy written to a file must not also be captured"
            Path(arguments[arguments.index("--output") + 1]).write_bytes(answer)
            return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=0)
        return ScriptedAnswer(CommandOutcome.COMPLETED, exit_code=0, standard_output=answer.decode("utf-8"))


def working_server() -> SimulatedActivityWatch:
    """A server with a window bucket and an AFK bucket holding events from before and inside the work block."""
    server = SimulatedActivityWatch()
    server.add_bucket(
        WINDOW_BUCKET_ID,
        "currentwindow",
        [
            window_event(1, BLOCK_START - timedelta(days=2), 60.0),
            window_event(2, BLOCK_START + timedelta(minutes=10), 300.0),
        ],
    )
    server.add_bucket(AFK_BUCKET_ID, "afkstatus", [afk_event(1, BLOCK_START + timedelta(minutes=1), 3000.0)])
    return server


# ---------------------------------------------------------------------------
# An eval account, and running goodall in it


@dataclass(frozen=True)
class EvalAccount:
    """The folders these tests touch, all inside tmp_path."""

    config_directory: Path
    record_repo: Path
    log_directory: Path

    @property
    def settings_file(self) -> Path:
        return self.config_directory / "local-settings.toml"

    @property
    def copies_folder(self) -> Path:
        return self.log_directory / "activitywatch"


EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "A test eval."
workstreams = ["research"]
intervention_categories = ["infrastructure"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""

EVAL_CONFIG_WITH_ACTIVITYWATCH_TEXT = EVAL_CONFIG_TEXT.replace(
    'description = "A test eval."\n', 'description = "A test eval."\nactivitywatch = true\n'
)


def git(working_directory: Path, *arguments: str) -> str:
    """Run git for the test's own setup and checks, free of any GIT_ variable the test run inherited."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments], cwd=working_directory, env=environment, capture_output=True, check=True, text=True
    )
    return completed.stdout


def make_eval_account(
    tmp_path: Path, includes_activitywatch: bool = True, device_id: str | None = DEVICE_ID
) -> EvalAccount:
    """Local settings, a record repo holding the eval config, and a log directory."""
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    eval_config_text = EVAL_CONFIG_TEXT
    if includes_activitywatch:
        eval_config_text = EVAL_CONFIG_WITH_ACTIVITYWATCH_TEXT
    (record_repo / "eval.toml").write_text(eval_config_text)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    settings_text = (
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{record_repo}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    if device_id is not None:
        settings_text += f'activitywatch_port = {PORT}\nactivitywatch_device_id = "{device_id}"\n'
    (config_directory / "local-settings.toml").write_text(settings_text)
    return EvalAccount(config_directory, record_repo, log_directory)


@dataclass(frozen=True)
class GoodallRun:
    """What one run of a goodall command did, as the person who ran it would see it."""

    exit_code: int
    standard_output: str
    standard_error: str

    @property
    def everything_printed(self) -> str:
        """Both streams, with each run of spaces and line breaks made one space, for phrase checks."""
        return " ".join((self.standard_output + "\n" + self.standard_error).split())


def make_runner(server: SimulatedActivityWatch | None) -> FakeRunner:
    """A fake runner whose `curl` reaches `server`; with None, any `curl` at all fails the test."""
    runner = FakeRunner()
    runner.answer(["claude", "--version"], standard_output="2.1.274 (Claude Code)\n")
    # goodall start's telemetry receiver, which these tests do not look at.
    runner.answer_background(
        [sys.executable, "-I", "-m", "goodall.telemetry_receiver"], lambda: PretendProgram(ready_line='{"port": 4318}')
    )
    if server is not None:
        runner.answer_with("curl", server.curl)
    return runner


def run_goodall(
    eval_account: EvalAccount,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    now: datetime,
    server: SimulatedActivityWatch | None,
) -> GoodallRun:
    """Run a goodall command in this process at the moment `now`, with a captured terminal and the fake runner."""
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(eval_account.config_directory))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=make_runner(server), clock=lambda: now)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


def files_and_bytes(folder: Path) -> dict[str, bytes]:
    """Every file under `folder`, by its path relative to `folder`, with its exact contents."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(folder))] = path.read_bytes()
    return found


def only_copy(eval_account: EvalAccount) -> Path:
    """The one copy in `activitywatch/` in the log directory."""
    [copy_folder] = sorted(eval_account.copies_folder.iterdir())
    return copy_folder


def events_file_name(bucket_id: str) -> str:
    return f"events/{bucket_id}.json"


def everything_in_the_record_repo(record_repo: Path) -> str:
    """Every file's text in the record repo and every commit ever made there, message and contents."""
    texts = [git(record_repo, "log", "--all", "--patch", "--format=fuller")]
    for path in sorted(record_repo.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(record_repo).parts:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(texts)


def assert_nothing_private_in(text: str) -> None:
    for private_text in PRIVATE_TEXTS:
        assert private_text not in text


# ---------------------------------------------------------------------------
# goodall stop


def test_goodall_stop_copies_what_activitywatch_answered_byte_for_byte_into_the_log_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()

    start = run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    assert (start.exit_code, stop.exit_code) == (0, 0), start.standard_error + stop.standard_error
    copy_folder = only_copy(eval_account)
    assert copy_folder.name == "20261116T103000.000000Z"
    # A bucket's first copy holds all of it, events from before the work block included.
    assert files_and_bytes(copy_folder) == {
        "info.json": server.info_bytes(),
        "buckets.json": server.buckets_bytes(),
        events_file_name(WINDOW_BUCKET_ID): as_served_json(server.events_in_range(WINDOW_BUCKET_ID, None, None)),
        events_file_name(AFK_BUCKET_ID): as_served_json(server.events_in_range(AFK_BUCKET_ID, None, None)),
    }
    assert "ActivityWatch copied, 2 buckets" in stop.everything_printed
    assert stop.standard_error == ""


def test_nothing_is_asked_while_the_work_block_runs_and_requests_use_the_shapes_the_hand_test_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()

    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
    run_goodall(eval_account, monkeypatch, ["status"], BLOCK_START + timedelta(minutes=30), server)
    assert server.requests == []
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    assert server.requests == [
        "/api/0/info",
        "/api/0/buckets/",
        f"/api/0/buckets/{WINDOW_BUCKET_ID}/events",
        f"/api/0/buckets/{AFK_BUCKET_ID}/events",
    ]


def test_the_copy_is_readable_only_by_the_account_and_no_file_name_ends_in_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    # A umask that would leave new files readable by everyone, as an account's default does.
    previous_umask = os.umask(0o022)
    try:
        run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
        run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)
    finally:
        os.umask(previous_umask)

    for folder_name in ("activitywatch", "activitywatch-sync"):
        checked = 0
        for path in [eval_account.log_directory / folder_name, *(eval_account.log_directory / folder_name).rglob("*")]:
            mode = stat.S_IMODE(path.stat().st_mode)
            if path.is_dir():
                assert mode == 0o700, path
            else:
                assert mode == 0o600, path
            assert not path.name.endswith(".jsonl")
            checked += 1
        assert checked > 1


def test_an_eval_without_activitywatch_asks_nothing_and_makes_no_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path, includes_activitywatch=False)

    # With no server handed to the fake runner, any curl would fail the test.
    start = run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, None)
    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, None)
    status = run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(minutes=1), None)

    assert (start.exit_code, stop.exit_code, status.exit_code) == (0, 0, 1)
    assert not eval_account.copies_folder.exists()
    assert not (eval_account.log_directory / "activitywatch-sync").exists()
    assert "ActivityWatch" not in stop.everything_printed


def test_the_second_copy_starts_where_the_first_ended_less_a_small_overlap_with_times_ending_in_z(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)
    second_start = BLOCK_END + timedelta(hours=2)
    second_end = second_start + timedelta(minutes=45)
    server.events[WINDOW_BUCKET_ID].append(window_event(3, second_start + timedelta(minutes=5), 120.0))
    server.requests.clear()

    run_goodall(eval_account, monkeypatch, ["start"], second_start, server)
    stop = run_goodall(eval_account, monkeypatch, ["stop"], second_end, server)

    assert stop.exit_code == 0, stop.standard_error
    # The first copy ended at 10:30:00; ten minutes before it is 10:20:00.
    asked_range = "?start=2026-11-16T10:20:00Z&end=2026-11-16T13:15:00Z"
    assert server.requests == [
        "/api/0/info",
        "/api/0/buckets/",
        f"/api/0/buckets/{WINDOW_BUCKET_ID}/events{asked_range}",
        f"/api/0/buckets/{AFK_BUCKET_ID}/events{asked_range}",
    ]
    first_copy, second_copy = sorted(eval_account.copies_folder.iterdir())
    second_window_events = json.loads((second_copy / events_file_name(WINDOW_BUCKET_ID)).read_bytes())
    assert [copied_event["id"] for copied_event in second_window_events] == [3]
    # The first copy is left as it was.
    assert len(json.loads((first_copy / events_file_name(WINDOW_BUCKET_ID)).read_bytes())) == 2


def test_a_bucket_that_appears_later_is_copied_whole_the_first_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)
    editor_bucket_id = f"aw-watcher-vscode_{SYNTHETIC_HOSTNAME}"
    server.add_bucket(editor_bucket_id, "app.editor.activity", [])
    server.requests.clear()

    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_END + timedelta(hours=1), server)
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END + timedelta(hours=2), server)

    assert server.requests[-1] == f"/api/0/buckets/{editor_bucket_id}/events"
    assert "?start=" in server.requests[-2]


def test_a_bucket_id_with_characters_unsafe_in_an_address_or_a_file_name_is_quoted_in_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = SimulatedActivityWatch()
    awkward_bucket_id = "aw-watcher-window_../Ada's Mac [2]?x=1&y#z"
    server.add_bucket(awkward_bucket_id, "currentwindow", [window_event(1, BLOCK_START, 5.0)])
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    assert stop.exit_code == 0, stop.standard_error
    quoted = "aw-watcher-window_..%2FAda%27s%20Mac%20%5B2%5D%3Fx%3D1%26y%23z"
    assert server.requests[-1] == f"/api/0/buckets/{quoted}/events"
    assert sorted(path.name for path in (only_copy(eval_account) / "events").iterdir()) == [f"{quoted}.json"]


# ---------------------------------------------------------------------------
# Whose ActivityWatch answered


def test_another_accounts_activitywatch_on_the_port_stops_the_copy_and_none_of_its_answer_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    server.device_id = OTHER_ACCOUNTS_DEVICE_ID
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    # The work block is not lost: it is stopped and recorded, with a warning.
    assert stop.exit_code == 0
    assert "Stopped your work block on research" in stop.everything_printed
    assert "WARN" in stop.everything_printed
    assert "reports a different device_id" in stop.everything_printed
    assert "goodall onboard" in stop.everything_printed
    assert "goodall start, stop or status" in stop.everything_printed
    # Only its answer about itself was asked for, and nothing of it stays on disk.
    assert server.requests == ["/api/0/info"]
    assert not eval_account.copies_folder.exists()
    for path in eval_account.log_directory.rglob("*"):
        if path.is_file():
            assert OTHER_ACCOUNTS_DEVICE_ID.encode() not in path.read_bytes(), path


def test_a_recorded_identifier_matches_whatever_case_the_server_answers_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    server.device_id = DEVICE_ID.upper()
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    assert "ActivityWatch copied, 2 buckets" in stop.everything_printed


def test_local_settings_without_the_port_and_identifier_are_a_warning_that_names_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path, device_id=None)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, None)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, None)

    assert stop.exit_code == 0
    assert "activitywatch_port" in stop.everything_printed
    assert "activitywatch_device_id" in stop.everything_printed


# ---------------------------------------------------------------------------
# A copy that fails is tried again


def test_a_copy_that_fails_at_stop_is_a_warning_and_the_next_status_takes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    server.answering = False
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    assert stop.exit_code == 0
    assert "Stopped your work block on research" in stop.everything_printed
    assert "did not copy ActivityWatch's data" in stop.everything_printed
    assert "Start ActivityWatch in this eval account" in stop.everything_printed
    assert "goodall start, stop or status" in stop.everything_printed
    assert not eval_account.copies_folder.exists()

    server.answering = True
    status = run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(minutes=5), server)

    assert "Copied ActivityWatch's data" in status.everything_printed
    assert "WARN" not in status.everything_printed
    assert only_copy(eval_account).name == "20261116T103500.000000Z"
    # Once taken, it is not asked for again.
    server.requests.clear()
    run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(minutes=6), server)
    assert server.requests == []


def test_a_copy_that_fails_part_way_leaves_nothing_in_activitywatch_and_the_retry_copies_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    server.failing_events_of = AFK_BUCKET_ID
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    assert stop.exit_code == 0
    assert "did not copy ActivityWatch's data" in stop.everything_printed
    assert not eval_account.copies_folder.exists()

    server.failing_events_of = None
    server.requests.clear()
    start = run_goodall(eval_account, monkeypatch, ["start"], BLOCK_END + timedelta(hours=1), server)

    assert "Copied ActivityWatch's data" in start.everything_printed
    # Nothing was copied before, so both buckets are asked for whole.
    assert server.requests[-2:] == [
        f"/api/0/buckets/{WINDOW_BUCKET_ID}/events",
        f"/api/0/buckets/{AFK_BUCKET_ID}/events",
    ]
    assert sorted(path.name for path in (only_copy(eval_account) / "events").iterdir()) == [
        f"{AFK_BUCKET_ID}.json",
        f"{WINDOW_BUCKET_ID}.json",
    ]


def test_a_work_block_nobody_stopped_has_its_copy_taken_by_the_next_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    # Four hours later, with nothing in between: goodall closes the work block itself.
    status = run_goodall(eval_account, monkeypatch, ["status"], BLOCK_START + timedelta(hours=4), server)

    assert "was left running" in status.everything_printed
    assert "Copied ActivityWatch's data" in status.everything_printed
    assert only_copy(eval_account).name == "20261116T133000.000000Z"


def test_goodall_stop_for_a_work_block_nobody_stopped_takes_one_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)

    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_START + timedelta(hours=4), server)

    assert stop.exit_code == 0, stop.standard_error
    assert "Copied ActivityWatch's data" in stop.everything_printed
    assert len(list(eval_account.copies_folder.iterdir())) == 1
    assert server.requests.count("/api/0/info") == 1


def test_goodall_stop_with_an_earlier_copy_still_wanted_takes_one_copy_after_the_work_block_has_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    server.answering = False
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_END + timedelta(hours=1), server)
    server.answering = True

    # A copy is still wanted when stop begins. Stop must not take it before
    # ending the work block and then want another at the very same moment.
    stop = run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END + timedelta(hours=2), server)

    assert stop.exit_code == 0, stop.standard_error
    assert "WARN" not in stop.everything_printed
    assert "ActivityWatch copied, 2 buckets" in stop.everything_printed
    assert only_copy(eval_account).name == "20261116T123000.000000Z"
    assert server.requests.count("/api/0/info") == 1


def test_status_as_json_carries_a_failed_copy_as_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    server.answering = False
    run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server)
    run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server)

    status = run_goodall(eval_account, monkeypatch, ["status", "--json"], BLOCK_END + timedelta(minutes=5), server)

    document = json.loads(status.standard_output)
    assert [warning["kind"] for warning in document["warnings"]] == ["activitywatch_not_copied"]


# ---------------------------------------------------------------------------
# What must never be shown or recorded


def test_titles_addresses_the_hostname_and_identifiers_never_reach_the_terminal_or_the_record_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eval_account = make_eval_account(tmp_path)
    server = working_server()
    printed = []

    # A copy that works, one stopped by another account's identifier, one whose events fail, and status as JSON.
    printed.append(run_goodall(eval_account, monkeypatch, ["start"], BLOCK_START, server))
    printed.append(run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END, server))
    server.device_id = OTHER_ACCOUNTS_DEVICE_ID
    printed.append(run_goodall(eval_account, monkeypatch, ["start"], BLOCK_END + timedelta(hours=1), server))
    printed.append(run_goodall(eval_account, monkeypatch, ["stop"], BLOCK_END + timedelta(hours=2), server))
    server.device_id = DEVICE_ID
    server.failing_events_of = WINDOW_BUCKET_ID
    printed.append(run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(hours=3), server))
    printed.append(run_goodall(eval_account, monkeypatch, ["status", "--json"], BLOCK_END + timedelta(hours=4), server))
    server.failing_events_of = None
    printed.append(run_goodall(eval_account, monkeypatch, ["status"], BLOCK_END + timedelta(hours=5), server))

    # The private text did reach the log directory, which is where it belongs.
    copied = b"".join(files_and_bytes(eval_account.copies_folder).values())
    assert SYNTHETIC_WINDOW_TITLE.encode() in copied
    assert SYNTHETIC_PAGE_ADDRESS.encode() in copied
    assert len(list(eval_account.copies_folder.iterdir())) == 2
    for run in printed:
        assert_nothing_private_in(run.standard_output)
        assert_nothing_private_in(run.standard_error)
    assert_nothing_private_in(everything_in_the_record_repo(eval_account.record_repo))
    assert "work-blocks" in everything_in_the_record_repo(eval_account.record_repo)


# ---------------------------------------------------------------------------
# The pieces of goodall.activitywatch_copies


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    folder.mkdir()
    return folder


NOW = datetime(2026, 11, 16, 10, 30, 0, 250000, tzinfo=UTC)


def take_copy(
    log_directory: Path, server: SimulatedActivityWatch, now: datetime = NOW
) -> activitywatch_copies.CopyResult:
    """Ask for a copy and take it, as goodall does when a work block has ended."""
    activitywatch_copies.note_copy_wanted(log_directory, now)
    return activitywatch_copies.copy_if_wanted(
        make_runner(server), log_directory, PORT, DEVICE_ID, log_directory / "local-settings.toml", now
    )


def test_with_no_copy_wanted_nothing_is_asked_and_no_folder_is_made(log_directory: Path) -> None:
    result = activitywatch_copies.copy_if_wanted(
        make_runner(None), log_directory, PORT, DEVICE_ID, log_directory / "local-settings.toml", NOW
    )

    assert (result.copied, result.problem) == (False, None)
    assert list(log_directory.iterdir()) == []


def test_a_copy_leaves_a_note_of_what_was_asked_outside_the_activitywatch_folder(log_directory: Path) -> None:
    server = working_server()
    take_copy(log_directory, server)
    later = NOW + timedelta(hours=2)

    result = take_copy(log_directory, server, later)

    assert (result.copied, result.bucket_count, result.problem) == (True, 2, None)
    notes_folder = log_directory / "activitywatch-sync" / "copies"
    first_note, second_note = sorted(notes_folder.iterdir())
    assert json.loads(first_note.read_text()) == {
        "schema_version": 1,
        "copied_at": "2026-11-16T10:30:00Z",
        "device_id": DEVICE_ID,
        "folder": "activitywatch/20261116T103000.250000Z",
        "overlap_seconds": 600,
        "buckets": [
            {"id": WINDOW_BUCKET_ID, "file": events_file_name(WINDOW_BUCKET_ID), "start": None, "end": None},
            {"id": AFK_BUCKET_ID, "file": events_file_name(AFK_BUCKET_ID), "start": None, "end": None},
        ],
    }
    assert json.loads(second_note.read_text())["buckets"][0] == {
        "id": WINDOW_BUCKET_ID,
        "file": events_file_name(WINDOW_BUCKET_ID),
        "start": "2026-11-16T10:20:00Z",
        "end": "2026-11-16T12:30:00Z",
    }
    # Goodall's own notes are kept out of the raw copies.
    assert sorted(path.name for path in (log_directory / "activitywatch").rglob("*") if path.is_file()) == sorted(
        ["info.json", "buckets.json", f"{WINDOW_BUCKET_ID}.json", f"{AFK_BUCKET_ID}.json"] * 2
    )
    assert not activitywatch_copies.copy_is_wanted(log_directory)


def test_a_new_identifier_recorded_through_onboarding_starts_from_the_beginning(log_directory: Path) -> None:
    server = working_server()
    take_copy(log_directory, server)
    new_device_id = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
    server.device_id = new_device_id
    server.requests.clear()
    activitywatch_copies.note_copy_wanted(log_directory, NOW)

    # Bucket ids are the same on every account's server, so only the identifier says the earlier copy was another's.
    result = activitywatch_copies.copy_if_wanted(
        make_runner(server),
        log_directory,
        PORT,
        new_device_id,
        log_directory / "local-settings.toml",
        NOW + timedelta(hours=1),
    )

    assert result.copied
    assert server.requests[-2:] == [
        f"/api/0/buckets/{WINDOW_BUCKET_ID}/events",
        f"/api/0/buckets/{AFK_BUCKET_ID}/events",
    ]


def test_an_events_answer_that_is_not_a_list_is_refused_and_not_kept(log_directory: Path) -> None:
    server = working_server()

    def curl_with_an_error_page(command: Command) -> ScriptedAnswer:
        answer = server.curl(command)
        if "/events" in command.arguments[-1]:
            Path(command.arguments[command.arguments.index("--output") + 1]).write_bytes(b"<html>not events</html>")
        return answer

    runner = make_runner(None)
    runner.answer_with("curl", curl_with_an_error_page)
    activitywatch_copies.note_copy_wanted(log_directory, NOW)

    result = activitywatch_copies.copy_if_wanted(
        runner, log_directory, PORT, DEVICE_ID, log_directory / "local-settings.toml", NOW
    )

    assert not result.copied
    assert result.problem is not None
    assert result.problem.kind == "activitywatch_answer_unfamiliar"
    assert not (log_directory / "activitywatch").exists()
    assert activitywatch_copies.copy_is_wanted(log_directory)


def test_the_simulated_server_refuses_the_request_shapes_the_real_one_refused_in_the_hand_test() -> None:
    """So the tests above, which pass against it, show that goodall asks in the shapes that work."""
    server = working_server()
    assert server.answer_for("/api/0/buckets") == 308
    assert server.answer_for(f"/api/0/buckets/{WINDOW_BUCKET_ID}/events/") == 404
    assert server.answer_for(f"/api/0/buckets/{WINDOW_BUCKET_ID}/events?start=2026-11-16T10:20:00+00:00") == 400


def test_curl_missing_is_a_problem_with_a_next_step(log_directory: Path) -> None:
    runner = make_runner(None)
    runner.answer_with("curl", lambda command: ScriptedAnswer(CommandOutcome.NOT_FOUND))
    activitywatch_copies.note_copy_wanted(log_directory, NOW)

    result = activitywatch_copies.copy_if_wanted(
        runner, log_directory, PORT, DEVICE_ID, log_directory / "local-settings.toml", NOW
    )

    assert result.problem is not None
    assert result.problem.kind == "activitywatch_curl_unavailable"


def test_a_second_copy_at_the_same_moment_does_not_replace_the_first(log_directory: Path) -> None:
    server = working_server()
    take_copy(log_directory, server)
    before = files_and_bytes(log_directory / "activitywatch")

    result = take_copy(log_directory, server)

    assert not result.copied
    assert result.problem is not None
    assert files_and_bytes(log_directory / "activitywatch") == before
    assert activitywatch_copies.copy_is_wanted(log_directory)


def test_what_a_killed_copy_left_half_done_is_cleared_before_the_next(log_directory: Path) -> None:
    in_progress = log_directory / "activitywatch-sync" / "copy-in-progress" / "events"
    in_progress.mkdir(parents=True)
    (in_progress / "left-behind.json").write_bytes(b"[")
    server = working_server()

    result = take_copy(log_directory, server)

    assert result.copied
    [copy_folder] = (log_directory / "activitywatch").iterdir()
    assert not (copy_folder / "events" / "left-behind.json").exists()
    assert not (log_directory / "activitywatch-sync" / "copy-in-progress").exists()
