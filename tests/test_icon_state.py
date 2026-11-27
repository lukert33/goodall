"""Outside-in tests for `goodall icon-state`, the read-only command the menu bar icon draws from.

Each test runs goodall the way the menu bar icon's helper does, through
`cli.main` with a captured terminal, and checks what the helper would get:
the exit code and the JSON document. The account is built inside tmp_path:
local settings, a record repo that is a real git repository, and a log
directory.

The command's promise is that it only reads (AGENTS.md invariant 5 is about
`goodall doctor`; this command makes the same promise for the same reason:
it runs every minute, unasked). So the tests that matter most take a
snapshot of every file under tmp_path, run the command, and compare. The
fake runner handed to `icon-state` has nothing scripted and no real
programs, so starting any program at all, git included, fails the test.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, work_blocks
from goodall.fake_runner import FakeBackgroundPrograms, FakeRunner, InProcessTelemetryReceiver
from goodall.terminal import Terminal

EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "A test eval."
workstreams = ["engineering", "research", "go-to-market"]
intervention_categories = ["infrastructure"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""

# A Monday morning in November, when London is on GMT, so local time and UTC agree.
MONDAY_MORNING = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)

# How every command that goodall runs to start its telemetry receiver begins.
RECEIVER_COMMAND_START = [sys.executable, "-I", "-m", "goodall.telemetry_receiver"]


def git(working_directory: Path, *arguments: str) -> str:
    """Run git for the test's own setup or inspection, and return what it printed."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments], cwd=working_directory, env=environment, capture_output=True, text=True, check=True
    )
    return completed.stdout


@dataclass
class Account:
    """A pretend macOS user account set up for goodall, and the time goodall will be told it is."""

    root: Path
    config_directory: Path
    record_repo: Path
    log_directory: Path
    now: datetime = MONDAY_MORNING
    background: FakeBackgroundPrograms = field(default_factory=FakeBackgroundPrograms)

    def clock(self) -> datetime:
        return self.now

    @property
    def work_blocks_folder(self) -> Path:
        return self.log_directory / "work-blocks"


@pytest.fixture
def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Account]:
    """An account whose local settings, eval config and record repo are in order, in London."""
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "records"
    log_directory = tmp_path / "logs"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{record_repo}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
    account = Account(tmp_path, config_directory, record_repo, log_directory)
    yield account
    account.background.stop_all()
    monkeypatch.undo()
    time.tzset()


def run_work_block_command(account: Account, arguments: list[str]) -> None:
    """Run `goodall start`, `pause` or `stop` to set the scene, with git real and the receiver simulated."""
    runner = FakeRunner(real_programs=["git"], background=account.background)
    runner.answer(["claude", "--version"], standard_output="2.3.4 (Claude Code)\n")
    runner.answer_background(RECEIVER_COMMAND_START, InProcessTelemetryReceiver)
    terminal = Terminal(standard_output=io.StringIO(), standard_error=io.StringIO())
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=account.clock)
    assert exit_code == exit_codes.SUCCESS, arguments


@dataclass
class IconStateRun:
    exit_code: int
    standard_output: str
    standard_error: str

    @property
    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = json.loads(self.standard_output)
        return document


def run_icon_state(account: Account, as_json: bool = True) -> IconStateRun:
    """Run `goodall icon-state` with a runner that fails the test if any program is started."""
    arguments = ["icon-state"]
    if as_json:
        arguments.append("--json")
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    runner = FakeRunner(real_programs=[])
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=account.clock)
    assert runner.calls == []
    return IconStateRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


def snapshot(folder: Path) -> dict[str, tuple[int, int, str]]:
    """Every file and folder under `folder`: its permissions, when it last changed, and what it holds.

    Folders are included, with an empty content hash, so a file created and
    removed again still shows up as a changed folder.
    """
    seen = {}
    for path in sorted(folder.rglob("*")):
        status = path.lstat()
        content_hash = ""
        if path.is_file() and not path.is_symlink():
            content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        seen[str(path)] = (status.st_mode, status.st_mtime_ns, content_hash)
    return seen


# ---------------------------------------------------------------------------
# What the document says in each state
# ---------------------------------------------------------------------------


