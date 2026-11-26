"""Outside-in tests for the timeline: `goodall timeline`, and the database `goodall stop` builds.

Each test runs goodall commands the way a person would, in this process,
with a captured terminal, the fake runner and a clock the test sets. Between
the commands the test writes what would have been written during a real
work block: a synthetic Claude session, a synthetic copy of ActivityWatch's
data in the log directory, and a journal entry and an intervention in the
record repo. The tests then check what the person sees, and what is in the
database file.

Every record here is synthetic. The texts in PRIVATE_TEXTS stand for what a
person or a website could have written: a prompt, a window title, a page
address, a domain name, a file path, a workstream's name, a note. None of
them may reach the timeline, in any column, or the terminal.
"""

import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from goodall import cli, records, timeline, timeline_entries
from goodall.fake_runner import FakeRunner, PretendProgram
from goodall.terminal import Terminal

PROJECT_FOLDER_NAME = "-Users-ada-work"
CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
DEVICE_ID = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"
HOSTNAME = "synthetic-mac"
WINDOW_BUCKET_ID = f"aw-watcher-window_{HOSTNAME}"
AFK_BUCKET_ID = f"aw-watcher-afk_{HOSTNAME}"

BLOCK_START = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
BLOCK_END = BLOCK_START + timedelta(hours=1)

PROMPT_TEXT = "SYNTHETIC-PROMPT find me a short domain name"
REPLY_TEXT = "SYNTHETIC-REPLY here are three names"
THINKING_TEXT = "SYNTHETIC-THINKING the person wants a short name"
WINDOW_TITLE = "SYNTHETIC-TITLE Buy a domain - Checkout"
PAGE_ADDRESS = "https://registrar.synthetic-domain.example/checkout?cart=SYNTHETIC-ADDRESS"
DOMAIN_NAME = "synthetic-domain.example"
FILE_PATH = "/Users/ada/Documents/SYNTHETIC-PATH/shortlist.txt"
WORKSTREAM = "synthetic-workstream"
JOURNAL_TEXT = "SYNTHETIC-JOURNAL decided on the second name."
INTERVENTION_TEXT = "SYNTHETIC-INTERVENTION the agent picked a name that was taken."
MCP_TOOL_NAME = "mcp__SYNTHETIC-SERVER__search"
UNLISTED_APPLICATION = "SYNTHETIC-APPLICATION"
PRIVATE_TEXTS = [
    "SYNTHETIC",
    PROMPT_TEXT,
    REPLY_TEXT,
    THINKING_TEXT,
    WINDOW_TITLE,
    PAGE_ADDRESS,
    DOMAIN_NAME,
    FILE_PATH,
    WORKSTREAM,
    JOURNAL_TEXT,
    INTERVENTION_TEXT,
    MCP_TOOL_NAME,
    UNLISTED_APPLICATION,
]


@dataclass(frozen=True)
class EvalAccount:
    """The folders these tests touch, all inside tmp_path."""

    config_directory: Path
    record_repo: Path
    log_directory: Path
    claude_config_directory: Path

    @property
    def transcript(self) -> Path:
        return self.claude_config_directory / "projects" / PROJECT_FOLDER_NAME / f"{CLAUDE_SESSION}.jsonl"

    @property
    def timeline_file(self) -> Path:
        return self.log_directory / "timeline" / "timeline.sqlite3"


EVAL_CONFIG_TEXT = f"""\
schema_version = 1
name = "goodall-pilot"
description = "A test eval."
workstreams = ["{WORKSTREAM}"]
intervention_categories = ["infrastructure", "agent-limitation"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""


def git(working_directory: Path, *arguments: str) -> None:
    """Run git for the test's own setup, free of any GIT_ variable the test run inherited."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    subprocess.run(["git", *arguments], cwd=working_directory, env=environment, capture_output=True, check=True)


