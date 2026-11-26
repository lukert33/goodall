"""Outside-in tests for `goodall logs`, driven through `cli.main`.

Each test builds a pretend eval account in tmp_path (local settings, a record
repo holding an eval config and records, and a log directory holding raw
records), runs goodall the way a contributor would, and checks what that
person would observe: the exit code, what was printed, and whether any file
changed. Every transcript and telemetry line here is synthetic.

`goodall logs` is read-only, so no test here runs a program: `cli.main` is
given no runner for it, and the one test that checks nothing changed also
checks that no network connection was tried.
"""

import hashlib
import io
import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import cli, exit_codes, records, telemetry_receipts
from goodall.terminal import Terminal

EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "Building a SaaS product to first recurring revenue with AI."
workstreams = ["engineering", "research", "go-to-market"]
intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]
activitywatch = false

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""

MONDAY_MORNING = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
OTHER_CLAUDE_SESSION = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"
THIRD_CLAUDE_SESSION = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"

MACHINE = records.MachineFacts(
    hostname="eval-mac-01", macos_version="26.1", goodall_version="0.0.1", claude_code_version="2.3.4"
)


@dataclass
class EvalAccount:
    """A pretend eval account: its folders on disk."""

    record_repo: Path
    log_directory: Path
    work_blocks: list[records.WorkBlockRecord] = field(default_factory=list)

    def write_record(self, record: records.Record) -> Path:
        """Write a record into the record repo exactly where goodall's own writer would."""
        record_file = self.record_repo / records.relative_path_for(record)
        record_file.parent.mkdir(parents=True, exist_ok=True)
        record_file.write_text(records.render_record(record), encoding="utf-8")
        return record_file

    def add_work_block(
        self,
        started_at: datetime = MONDAY_MORNING,
        length: timedelta = timedelta(hours=3),
        workstream: str = "research",
        end_state: str | None = "clean",
        claude_sessions: tuple[str, ...] = (),
        telemetry: str | None = "captured",
        suffix: str = "0a1b2c3d",
    ) -> records.WorkBlockRecord:
        """Record one ended work block in the record repo and return it."""
        record = records.build_work_block_record(
            contributor="ada-lovelace",
            workstream=workstream,
            started_at=started_at,
            machine=MACHINE,
            ended_at=started_at + length if end_state is not None else None,
            end_state=end_state,
            claude_sessions=claude_sessions,
            suffix=suffix,
            telemetry=telemetry,
        )
        self.write_record(record)
        self.work_blocks.append(record)
        return record

    def add_transcript(self, claude_session: str, lines: list[str] | None = None) -> Path:
        """Copy a synthetic transcript into the log directory's mirror of Claude Code's projects folder."""
        if lines is None:
            lines = ['{"type":"user","n":1}', '{"type":"assistant","n":2}']
        transcript = self.log_directory / "transcripts" / "-Users-ada-project" / f"{claude_session}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return transcript

    def add_telemetry(self, work_block_id: str, bodies: list[str], receipts: bool = True) -> Path:
        """Write synthetic telemetry lines, with a receipt for each, as the receiver would."""
        telemetry_directory = self.log_directory / "telemetry" / work_block_id
        telemetry_directory.mkdir(parents=True, exist_ok=True)
        logs_file = telemetry_directory / "logs.jsonl"
        receipt_lines = []
        offset = 0
        text = ""
        for sequence, body in enumerate(bodies, start=1):
            body_bytes = body.encode("utf-8")
            receipt = telemetry_receipts.Receipt(
                sequence=sequence,
                received_at="2026-11-16T09:10:00.000000+00:00",
                file="logs.jsonl",
                offset=offset,
                body_length=len(body_bytes),
                body_sha256=hashlib.sha256(body_bytes).hexdigest(),
                line_break_replaced=False,
            )
            receipt_lines.append(telemetry_receipts.receipt_line(receipt).decode("utf-8"))
            text += body + "\n"
            offset += len(body_bytes) + 1
        logs_file.write_text(text, encoding="utf-8")
        if receipts:
            (telemetry_directory / "receipts.jsonl").write_text("\n".join(receipt_lines) + "\n", encoding="utf-8")
        return telemetry_directory

    def add_journal_entry(self, work_block_id: str | None, body: str = "# Chose Postgres\n\nBecause.") -> None:
        entry = records.build_journal_entry(
            contributor="ada-lovelace",
            written_at=MONDAY_MORNING + timedelta(minutes=30),
            work_block=work_block_id,
            body=body,
        )
        self.write_record(entry)

    def add_intervention(self, work_block_id: str | None) -> None:
        intervention = records.build_intervention(
            contributor="ada-lovelace",
            occurred_at=MONDAY_MORNING + timedelta(minutes=40),
            written_at=MONDAY_MORNING + timedelta(minutes=45),
            work_block=work_block_id,
            category="agent-limitation",
            decision="modify",
            body="The agent deleted an assertion; I told it to fix the parser instead.",
        )
        self.write_record(intervention)


