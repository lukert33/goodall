"""Outside-in tests for `goodall export scout`, driven through `cli.main`.

Each test builds a pretend eval inside tmp_path: local settings, a record
repo holding work block records, journal entries and interventions, and a
log directory whose `transcripts/` folder holds hand-written Claude Code
transcripts (synthetic: a real transcript holds a person's private prompts
and is never copied into a test). It then runs `goodall export scout` the way
a researcher would and opens the database with Inspect Scout itself, as an
analyst would, to check what can be found in it.

Most tests need the optional `scout` extra (inspect-ai and inspect-scout) and
skip cleanly when it is not installed. The test that the export explains how
to install the extra hides the packages instead, so it runs either way.
"""

import asyncio
import importlib.util
import io
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, records
from goodall.actor_labels import ACTOR_LABEL_RULES_VERSION
from goodall.fake_runner import FakeRunner
from goodall.human_actions import RULES_VERSION as HUMAN_ACTION_RULES_VERSION
from goodall.scout_additions import EXPORT_RULES_VERSION
from goodall.terminal import Terminal

EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "A test eval."
workstreams = ["engineering", "research"]
intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""

# Claude session ids, in the form Claude Code writes them.
WORKING_SESSION = "11111111-2222-4333-8444-555555555555"
REFUSED_ONLY_SESSION = "22222222-3333-4444-8555-666666666666"
SUPERSEDED_SESSION = "33333333-4444-4555-8666-777777777777"

PROJECT_FOLDER = "-work-example"
MACHINE = records.MachineFacts(
    hostname="example-mac", macos_version="26.1", goodall_version="0.0.1", claude_code_version="2.1.326"
)


def moment(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, second, tzinfo=UTC)


def entry(session_id: str, **fields: Any) -> dict[str, Any]:
    """One transcript entry with the fields every Claude Code entry carries."""
    base: dict[str, Any] = {
        "sessionId": session_id,
        "isSidechain": False,
        "cwd": "/work/example",
        "version": "2.1.326",
        "gitBranch": "main",
        "userType": "external",
    }
    base.update(fields)
    return base


def assistant_message(message_id: str, content: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": "example-model",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


REJECTION_TEXT = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
    "the new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how "
    "to proceed."
)


def working_session_entries() -> list[dict[str, Any]]:
    """A Claude session in which the person typed a prompt, approved one tool call and rejected another."""
    session = WORKING_SESSION
    return [
        entry(
            session,
            type="user",
            uuid="u-1",
            parentUuid=None,
            timestamp="2026-11-16T09:31:00.000Z",
            origin={"kind": "human"},
            permissionMode="default",
            message={"role": "user", "content": "Build the example."},
        ),
        entry(
            session,
            type="assistant",
            uuid="a-2",
            parentUuid="u-1",
            timestamp="2026-11-16T09:31:05.000Z",
            message=assistant_message(
                "msg_1",
                [
                    {"type": "text", "text": "Running the example build."},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "make example"}},
                ],
                "tool_use",
            ),
        ),
        entry(
            session,
            type="user",
            uuid="u-3",
            parentUuid="a-2",
            timestamp="2026-11-16T09:31:09.000Z",
            permissionDecision={"decision": "accept", "source": "user_temporary", "grantScope": "once"},
            toolUseResult={"stdout": "Example build finished.", "stderr": "", "interrupted": False},
            message={
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Example build finished."}],
            },
        ),
        entry(
            session,
            type="assistant",
            uuid="a-4",
            parentUuid="u-3",
            timestamp="2026-11-16T09:32:00.000Z",
            message=assistant_message(
                "msg_2",
                [{"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "rm -f example"}}],
                "tool_use",
            ),
        ),
        entry(
            session,
            type="user",
            uuid="u-5",
            parentUuid="a-4",
            timestamp="2026-11-16T09:32:04.000Z",
            toolUseResult="User rejected tool use",
            message={
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_2", "content": REJECTION_TEXT, "is_error": True}
                ],
            },
        ),
        entry(
            session,
            type="assistant",
            uuid="a-6",
            parentUuid="u-5",
            timestamp="2026-11-16T09:32:10.000Z",
            message=assistant_message("msg_3", [{"type": "text", "text": "Stopped as asked."}], "end_turn"),
        ),
    ]