@pytest.fixture
def eval_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvalAccount:
    """Local settings, a record repo holding the eval config, a log directory, and an empty Claude projects folder."""
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    record_repo = tmp_path / "records"
    record_repo.mkdir()
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        f'main_workstream = "{WORKSTREAM}"\n'
        f'record_repo = "{record_repo}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    claude_config_directory = tmp_path / "claude-config"
    (claude_config_directory / "projects").mkdir(parents=True)
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config_directory))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    return EvalAccount(config_directory, record_repo, log_directory, claude_config_directory)


@dataclass(frozen=True)
class GoodallRun:
    """What one run of a goodall command did, as the person who ran it would see it."""

    exit_code: int
    standard_output: str
    standard_error: str

    @property
    def everything_printed(self) -> str:
        return self.standard_output + "\n" + self.standard_error


def run_goodall(arguments: list[str], now: datetime) -> GoodallRun:
    """Run a goodall command in this process at the moment `now`, with a captured terminal and the fake runner."""
    runner = FakeRunner()
    runner.answer(["claude", "--version"], standard_output="2.1.274 (Claude Code)\n")
    # goodall start's telemetry receiver, which these tests do not look at.
    runner.answer_background(
        [sys.executable, "-I", "-m", "goodall.telemetry_receiver"], lambda: PretendProgram(ready_line='{"port": 4318}')
    )
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: now)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


# ---------------------------------------------------------------------------
# What a work block leaves behind, written by hand


def claude_timestamp(moment: datetime) -> str:
    """A moment as the Claude family writes it: UTC to the millisecond, ending in Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def transcript_lines(first_at: datetime) -> list[dict[str, object]]:
    """A short synthetic Claude session: a prompt, thinking, two tool calls with their results, and a reply.

    Line 1 carries no time, so it is not in the timeline. Lines 2 to 9 are
    ten seconds apart, starting at `first_at`.
    """
    shared = {"sessionId": CLAUDE_SESSION, "entrypoint": "claude-desktop", "cwd": FILE_PATH, "version": "2.1.274"}
    timed: list[dict[str, object]] = [
        {
            "type": "user",
            "uuid": "u-2",
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": PROMPT_TEXT},
        },
        assistant_line("a-3", {"type": "thinking", "thinking": THINKING_TEXT, "signature": "synthetic"}),
        assistant_line(
            "a-4", {"type": "tool_use", "id": "toolu_1", "name": "WebSearch", "input": {"query": DOMAIN_NAME}}
        ),
        tool_result_line("u-5", {"type": "tool_result", "tool_use_id": "toolu_1", "content": PAGE_ADDRESS}),
        assistant_line(
            "a-6", {"type": "tool_use", "id": "toolu_2", "name": MCP_TOOL_NAME, "input": {"path": FILE_PATH}}
        ),
        tool_result_line(
            "u-7", {"type": "tool_result", "tool_use_id": "toolu_2", "content": FILE_PATH, "is_error": True}
        ),
        assistant_line("a-8", {"type": "text", "text": REPLY_TEXT}),
        {"type": "system", "uuid": "s-9", "subtype": "turn_duration", "durationMs": 61000},
    ]
    lines: list[dict[str, object]] = [
        {"type": "custom-title", "customTitle": WINDOW_TITLE, "sessionId": CLAUDE_SESSION}
    ]
    for number, line in enumerate(timed):
        lines.append({**shared, **line, "timestamp": claude_timestamp(first_at + timedelta(seconds=10 * number))})
    return lines


def assistant_line(entry_id: str, content_block: dict[str, object]) -> dict[str, object]:
    message = {"role": "assistant", "model": "example-model", "content": [content_block]}
    return {"type": "assistant", "uuid": entry_id, "message": message}


def tool_result_line(entry_id: str, content_block: dict[str, object]) -> dict[str, object]:
    return {"type": "user", "uuid": entry_id, "message": {"role": "user", "content": [content_block]}}


def write_claude_session(eval_account: EvalAccount, first_at: datetime) -> None:
    """Write the synthetic Claude session where the Claude family would, last changed at its last record's time."""
    lines = transcript_lines(first_at)
    eval_account.transcript.parent.mkdir(parents=True, exist_ok=True)
    eval_account.transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))
    last_written_at = (first_at + timedelta(seconds=10 * (len(lines) - 2))).timestamp()
    os.utime(eval_account.transcript, (last_written_at, last_written_at))


