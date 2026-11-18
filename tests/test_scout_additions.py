"""Unit tests for goodall.scout_additions: what goodall adds to the Scout export, as plain values.

These need no Inspect package: the module describes events and metadata in
Inspect's words without importing it. Transcripts are the hand-written
fixtures in tests/fixtures/, copied into a pretend log directory laid out as
transcript sync lays it out.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from goodall import records
from goodall.scout_additions import AddedEvent, gather_additions

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_transcripts"
SESSION = "11111111-2222-4333-8444-555555555555"
MACHINE = records.MachineFacts("example-mac", "26.1", "0.0.1", None)


def copy_fixture_as_session(fixture_name: str, log_directory: Path, session_id: str = SESSION) -> None:
    project_folder = log_directory / "transcripts" / "-work-example"
    project_folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture_name, project_folder / f"{session_id}.jsonl")


def write_record(record_repo: Path, record: records.Record) -> None:
    path = record_repo / records.relative_path_for(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(records.render_record(record))


def kinds_and_types(events: list[AddedEvent]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for event in events:
        found.append((event.metadata["goodall_kind"], event.event_type))
    return found


def test_each_kind_of_human_action_becomes_the_nearest_inspect_event(tmp_path: Path) -> None:
    copy_fixture_as_session("questions_and_plans.jsonl", tmp_path / "logs")

    additions = gather_additions(tmp_path / "records", tmp_path / "logs", "goodall-pilot")

    events = additions.claude_sessions[SESSION].human_action_events
    assert kinds_and_types(events) == [
        ("question_answer", "input"),
        ("tool_call_rejection", "approval"),
        ("plan_approval", "approval"),
        ("tool_call_rejection", "approval"),
    ]
    question = events[0]
    assert question.fields["input"] == question.fields["input_ansi"]
    assert question.fields["outcome"] == "accepted"
    for event in events[1:]:
        assert event.tool_call_id is not None
        assert event.fields["approver"] == "human"


def test_interruptions_become_interrupt_events_and_other_kinds_info_events(tmp_path: Path) -> None:
    copy_fixture_as_session("interruptions.jsonl", tmp_path / "logs")

    additions = gather_additions(tmp_path / "records", tmp_path / "logs", "goodall-pilot")

    events = additions.claude_sessions[SESSION].human_action_events
    interrupts = []
    for event in events:
        if event.event_type == "interrupt":
            interrupts.append(event)
        else:
            assert event.event_type == "info"
            assert event.fields["source"] == "goodall.human_action"
            assert event.fields["data"]["kind"] == event.metadata["goodall_kind"]
    assert len(interrupts) == 3
    for interrupt in interrupts:
        assert interrupt.fields["source"] == "user_cancel"
        assert interrupt.fields["interrupted"] in ("generate", "tool_call")


def test_added_events_say_where_they_were_found_and_keep_their_ids_from_one_export_to_the_next(
    tmp_path: Path,
) -> None:
    copy_fixture_as_session("permission_decisions.jsonl", tmp_path / "logs")

    first = gather_additions(tmp_path / "records", tmp_path / "logs", "goodall-pilot")
    second = gather_additions(tmp_path / "records", tmp_path / "logs", "goodall-pilot")

    first_ids = []
    for event in first.claude_sessions[SESSION].human_action_events:
        first_ids.append(event.uuid)
        assert event.metadata["goodall_transcript_file"] == f"transcripts/-work-example/{SESSION}.jsonl"
        assert isinstance(event.metadata["goodall_line_number"], int)
        assert event.metadata["goodall_actor_label"] == "human"
    second_ids = []
    for event in second.claude_sessions[SESSION].human_action_events:
        second_ids.append(event.uuid)
    assert first_ids == second_ids
    assert len(set(first_ids)) == len(first_ids)


def test_a_session_in_two_work_blocks_lists_both_and_names_neither_alone(tmp_path: Path) -> None:
    copy_fixture_as_session("typed_prompts.jsonl", tmp_path / "logs")
    record_repo = tmp_path / "records"
    morning = records.build_work_block_record(
        "ada-lovelace", "research", datetime(2026, 11, 16, 9, tzinfo=UTC), MACHINE, claude_sessions=[SESSION]
    )
    afternoon = records.build_work_block_record(
        "ada-lovelace", "engineering", datetime(2026, 11, 16, 14, tzinfo=UTC), MACHINE, claude_sessions=[SESSION]
    )
    write_record(record_repo, morning)
    write_record(record_repo, afternoon)

    additions = gather_additions(record_repo, tmp_path / "logs", "goodall-pilot")

    metadata = additions.claude_sessions[SESSION].metadata
    assert metadata["goodall_work_block"] is None
    assert sorted(metadata["goodall_work_blocks"]) == sorted([morning.id, afternoon.id])
    assert metadata["goodall_workstream"] is None
    assert sorted(metadata["goodall_workstreams"]) == ["engineering", "research"]
    assert metadata["goodall_contributor"] == "ada-lovelace"


def test_a_work_block_fix_decides_the_workstream_the_export_shows(tmp_path: Path) -> None:
    record_repo = tmp_path / "records"
    block = records.build_work_block_record(
        "ada-lovelace",
        records.UNKNOWN_WORKSTREAM,
        datetime(2026, 11, 16, 9, tzinfo=UTC),
        MACHINE,
        ended_at=datetime(2026, 11, 16, 10, tzinfo=UTC),
        end_state="abnormal",
    )
    write_record(record_repo, block)
    write_record(
        record_repo,
        records.build_work_block_fix("ada-lovelace", datetime(2026, 11, 17, tzinfo=UTC), block.id, "research"),
    )

    additions = gather_additions(record_repo, tmp_path / "logs", "goodall-pilot")

    [transcript] = additions.work_block_transcripts
    assert transcript.metadata["goodall_workstream"] == "research"
    assert transcript.events[0].fields["data"]["effective_workstream"] == "research"
    assert transcript.events[0].fields["data"]["workstream"] == records.UNKNOWN_WORKSTREAM


def test_an_unreadable_record_is_left_out_with_a_warning_naming_the_file(tmp_path: Path) -> None:
    record_repo = tmp_path / "records"
    (record_repo / "journal").mkdir(parents=True)
    (record_repo / "journal" / "20261116T093000Z-ada-lovelace-7f3a9c2e.md").write_text("not a journal entry\n")
    write_record(
        record_repo,
        records.build_journal_entry("ada-lovelace", datetime(2026, 11, 16, 10, tzinfo=UTC), "A readable entry."),
    )

    additions = gather_additions(record_repo, tmp_path / "logs", "goodall-pilot")

    assert additions.record_counts["journal_entries"] == 1
    [warning] = additions.warnings
    assert warning.kind == "record_unreadable"
    assert "journal/20261116T093000Z-ada-lovelace-7f3a9c2e.md" in warning.what_happened
    assert "not a journal entry" not in warning.what_happened
    assert len(additions.outside_work_blocks.events) == 1


def test_superseded_copies_are_never_read(tmp_path: Path) -> None:
    log_directory = tmp_path / "logs"
    superseded = log_directory / "superseded-transcripts" / "20261116T120000Z" / "-work-example"
    superseded.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "typed_prompts.jsonl", superseded / f"{SESSION}.jsonl")

    additions = gather_additions(tmp_path / "records", log_directory, "goodall-pilot")

    assert additions.claude_sessions == {}