def refused_only_session_entries() -> list[dict[str, Any]]:
    """A Claude session whose only prompt the prompt gate refused, which Scout's importer skips."""
    session = REFUSED_ONLY_SESSION
    return [
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-11-16T08:00:00.000Z",
            "sessionId": session,
            "content": "Start the example task.",
        },
        entry(
            session,
            type="system",
            uuid="s-2",
            parentUuid=None,
            timestamp="2026-11-16T08:00:00.200Z",
            subtype="informational",
            level="warning",
            content="UserPromptSubmit operation blocked by hook: [goodall]: No work block is active. Start one first.",
        ),
    ]


def write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for one_entry in entries:
        lines.append(json.dumps(one_entry) + "\n")
    path.write_text("".join(lines))


def write_record(record_repo: Path, record: records.Record) -> None:
    path = record_repo / records.relative_path_for(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(records.render_record(record))


@dataclass
class PretendEval:
    """The folders of a pretend eval, and the ids of the records written into it."""

    record_repo: Path
    log_directory: Path
    research_block: str
    engineering_block: str
    intervention_in_session: str
    intervention_outside_work_blocks: str

    @property
    def default_database(self) -> Path:
        return self.log_directory / "exports" / "scout"


@pytest.fixture
def pretend_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PretendEval:
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "evals" / "goodall-pilot-records"
    log_directory = tmp_path / "EvalLogs" / "goodall-pilot"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        'record_repo = "~/evals/goodall-pilot-records"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)

    research_block = records.build_work_block_record(
        "ada-lovelace",
        "research",
        started_at=moment(9, 30),
        machine=MACHINE,
        ended_at=moment(10, 30),
        end_state="clean",
        claude_sessions=[WORKING_SESSION],
        suffix="0000000a",
    )
    engineering_block = records.build_work_block_record(
        "ada-lovelace",
        "engineering",
        started_at=moment(14, 0),
        machine=MACHINE,
        ended_at=moment(15, 0),
        end_state="clean",
        suffix="0000000b",
    )
    write_record(record_repo, research_block)
    write_record(record_repo, engineering_block)
    write_record(
        record_repo,
        records.build_journal_entry(
            "ada-lovelace", moment(10, 0), "Chose the example design; I decided.", research_block.id, "0000000c"
        ),
    )
    write_record(
        record_repo,
        records.build_journal_entry(
            "ada-lovelace", moment(14, 30), "Wrote the example release notes myself.", engineering_block.id, "0000000d"
        ),
    )
    intervention_in_session = records.build_intervention(
        "ada-lovelace",
        occurred_at=moment(9, 32, 30),
        written_at=moment(9, 40),
        category="agent-limitation",
        decision="modify",
        body="It tried to delete the example; I told it to keep it.",
        work_block=research_block.id,
        claude_session=WORKING_SESSION,
        suffix="0000000e",
    )
    intervention_outside_work_blocks = records.build_intervention(
        "ada-lovelace",
        occurred_at=moment(18, 0),
        written_at=moment(18, 5),
        category="infrastructure",
        decision="escalate",
        body="Fixed the example build machine by hand.",
        suffix="0000000f",
    )
    write_record(record_repo, intervention_in_session)
    write_record(record_repo, intervention_outside_work_blocks)

    transcripts = log_directory / "transcripts" / PROJECT_FOLDER
    write_jsonl(transcripts / f"{WORKING_SESSION}.jsonl", working_session_entries())
    write_jsonl(transcripts / f"{REFUSED_ONLY_SESSION}.jsonl", refused_only_session_entries())
    # An earlier copy kept aside by transcript sync. Importing it would add a
    # session that never happened, so the export must not look here.
    superseded_entries = []
    for one_entry in working_session_entries():
        one_entry["sessionId"] = SUPERSEDED_SESSION
        superseded_entries.append(one_entry)
    superseded = log_directory / "superseded-transcripts" / "20261116T120000Z" / PROJECT_FOLDER
    write_jsonl(superseded / f"{SUPERSEDED_SESSION}.jsonl", superseded_entries)

    return PretendEval(
        record_repo=record_repo,
        log_directory=log_directory,
        research_block=research_block.id,
        engineering_block=engineering_block.id,
        intervention_in_session=intervention_in_session.id,
        intervention_outside_work_blocks=intervention_outside_work_blocks.id,
    )


@dataclass
class GoodallRun:
    exit_code: int
    standard_output: str
    standard_error: str


def run_goodall(arguments: list[str]) -> GoodallRun:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error, standard_input=io.StringIO())
    exit_code = cli.main(arguments, terminal=terminal, runner=FakeRunner())
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


