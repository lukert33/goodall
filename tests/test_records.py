"""Tests for goodall.records: goodall's own records and their files.

These drive the module the way the later `goodall journal`, `goodall
intervene` and work-block commands will: build a record, render it to the
text of its file, parse that text back, and validate it. They never touch the
disk; the module works on text and values only.

Expected values are written out as literals (a known file, a known name)
rather than recomputed the way the module computes them, so a test can
disagree with the code. The three documented examples are read from
docs/record-schemas.md, so the page cannot drift from the code.

The standard-library check starts a fresh Python with `subprocess` directly,
not through the package's runner: it tests the import itself, and a test
helper is outside the code the runner rule (AGENTS.md invariant 9) governs.
"""

import dataclasses
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import records

ADA = "ada-lovelace"
MORNING = datetime(2026, 11, 16, 9, 30, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 16, 11, 45, 10, tzinfo=UTC)


def test_journal_entry_survives_render_and_parse() -> None:
    entry = records.build_journal_entry(
        contributor=ADA,
        written_at=MORNING,
        body="Chose Postgres over SQLite.\n\nThe agent proposed it; I agreed after reading the trade-offs.",
        work_block="20261116T090000Z-ada-lovelace-0a1b2c3d",
        suffix="7f3a9c2e",
    )

    text = records.render_record(entry)
    parsed = records.parse_journal_entry(text)

    assert parsed.problems == []
    assert parsed.record == entry


def test_intervention_survives_render_and_parse() -> None:
    intervention = records.build_intervention(
        contributor=ADA,
        occurred_at=MORNING,
        written_at=LATER,
        category="agent-limitation",
        decision="modify",
        body='The agent tried to "fix" the test by deleting it.\nI stopped it and rewrote the assertion.',
        work_block="20261116T090000Z-ada-lovelace-0a1b2c3d",
        claude_session="0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5",
        interrupted="tool_call",
        question_outcome=None,
        suffix="1c2d3e4f",
    )

    text = records.render_record(intervention)
    parsed = records.parse_intervention(text)

    assert parsed.problems == []
    assert parsed.record == intervention


def test_work_block_record_survives_render_and_parse() -> None:
    work_block = records.build_work_block_record(
        contributor=ADA,
        workstream="research",
        started_at=MORNING,
        ended_at=LATER,
        end_state="clean",
        machine=records.MachineFacts(
            hostname="eval-mac-01",
            macos_version="26.1",
            goodall_version="0.0.1",
            claude_code_version="2.3.4",
        ),
        claude_sessions=["0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"],
        suffix="0a1b2c3d",
    )

    text = records.render_record(work_block)
    parsed = records.parse_work_block_record(text)

    assert parsed.problems == []
    assert parsed.record == work_block


def test_an_unfinished_work_block_has_no_end() -> None:
    work_block = records.build_work_block_record(
        contributor=ADA,
        workstream="research",
        started_at=MORNING,
        machine=records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None),
        suffix="0a1b2c3d",
    )

    parsed = records.parse_work_block_record(records.render_record(work_block))

    assert parsed.problems == []
    assert parsed.record == work_block
    assert parsed.record.ended_at is None
    assert parsed.record.end_state is None


def test_a_work_block_off_macos_has_a_null_macos_version() -> None:
    # Goodall's tests run on Linux in CI, where there is no macOS version to
    # observe; the record says so with null rather than an invented value.
    work_block = records.build_work_block_record(
        contributor=ADA,
        workstream="research",
        started_at=MORNING,
        machine=records.MachineFacts("ci-runner", None, "0.0.1", None),
        suffix="0a1b2c3d",
    )

    text = records.render_record(work_block)
    parsed = records.parse_work_block_record(text)

    assert '"macos_version": null' in text
    assert parsed.problems == []
    assert parsed.record == work_block


# ---------------------------------------------------------------------------
# File names and folders
# ---------------------------------------------------------------------------


def test_record_id_starts_with_the_utc_time_then_the_contributor() -> None:
    record_id = records.make_record_id(MORNING, ADA, suffix="7f3a9c2e")

    assert record_id == "20261116T093000Z-ada-lovelace-7f3a9c2e"