def test_icon_state_when_not_recording_gives_the_whole_documented_shape(account: Account) -> None:
    run = run_icon_state(account)

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_error == ""
    assert run.document == {
        "schema_version": 1,
        "state": "not_recording",
        "status_line": "Not recording",
        "account_mode": "dedicated",
        "work_block": None,
        "paused_work_block": None,
        "workstreams": [
            {"name": "research", "main": True},
            {"name": "engineering", "main": False},
            {"name": "go-to-market", "main": False},
        ],
        "enabled": {"record_work_block": True, "pause": False, "stop_recording": False},
        "setup": {"usable": True, "reason": None},
        # No work block has ever started, so the folder that will hold the
        # marker is not there yet: the icon watches the one it will appear in.
        "watch_folder": str(account.log_directory),
        # goodall is set up here, so the icon may ask macOS for permission
        # to show notifications; macOS itself asks the person only once.
        "ask_notification_permission": True,
    }


def test_icon_state_while_recording_names_the_work_block_its_workstream_and_its_start(account: Account) -> None:
    run_work_block_command(account, ["start", "--workstream", "engineering"])
    marker = json.loads((account.work_blocks_folder / "active-work-block.json").read_text())
    account.now = MONDAY_MORNING + timedelta(minutes=42)

    document = run_icon_state(account).document

    assert document["state"] == "recording"
    assert document["status_line"] == "Recording engineering since 09:30"
    assert document["work_block"] == {
        "id": marker["id"],
        "workstream": "engineering",
        "started_at": "2026-11-16T09:30:00Z",
    }
    assert document["paused_work_block"] is None
    assert document["enabled"] == {"record_work_block": False, "pause": True, "stop_recording": True}
    assert document["watch_folder"] == str(account.work_blocks_folder)


def test_icon_state_while_paused_names_the_paused_work_block(account: Account) -> None:
    run_work_block_command(account, ["start"])
    marker = json.loads((account.work_blocks_folder / "active-work-block.json").read_text())
    account.now = MONDAY_MORNING + timedelta(minutes=40)
    run_work_block_command(account, ["pause"])

    document = run_icon_state(account).document

    assert document["state"] == "paused"
    assert document["status_line"] == "Paused on research since 10:10"
    assert document["work_block"] is None
    assert document["paused_work_block"] == {
        "id": marker["id"],
        "workstream": "research",
        "paused_at": "2026-11-16T10:10:00Z",
    }
    # Stop recording is offered while paused: `goodall stop` then forgets the pause.
    assert document["enabled"] == {"record_work_block": True, "pause": False, "stop_recording": True}


def test_icon_state_after_stop_is_not_recording_again(account: Account) -> None:
    run_work_block_command(account, ["start"])
    account.now = MONDAY_MORNING + timedelta(minutes=40)
    run_work_block_command(account, ["stop"])

    document = run_icon_state(account).document

    assert document["state"] == "not_recording"
    assert document["work_block"] is None
    assert document["paused_work_block"] is None


def test_icon_state_says_the_day_when_the_work_block_did_not_start_today(account: Account) -> None:
    run_work_block_command(account, ["start"])
    # Kept alive past midnight by heartbeats.
    heartbeat_at = MONDAY_MORNING
    while heartbeat_at < MONDAY_MORNING + timedelta(hours=15):
        heartbeat_at = heartbeat_at + timedelta(hours=2)
        work_blocks.record_heartbeat(account.log_directory, heartbeat_at)
    account.now = heartbeat_at + timedelta(minutes=5)

    document = run_icon_state(account).document

    assert document["status_line"] == "Recording research since Mon 16 Nov, 09:30"


def test_icon_state_shows_mixed_account_mode(account: Account) -> None:
    settings_file = account.config_directory / "local-settings.toml"
    settings_file.write_text(settings_file.read_text() + 'account_mode = "mixed"\n')

    assert run_icon_state(account).document["account_mode"] == "mixed"


def test_a_work_block_idle_past_the_idle_limit_is_shown_as_not_recording(account: Account) -> None:
    # The prompt gate treats it the same way: nobody stopped it, and it is
    # no longer active. Closing it is left to the next start, stop or status.
    run_work_block_command(account, ["start"])
    account.now = MONDAY_MORNING + work_blocks.IDLE_LIMIT + timedelta(minutes=1)

    document = run_icon_state(account).document

    assert document["state"] == "not_recording"
    assert document["work_block"] is None
    assert document["enabled"] == {"record_work_block": True, "pause": False, "stop_recording": False}


# ---------------------------------------------------------------------------
# It only reads
# ---------------------------------------------------------------------------


def test_icon_state_changes_nothing_on_disk_when_not_recording(account: Account) -> None:
    before = snapshot(account.root)

    run_icon_state(account)
    run_icon_state(account, as_json=False)

    assert snapshot(account.root) == before