@pytest.fixture
def eval_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[EvalAccount]:
    """An eval account whose local settings, eval config, record repo and log directory are all in order."""
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "evals" / "goodall-pilot-records"
    log_directory = tmp_path / "EvalLogs" / "goodall-pilot"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{record_repo}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    yield EvalAccount(record_repo, log_directory)
    monkeypatch.undo()
    time.tzset()


@dataclass
class GoodallRun:
    exit_code: int
    standard_output: str
    standard_error: str

    @property
    def everything_printed(self) -> str:
        """Both streams with every run of white space made one space, for phrase checks."""
        return " ".join((self.standard_output + "\n" + self.standard_error).split())


def run_goodall(arguments: list[str]) -> GoodallRun:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, clock=lambda: NOW)
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


# ---------------------------------------------------------------------------
# goodall logs: the list
# ---------------------------------------------------------------------------


def test_logs_lists_work_blocks_newest_first_with_what_raw_records_exist(eval_account: EvalAccount) -> None:
    older = eval_account.add_work_block(claude_sessions=(CLAUDE_SESSION,), suffix="0a1b2c3d")
    newer = eval_account.add_work_block(
        started_at=MONDAY_MORNING + timedelta(days=1),
        workstream="engineering",
        claude_sessions=(OTHER_CLAUDE_SESSION, THIRD_CLAUDE_SESSION),
        suffix="1b2c3d4e",
    )
    eval_account.add_transcript(CLAUDE_SESSION)
    eval_account.add_transcript(OTHER_CLAUDE_SESSION)
    eval_account.add_telemetry(older.id, ['{"a":1}', '{"a":2}', '{"a":3}'])
    eval_account.add_journal_entry(older.id)
    eval_account.add_intervention(older.id)

    run = run_goodall(["logs"])

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_error == ""
    assert run.standard_output.index(newer.id) < run.standard_output.index(older.id)
    assert "2 work blocks" in run.everything_printed
    assert "engineering" in run.everything_printed
    # The newer work block copied 1 of its 2 Claude sessions; the older 1 of 1.
    assert "1 of 2 Claude sessions copied" in run.everything_printed
    assert "1 of 1 Claude session copied" in run.everything_printed
    assert "3 telemetry lines" in run.everything_printed
    assert "1 journal entry" in run.everything_printed
    assert "1 intervention" in run.everything_printed


def test_logs_applies_work_block_fixes_and_flags_the_end_states(eval_account: EvalAccount) -> None:
    unfixed = eval_account.add_work_block(workstream="unknown", end_state="abnormal", suffix="0a1b2c3d")
    fixed = eval_account.add_work_block(
        started_at=MONDAY_MORNING + timedelta(days=1), workstream="unknown", end_state="abnormal", suffix="1b2c3d4e"
    )
    inferred = eval_account.add_work_block(
        started_at=MONDAY_MORNING + timedelta(days=3), end_state="inferred", suffix="2c3d4e5f"
    )
    eval_account.write_record(
        records.build_work_block_fix(
            contributor="ada-lovelace",
            written_at=MONDAY_MORNING + timedelta(days=2),
            work_block=fixed.id,
            workstream="engineering",
        )
    )

    run = run_goodall(["logs", "--json"])

    documents = {document["id"]: document for document in json.loads(run.standard_output)["work_blocks"]}
    assert documents[fixed.id]["workstream"] == "engineering"
    assert documents[fixed.id]["recorded_workstream"] == "unknown"
    assert documents[fixed.id]["end_state"] == "abnormal"
    assert documents[inferred.id]["end_state"] == "inferred"
    assert documents[unfixed.id]["workstream"] == "unknown"
    assert documents[unfixed.id]["end_state"] == "abnormal"

    text = run_goodall(["logs"]).everything_printed
    assert "engineering, by a fix; its record says unknown" in text
    assert "inferred from its last heartbeat" in text
    assert "abnormal" in text
    assert "unknown, so far" in text