def test_record_ids_from_the_same_contributor_in_the_same_second_differ() -> None:
    first = records.make_record_id(MORNING, ADA)
    second = records.make_record_id(MORNING, ADA)

    assert first != second


def test_record_ids_sort_in_time_order() -> None:
    earlier_but_later_in_the_alphabet = records.make_record_id(MORNING, "zoe-zed", suffix="ffffffff")
    later_but_earlier_in_the_alphabet = records.make_record_id(LATER, "ada-lovelace", suffix="00000000")

    assert sorted([later_but_earlier_in_the_alphabet, earlier_but_later_in_the_alphabet]) == [
        earlier_but_later_in_the_alphabet,
        later_but_earlier_in_the_alphabet,
    ]


def test_each_kind_of_record_has_its_own_folder_and_extension() -> None:
    entry = records.build_journal_entry(ADA, MORNING, "Note.", suffix="7f3a9c2e")
    intervention = records.build_intervention(
        ADA, MORNING, LATER, "infrastructure", "reject", "Why.", suffix="1c2d3e4f"
    )
    work_block = records.build_work_block_record(
        ADA, "research", MORNING, records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None), suffix="0a1b2c3d"
    )

    assert str(records.relative_path_for(entry)) == "journal/20261116T093000Z-ada-lovelace-7f3a9c2e.md"
    assert str(records.relative_path_for(intervention)) == "interventions/20261116T093000Z-ada-lovelace-1c2d3e4f.md"
    assert str(records.relative_path_for(work_block)) == "work-blocks/20261116T093000Z-ada-lovelace-0a1b2c3d.json"


# ---------------------------------------------------------------------------
# Text that needs escaping survives the small TOML writer
# ---------------------------------------------------------------------------


def test_the_toml_writer_escapes_awkward_text_so_tomllib_reads_it_back() -> None:
    # Validation keeps today's header fields to plain identifiers, so no valid
    # record can carry this text. The writer is tested directly because it is
    # the last line of defense if a free-text header field is added later.
    awkward = 'quote " backslash \\ tab \t newline \n delete \x7f bell \x07 accent é'

    written = "value = " + records._toml_value(awkward)

    assert tomllib.loads(written)["value"] == awkward


def test_a_body_containing_the_header_fence_survives_render_and_parse() -> None:
    entry = records.build_journal_entry(ADA, MORNING, "Before\n+++\nafter", suffix="7f3a9c2e")

    parsed = records.parse_journal_entry(records.render_record(entry))

    assert parsed.record == entry


# ---------------------------------------------------------------------------
# Broken files give readable problems, never exceptions
# ---------------------------------------------------------------------------

GOOD_JOURNAL_TEXT = """+++
schema_version = 1
record_type = "journal_entry"
id = "20261116T093000Z-ada-lovelace-7f3a9c2e"
contributor = "ada-lovelace"
written_at = 2026-11-16T09:30:00Z
+++

A note.
"""


def only_problem(problems: list[records.RecordProblem]) -> records.RecordProblem:
    assert len(problems) == 1, problems
    return problems[0]


def test_a_file_without_a_header_is_reported() -> None:
    parsed = records.parse_journal_entry("Just a note.\n")

    assert parsed.record is None
    assert "put the metadata header first" in only_problem(parsed.problems).message


def test_a_header_that_is_never_closed_is_reported() -> None:
    parsed = records.parse_journal_entry("+++\nschema_version = 1\n\nA note.\n")

    assert parsed.record is None
    assert "add a `+++` line" in only_problem(parsed.problems).message


def test_invalid_toml_names_the_line_of_the_file() -> None:
    text = GOOD_JOURNAL_TEXT.replace('contributor = "ada-lovelace"', "contributor = ada-lovelace")

    parsed = records.parse_journal_entry(text)

    assert parsed.record is None
    assert "line 5 of the file" in only_problem(parsed.problems).message


