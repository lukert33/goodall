"""Unit tests for goodall.log_reading: the read-only pieces behind `goodall logs`.

The outside-in tests are in tests/test_logs.py; these pin down the edges of
single functions: what makes a JSON Lines file unparseable, and that nothing
the reader returns holds a line's content.
"""

from pathlib import Path

from goodall import log_reading


def test_a_file_whose_every_line_is_json_is_readable_and_blank_lines_are_not_counted(tmp_path: Path) -> None:
    raw_file = tmp_path / "session.jsonl"
    raw_file.write_bytes(b'{"a":1}\n\n{"b":2}\n   \n')

    result = log_reading.check_jsonl_file(raw_file)

    assert (result.status, result.problem, result.line_count) == (log_reading.READABLE, None, 2)


def test_a_last_line_cut_short_is_unparseable_not_skipped(tmp_path: Path) -> None:
    raw_file = tmp_path / "session.jsonl"
    raw_file.write_bytes(b'{"a":1}\n{"b":')

    result = log_reading.check_jsonl_file(raw_file)

    assert result.status == log_reading.UNPARSEABLE
    assert result.problem == "1 line is not valid JSON, the first is line 2."


def test_the_problem_counts_bad_lines_and_never_quotes_one(tmp_path: Path) -> None:
    raw_file = tmp_path / "session.jsonl"
    raw_file.write_bytes(b'{"a":1}\nPRIVATE ONE\nPRIVATE TWO\n')

    result = log_reading.check_jsonl_file(raw_file)

    assert result.problem == "2 lines are not valid JSON, the first is line 2."
    assert "PRIVATE" not in str(result.problem)


def test_a_file_that_cannot_be_opened_is_unparseable_with_no_line_count(tmp_path: Path) -> None:
    result = log_reading.check_jsonl_file(tmp_path / "missing.jsonl")

    assert result.status == log_reading.UNPARSEABLE
    assert result.line_count is None
    assert result.problem is not None


def test_a_folder_that_is_not_there_has_no_records_and_no_problems(tmp_path: Path) -> None:
    contents = log_reading.read_record_repo(tmp_path / "no-such-record-repo")

    assert contents.work_blocks == []
    assert contents.problems == []


def test_a_work_block_with_no_telemetry_folder_has_no_telemetry_summary(tmp_path: Path) -> None:
    assert log_reading.summarize_telemetry(tmp_path, "20261116T090000Z-ada-lovelace-0a1b2c3d") is None