def test_logs_reports_telemetry_gaps_and_repeated_requests_and_a_missing_receipt_log(
    eval_account: EvalAccount,
) -> None:
    gappy = eval_account.add_work_block(suffix="0a1b2c3d")
    repeated = eval_account.add_work_block(started_at=MONDAY_MORNING + timedelta(days=1), suffix="1b2c3d4e")
    collector = eval_account.add_work_block(started_at=MONDAY_MORNING + timedelta(days=2), suffix="2c3d4e5f")
    gappy_directory = eval_account.add_telemetry(gappy.id, ['{"a":1}', '{"a":2}', '{"a":3}'])
    # Remove the receipt for line 2, as a lost write would leave things.
    receipt_file = gappy_directory / "receipts.jsonl"
    receipt_lines = receipt_file.read_text().splitlines()
    receipt_file.write_text("\n".join([receipt_lines[0], receipt_lines[2]]) + "\n")
    eval_account.add_telemetry(repeated.id, ['{"a":1}', '{"a":1}'])
    eval_account.add_telemetry(collector.id, ['{"a":1}'], receipts=False)

    run = run_goodall(["logs", "--json"])

    documents = {document["id"]: document for document in json.loads(run.standard_output)["work_blocks"]}
    gappy_telemetry = documents[gappy.id]["counts"]["telemetry"]
    assert gappy_telemetry["line_count"] == 3
    assert gappy_telemetry["receipt_problems"]
    assert documents[repeated.id]["counts"]["telemetry"]["repeated_requests"] == 1
    assert documents[collector.id]["counts"]["telemetry"]["has_receipt_log"] is False
    assert documents[collector.id]["counts"]["telemetry"]["receipt_problems"] == []

    text = run_goodall(["logs"]).everything_printed
    assert "against the receipts" in text
    assert "1 repeated request" in text
    assert "no receipt log to check them against" in text


def test_logs_flags_a_record_file_it_cannot_parse_and_still_lists_the_rest(eval_account: EvalAccount) -> None:
    good = eval_account.add_work_block()
    broken_file = eval_account.record_repo / "work-blocks" / "20261117T090000Z-ada-lovelace-ffffffff.json"
    broken_file.write_text("{ this is not json")

    run = run_goodall(["logs"])

    assert run.exit_code == exit_codes.SUCCESS
    assert good.id in run.standard_output
    assert "WARN" in run.standard_error
    assert str(broken_file) in run.standard_error
    assert "Next" in run.standard_error

    json_run = run_goodall(["logs", "--json"])
    document = json.loads(json_run.standard_output)
    assert [work_block["id"] for work_block in document["work_blocks"]] == [good.id]
    assert document["warnings"][0]["path"] == str(broken_file)
    assert document["warnings"][0]["usable"] is False


def test_logs_shows_a_running_work_block_and_one_waiting_for_the_record_repo_without_settling_either(
    eval_account: EvalAccount,
) -> None:
    waiting = records.build_work_block_record(
        contributor="ada-lovelace",
        workstream="research",
        started_at=MONDAY_MORNING,
        machine=MACHINE,
        ended_at=MONDAY_MORNING + timedelta(hours=1),
        end_state="clean",
        suffix="0a1b2c3d",
    )
    running = records.build_work_block_record(
        contributor="ada-lovelace",
        workstream="engineering",
        started_at=NOW - timedelta(days=1),
        machine=MACHINE,
        suffix="1b2c3d4e",
    )
    work_block_folder = eval_account.log_directory / "work-blocks"
    (work_block_folder / "unrecorded").mkdir(parents=True)
    (work_block_folder / "unrecorded" / f"{waiting.id}.json").write_text(records.render_record(waiting))
    (work_block_folder / "active-work-block.json").write_text(records.render_record(running))
    (work_block_folder / "heartbeats").mkdir()
    (work_block_folder / "heartbeats" / f"{running.id}.jsonl").write_text(
        json.dumps({"at": "2026-11-17T12:00:00Z", "claude_session": CLAUDE_SESSION}) + "\n"
    )
    before = snapshot(eval_account)

    run = run_goodall(["logs", "--json"])

    documents = {document["id"]: document for document in json.loads(run.standard_output)["work_blocks"]}
    assert documents[waiting.id]["state"] == "waiting"
    # Last heartbeat was 24 hours before NOW, so it is overdue but not closed.
    assert documents[running.id]["state"] == "overdue"
    assert documents[running.id]["claude_sessions"] == [CLAUDE_SESSION]
    assert snapshot(eval_account) == before
    assert not (eval_account.record_repo / "work-blocks").exists()