def read_transcripts(database: Path, condition: Any = None) -> dict[str, Any]:
    """Open the database with Scout and return every transcript matching `condition`, by id."""
    from inspect_scout import TranscriptContent, transcripts_from

    async def read_all() -> dict[str, Any]:
        transcripts = transcripts_from(str(database))
        if condition is not None:
            transcripts = transcripts.where(condition)
        found: dict[str, Any] = {}
        async with transcripts.reader() as reader:
            async for info in reader.index():
                found[info.transcript_id] = await reader.read(info, TranscriptContent(messages="all", events="all"))
        return found

    return asyncio.run(read_all())


def events_of_type(transcript: Any, event_type: str) -> list[Any]:
    matching = []
    for event in transcript.events:
        if event.event == event_type:
            matching.append(event)
    return matching


def work_block_transcript_id(work_block_id: str) -> str:
    return f"goodall-work-block-{work_block_id}"


needs_scout = pytest.mark.skipif(
    importlib.util.find_spec("inspect_scout") is None,
    reason="needs the optional scout extra: uv sync --extra scout",
)


@needs_scout
def test_export_makes_a_database_scout_can_filter_by_work_block(pretend_eval: PretendEval) -> None:
    run = run_goodall(["export", "scout"])

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    from inspect_scout import columns

    in_research_block = read_transcripts(
        pretend_eval.default_database, columns.goodall_work_block == pretend_eval.research_block
    )
    assert sorted(in_research_block) == sorted([WORKING_SESSION, work_block_transcript_id(pretend_eval.research_block)])
    in_engineering_block = read_transcripts(
        pretend_eval.default_database, columns.goodall_work_block == pretend_eval.engineering_block
    )
    assert sorted(in_engineering_block) == [work_block_transcript_id(pretend_eval.engineering_block)]


@needs_scout
def test_export_can_be_filtered_by_actor_label_and_intervention_category(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])
    from inspect_scout import columns

    with_human_entries = read_transcripts(pretend_eval.default_database, columns.goodall_human_entries > 0)
    assert list(with_human_entries) == [WORKING_SESSION]
    working = with_human_entries[WORKING_SESSION]
    assert working.metadata["goodall_ai_entries"] == 3
    assert working.metadata["goodall_actor_label_rules_version"] == ACTOR_LABEL_RULES_VERSION
    assert working.metadata["goodall_human_action_rules_version"] == HUMAN_ACTION_RULES_VERSION

    agent_limitation = read_transcripts(
        pretend_eval.default_database, columns.goodall_intervention_categories.like('%"agent-limitation"%')
    )
    assert sorted(agent_limitation) == sorted([WORKING_SESSION, work_block_transcript_id(pretend_eval.research_block)])
    infrastructure = read_transcripts(
        pretend_eval.default_database, columns.goodall_intervention_categories.like('%"infrastructure"%')
    )
    assert list(infrastructure) == ["goodall-outside-work-blocks"]