def test_icon_state_changes_nothing_on_disk_while_recording_or_paused(account: Account) -> None:
    run_work_block_command(account, ["start"])
    before = snapshot(account.root)
    run_icon_state(account)
    assert snapshot(account.root) == before

    account.now = MONDAY_MORNING + timedelta(minutes=40)
    run_work_block_command(account, ["pause"])
    before = snapshot(account.root)
    run_icon_state(account)
    assert snapshot(account.root) == before


def test_icon_state_leaves_a_work_block_idle_past_the_idle_limit_exactly_as_it_is(account: Account) -> None:
    # `goodall status` would close this work block and commit its record.
    # The icon asks every minute, unasked, so its command must not.
    run_work_block_command(account, ["start"])
    account.now = MONDAY_MORNING + work_blocks.IDLE_LIMIT + timedelta(hours=12)
    commits_before = git(account.record_repo, "log", "--format=%H")
    before = snapshot(account.root)

    run_icon_state(account)

    assert snapshot(account.root) == before
    assert git(account.record_repo, "log", "--format=%H") == commits_before
    assert (account.work_blocks_folder / "active-work-block.json").exists()
    assert list((account.work_blocks_folder / "unrecorded").iterdir()) == []


def test_icon_state_leaves_a_damaged_marker_where_it_is(account: Account) -> None:
    run_work_block_command(account, ["start"])
    marker_file = account.work_blocks_folder / "active-work-block.json"
    marker_file.chmod(0o600)
    marker_file.write_text("{ this is not a work block")
    before = snapshot(account.root)

    document = run_icon_state(account).document

    assert document["state"] == "not_recording"
    assert snapshot(account.root) == before


# ---------------------------------------------------------------------------
# When goodall cannot be used in this account
# ---------------------------------------------------------------------------


def test_icon_state_without_local_settings_says_setup_is_not_usable_and_exits_1(account: Account) -> None:
    (account.config_directory / "local-settings.toml").unlink()
    before = snapshot(account.root)

    run = run_icon_state(account)

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_error == ""
    assert run.document == {
        "schema_version": 1,
        "state": "not_recording",
        "status_line": "Not recording",
        "account_mode": None,
        "work_block": None,
        "paused_work_block": None,
        "workstreams": [],
        "enabled": {"record_work_block": False, "pause": False, "stop_recording": False},
        "setup": {
            "usable": False,
            "reason": (
                "goodall is not set up in this macOS user account yet. Run goodall status in Terminal to see "
                "what is needed."
            ),
        },
        "watch_folder": None,
        # Not before setup: the person has not yet been told the question is coming.
        "ask_notification_permission": False,
    }
    assert snapshot(account.root) == before


def test_icon_state_with_an_unreachable_log_directory_says_goodall_cannot_record(account: Account) -> None:
    account.log_directory.rmdir()

    run = run_icon_state(account)

    assert run.exit_code == exit_codes.ERROR
    assert run.document["setup"] == {
        "usable": False,
        "reason": "goodall cannot record right now. Run goodall status in Terminal to see why.",
    }
    assert run.document["enabled"] == {"record_work_block": False, "pause": False, "stop_recording": False}


def test_nothing_the_icon_shows_says_eval(account: Account) -> None:
    # The menu bar icon never says "eval" (ADR 0021), and it shows these
    # words as they are, so they must not either. Paths are not shown.
    shown = []
    document = run_icon_state(account).document
    shown.append(document["status_line"])
    run_work_block_command(account, ["start"])
    shown.append(run_icon_state(account).document["status_line"])
    run_work_block_command(account, ["pause"])
    shown.append(run_icon_state(account).document["status_line"])
    (account.record_repo / "eval.toml").unlink()
    broken = run_icon_state(account).document
    shown.append(broken["status_line"])
    shown.append(broken["setup"]["reason"])
    (account.config_directory / "local-settings.toml").unlink()
    shown.append(run_icon_state(account).document["setup"]["reason"])

    for words in shown:
        assert "eval" not in words.lower(), words


# ---------------------------------------------------------------------------
# For a person: the same facts as text
# ---------------------------------------------------------------------------


def test_icon_state_without_json_prints_the_status_line(account: Account) -> None:
    run_work_block_command(account, ["start"])

    run = run_icon_state(account, as_json=False)

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_output == "Recording research since 09:30\n"


def test_icon_state_without_json_prints_the_reason_when_setup_is_not_usable(account: Account) -> None:
    (account.config_directory / "local-settings.toml").unlink()

    run = run_icon_state(account, as_json=False)

    assert run.exit_code == exit_codes.ERROR
    assert "Not recording" in run.standard_output
    assert "goodall is not set up in this macOS user account yet." in " ".join(run.standard_output.split())