def activitywatch_event(event_id: int, started: datetime, seconds: float, data: dict[str, object]) -> dict[str, object]:
    """One event as ActivityWatch's server answers it: a time ending in +00:00, a duration in seconds, an id."""
    timestamp = started.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    return {"id": event_id, "timestamp": timestamp, "duration": seconds, "data": data}


def write_activitywatch_copy(
    eval_account: EvalAccount,
    copy_name: str,
    window_events: list[dict[str, object]],
    afk_events: list[dict[str, object]],
) -> None:
    """Write one copy of ActivityWatch's data in the layout goodall.activitywatch_copies leaves, newest event first."""
    copy_folder = eval_account.log_directory / "activitywatch" / copy_name
    (copy_folder / "events").mkdir(parents=True)
    (copy_folder / "info.json").write_text(json.dumps({"hostname": HOSTNAME, "device_id": DEVICE_ID, "testing": False}))
    buckets = {
        WINDOW_BUCKET_ID: {"id": WINDOW_BUCKET_ID, "type": "currentwindow", "hostname": HOSTNAME},
        AFK_BUCKET_ID: {"id": AFK_BUCKET_ID, "type": "afkstatus", "hostname": HOSTNAME},
    }
    (copy_folder / "buckets.json").write_text(json.dumps(buckets))
    for bucket_id, events in [(WINDOW_BUCKET_ID, window_events), (AFK_BUCKET_ID, afk_events)]:
        newest_first = sorted(events, key=lambda event: str(event["timestamp"]), reverse=True)
        (copy_folder / "events" / f"{quote(bucket_id, safe='')}.json").write_text(json.dumps(newest_first))


def write_record(eval_account: EvalAccount, record: records.JournalEntry | records.Intervention) -> None:
    """Write one of goodall's own records into the record repo, as `goodall journal` and `goodall intervene` do."""
    record_file = eval_account.record_repo / records.relative_path_for(record)
    record_file.parent.mkdir(parents=True, exist_ok=True)
    record_file.write_text(records.render_record(record))


def run_a_work_block(eval_account: EvalAccount) -> GoodallRun:
    """Start a work block, leave behind what an hour of choosing a domain name would, and stop it."""
    start = run_goodall(["start"], BLOCK_START)
    assert start.exit_code == 0, start.standard_error
    # 09:35:00 to 09:36:10: the Claude session.
    write_claude_session(eval_account, BLOCK_START + timedelta(minutes=5))
    # The person was at the keyboard throughout, in the agent's application
    # until 09:40, then in a browser and in an application goodall has no name for.
    write_activitywatch_copy(
        eval_account,
        "20261116T103000.000000Z",
        window_events=[
            activitywatch_event(1, BLOCK_START, 600.0, {"app": "Claude", "title": WINDOW_TITLE}),
            activitywatch_event(
                2,
                BLOCK_START + timedelta(minutes=10),
                125.0,
                {"app": "Safari", "title": WINDOW_TITLE, "url": PAGE_ADDRESS},
            ),
            activitywatch_event(
                3, BLOCK_START + timedelta(minutes=13), 30.0, {"app": UNLISTED_APPLICATION, "title": WINDOW_TITLE}
            ),
        ],
        afk_events=[activitywatch_event(1, BLOCK_START + timedelta(seconds=1), 3500.0, {"status": "not-afk"})],
    )
    write_record(
        eval_account,
        records.build_journal_entry("ada-lovelace", BLOCK_START + timedelta(minutes=20), JOURNAL_TEXT),
    )
    write_record(
        eval_account,
        records.build_intervention(
            "ada-lovelace",
            occurred_at=BLOCK_START + timedelta(minutes=15),
            written_at=BLOCK_START + timedelta(minutes=21),
            category="agent-limitation",
            decision="modify",
            body=INTERVENTION_TEXT,
        ),
    )
    stop = run_goodall(["stop"], BLOCK_END)
    assert stop.exit_code == 0, stop.standard_error
    return stop