@needs_scout
def test_approvals_and_rejections_become_approval_events_with_the_tool_call(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])

    working = read_transcripts(pretend_eval.default_database)[WORKING_SESSION]
    approvals = events_of_type(working, "approval")
    decisions = []
    for approval in approvals:
        decisions.append((approval.decision, approval.call.id, approval.call.function, approval.call.arguments))
    assert decisions == [
        ("approve", "toolu_1", "Bash", {"command": "make example"}),
        ("reject", "toolu_2", "Bash", {"command": "rm -f example"}),
    ]
    for approval in approvals:
        assert approval.approver == "human"
        assert approval.metadata["goodall_actor_label"] == "human"
    # Each approval sits just before the tool call it decided, in the same span.
    for approval in approvals:
        position = working.events.index(approval)
        following = working.events[position + 1]
        assert following.event == "tool"
        assert following.id == approval.call.id
        assert approval.span_id == following.span_id


@needs_scout
def test_journal_entries_and_interventions_become_info_events_not_approvals(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])

    transcripts = read_transcripts(pretend_eval.default_database)
    research = transcripts[work_block_transcript_id(pretend_eval.research_block)]
    assert events_of_type(research, "approval") == []
    sources = []
    for info in events_of_type(research, "info"):
        sources.append(info.source)
    # In time order: the block started at 09:30, the intervention happened at
    # 09:32 and the journal entry was written at 10:00.
    assert sources == ["goodall.work_block", "goodall.intervention", "goodall.journal_entry"]
    assert research.metadata["goodall_workstream"] == "research"

    working = transcripts[WORKING_SESSION]
    interventions = []
    for info in events_of_type(working, "info"):
        if info.source == "goodall.intervention":
            interventions.append(info.data)
    assert len(interventions) == 1
    assert interventions[0]["id"] == pretend_eval.intervention_in_session
    assert interventions[0]["category"] == "agent-limitation"
    assert interventions[0]["decision"] == "modify"


@needs_scout
def test_prompts_refused_by_the_prompt_gate_are_carried_from_goodalls_side(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])

    transcripts = read_transcripts(pretend_eval.default_database)
    # Scout's importer skips a session whose only prompt was refused.
    assert REFUSED_ONLY_SESSION not in transcripts
    outside = transcripts["goodall-outside-work-blocks"]
    refused = []
    for info in events_of_type(outside, "info"):
        if info.source == "goodall.human_action" and info.data["kind"] == "blocked_prompt":
            refused.append(info.data)
    assert len(refused) == 1
    assert refused[0]["session_id"] == REFUSED_ONLY_SESSION
    assert refused[0]["text"] == "Start the example task."


@needs_scout
def test_superseded_transcripts_are_never_imported(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])

    transcripts = read_transcripts(pretend_eval.default_database)
    source_ids = set()
    for transcript in transcripts.values():
        source_ids.add(transcript.source_id)
    assert SUPERSEDED_SESSION not in source_ids


@needs_scout
def test_exporting_again_replaces_the_database_with_the_same_result(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])
    first = read_transcripts(pretend_eval.default_database)

    second_run = run_goodall(["export", "scout"])

    assert second_run.exit_code == exit_codes.SUCCESS, second_run.standard_error
    second = read_transcripts(pretend_eval.default_database)
    assert sorted(second) == sorted(first)
    first_uuids = []
    for event in first[WORKING_SESSION].events:
        if event.event in ("approval", "info"):
            first_uuids.append(event.uuid)
    second_uuids = []
    for event in second[WORKING_SESSION].events:
        if event.event in ("approval", "info"):
            second_uuids.append(event.uuid)
    assert first_uuids == second_uuids