def test_a_missing_field_is_reported_by_name() -> None:
    text = GOOD_JOURNAL_TEXT.replace('contributor = "ada-lovelace"\n', "")

    parsed = records.parse_journal_entry(text)

    assert parsed.record is None
    problem = only_problem(parsed.problems)
    assert problem.field == "contributor"
    assert "add it" in problem.message


def test_an_unknown_key_is_reported_but_the_record_is_still_read() -> None:
    text = GOOD_JOURNAL_TEXT.replace("written_at", 'contributer = "x"\nwritten_at')

    parsed = records.parse_journal_entry(text)

    assert parsed.record is not None
    problem = only_problem(parsed.problems)
    assert problem.field == "contributer"
    assert "correct its spelling" in problem.message


def test_a_file_of_the_wrong_kind_is_reported() -> None:
    parsed = records.parse_intervention(GOOD_JOURNAL_TEXT)

    record_type_problems = [problem for problem in parsed.problems if problem.field == "record_type"]
    assert "move the file to the right folder" in only_problem(record_type_problems).message


def test_a_timestamp_without_a_time_zone_is_reported() -> None:
    text = GOOD_JOURNAL_TEXT.replace("2026-11-16T09:30:00Z", "2026-11-16T09:30:00")

    parsed = records.parse_journal_entry(text)

    problem = only_problem(parsed.problems)
    assert problem.field == "written_at"
    assert "has no time zone" in problem.message


def test_a_work_block_file_that_is_not_json_is_reported_with_its_line() -> None:
    parsed = records.parse_work_block_record('{\n  "schema_version": 1,\n  oops\n}\n')

    assert parsed.record is None
    assert "line 3" in only_problem(parsed.problems).message


def ended_work_block_with_telemetry(telemetry: str | None) -> records.WorkBlockRecord:
    return records.build_work_block_record(
        ADA,
        "research",
        MORNING,
        records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None),
        ended_at=LATER,
        end_state="clean",
        telemetry=telemetry,
        suffix="0a1b2c3d",
    )


@pytest.mark.parametrize("telemetry", ["captured", "incomplete", "missing"])
def test_a_work_block_says_whether_its_telemetry_was_captured(telemetry: str) -> None:
    work_block = ended_work_block_with_telemetry(telemetry)

    text = records.render_record(work_block)
    parsed = records.parse_work_block_record(text)

    assert f'"telemetry": "{telemetry}"' in text
    assert parsed.problems == []
    assert parsed.record == work_block


def test_a_work_block_record_written_before_telemetry_was_captured_still_reads() -> None:
    # Records already in record repos have no telemetry field; they read as
    # not saying, never as a problem.
    text = records.render_record(ended_work_block_with_telemetry(None))
    assert '"telemetry"' not in text

    parsed = records.parse_work_block_record(text)

    assert parsed.problems == []
    assert parsed.record is not None
    assert parsed.record.telemetry is None


def test_a_work_block_with_an_unknown_telemetry_value_is_refused() -> None:
    work_block = ended_work_block_with_telemetry("partly")

    [problem] = records.validate_record(work_block)

    assert problem.field == "telemetry"
    assert "captured, incomplete or missing" in problem.message


def test_a_work_block_that_continues_a_paused_one_names_it() -> None:
    work_block = dataclasses.replace(
        ended_work_block_with_telemetry(None), continues_work_block="20261116T080000Z-ada-lovelace-1a2b3c4d"
    )

    text = records.render_record(work_block)
    parsed = records.parse_work_block_record(text)

    assert '"continues_work_block": "20261116T080000Z-ada-lovelace-1a2b3c4d"' in text
    assert parsed.problems == []
    assert parsed.record == work_block


def test_a_work_block_that_continues_none_leaves_the_field_out_and_older_records_still_read() -> None:
    text = records.render_record(ended_work_block_with_telemetry(None))
    assert "continues_work_block" not in text

    parsed = records.parse_work_block_record(text)

    assert parsed.problems == []
    assert parsed.record is not None
    assert parsed.record.continues_work_block is None


def test_a_work_block_that_continues_something_that_is_not_a_work_block_id_is_refused() -> None:
    work_block = dataclasses.replace(ended_work_block_with_telemetry(None), continues_work_block="the last one")

    [problem] = records.validate_record(work_block)

    assert problem.field == "continues_work_block"
    assert "is not a record id" in problem.message


