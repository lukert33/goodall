"""The telemetry receipt log: its format, and a check of a directory's lines against it (ADR 0013).

Beside the telemetry files it writes (`logs.jsonl`, `metrics.jsonl`,
`traces.jsonl`), the telemetry receiver keeps `receipts.jsonl`: one line of
compact JSON for every request it stored, written after the request's own
line is on disk and before the sender is told it succeeded. For example:

    {"sequence":7,"received_at":"2026-11-18T09:00:00.123456+00:00","file":"logs.jsonl","offset":5120,
     "body_length":2048,"body_sha256":"9f86d0...","line_break_replaced":false}

(shown on two lines here; in the file it is one). The fields:

- `sequence`: goodall's own number for the request, counting up from 1
  across all three files and across restarts of the receiver. OTLP carries
  no request id, so this is the only one there is. A number that never
  appears means a request was numbered but not finished.
- `received_at`: when the whole body had arrived, in UTC. Under concurrent
  requests two neighboring receipts may be a few microseconds out of order;
  `sequence` is the order the lines were written in.
- `file`: which telemetry file the line went to.
- `offset`: the byte position in that file where the line begins. ADR 0013
  does not list it; it is added so that a receipt names exactly one line,
  and a check never has to guess which line a receipt belongs to.
- `body_length` and `body_sha256`: of the body exactly as received.
- `line_break_replaced`: true when the body held raw line breaks, which the
  receiver turned into spaces to keep it on one line. The stored line then
  has the same length but a different hash, and the original cannot be
  restored; see `telemetry_receiver.as_one_line`.

The receipt log is a raw record, not derived data: the arrival time and the
original hash cannot be regenerated, so it is kept and published with the
telemetry it describes. Telemetry written by an OpenTelemetry Collector has
no receipt log.

Who calls this module: the receiver, to write receipts and to find the
next sequence number when it restarts into a directory that already has
some, and tests (later, analysis code) that call `check_receipts`. It is
standard library only, because the receiver imports it (ADR 0005).
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

RECEIPT_FILE_NAME = "receipts.jsonl"

# The files a receipt may name. Kept here, not imported from the receiver,
# so the receiver can import this module without a loop.
TELEMETRY_FILE_NAMES = ("logs.jsonl", "metrics.jsonl", "traces.jsonl")


@dataclass
class Receipt:
    """One request the receiver stored, described as the module docstring says."""

    sequence: int
    received_at: str
    file: str
    offset: int
    body_length: int
    body_sha256: str
    line_break_replaced: bool


def receipt_line(receipt: Receipt) -> bytes:
    """Encode a receipt as one line of compact JSON, without its line ending, fields in a fixed order."""
    receipt_fields = {
        "sequence": receipt.sequence,
        "received_at": receipt.received_at,
        "file": receipt.file,
        "offset": receipt.offset,
        "body_length": receipt.body_length,
        "body_sha256": receipt.body_sha256,
        "line_break_replaced": receipt.line_break_replaced,
    }
    return json.dumps(receipt_fields, separators=(",", ":")).encode("utf-8")


def parse_receipt_line(line: bytes) -> Receipt | None:
    """Read one receipt line back, or return None if it is torn or not a receipt.

    Trap: in Python `True` is an int, so `sequence` and the other numbers are
    checked to be ints that are not bools.
    """
    try:
        parsed_line = json.loads(line)
    except ValueError:
        return None
    if not isinstance(parsed_line, dict):
        return None
    try:
        receipt = Receipt(
            sequence=parsed_line["sequence"],
            received_at=parsed_line["received_at"],
            file=parsed_line["file"],
            offset=parsed_line["offset"],
            body_length=parsed_line["body_length"],
            body_sha256=parsed_line["body_sha256"],
            line_break_replaced=parsed_line["line_break_replaced"],
        )
    except KeyError:
        return None
    for number in (receipt.sequence, receipt.offset, receipt.body_length):
        if not isinstance(number, int) or isinstance(number, bool):
            return None
    if not isinstance(receipt.line_break_replaced, bool):
        return None
    if not isinstance(receipt.file, str) or not isinstance(receipt.body_sha256, str):
        return None
    return receipt


@dataclass
class ReceiptLogContent:
    """Everything in a receipt log: the receipts that could be read, and the line numbers of those that could not."""

    receipts: list[Receipt] = field(default_factory=list)
    unreadable_line_numbers: list[int] = field(default_factory=list)


def read_receipt_log(receipt_file: Path) -> ReceiptLogContent:
    """Read every line of a receipt log; a missing file reads as empty.

    Splits on b"\\n" only, like every reader of these files. A last line
    with no line ending is still read: it is usually a receipt torn short by
    a receiver that was killed, and is then reported as unreadable.
    """
    content = ReceiptLogContent()
    if not receipt_file.exists():
        return content
    file_bytes = receipt_file.read_bytes()
    if file_bytes == b"":
        return content
    lines = file_bytes.split(b"\n")
    if file_bytes.endswith(b"\n"):
        lines = lines[:-1]
    for line_number, line in enumerate(lines, start=1):
        receipt = parse_receipt_line(line)
        if receipt is None:
            content.unreadable_line_numbers.append(line_number)
        else:
            content.receipts.append(receipt)
    return content


def next_sequence_number(receipt_file: Path) -> int:
    """The number the next receipt should carry: one more than the highest readable one, or 1.

    This is how numbering survives a restart: the receiver reads the whole
    log once when it starts. A receipt torn short by a killed receiver
    cannot be read, so its number may be given out again; the torn line is
    still reported by `check_receipts`, so nothing is hidden.
    """
    highest_sequence = 0
    for receipt in read_receipt_log(receipt_file).receipts:
        if receipt.sequence > highest_sequence:
            highest_sequence = receipt.sequence
    return highest_sequence + 1


@dataclass
class UnreceiptedLine:
    """A line in a telemetry file that no receipt names: lost or torn, or written just before a crash."""

    file: str
    line_number: int
    offset: int


@dataclass
class ReceiptCheck:
    """What `check_receipts` found. Every list is empty when lines and receipts agree."""

    receipt_count: int = 0
    line_count: int = 0
    # Numbers between 1 and the highest seen that no receipt carries.
    missing_sequence_numbers: list[int] = field(default_factory=list)
    # Lines that no receipt names.
    unreceipted_lines: list[UnreceiptedLine] = field(default_factory=list)
    # Sequence numbers of receipts whose line is missing or different.
    receipts_without_a_line: list[int] = field(default_factory=list)
    # Line numbers in the receipt log that are not readable receipts.
    unreadable_receipt_line_numbers: list[int] = field(default_factory=list)
    # Groups of sequence numbers whose bodies had the same hash: a request
    # the sender retried. Not a fault in the receiver, but readers must drop
    # all but one of each group.
    repeated_bodies: list[list[int]] = field(default_factory=list)

    def problems(self) -> list[str]:
        """One plain sentence per fault found, for a test or a person to read. Repeated bodies are not faults."""
        sentences = []
        for sequence in self.missing_sequence_numbers:
            sentences.append(f"No receipt carries sequence number {sequence}.")
        for unreceipted_line in self.unreceipted_lines:
            sentences.append(
                f"Line {unreceipted_line.line_number} of {unreceipted_line.file} "
                f"(byte {unreceipted_line.offset}) has no receipt."
            )
        for sequence in self.receipts_without_a_line:
            sentences.append(f"Receipt {sequence} names a line that is missing or different.")
        for line_number in self.unreadable_receipt_line_numbers:
            sentences.append(f"Line {line_number} of {RECEIPT_FILE_NAME} is not a readable receipt.")
        return sentences


@dataclass
class StoredLine:
    """One line of a telemetry file, where it starts, and its bytes without the line ending."""

    line_number: int
    offset: int
    content: bytes


def stored_lines_of(telemetry_file: Path) -> list[StoredLine]:
    """Split a telemetry file into its lines and their starting offsets; a missing file has none."""
    stored_lines: list[StoredLine] = []
    if not telemetry_file.exists():
        return stored_lines
    file_bytes = telemetry_file.read_bytes()
    if file_bytes == b"":
        return stored_lines
    lines = file_bytes.split(b"\n")
    if file_bytes.endswith(b"\n"):
        lines = lines[:-1]
    offset = 0
    for line_number, line in enumerate(lines, start=1):
        stored_lines.append(StoredLine(line_number=line_number, offset=offset, content=line))
        offset += len(line) + 1
    return stored_lines


def line_matches_receipt(stored_line: StoredLine, receipt: Receipt) -> bool:
    """Whether a stored line is the one a receipt describes.

    The length always has to agree. The hash can only be compared when no
    line break was replaced, because the hash is of the body as received.
    """
    if len(stored_line.content) != receipt.body_length:
        return False
    if receipt.line_break_replaced:
        return True
    return hashlib.sha256(stored_line.content).hexdigest() == receipt.body_sha256


def check_receipts(telemetry_directory: Path) -> ReceiptCheck:
    """Check a telemetry directory's lines against its receipt log.

    Each receipt is paired with the line starting at its offset in its file.
    A sequence number missing at the very end cannot be seen, because
    nothing comes after it; the line it belonged to, if any, still shows as
    unreceipted.
    """
    receipt_log = read_receipt_log(telemetry_directory / RECEIPT_FILE_NAME)
    check = ReceiptCheck(
        receipt_count=len(receipt_log.receipts),
        unreadable_receipt_line_numbers=receipt_log.unreadable_line_numbers,
    )

    lines_by_file_and_offset: dict[tuple[str, int], StoredLine] = {}
    for file_name in TELEMETRY_FILE_NAMES:
        for stored_line in stored_lines_of(telemetry_directory / file_name):
            lines_by_file_and_offset[(file_name, stored_line.offset)] = stored_line
    check.line_count = len(lines_by_file_and_offset)

    receipted_lines: set[tuple[str, int]] = set()
    for receipt in receipt_log.receipts:
        stored_line_or_none = lines_by_file_and_offset.get((receipt.file, receipt.offset))
        if stored_line_or_none is not None and line_matches_receipt(stored_line_or_none, receipt):
            receipted_lines.add((receipt.file, receipt.offset))
        else:
            check.receipts_without_a_line.append(receipt.sequence)

    for (file_name, offset), stored_line in lines_by_file_and_offset.items():
        if (file_name, offset) not in receipted_lines:
            check.unreceipted_lines.append(
                UnreceiptedLine(file=file_name, line_number=stored_line.line_number, offset=offset)
            )

    sequences_seen: set[int] = set()
    for receipt in receipt_log.receipts:
        sequences_seen.add(receipt.sequence)
    if sequences_seen:
        for sequence in range(1, max(sequences_seen) + 1):
            if sequence not in sequences_seen:
                check.missing_sequence_numbers.append(sequence)

    sequences_by_hash: dict[str, list[int]] = {}
    for receipt in receipt_log.receipts:
        sequences_by_hash.setdefault(receipt.body_sha256, []).append(receipt.sequence)
    for sequences in sequences_by_hash.values():
        if len(sequences) > 1:
            check.repeated_bodies.append(sequences)
    return check