@needs_scout
def test_export_json_has_a_stable_shape(pretend_eval: PretendEval, tmp_path: Path) -> None:
    database = tmp_path / "analysis" / "scout-db"

    run = run_goodall(["export", "scout", "--output", str(database), "--json"])

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    document = json.loads(run.standard_output)
    assert sorted(document) == [
        "database",
        "events_added",
        "exported",
        "failures",
        "records",
        "rules_versions",
        "transcripts",
        "versions",
        "warnings",
    ]
    assert document["exported"] is True
    assert document["database"] == str(database)
    assert document["transcripts"] == {"claude_code": 1, "work_blocks": 2, "outside_work_blocks": 1}
    assert document["records"] == {"work_blocks": 2, "journal_entries": 2, "interventions": 2}
    assert document["events_added"]["approval"] == 2
    assert document["rules_versions"] == {
        "actor_labels": ACTOR_LABEL_RULES_VERSION,
        "human_actions": HUMAN_ACTION_RULES_VERSION,
        "export": EXPORT_RULES_VERSION,
    }
    assert document["warnings"] == []
    assert document["failures"] == []
    assert sorted(read_transcripts(database)) == sorted(
        [
            WORKING_SESSION,
            work_block_transcript_id(pretend_eval.research_block),
            work_block_transcript_id(pretend_eval.engineering_block),
            "goodall-outside-work-blocks",
        ]
    )


@needs_scout
def test_export_says_where_the_database_is_and_how_to_open_it(pretend_eval: PretendEval) -> None:
    run = run_goodall(["export", "scout"])

    assert run.exit_code == exit_codes.SUCCESS
    assert str(pretend_eval.default_database) in run.standard_output
    assert f"scout view -T {pretend_eval.default_database}" in run.standard_output


@needs_scout
def test_export_refuses_to_replace_a_folder_it_did_not_make(pretend_eval: PretendEval, tmp_path: Path) -> None:
    someone_elses = tmp_path / "notes"
    someone_elses.mkdir()
    (someone_elses / "keep-me.txt").write_text("Not goodall's.\n")

    run = run_goodall(["export", "scout", "--output", str(someone_elses), "--json"])

    assert run.exit_code == exit_codes.ERROR
    document = json.loads(run.standard_output)
    assert document["exported"] is False
    assert document["failures"][0]["kind"] == "export_folder_not_goodalls"
    assert sorted(path.name for path in someone_elses.iterdir()) == ["keep-me.txt"]


def test_export_refuses_to_write_inside_the_raw_transcripts(pretend_eval: PretendEval) -> None:
    inside = pretend_eval.log_directory / "transcripts" / "scout"

    run = run_goodall(["export", "scout", "--output", str(inside), "--json"])

    assert run.exit_code == exit_codes.ERROR
    document = json.loads(run.standard_output)
    assert document["failures"][0]["kind"] == "export_folder_inside_raw_records"
    assert not inside.exists()


def test_export_without_the_scout_extra_says_how_to_install_it(
    pretend_eval: PretendEval, monkeypatch: pytest.MonkeyPatch
) -> None:
    # None in sys.modules makes `import inspect_scout` fail as if the package
    # were not installed, whether or not it is.
    monkeypatch.setitem(sys.modules, "inspect_scout", None)
    monkeypatch.setitem(sys.modules, "inspect_ai", None)
    monkeypatch.delitem(sys.modules, "goodall.scout_database", raising=False)

    run = run_goodall(["export", "scout", "--json"])

    assert run.exit_code == exit_codes.ERROR
    document = json.loads(run.standard_output)
    assert document["exported"] is False
    failure = document["failures"][0]
    assert failure["kind"] == "scout_extra_missing"
    assert "uv sync --extra scout" in failure["commands_to_try"]
    assert not pretend_eval.default_database.exists()


@needs_scout
def test_scouts_own_validator_accepts_the_exported_database(pretend_eval: PretendEval) -> None:
    run_goodall(["export", "scout"])

    # Scout's command-line tool, run as a person would, beside this Python.
    scout = Path(sys.executable).parent / "scout"
    completed = subprocess.run(
        [str(scout), "db", "validate", str(pretend_eval.default_database)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Schema validation passed." in completed.stdout