def rows_shown(run: GoodallRun) -> list[str]:
    """The timeline's rows as printed, each without its time of day and with runs of spaces made one space."""
    rows = []
    for line in run.standard_output.splitlines():
        words = line.split()
        if len(words) >= 2 and len(words[0]) == 8 and words[0][2] == ":" and words[0][5] == ":":
            rows.append(" ".join(words[1:]))
    return rows


def assert_nothing_private_in(text: str) -> None:
    for private_text in PRIVATE_TEXTS:
        assert private_text not in text


# ---------------------------------------------------------------------------


def test_goodall_timeline_shows_the_work_block_just_stopped_in_time_order_with_no_private_text(
    eval_account: EvalAccount,
) -> None:
    run_a_work_block(eval_account)

    timeline = run_goodall(["timeline"], BLOCK_END + timedelta(minutes=1))

    assert timeline.exit_code == 0, timeline.standard_error
    assert rows_shown(timeline) == [
        "human application_focus Claude (10 min) activitywatch",
        "human active_at_keyboard (58 min 20 s) activitywatch",
        "human typed_prompt claude_desktop_code_tab",
        "ai thinking claude_desktop_code_tab",
        "ai tool_call WebSearch claude_desktop_code_tab",
        "harness tool_result WebSearch ok claude_desktop_code_tab",
        "ai tool_call mcp_tool claude_desktop_code_tab",
        "harness tool_result mcp_tool error claude_desktop_code_tab",
        "ai message_text claude_desktop_code_tab",
        "human application_focus Safari (2 min 5 s) activitywatch",
        "human application_focus other_application (30 s) activitywatch",
        "human intervention agent-limitation modify record_repo",
        "human journal_entry record_repo",
    ]
    assert "1 bookkeeping entry is not shown" in timeline.standard_output
    assert_nothing_private_in(timeline.everything_printed)