def test_a_work_block_cannot_continue_itself() -> None:
    work_block = ended_work_block_with_telemetry(None)
    work_block = dataclasses.replace(work_block, continues_work_block=work_block.id)

    [problem] = records.validate_record(work_block)

    assert problem.field == "continues_work_block"
    assert "itself" in problem.message


def test_a_problem_inside_machine_facts_names_the_nested_field() -> None:
    work_block = records.build_work_block_record(
        ADA, "research", MORNING, records.MachineFacts("eval-mac-01", "26.1", "0.0.1", None), suffix="0a1b2c3d"
    )
    text = records.render_record(work_block).replace('"hostname"', '"host_name"')

    parsed = records.parse_work_block_record(text)

    fields = sorted(problem.field for problem in parsed.problems)
    assert fields == ["machine.host_name", "machine.hostname"]


# ---------------------------------------------------------------------------
# Validation: values the schema does not allow
# ---------------------------------------------------------------------------

ROSTER = ["ada-lovelace", "grace-hopper"]
WORKSTREAMS = ["engineering", "research", "go-to-market"]
MACHINE = records.MachineFacts("eval-mac-01", "26.1", "0.0.1", "2.3.4")


def good_intervention(**changes: object) -> records.Intervention:
    """A valid intervention, with any fields replaced by `changes`."""
    intervention = records.build_intervention(
        contributor=ADA,
        occurred_at=MORNING,
        written_at=LATER,
        category="agent-limitation",
        decision="modify",
        body="I rewrote the assertion.",
        suffix="1c2d3e4f",
    )
    return dataclasses.replace(intervention, **changes)  # type: ignore[arg-type]


def good_work_block(**changes: object) -> records.WorkBlockRecord:
    """A valid, ended work block record, with any fields replaced by `changes`."""
    work_block = records.build_work_block_record(
        contributor=ADA,
        workstream="research",
        started_at=MORNING,
        machine=MACHINE,
        ended_at=LATER,
        end_state="clean",
        suffix="0a1b2c3d",
    )
    return dataclasses.replace(work_block, **changes)  # type: ignore[arg-type]


def problem_with(record: records.Record, field: str) -> records.RecordProblem:
    """Validate `record` against the example eval config and return its one problem with `field`."""
    problems = records.validate_record(
        record,
        contributor_ids=ROSTER,
        workstreams=WORKSTREAMS,
        intervention_categories=records.DEFAULT_INTERVENTION_CATEGORIES,
    )
    return only_problem([problem for problem in problems if problem.field == field])


def test_valid_records_have_no_problems() -> None:
    entry = records.build_journal_entry(ADA, MORNING, "A note.", suffix="7f3a9c2e")
    valid_records: list[records.Record] = [entry, good_intervention(), good_work_block()]
    for record in valid_records:
        assert records.validate_record(record, contributor_ids=ROSTER, workstreams=WORKSTREAMS) == []


def test_a_decision_inspect_does_not_know_is_reported_with_the_allowed_values() -> None:
    message = problem_with(good_intervention(decision="overrode"), "decision").message

    assert "approve, modify, reject, escalate, terminate" in message


def test_an_abnormal_work_block_may_have_the_unknown_workstream_though_the_eval_lists_no_such_workstream() -> None:
    work_block = good_work_block(workstream="unknown", end_state="abnormal")

    assert records.validate_record(work_block, contributor_ids=ROSTER, workstreams=WORKSTREAMS) == []


def test_only_an_abnormal_work_block_may_have_the_unknown_workstream() -> None:
    message = problem_with(good_work_block(workstream="unknown", end_state="clean"), "workstream").message

    assert "unknown" in message
    assert "abnormal" in message


def test_an_unknown_interrupted_activity_is_reported() -> None:
    message = problem_with(good_intervention(interrupted="thinking"), "interrupted").message

    assert "generate, tool_call, between_turns" in message


def test_an_unknown_question_outcome_is_reported() -> None:
    message = problem_with(good_intervention(question_outcome="ignored"), "question_outcome").message

    assert "accepted, declined, cancelled" in message