def test_logs_with_no_work_blocks_says_how_to_begin_one(eval_account: EvalAccount) -> None:
    run = run_goodall(["logs"])

    assert run.exit_code == exit_codes.SUCCESS
    assert "no work blocks yet" in run.everything_printed
    assert "goodall start" in run.everything_printed


def test_logs_counts_activitywatch_copies_for_the_whole_eval(eval_account: EvalAccount) -> None:
    eval_account.add_work_block()
    (eval_account.log_directory / "activitywatch" / "20261116T120000Z").mkdir(parents=True)
    (eval_account.log_directory / "activitywatch" / "20261117T120000Z").mkdir(parents=True)

    document = json.loads(run_goodall(["logs", "--json"]).standard_output)
    text = run_goodall(["logs"]).everything_printed

    assert document["activitywatch"]["copies"] == 2
    assert "ActivityWatch: 2 copies" in text


# ---------------------------------------------------------------------------
# goodall logs <work block id>
# ---------------------------------------------------------------------------


def test_logs_for_one_work_block_shows_its_notes_and_the_paths_of_its_raw_records_flagged(
    eval_account: EvalAccount,
) -> None:
    work_block = eval_account.add_work_block(
        claude_sessions=(CLAUDE_SESSION, OTHER_CLAUDE_SESSION, THIRD_CLAUDE_SESSION)
    )
    readable = eval_account.add_transcript(CLAUDE_SESSION)
    unparseable = eval_account.add_transcript(
        OTHER_CLAUDE_SESSION, ['{"type":"user"}', "SECRET-LOOKING TEXT {", '{"n":3}']
    )
    telemetry_directory = eval_account.add_telemetry(work_block.id, ['{"a":1}', '{"a":2}'])
    eval_account.add_journal_entry(work_block.id, body="# Chose Postgres over SQLite\n\nBecause two workers write.")
    eval_account.add_intervention(work_block.id)

    run = run_goodall(["logs", work_block.id])

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_error == ""
    printed = run.everything_printed
    assert work_block.id in printed
    assert f"{readable} [readable, 2 lines]" in printed
    assert f"{unparseable} [unparseable: 1 line is not valid JSON, the first is line 2.]" in printed
    assert f"Claude session {THIRD_CLAUDE_SESSION}: no copy in the log directory" in printed
    assert f"{telemetry_directory / 'logs.jsonl'} [readable, 2 lines]" in printed
    assert "Chose Postgres over SQLite" in printed
    assert "agent-limitation, modify" in printed
    assert "goodall logs --raw" in printed
    # Raw content is never printed by the detail view.
    assert "SECRET-LOOKING" not in printed
    assert '"a":1' not in printed


def test_logs_json_for_one_work_block_has_a_stable_shape(eval_account: EvalAccount) -> None:
    work_block = eval_account.add_work_block(claude_sessions=(CLAUDE_SESSION,))
    transcript = eval_account.add_transcript(CLAUDE_SESSION)
    eval_account.add_telemetry(work_block.id, ['{"a":1}'])
    eval_account.add_journal_entry(work_block.id)
    eval_account.add_intervention(work_block.id)
    eval_account.add_journal_entry(None, body="Not about any work block.")

    run = run_goodall(["logs", work_block.id, "--json"])

    document = json.loads(run.standard_output)
    assert run.exit_code == exit_codes.SUCCESS
    assert set(document) == {
        "eval",
        "log_directory",
        "record_repo",
        "work_blocks",
        "activitywatch",
        "work_block",
        "fixes",
        "journal_entries",
        "interventions",
        "raw_records",
        "warnings",
        "failures",
    }
    assert document["work_block"]["id"] == work_block.id
    assert document["work_block"]["state"] == "recorded"
    assert len(document["journal_entries"]) == 1
    assert len(document["interventions"]) == 1
    [session] = document["raw_records"]["transcripts"]
    assert session["claude_session"] == CLAUDE_SESSION
    assert session["copies"] == [{"path": str(transcript), "status": "readable", "problem": None, "line_count": 2}]
    assert document["raw_records"]["telemetry"]["files"][0]["status"] == "readable"
    assert document["failures"] == []


