"""Tests for goodall.telemetry_capture: the receiver checks cheap enough for the prompt gate.

The outside-in tests in tests/test_work_blocks.py drive these through
`goodall start`, `stop`, `status` and `hook`. These pin the edge cases one
by one: what counts as a receiver listening, what a damaged receiver record
reads as, and what a work block record says about telemetry in each case.
"""

from datetime import UTC, datetime
from pathlib import Path

from goodall import telemetry_capture, telemetry_receiver

WORK_BLOCK_ID = "20261116T093000Z-ada-lovelace-7f3a9c2e"
MORNING = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)


def save_a_receiver_record(log_directory: Path) -> None:
    telemetry_capture.save_receiver_record(
        log_directory,
        telemetry_capture.ReceiverRecord(WORK_BLOCK_ID, 4318, MORNING, {"process_id": 12345}),
    )


def test_a_receiver_counts_as_listening_exactly_while_it_holds_its_receipt_log(tmp_path: Path) -> None:
    directory = telemetry_capture.make_telemetry_directory(tmp_path, WORK_BLOCK_ID)
    assert not telemetry_capture.receiver_is_listening(tmp_path, WORK_BLOCK_ID)

    running_receiver = telemetry_receiver.start_receiver(directory)
    try:
        assert telemetry_capture.receiver_is_listening(tmp_path, WORK_BLOCK_ID)
        # Checking took nothing from it: asking again gives the same answer.
        assert telemetry_capture.receiver_is_listening(tmp_path, WORK_BLOCK_ID)
    finally:
        running_receiver.stop()

    assert not telemetry_capture.receiver_is_listening(tmp_path, WORK_BLOCK_ID)


def test_the_prompt_gate_says_nothing_about_a_work_block_goodall_started_no_receiver_for(tmp_path: Path) -> None:
    # A work block begun by a goodall from before telemetry capture.
    assert telemetry_capture.notice_receiver_down(tmp_path, WORK_BLOCK_ID, MORNING) is False
    assert not telemetry_capture.receiver_was_noted_down(tmp_path, WORK_BLOCK_ID)


def test_the_prompt_gate_notices_a_receiver_down_once_and_notes_it_every_time(tmp_path: Path) -> None:
    telemetry_capture.make_telemetry_directory(tmp_path, WORK_BLOCK_ID)
    save_a_receiver_record(tmp_path)

    first = telemetry_capture.notice_receiver_down(tmp_path, WORK_BLOCK_ID, MORNING)
    second = telemetry_capture.notice_receiver_down(tmp_path, WORK_BLOCK_ID, MORNING)

    assert (first, second) == (True, False)
    notes = telemetry_capture.TelemetryFiles(tmp_path, WORK_BLOCK_ID).receiver_down_notes
    assert len(notes.read_text().splitlines()) == 2


def test_a_receiver_record_that_is_not_one_reads_as_none(tmp_path: Path) -> None:
    save_a_receiver_record(tmp_path)
    record_file = telemetry_capture.TelemetryFiles(tmp_path, WORK_BLOCK_ID).receiver_record
    assert telemetry_capture.read_receiver_record(record_file) is not None

    record_file.write_text('{"work_block": "x", "port": 4318}')

    assert telemetry_capture.read_receiver_record(record_file) is None


def test_what_a_work_block_record_says_about_telemetry(tmp_path: Path) -> None:
    # Nothing was ever tried: the record says nothing.
    assert telemetry_capture.telemetry_outcome(tmp_path, WORK_BLOCK_ID) is None
    # A receiver ran and nothing arrived.
    telemetry_capture.make_telemetry_directory(tmp_path, WORK_BLOCK_ID)
    assert telemetry_capture.telemetry_outcome(tmp_path, WORK_BLOCK_ID) == "missing"
    # Something arrived.
    receipt_log = telemetry_capture.TelemetryFiles(tmp_path, WORK_BLOCK_ID).receipt_log
    receipt_log.write_text(
        '{"sequence": 1, "received_at": "2026-11-16T09:31:00.000000+00:00", "file": "logs.jsonl", "offset": 0, '
        '"body_length": 2, "body_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a", '
        '"line_break_replaced": false}\n'
    )
    assert telemetry_capture.telemetry_outcome(tmp_path, WORK_BLOCK_ID) == "captured"
    # And the receiver was down for part of it.
    telemetry_capture.note_receiver_down(tmp_path, WORK_BLOCK_ID, MORNING, telemetry_capture.NOT_RUNNING)
    assert telemetry_capture.telemetry_outcome(tmp_path, WORK_BLOCK_ID) == "incomplete"