def test_a_category_missing_from_the_eval_config_is_reported() -> None:
    message = problem_with(good_intervention(category="tooling"), "category").message

    assert "intervention_categories in eval.toml" in message


def test_a_contributor_missing_from_the_roster_is_reported() -> None:
    message = problem_with(good_intervention(contributor="alan-turing"), "contributor").message

    assert "roster" in message


def test_a_contributor_id_that_is_an_email_address_is_refused_without_echoing_it() -> None:
    message = problem_with(good_intervention(contributor="ada@example.com"), "contributor").message

    assert "email address" in message
    assert "ada@example.com" not in message


def test_a_contributor_id_with_capitals_or_spaces_is_reported() -> None:
    message = problem_with(good_intervention(contributor="Ada Lovelace"), "contributor").message

    assert "lowercase letters, digits and hyphens" in message


def test_an_unknown_workstream_is_reported() -> None:
    message = problem_with(good_work_block(workstream="marketing"), "workstream").message

    assert "workstreams in eval.toml" in message


def test_an_unknown_end_state_is_reported() -> None:
    message = problem_with(good_work_block(end_state="crashed"), "end_state").message

    assert "clean, abnormal, inferred" in message


def test_an_end_time_without_an_end_state_is_reported() -> None:
    message = problem_with(good_work_block(end_state=None), "end_state").message

    assert "set both or neither" in message


def test_a_work_block_that_ends_before_it_starts_is_reported() -> None:
    message = problem_with(good_work_block(ended_at=datetime(2026, 11, 16, 8, 0, tzinfo=UTC)), "ended_at").message

    assert "before" in message


def test_an_intervention_written_before_it_happened_is_reported() -> None:
    record = good_intervention(written_at=datetime(2026, 11, 16, 8, 0, tzinfo=UTC))

    assert "before" in problem_with(record, "written_at").message


def test_a_timestamp_with_no_time_zone_is_reported_when_building_in_code() -> None:
    record = good_intervention(occurred_at=datetime(2026, 11, 16, 9, 30))

    assert "time zone" in problem_with(record, "occurred_at").message


def test_an_empty_body_is_reported() -> None:
    assert "write what happened" in problem_with(good_intervention(body=""), "body").message


def test_a_claude_session_that_is_not_a_session_id_is_reported() -> None:
    message = problem_with(good_intervention(claude_session="yesterday"), "claude_session").message

    assert "Claude Code session id" in message


def test_a_malformed_record_id_is_reported() -> None:
    record = good_intervention(id="my intervention")

    assert "20261116T093000Z-ada-lovelace-7f3a9c2e" in problem_with(record, "id").message


def test_a_record_from_a_newer_goodall_is_reported() -> None:
    record = good_intervention(schema_version=2)

    assert "newer goodall" in problem_with(record, "schema_version").message


def test_an_email_address_in_a_body_is_refused_without_echoing_it() -> None:
    body = "Fine.\nI emailed grace.hopper@example.com about it."
    message = problem_with(good_intervention(body=body), "body").message

    assert "email address" in message
    assert "line 2" in message
    assert "grace.hopper@example.com" not in message


def test_a_secret_looking_value_in_a_body_is_refused_without_echoing_it() -> None:
    fake_key = "sk-ant-api03-" + "x" * 40
    message = problem_with(good_intervention(body=f"The agent printed {fake_key} oops"), "body").message

    assert "secret" in message
    assert fake_key not in message


def test_a_secret_looking_value_in_a_header_field_is_refused() -> None:
    fake_token = "ghp_" + "a" * 36
    record = good_intervention(work_block=fake_token)

    assert "secret" in problem_with(record, "work_block").message


def test_rendering_an_invalid_record_is_refused() -> None:
    with pytest.raises(ValueError, match="decision"):
        records.render_record(good_intervention(decision="overrode"))


# ---------------------------------------------------------------------------
# Work block fixes, and the workstream a work block counts towards
# ---------------------------------------------------------------------------