def test_goodall_stop_builds_the_timeline_as_one_file_only_this_macos_user_account_can_read(
    eval_account: EvalAccount,
) -> None:
    stop = run_a_work_block(eval_account)

    assert "Timeline" in stop.standard_output
    timeline_folder = eval_account.timeline_file.parent
    assert [path.name for path in timeline_folder.iterdir()] == ["timeline.sqlite3"]
    assert stat.S_IMODE(timeline_folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(eval_account.timeline_file.stat().st_mode) == 0o600


def test_no_text_a_person_or_a_website_could_have_written_is_anywhere_in_the_timeline_file(
    eval_account: EvalAccount,
) -> None:
    run_a_work_block(eval_account)

    for path in eval_account.timeline_file.parent.iterdir():
        contents = path.read_bytes()
        for private_text in PRIVATE_TEXTS:
            assert private_text.encode("utf-8") not in contents, f"{path.name} holds private text"


# What each column of the timeline may hold. A column that is not here, or
# that holds text off its list, fails the test below: that is how a new
# free-text column is caught.
TIMES = "times"
NUMBERS = "numbers"
ALLOWED_VALUES: dict[tuple[str, str], str | tuple[str, ...]] = {
    ("raw_records", "raw_record"): NUMBERS,
    ("raw_records", "kept_in"): timeline_entries.KEPT_IN,
    ("work_blocks", "work_block"): NUMBERS,
    ("work_blocks", "started_at"): TIMES,
    ("work_blocks", "ended_at"): TIMES,
    ("work_blocks", "end_state"): records.END_STATES,
    ("work_blocks", "raw_record"): NUMBERS,
    ("entries", "entry"): NUMBERS,
    ("entries", "started_at"): TIMES,
    ("entries", "ended_at"): TIMES,
    ("entries", "duration_seconds"): NUMBERS,
    ("entries", "source"): timeline_entries.SOURCES,
    ("entries", "source_application"): timeline_entries.SOURCE_APPLICATIONS,
    ("entries", "actor"): timeline_entries.ACTORS,
    ("entries", "kind"): timeline_entries.KINDS,
    ("entries", "entry_type"): timeline_entries.ENTRY_TYPES,
    ("entries", "label_rule"): timeline_entries.LABEL_RULES,
    ("entries", "tool"): timeline_entries.TOOLS,
    ("entries", "application"): timeline_entries.APPLICATIONS,
    ("entries", "category"): timeline_entries.CATEGORIES,
    ("entries", "intervention_category"): ("infrastructure", "agent-limitation", "other_category"),
    ("entries", "outcome"): timeline_entries.OUTCOMES,
    ("entries", "in_subagent"): NUMBERS,
    ("entries", "raw_record"): NUMBERS,
    ("entries", "position"): NUMBERS,
}

# The two columns that are not held to a list, each checked in its own way
# by assert_every_column_holds_only_what_it_may.
POINTER_PATH_COLUMN = ("raw_records", "path")
INFORMATION_COLUMNS = [("information", "name"), ("information", "value")]

TIME_AS_WRITTEN = "%Y-%m-%dT%H:%M:%S.%fZ"


def assert_every_column_holds_only_what_it_may(timeline_file: Path) -> None:
    """Check every value of every column of every table: a number, a time, or a word from that column's list.

    The pointer's path is the one text that is not from a list: it must be
    a path that stays inside its folder. `information` holds versions,
    counts and one time, by name.
    """
    connection = sqlite3.connect(timeline_file)
    try:
        table_names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        assert sorted(table_names) == ["entries", "information", "raw_records", "work_blocks"]
        for table_name in table_names:
            for column in connection.execute(f"PRAGMA table_info({table_name})").fetchall():
                column_name = column[1]
                values = [row[0] for row in connection.execute(f"SELECT DISTINCT {column_name} FROM {table_name}")]
                where = (table_name, column_name)
                if where == POINTER_PATH_COLUMN:
                    for value in values:
                        assert not value.startswith("/") and ".." not in value.split("/")
                    continue
                if where in INFORMATION_COLUMNS:
                    continue
                assert where in ALLOWED_VALUES, f"{table_name}.{column_name} is a column this test has no list for"
                allowed = ALLOWED_VALUES[where]
                for value in values:
                    if value is None:
                        continue
                    if allowed == NUMBERS:
                        assert isinstance(value, int | float), f"{table_name}.{column_name} holds text"
                    elif allowed == TIMES:
                        datetime.strptime(value, TIME_AS_WRITTEN)
                    else:
                        assert value in allowed, f"{table_name}.{column_name} holds a value off its list"
        information = dict(connection.execute("SELECT name, value FROM information"))
    finally:
        connection.close()
    assert sorted(information) == [
        "activitywatch_rules_version",
        "actor_label_rules_version",
        "built_at",
        "goodall_version",
        "human_action_rules_version",
        "records_without_a_time",
        "schema_version",
        "timeline_rules_version",
    ]
    datetime.strptime(information.pop("built_at"), TIME_AS_WRITTEN)
    for name, value in information.items():
        assert len(value) <= 40 and all(character.isalnum() or character in ".+-" for character in value), name


def test_every_column_of_the_timeline_holds_only_times_numbers_and_values_from_fixed_lists(
    eval_account: EvalAccount,
) -> None:
    run_a_work_block(eval_account)

    assert_every_column_holds_only_what_it_may(eval_account.timeline_file)


def test_the_transcript_fixtures_of_every_other_rule_also_leave_only_listed_values_in_the_timeline(
    eval_account: EvalAccount,
) -> None:
    # Every synthetic transcript goodall's other rules are tested with:
    # prompts, shell commands, slash commands, rejections with feedback,
    # questions, plans, titles, malformed lines. None of their text may
    # reach a column either.
    fixtures = Path(__file__).parent / "fixtures" / "claude_code_transcripts"
    project_folder = eval_account.log_directory / "transcripts" / PROJECT_FOLDER_NAME
    project_folder.mkdir(parents=True)
    session_number = 0
    for fixture in sorted(fixtures.glob("*.jsonl")):
        session_number += 1
        claude_session = f"00000000-0000-4000-8000-{session_number:012d}"
        (project_folder / f"{claude_session}.jsonl").write_bytes(fixture.read_bytes())
    subagent_fixture = fixtures / "session-u" / "subagents" / "agent-example.jsonl"
    subagents_folder = project_folder / "00000000-0000-4000-8000-000000000001" / "subagents"
    subagents_folder.mkdir(parents=True)
    (subagents_folder / "agent-example.jsonl").write_bytes(subagent_fixture.read_bytes())

    build = timeline.build_timeline(
        eval_account.log_directory, eval_account.record_repo, ("infrastructure", "agent-limitation"), BLOCK_END
    )

    assert build.built and build.problems == []
    assert build.entry_count > 100
    assert_every_column_holds_only_what_it_may(eval_account.timeline_file)


def timeline_as_json(arguments: list[str], now: datetime) -> dict[str, object]:
    run = run_goodall(["timeline", "--json", *arguments], now)
    assert run.exit_code == 0, run.everything_printed
    document: dict[str, object] = json.loads(run.standard_output)
    return document


def raw_record_file(eval_account: EvalAccount, raw_record: dict[str, object]) -> Path:
    """The file a pointer leads to."""
    folders = {"log_directory": eval_account.log_directory, "record_repo": eval_account.record_repo}
    return folders[str(raw_record["kept_in"])] / str(raw_record["path"])


def test_each_entrys_pointer_leads_to_its_place_in_a_raw_record_where_the_private_text_still_is(
    eval_account: EvalAccount,
) -> None:
    run_a_work_block(eval_account)

    document = timeline_as_json(["--all"], BLOCK_END + timedelta(minutes=1))

    entries = document["entries"]
    assert isinstance(entries, list)
    text_at_pointer = {}
    for entry in entries:
        raw_record = entry["raw_record"]
        raw_file = raw_record_file(eval_account, raw_record)
        if raw_file.suffix == ".jsonl":
            # A transcript: the position is the line's number, counting from 1.
            pointed_at = raw_file.read_text().splitlines()[raw_record["position"] - 1]
            assert json.loads(pointed_at)["timestamp"] == claude_timestamp(datetime.fromisoformat(entry["started_at"]))
        elif raw_file.suffix == ".json":
            # A copy of ActivityWatch's events: the position is the event's place in the list, counting from 0.
            pointed_at = json.dumps(json.loads(raw_file.read_text())[raw_record["position"]])
        else:
            # One of goodall's own records: the whole file.
            assert raw_record["position"] is None
            pointed_at = raw_file.read_text()
        text_at_pointer[(entry["kind"], entry["tool"] or entry["application"])] = pointed_at
    assert PROMPT_TEXT in text_at_pointer[("typed_prompt", None)]
    assert DOMAIN_NAME in text_at_pointer[("tool_call", "WebSearch")]
    assert MCP_TOOL_NAME in text_at_pointer[("tool_call", "mcp_tool")]
    assert REPLY_TEXT in text_at_pointer[("message_text", None)]
    assert PAGE_ADDRESS in text_at_pointer[("application_focus", "Safari")]
    assert UNLISTED_APPLICATION in text_at_pointer[("application_focus", "other_application")]
    assert JOURNAL_TEXT in text_at_pointer[("journal_entry", None)]
    assert INTERVENTION_TEXT in text_at_pointer[("intervention", None)]


def test_goodall_timeline_json_has_a_stable_shape_with_the_rule_versions_used(eval_account: EvalAccount) -> None:
    run_a_work_block(eval_account)

    document = timeline_as_json([], BLOCK_END + timedelta(minutes=1))

    assert sorted(document) == ["entries", "entries_not_shown", "failures", "rule_versions", "warnings", "work_block"]
    assert document["entries_not_shown"] == 1
    assert document["rule_versions"] == {
        "schema_version": "1",
        "timeline_rules_version": "1",
        "actor_label_rules_version": "3",
        "human_action_rules_version": "3",
        "activitywatch_rules_version": "1",
    }
    work_block = document["work_block"]
    assert isinstance(work_block, dict)
    assert work_block["started_at"] == "2026-11-16T09:30:00.000000Z"
    assert work_block["ended_at"] == "2026-11-16T10:30:00.000000Z"
    assert work_block["end_state"] == "clean"
    entries = document["entries"]
    assert isinstance(entries, list)
    assert entries[2] == {
        "started_at": "2026-11-16T09:35:00.000000Z",
        "duration_seconds": None,
        "source": "claude",
        "source_application": "claude_desktop_code_tab",
        "actor": "human",
        "kind": "typed_prompt",
        "entry_type": "user",
        "label_rule": "person_typed_prompt",
        "tool": None,
        "application": None,
        "category": "directed_the_agent",
        "intervention_category": None,
        "outcome": None,
        "in_subagent": False,
        "raw_record": {
            "kept_in": "log_directory",
            "path": f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl",
            "position": 2,
        },
    }
    assert_nothing_private_in(json.dumps(document))


def everything_in(timeline_file: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of the tables that hold what happened, for comparing two builds."""
    connection = sqlite3.connect(timeline_file)
    try:
        return {
            "raw_records": connection.execute("SELECT * FROM raw_records ORDER BY raw_record").fetchall(),
            "work_blocks": connection.execute("SELECT * FROM work_blocks ORDER BY work_block").fetchall(),
            "entries": connection.execute("SELECT * FROM entries ORDER BY entry").fetchall(),
        }
    finally:
        connection.close()


def test_a_deleted_timeline_is_built_again_with_the_same_entries(eval_account: EvalAccount) -> None:
    run_a_work_block(eval_account)
    before = everything_in(eval_account.timeline_file)
    assert len(before["entries"]) == 14
    eval_account.timeline_file.unlink()

    timeline_run = run_goodall(["timeline"], BLOCK_END + timedelta(hours=3))

    assert timeline_run.exit_code == 0, timeline_run.standard_error
    assert everything_in(eval_account.timeline_file) == before


def test_a_timeline_built_by_other_rules_is_built_again_before_it_is_read(eval_account: EvalAccount) -> None:
    run_a_work_block(eval_account)
    connection = sqlite3.connect(eval_account.timeline_file)
    with connection:
        connection.execute("UPDATE information SET value = '0' WHERE name = 'timeline_rules_version'")
        connection.execute("DELETE FROM entries")
    connection.close()

    timeline_run = run_goodall(["timeline"], BLOCK_END + timedelta(hours=3))

    assert len(rows_shown(timeline_run)) == 13
    assert timeline.read_information(eval_account.timeline_file)["timeline_rules_version"] == "1"


def test_rebuild_takes_in_what_was_written_after_the_work_block_ended(eval_account: EvalAccount) -> None:
    run_a_work_block(eval_account)
    # An intervention written down the next day, about a moment inside the work block.
    write_record(
        eval_account,
        records.build_intervention(
            "ada-lovelace",
            occurred_at=BLOCK_START + timedelta(minutes=30),
            written_at=BLOCK_START + timedelta(days=1),
            category="a-category-the-eval-does-not-list",
            decision="reject",
            body=INTERVENTION_TEXT,
        ),
    )

    as_it_was = run_goodall(["timeline"], BLOCK_END + timedelta(days=1))
    rebuilt = run_goodall(["timeline", "--rebuild"], BLOCK_END + timedelta(days=1))

    assert len(rows_shown(as_it_was)) == 13
    assert rows_shown(rebuilt)[-1] == "human intervention other_category reject record_repo"
    assert len(rows_shown(rebuilt)) == 14


def test_an_id_picks_an_earlier_work_block_an_unknown_one_lists_the_latest_and_a_malformed_one_is_a_usage_error(
    eval_account: EvalAccount,
) -> None:
    run_a_work_block(eval_account)
    afternoon = BLOCK_START + timedelta(hours=5)
    assert run_goodall(["start"], afternoon).exit_code == 0
    assert run_goodall(["stop"], afternoon + timedelta(hours=1)).exit_code == 0
    work_block_ids = sorted(path.stem for path in (eval_account.record_repo / "work-blocks").glob("*.json"))
    assert len(work_block_ids) == 2

    latest = run_goodall(["timeline"], afternoon + timedelta(hours=2))
    morning = run_goodall(["timeline", work_block_ids[0]], afternoon + timedelta(hours=2))
    # Shaped like an id, as goodall fixblock checks it, but no work block has it.
    unknown = run_goodall(["timeline", "20261116T093000Z-ada-lovelace-00000000"], afternoon + timedelta(hours=2))
    not_an_id = run_goodall(["timeline", "SYNTHETIC-not-an-id"], afternoon + timedelta(hours=2))

    assert work_block_ids[1] in latest.standard_output
    assert rows_shown(latest) == []
    assert work_block_ids[0] in morning.standard_output
    assert len(rows_shown(morning)) == 13
    assert unknown.exit_code == 1
    assert f"goodall timeline {work_block_ids[1]}" in " ".join(unknown.standard_error.split())
    assert "00000000" not in unknown.everything_printed
    assert not_an_id.exit_code == 2
    assert "is not a work block id" in " ".join(not_an_id.standard_error.split())
    assert "SYNTHETIC" not in not_an_id.everything_printed


def test_with_no_ended_work_block_goodall_timeline_says_so_and_exits_1(eval_account: EvalAccount) -> None:
    timeline_run = run_goodall(["timeline"], BLOCK_START)

    assert timeline_run.exit_code == 1
    assert "No work block has ended yet" in " ".join(timeline_run.standard_error.split())

    as_json = run_goodall(["timeline", "--json"], BLOCK_START)

    assert as_json.exit_code == 1
    document = json.loads(as_json.standard_output)
    assert document["work_block"] is None
    assert [failure["kind"] for failure in document["failures"]] == ["timeline_has_no_work_block"]


def test_a_work_block_nobody_stopped_is_in_the_timeline_once_the_next_goodall_start_has_closed_it(
    eval_account: EvalAccount,
) -> None:
    assert run_goodall(["start"], BLOCK_START).exit_code == 0
    write_claude_session(eval_account, BLOCK_START + timedelta(minutes=5))

    next_morning = run_goodall(["start"], BLOCK_START + timedelta(hours=24))

    assert "was left running" in " ".join(next_morning.standard_output.split())
    assert len(everything_in(eval_account.timeline_file)["work_blocks"]) == 1
    document = timeline_as_json([], BLOCK_START + timedelta(hours=25))
    work_block = document["work_block"]
    assert isinstance(work_block, dict)
    assert work_block["end_state"] == "inferred"


def test_a_transcript_goodall_cannot_read_is_a_warning_at_stop_and_the_rest_is_still_in_the_timeline(
    eval_account: EvalAccount,
) -> None:
    assert run_goodall(["start"], BLOCK_START).exit_code == 0
    write_claude_session(eval_account, BLOCK_START + timedelta(minutes=5))
    write_record(eval_account, records.build_journal_entry("ada-lovelace", BLOCK_START + timedelta(minutes=20), "x"))
    # A copied transcript from an earlier work block that this account can no longer read.
    earlier_copy = eval_account.log_directory / "transcripts" / "-Users-ada-earlier"
    earlier_copy.mkdir(parents=True)
    unreadable = earlier_copy / "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3.jsonl"
    unreadable.write_text("{}\n")
    unreadable.chmod(0o000)
    try:
        stop = run_goodall(["stop"], BLOCK_END)
    finally:
        unreadable.chmod(0o600)

    assert stop.exit_code == 0
    everything = " ".join(stop.everything_printed.split())
    assert "The timeline was built, but not from everything." in everything
    assert "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3.jsonl: Permission denied." in everything
    assert "goodall timeline --rebuild" in everything
    assert len(rows_shown(run_goodall(["timeline"], BLOCK_END + timedelta(minutes=1)))) == 8