def test_logs_for_a_work_block_that_does_not_exist_exits_1_and_says_where_to_look(eval_account: EvalAccount) -> None:
    eval_account.add_work_block()

    run = run_goodall(["logs", records.EXAMPLE_RECORD_ID])

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_output == ""
    assert "FAIL" in run.standard_error
    assert "Next" in run.standard_error
    assert "goodall logs" in run.standard_error


def test_logs_for_something_that_is_not_a_work_block_id_exits_2_and_does_not_repeat_it(
    eval_account: EvalAccount,
) -> None:
    run = run_goodall(["logs", "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC"])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert "SYNTHETIC" not in run.everything_printed
    assert "Next" in run.standard_error


# ---------------------------------------------------------------------------
# goodall logs --raw
# ---------------------------------------------------------------------------


def run_raw(arguments: list[str]) -> tuple[int, bytes, str]:
    """Run goodall with a standard output that keeps bytes, to compare a raw file byte for byte."""
    output_bytes = io.BytesIO()
    standard_output = io.TextIOWrapper(output_bytes, encoding="utf-8", newline="")
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, clock=lambda: NOW)
    standard_output.flush()
    return exit_code, output_bytes.getvalue(), standard_error.getvalue()


def test_raw_prints_a_file_in_the_log_directory_byte_for_byte_by_absolute_or_relative_path(
    eval_account: EvalAccount,
) -> None:
    eval_account.add_work_block()
    transcript = eval_account.log_directory / "transcripts" / "-Users-ada-project" / f"{CLAUDE_SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    # CRLF, a lone carriage return, bytes that are not UTF-8, and no final newline: all must survive.
    original = b'{"a":"caf\xc3\xa9"}\r\n{"b":1}\r{"c":\xff\xfe}'
    transcript.write_bytes(original)

    absolute_code, absolute_output, absolute_error = run_raw(["logs", "--raw", str(transcript)])
    relative_code, relative_output, _ = run_raw(
        ["logs", "--raw", f"transcripts/-Users-ada-project/{CLAUDE_SESSION}.jsonl"]
    )

    assert (absolute_code, absolute_error) == (exit_codes.SUCCESS, "")
    assert absolute_output == original
    assert relative_code == exit_codes.SUCCESS
    assert relative_output == original


def test_raw_refuses_a_path_outside_the_log_directory_and_does_not_repeat_it(
    eval_account: EvalAccount, tmp_path: Path
) -> None:
    outside = tmp_path / "SYNTHETIC-outside.txt"
    outside.write_text("not a raw record")
    link = eval_account.log_directory / "transcripts-link"
    link.symlink_to(outside)

    for typed_path in [str(outside), "../SYNTHETIC-outside.txt", str(link)]:
        exit_code, output, standard_error = run_raw(["logs", "--raw", typed_path])

        assert exit_code == exit_codes.USAGE_ERROR
        assert output == b""
        assert "SYNTHETIC" not in standard_error
        assert "Next" in standard_error


def test_raw_of_a_missing_file_exits_1_with_a_next_step(eval_account: EvalAccount) -> None:
    exit_code, output, standard_error = run_raw(["logs", "--raw", "transcripts/nothing-here.jsonl"])

    assert exit_code == exit_codes.ERROR
    assert output == b""
    assert "Next" in standard_error
    assert "nothing-here" not in standard_error


def test_raw_cannot_be_combined_with_a_work_block_id_or_json(eval_account: EvalAccount) -> None:
    combined_with_id = run_goodall(["logs", records.EXAMPLE_RECORD_ID, "--raw", "x"])
    combined_with_json = run_goodall(["logs", "--raw", "x", "--json"])

    assert combined_with_id.exit_code == exit_codes.USAGE_ERROR
    assert combined_with_json.exit_code == exit_codes.USAGE_ERROR
    assert "Next" in combined_with_json.standard_error


# ---------------------------------------------------------------------------
# Failures before anything can be shown
# ---------------------------------------------------------------------------