UNKNOWN_BLOCK_ID = "20261116T090000Z-ada-lovelace-0a1b2c3d"


def good_fix(**changes: object) -> records.WorkBlockFix:
    """A valid work block fix, with any fields replaced by `changes`."""
    fix = records.build_work_block_fix(
        contributor=ADA, written_at=LATER, work_block=UNKNOWN_BLOCK_ID, workstream="research", suffix="5e6f7a8b"
    )
    return dataclasses.replace(fix, **changes)  # type: ignore[arg-type]


def test_a_work_block_fix_survives_render_and_parse_and_is_named_from_when_it_was_written() -> None:
    fix = good_fix()

    text = records.render_record(fix)
    parsed = records.parse_work_block_fix(text)

    assert parsed.problems == []
    assert parsed.record == fix
    assert fix.id == "20261116T114510Z-ada-lovelace-5e6f7a8b"
    assert str(records.relative_path_for(fix)) == "work-block-fixes/20261116T114510Z-ada-lovelace-5e6f7a8b.json"


def test_a_misspelled_key_in_a_work_block_fix_is_reported() -> None:
    text = records.render_record(good_fix()).replace('"workstream"', '"work_stream"')

    parsed = records.parse_work_block_fix(text)

    assert sorted(problem.field for problem in parsed.problems) == ["work_stream"]


def test_a_work_block_fix_that_corrects_nothing_is_reported() -> None:
    message = problem_with(good_fix(workstream=None), "workstream").message

    assert "at least one corrected value" in message


def test_a_work_block_fix_may_not_set_the_unknown_workstream() -> None:
    message = problem_with(good_fix(workstream="unknown"), "workstream").message

    assert "placeholder" in message


def test_a_work_block_fix_to_a_workstream_the_eval_does_not_have_is_reported() -> None:
    message = problem_with(good_fix(workstream="sales"), "workstream").message

    assert "workstreams in eval.toml" in message


def test_a_work_block_fix_must_name_its_work_block_by_id() -> None:
    message = problem_with(good_fix(work_block="last tuesday"), "work_block").message

    assert "not a record id" in message


def test_a_work_block_fix_with_a_secret_is_refused_without_repeating_it() -> None:
    secret = "sk-" + "a" * 30
    message = problem_with(good_fix(work_block=secret), "work_block").message

    assert "secret" in message
    assert secret not in message


def test_a_work_block_counts_towards_its_own_workstream_until_a_fix_says_otherwise() -> None:
    unknown_block = good_work_block(id=UNKNOWN_BLOCK_ID, workstream="unknown", end_state="abnormal")
    other_block = good_work_block(id="20261116T130000Z-ada-lovelace-9a8b7c6d", workstream="engineering")

    workstreams = records.effective_workstreams([unknown_block, other_block], [])

    assert workstreams == {UNKNOWN_BLOCK_ID: "unknown", "20261116T130000Z-ada-lovelace-9a8b7c6d": "engineering"}


def test_the_latest_fix_of_a_work_block_wins_and_fixes_of_other_blocks_are_ignored() -> None:
    unknown_block = good_work_block(id=UNKNOWN_BLOCK_ID, workstream="unknown", end_state="abnormal")
    later_fix = good_fix(written_at=datetime(2026, 11, 17, 9, 0, tzinfo=UTC), workstream="go-to-market")
    earlier_fix = good_fix(written_at=datetime(2026, 11, 16, 12, 0, tzinfo=UTC), workstream="research")
    stray_fix = good_fix(work_block="20261101T090000Z-grace-hopper-11111111", workstream="engineering")

    # Given in no particular order, as files are found on disk.
    workstreams = records.effective_workstreams([unknown_block], [later_fix, stray_fix, earlier_fix])

    assert workstreams == {UNKNOWN_BLOCK_ID: "go-to-market"}


# ---------------------------------------------------------------------------
# The examples in docs/record-schemas.md are real, valid files
# ---------------------------------------------------------------------------

SCHEMA_DOCUMENT = Path(__file__).parent.parent / "docs" / "record-schemas.md"

