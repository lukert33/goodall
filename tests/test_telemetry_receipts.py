"""Tests for the telemetry receipt log (ADR 0013), driven the way Claude Code drives the receiver.

Each test starts a real receiver on a free port, sends it real HTTP requests,
and reads back both the telemetry files and `receipts.jsonl` from `tmp_path`.
The faults a real disk or a killed process would cause (a receipt that could
not be written, a write that was silently dropped, a line torn short) are
made on purpose, by replacing one of the receiver's own write functions for
one request, so that the test can show each fault is visible afterwards.
Every body is synthetic.
"""

import hashlib
import http.client
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import telemetry_receipts, telemetry_receiver
from goodall.telemetry_receipts import UnreceiptedLine
from goodall.telemetry_receiver import RunningReceiver


@pytest.fixture
def receiver(tmp_path: Path) -> Iterator[RunningReceiver]:
    """Start a receiver writing into tmp_path, and always stop it afterwards."""
    running_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        yield running_receiver
    finally:
        running_receiver.stop()


def post(port: int, path: str, body: bytes) -> int:
    """Send one OTLP http/json export, as Claude Code does, and return the HTTP status."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def receipts_in(telemetry_directory: Path) -> list[dict[str, object]]:
    """Read the receipt log back as parsed JSON objects, insisting its last line is complete."""
    content = (telemetry_directory / "receipts.jsonl").read_bytes()
    if content == b"":
        return []
    assert content.endswith(b"\n"), "the last receipt is incomplete"
    receipts = []
    for line in content[:-1].split(b"\n"):
        receipts.append(json.loads(line))
    return receipts


def sha256_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def test_stored_request_gets_a_receipt_describing_the_body_as_received(
    receiver: RunningReceiver, tmp_path: Path
) -> None:
    body = b'{"resourceLogs":[],"marker":"first"}'
    before_sending = datetime.now(UTC)

    status = post(receiver.port, "/v1/logs", body)

    assert status == 200
    receipts = receipts_in(tmp_path)
    assert len(receipts) == 1
    receipt = receipts[0]
    received_at = datetime.fromisoformat(str(receipt.pop("received_at")))
    assert received_at.tzinfo is not None
    assert before_sending - timedelta(seconds=1) <= received_at <= datetime.now(UTC)
    assert receipt == {
        "sequence": 1,
        "file": "logs.jsonl",
        "offset": 0,
        "body_length": len(body),
        "body_sha256": sha256_of(body),
        "line_break_replaced": False,
    }


def test_receipts_number_requests_across_files_and_say_where_each_line_starts(
    receiver: RunningReceiver, tmp_path: Path
) -> None:
    first_log = b'{"resourceLogs":[],"n":1}'
    metric = b'{"resourceMetrics":[]}'
    second_log = b'{"resourceLogs":[],"n":2}'

    post(receiver.port, "/v1/logs", first_log)
    post(receiver.port, "/v1/metrics", metric)
    post(receiver.port, "/v1/logs", second_log)

    placements = []
    for receipt in receipts_in(tmp_path):
        placements.append((receipt["sequence"], receipt["file"], receipt["offset"]))
    assert placements == [
        (1, "logs.jsonl", 0),
        (2, "metrics.jsonl", 0),
        (3, "logs.jsonl", len(first_log) + 1),
    ]
    assert telemetry_receipts.check_receipts(tmp_path).problems() == []


def test_line_is_forced_to_disk_before_its_receipt(
    receiver: RunningReceiver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Which file each forced write belonged to, identified by inode, in order.
    forced_inodes: list[int] = []
    real_force_to_disk = telemetry_receiver.force_to_disk

    def recording_force_to_disk(file_descriptor: int) -> None:
        real_force_to_disk(file_descriptor)
        forced_inodes.append(os.fstat(file_descriptor).st_ino)

    monkeypatch.setattr(telemetry_receiver, "force_to_disk", recording_force_to_disk)

    status = post(receiver.port, "/v1/logs", b'{"resourceLogs":[]}')

    assert status == 200
    assert forced_inodes == [
        (tmp_path / "logs.jsonl").stat().st_ino,
        (tmp_path / "receipts.jsonl").stat().st_ino,
    ]


def test_body_with_line_breaks_is_flagged_and_hashed_as_received(receiver: RunningReceiver, tmp_path: Path) -> None:
    pretty_body = b'{\r\n  "resourceLogs": []\n}\n'

    post(receiver.port, "/v1/logs", pretty_body)

    receipt = receipts_in(tmp_path)[0]
    assert receipt["line_break_replaced"] is True
    assert receipt["body_sha256"] == sha256_of(pretty_body)
    assert receipt["body_length"] == len(pretty_body)
    # The stored line differs from the body, but the check still pairs them.
    assert telemetry_receipts.check_receipts(tmp_path).problems() == []


def test_retried_request_shows_as_two_receipts_with_one_hash(receiver: RunningReceiver, tmp_path: Path) -> None:
    # OTLP carries no request id, so a sender that retries after a lost answer
    # sends the same bytes again, and both copies are stored.
    retried_body = b'{"resourceLogs":[],"batch":"retried"}'
    other_body = b'{"resourceLogs":[],"batch":"other"}'

    post(receiver.port, "/v1/logs", retried_body)
    post(receiver.port, "/v1/logs", other_body)
    post(receiver.port, "/v1/logs", retried_body)

    receipts = receipts_in(tmp_path)
    assert receipts[0]["body_sha256"] == receipts[2]["body_sha256"] == sha256_of(retried_body)
    check = telemetry_receipts.check_receipts(tmp_path)
    assert check.repeated_bodies == [[1, 3]]
    assert check.missing_sequence_numbers == []
    assert check.unreceipted_lines == []


def test_sequence_numbers_continue_after_a_restart_into_the_same_directory(tmp_path: Path) -> None:
    for run_number in range(3):
        running_receiver = telemetry_receiver.start_receiver(tmp_path)
        try:
            post(running_receiver.port, "/v1/logs", b'{"resourceLogs":[],"run":' + str(run_number).encode() + b"}")
            post(running_receiver.port, "/v1/traces", b'{"resourceSpans":[]}')
        finally:
            running_receiver.stop()

    sequence_numbers = []
    for receipt in receipts_in(tmp_path):
        sequence_numbers.append(receipt["sequence"])
    assert sequence_numbers == [1, 2, 3, 4, 5, 6]
    assert telemetry_receipts.check_receipts(tmp_path).problems() == []


def test_second_receiver_on_the_same_directory_refuses_to_start(receiver: RunningReceiver, tmp_path: Path) -> None:
    # Two receivers numbering requests into one receipt log would hand out the
    # same sequence numbers twice.
    second_receiver = None
    try:
        with pytest.raises(OSError, match="Another telemetry receiver is already writing"):
            second_receiver = telemetry_receiver.start_receiver(tmp_path)
    finally:
        if second_receiver is not None:
            second_receiver.stop()


def test_receipt_that_cannot_be_written_shows_as_a_gap_and_an_unreceipted_line(
    receiver: RunningReceiver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shape of the line lost in CI (Q22): something went wrong between
    # storing a request and finishing with it. Here the receipt write fails
    # once, after the line is already on disk.
    real_write_receipt = telemetry_receiver.write_receipt
    failures_left = [1]

    def failing_once_write_receipt(file_descriptor: int, receipt_line: bytes) -> None:
        if failures_left[0] > 0:
            failures_left[0] -= 1
            raise OSError("disk full (simulated)")
        real_write_receipt(file_descriptor, receipt_line)

    monkeypatch.setattr(telemetry_receiver, "write_receipt", failing_once_write_receipt)
    lost_body = b'{"resourceLogs":[],"n":"lost"}'
    kept_body = b'{"resourceLogs":[],"n":"kept"}'

    # 503 tells the sender to retry, so the request is not acknowledged.
    assert post(receiver.port, "/v1/logs", lost_body) == 503
    assert post(receiver.port, "/v1/logs", kept_body) == 200

    check = telemetry_receipts.check_receipts(tmp_path)
    assert check.missing_sequence_numbers == [1]
    assert check.unreceipted_lines == [UnreceiptedLine(file="logs.jsonl", line_number=1, offset=0)]
    assert check.receipts_without_a_line == []


def test_write_that_was_silently_dropped_shows_as_a_receipt_without_a_line(
    receiver: RunningReceiver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A disk that reports success but keeps nothing: the receiver cannot see
    # it, so the receipt is written, and the check finds no line where it says.
    real_write_line = telemetry_receiver.write_line
    dropped_body = b'{"resourceLogs":[],"n":"dropped"}'

    def dropping_write_line(file_descriptor: int, line: bytes) -> None:
        if line != dropped_body:
            real_write_line(file_descriptor, line)

    monkeypatch.setattr(telemetry_receiver, "write_line", dropping_write_line)

    post(receiver.port, "/v1/logs", b'{"resourceLogs":[],"n":"before"}')
    post(receiver.port, "/v1/logs", dropped_body)
    post(receiver.port, "/v1/logs", b'{"resourceLogs":[],"n":"after"}')

    check = telemetry_receipts.check_receipts(tmp_path)
    assert check.receipts_without_a_line == [2]
    assert check.unreceipted_lines == []
    assert check.missing_sequence_numbers == []


def test_line_torn_by_a_killed_receiver_is_ended_and_shows_as_unreceipted(tmp_path: Path) -> None:
    # A receiver killed mid-write leaves part of a line and no receipt. The
    # next receiver ends that fragment with a line break before appending, so
    # the fragment stays visible on its own and the new line is whole.
    torn_fragment = b'{"resourceLogs":[{"scopeLo'
    (tmp_path / "logs.jsonl").write_bytes(torn_fragment)
    whole_body = b'{"resourceLogs":[]}'

    running_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        assert post(running_receiver.port, "/v1/logs", whole_body) == 200
    finally:
        running_receiver.stop()

    assert (tmp_path / "logs.jsonl").read_bytes() == torn_fragment + b"\n" + whole_body + b"\n"
    assert receipts_in(tmp_path)[0]["offset"] == len(torn_fragment) + 1
    check = telemetry_receipts.check_receipts(tmp_path)
    assert check.unreceipted_lines == [UnreceiptedLine(file="logs.jsonl", line_number=1, offset=0)]
    assert check.receipts_without_a_line == []


def test_receipt_torn_by_a_killed_receiver_is_reported_and_numbering_continues(tmp_path: Path) -> None:
    first_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        post(first_receiver.port, "/v1/logs", b'{"resourceLogs":[],"n":1}')
    finally:
        first_receiver.stop()
    with (tmp_path / "receipts.jsonl").open("ab") as receipt_file:
        receipt_file.write(b'{"sequence":2,"rece')

    second_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        post(second_receiver.port, "/v1/logs", b'{"resourceLogs":[],"n":3}')
    finally:
        second_receiver.stop()

    check = telemetry_receipts.check_receipts(tmp_path)
    assert check.unreadable_receipt_line_numbers == [2]
    # The new receipt is on a line of its own, after the fragment.
    assert check.receipt_count == 2


def test_receipt_log_is_readable_by_its_owner_only(receiver: RunningReceiver, tmp_path: Path) -> None:
    post(receiver.port, "/v1/logs", b'{"resourceLogs":[]}')

    assert (tmp_path / "receipts.jsonl").stat().st_mode & 0o777 == 0o600


def test_symbolic_link_at_the_receipt_log_stops_the_receiver_starting(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"untouched\n")
    (tmp_path / "receipts.jsonl").symlink_to(elsewhere)

    started_receiver = None
    try:
        with pytest.raises(OSError):
            started_receiver = telemetry_receiver.start_receiver(tmp_path)
    finally:
        if started_receiver is not None:
            started_receiver.stop()
    assert elsewhere.read_bytes() == b"untouched\n"


# Unit tests for goodall.telemetry_receipts on directories built by hand, with
# no receiver running: the receipt's line format and the check's pairing rule.


def write_by_hand(telemetry_directory: Path, logs_content: bytes, receipts: list[telemetry_receipts.Receipt]) -> None:
    """Write a logs file and a receipt log exactly as given."""
    (telemetry_directory / "logs.jsonl").write_bytes(logs_content)
    receipt_lines = b""
    for receipt in receipts:
        receipt_lines += telemetry_receipts.receipt_line(receipt) + b"\n"
    (telemetry_directory / "receipts.jsonl").write_bytes(receipt_lines)


def receipt_for(sequence: int, offset: int, body: bytes) -> telemetry_receipts.Receipt:
    return telemetry_receipts.Receipt(
        sequence=sequence,
        received_at="2026-11-18T09:00:00.000000+00:00",
        file="logs.jsonl",
        offset=offset,
        body_length=len(body),
        body_sha256=sha256_of(body),
        line_break_replaced=False,
    )


def test_receipt_line_is_compact_json_in_a_fixed_field_order() -> None:
    receipt = receipt_for(7, 120, b"{}")

    assert telemetry_receipts.receipt_line(receipt) == (
        b'{"sequence":7,"received_at":"2026-11-18T09:00:00.000000+00:00","file":"logs.jsonl","offset":120,'
        b'"body_length":2,"body_sha256":"' + sha256_of(b"{}").encode() + b'","line_break_replaced":false}'
    )


def test_next_sequence_number_starts_at_one_and_follows_the_highest_readable_receipt(tmp_path: Path) -> None:
    receipt_file = tmp_path / "receipts.jsonl"
    assert telemetry_receipts.next_sequence_number(receipt_file) == 1

    receipt_file.write_bytes(b"")
    assert telemetry_receipts.next_sequence_number(receipt_file) == 1

    write_by_hand(tmp_path, b"", [receipt_for(3, 0, b"{}"), receipt_for(9, 3, b"{}"), receipt_for(5, 6, b"{}")])
    with receipt_file.open("ab") as receipt_file_handle:
        receipt_file_handle.write(b'{"sequence":10')
    assert telemetry_receipts.next_sequence_number(receipt_file) == 10


def test_check_of_a_line_that_was_deleted_reports_its_receipt_and_the_gap_it_leaves(tmp_path: Path) -> None:
    first, second, third = b'{"n":1}', b'{"n":2}', b'{"n":3}'
    receipts = [receipt_for(1, 0, first), receipt_for(2, 8, second), receipt_for(3, 16, third)]
    # The second line is missing: the third now starts where the second should.
    write_by_hand(tmp_path, first + b"\n" + third + b"\n", receipts)

    check = telemetry_receipts.check_receipts(tmp_path)

    assert check.receipts_without_a_line == [2, 3]
    assert check.unreceipted_lines == [UnreceiptedLine(file="logs.jsonl", line_number=2, offset=8)]
    assert len(check.problems()) == 3


def test_check_reports_sequence_numbers_missing_between_receipts(tmp_path: Path) -> None:
    first, fourth = b'{"n":1}', b'{"n":4}'
    write_by_hand(tmp_path, first + b"\n" + fourth + b"\n", [receipt_for(1, 0, first), receipt_for(4, 8, fourth)])

    check = telemetry_receipts.check_receipts(tmp_path)

    assert check.missing_sequence_numbers == [2, 3]
    assert check.unreceipted_lines == []
    assert check.receipts_without_a_line == []