def test_logs_without_local_settings_fails_with_a_next_step_in_text_and_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_config = tmp_path / "empty-config"
    empty_config.mkdir()
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(empty_config))

    text_run = run_goodall(["logs"])
    json_run = run_goodall(["logs", "--json"])

    assert text_run.exit_code == exit_codes.ERROR
    assert text_run.standard_output == ""
    assert "Next" in text_run.standard_error
    document = json.loads(json_run.standard_output)
    assert json_run.exit_code == exit_codes.ERROR
    assert document["eval"] is None
    assert document["failures"][0]["what_to_do_next"]
    assert list(empty_config.iterdir()) == []


def test_logs_with_an_unplugged_log_directory_says_to_connect_it(eval_account: EvalAccount) -> None:
    eval_account.add_work_block()
    eval_account.log_directory.rmdir()

    run = run_goodall(["logs"])

    assert run.exit_code == exit_codes.ERROR
    assert "log directory" in run.everything_printed
    assert "connect it" in run.everything_printed


def test_logs_browses_a_log_directory_that_cannot_be_written_to(eval_account: EvalAccount) -> None:
    work_block = eval_account.add_work_block(claude_sessions=(CLAUDE_SESSION,))
    eval_account.add_transcript(CLAUDE_SESSION)
    folders = [eval_account.log_directory]
    for path in eval_account.log_directory.rglob("*"):
        if path.is_dir():
            folders.append(path)
    for folder in folders:
        folder.chmod(0o500)
    try:
        run = run_goodall(["logs", work_block.id])
    finally:
        for folder in folders:
            folder.chmod(0o700)

    assert run.exit_code == exit_codes.SUCCESS
    assert "readable" in run.everything_printed


# ---------------------------------------------------------------------------
# Read-only: nothing changes, nothing is run, nothing leaves the Mac
# ---------------------------------------------------------------------------


def snapshot(eval_account: EvalAccount) -> dict[str, tuple[str, int, int, str]]:
    """Every file and folder under the log directory and the record repo: kind, size, change time, content hash."""
    state: dict[str, tuple[str, int, int, str]] = {}
    for root in (eval_account.log_directory, eval_account.record_repo):
        for path in [root, *sorted(root.rglob("*"))]:
            path_status = path.lstat()
            kind = "other"
            digest = ""
            if path.is_file() and not path.is_symlink():
                kind = "file"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state[str(path)] = (kind, path_status.st_size, path_status.st_mtime_ns, digest)
    return state


def test_logs_never_changes_the_log_directory_or_the_record_repo_and_never_touches_the_network(
    eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_block = eval_account.add_work_block(claude_sessions=(CLAUDE_SESSION,))
    eval_account.add_transcript(CLAUDE_SESSION)
    eval_account.add_telemetry(work_block.id, ['{"a":1}', '{"a":2}'])
    eval_account.add_journal_entry(work_block.id)
    eval_account.add_intervention(work_block.id)
    (eval_account.record_repo / "work-blocks" / "20261117T090000Z-ada-lovelace-ffffffff.json").write_text("{ broken")
    (eval_account.log_directory / "activitywatch" / "20261116T120000Z").mkdir(parents=True)
    before = snapshot(eval_account)

    def refuse_network(*arguments: object, **keyword_arguments: object) -> None:
        raise AssertionError("goodall logs tried to use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    transcript = f"transcripts/-Users-ada-project/{CLAUDE_SESSION}.jsonl"
    for arguments in (
        ["logs"],
        ["logs", "--json"],
        ["logs", work_block.id],
        ["logs", work_block.id, "--json"],
        ["logs", "--raw", transcript],
    ):
        exit_code, _, _ = run_raw(arguments)
        assert exit_code == exit_codes.SUCCESS

    assert snapshot(eval_account) == before


def test_logs_runs_no_program(eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_account.add_work_block()

    def refuse_program(*arguments: object, **keyword_arguments: object) -> None:
        raise AssertionError("goodall logs tried to run a program")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse_program)

    assert run_goodall(["logs"]).exit_code == exit_codes.SUCCESS


def test_logs_leaves_the_settings_folder_alone(eval_account: EvalAccount) -> None:
    config_directory = Path(os.environ["GOODALL_CONFIG_DIR"])
    before = sorted(path.name for path in config_directory.iterdir())

    run_goodall(["logs"])

    assert sorted(path.name for path in config_directory.iterdir()) == before