# Each example in the schema document follows a marker comment naming its
# record type, and sits in a fenced block of three or more backticks.
EXAMPLE_PATTERN = re.compile(r"<!-- example: (\w+) -->\n(`{3,})\w*\n(.*?\n)\2\n", re.DOTALL)


def examples_in_schema_document() -> dict[str, str]:
    examples: dict[str, str] = {}
    for match in EXAMPLE_PATTERN.finditer(SCHEMA_DOCUMENT.read_text(encoding="utf-8")):
        examples[match.group(1)] = match.group(3)
    return examples


def test_the_schema_document_has_one_example_of_each_record_type() -> None:
    assert sorted(examples_in_schema_document()) == ["intervention", "journal_entry", "work_block", "work_block_fix"]


def test_the_documented_work_block_fix_example_parses_and_validates() -> None:
    parsed = records.parse_work_block_fix(examples_in_schema_document()["work_block_fix"])

    assert parsed.problems == []
    assert parsed.record is not None
    assert records.validate_record(parsed.record, contributor_ids=ROSTER, workstreams=WORKSTREAMS) == []


def test_the_documented_work_block_example_parses_and_validates() -> None:
    parsed = records.parse_work_block_record(examples_in_schema_document()["work_block"])

    assert parsed.problems == []
    assert parsed.record is not None
    assert records.validate_record(parsed.record, contributor_ids=ROSTER, workstreams=WORKSTREAMS) == []


def test_the_documented_journal_entry_example_parses_and_validates() -> None:
    parsed = records.parse_journal_entry(examples_in_schema_document()["journal_entry"])

    assert parsed.problems == []
    assert parsed.record is not None
    assert records.validate_record(parsed.record, contributor_ids=ROSTER) == []


def test_the_documented_intervention_example_parses_and_validates() -> None:
    parsed = records.parse_intervention(examples_in_schema_document()["intervention"])

    assert parsed.problems == []
    assert parsed.record is not None
    problems = records.validate_record(
        parsed.record,
        contributor_ids=ROSTER,
        intervention_categories=records.DEFAULT_INTERVENTION_CATEGORIES,
    )
    assert problems == []


def test_the_documented_examples_are_exactly_what_goodall_would_write() -> None:
    # So the documents show the real layout, not a hand-tidied version of it.
    examples = examples_in_schema_document()
    work_block = records.parse_work_block_record(examples["work_block"]).record
    journal_entry = records.parse_journal_entry(examples["journal_entry"]).record
    intervention = records.parse_intervention(examples["intervention"]).record
    work_block_fix = records.parse_work_block_fix(examples["work_block_fix"]).record
    assert work_block is not None and journal_entry is not None and intervention is not None
    assert work_block_fix is not None

    assert records.render_record(work_block_fix) == examples["work_block_fix"]
    assert records.render_record(work_block) == examples["work_block"]
    assert records.render_record(journal_entry) == examples["journal_entry"]
    assert records.render_record(intervention) == examples["intervention"]


# ---------------------------------------------------------------------------
# Standard library only
# ---------------------------------------------------------------------------

# Run in a fresh interpreter, so modules this test process already loaded
# (pytest and its plugins) cannot hide or fake an import. It prints the
# top-level name of every module the import loaded that is neither part of
# the standard library nor goodall itself.
LIST_THIRD_PARTY_IMPORTS = """
import sys
loaded_before = set(sys.modules)
__import__(sys.argv[1])
for module_name in sorted(set(sys.modules) - loaded_before):
    top_level_name = module_name.split(".")[0]
    if top_level_name not in sys.stdlib_module_names and top_level_name != "goodall":
        print(top_level_name)
"""


def third_party_modules_loaded_by(module_name: str) -> set[str]:
    completed = subprocess.run(
        [sys.executable, "-c", LIST_THIRD_PARTY_IMPORTS, module_name],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(completed.stdout.split())


def test_the_import_check_notices_a_third_party_package() -> None:
    # Proves the check below can fail: pytest is not in the standard library.
    assert "pytest" in third_party_modules_loaded_by("pytest")


def test_importing_records_loads_only_the_standard_library() -> None:
    assert third_party_modules_loaded_by("goodall.records") == set()
