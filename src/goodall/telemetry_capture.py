"""Where a work block's telemetry lives, and the cheap checks of its receiver that the hook can afford.

goodall.telemetry_receivers starts and stops the receiver through the
runner; this module holds everything about it that needs no program: the
paths, the saved record of the running receiver, the notes that it was
found down, and what the work block record says about telemetry at the end.
The prompt gate (goodall.hook) calls `notice_receiver_down` before every
prompt, so this module imports only the standard library and hook-safe
goodall modules (ADR 0005), and never the runner.

The layout in the log directory (PROVISIONAL, the owner's to confirm,
because a log directory layout is hard to change once evals have used it):

    <log directory>/
        telemetry/<work block id>/              raw: what the receiver stored
            logs.jsonl, metrics.jsonl, traces.jsonl, receipts.jsonl
        work-blocks/receivers/<id>.json         bookkeeping: the receiver now running for that work block
        work-blocks/receiver-down/<id>.jsonl    bookkeeping: each time it was found not running

Why there:

- Inspect Scout's importer reads every *.jsonl directly in the folder it is
  given, or in each direct subfolder, as a Claude session (finding recorded
  with goodall-fs9.16). So no telemetry file may sit one folder below the
  log directory; `telemetry/<id>/` puts them two below. The bookkeeping
  files are two below too, as heartbeats are.
- One folder per work block, so each receiver writes to a folder of its own
  (the receiver refuses to share one; ADR 0013), telemetry is easy to match
  to its work block, and a restarted receiver carries on in the same folder
  and the same receipt log.
- Raw records and goodall's bookkeeping never mix (ADR 0015): the receiver's
  own files are raw, and everything goodall writes about it lives under
  `work-blocks/`.
- Folders are created readable by this macOS user account only, because
  telemetry carries the person's email address on every record.

How the hook tells that the receiver is down, in microseconds and with no
network call (AGENTS.md invariants 7 and 9): a running receiver holds an
exclusive lock on its receipt log for as long as it runs, and the operating
system drops that lock the moment the receiver dies, however it dies. So
the hook tries to take the same lock without waiting: if it can, nobody
holds it, and the receiver is down. Unlike a process id, a lock cannot be
inherited by an unrelated program. Trap: the hook releases the lock at
once, and only checks a work block whose receiver record exists. A first
receiver writes that record only after it is listening, but a restarted
one starts while the old record is still there, so the hook can hold the
lock for an instant while it starts. The receiver therefore retries its
lock for a short time (goodall.telemetry_receiver.ReceiptLog) instead of
refusing; the hook never waits.
"""

import fcntl
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from goodall import records, telemetry_receipts, work_blocks

TELEMETRY_FOLDER_NAME = "telemetry"
RECEIVERS_FOLDER_NAME = "receivers"
RECEIVER_DOWN_FOLDER_NAME = "receiver-down"

# Why a receiver was noted down, one fixed word each.
DID_NOT_START = "did_not_start"
NOT_LISTENING = "not_listening"
NOT_RUNNING = "not_running"


@dataclass(frozen=True)
class TelemetryFiles:
    """Where each file about one work block's telemetry lives, inside one log directory."""

    log_directory: Path
    work_block_id: str

    @property
    def telemetry_folder(self) -> Path:
        return self.log_directory / TELEMETRY_FOLDER_NAME

    @property
    def telemetry_directory(self) -> Path:
        return self.telemetry_folder / self.work_block_id

    @property
    def receipt_log(self) -> Path:
        return self.telemetry_directory / telemetry_receipts.RECEIPT_FILE_NAME

    @property
    def receivers_folder(self) -> Path:
        return work_blocks.WorkBlockFiles(self.log_directory).folder / RECEIVERS_FOLDER_NAME

    @property
    def receiver_record(self) -> Path:
        return self.receivers_folder / f"{self.work_block_id}.json"

    @property
    def receiver_down_folder(self) -> Path:
        return work_blocks.WorkBlockFiles(self.log_directory).folder / RECEIVER_DOWN_FOLDER_NAME

    @property
    def receiver_down_notes(self) -> Path:
        return self.receiver_down_folder / f"{self.work_block_id}.jsonl"


@dataclass(frozen=True)
class ReceiverRecord:
    """The saved record of the receiver running for one work block.

    `program` is the runner's record of the process (goodall.runner's
    BackgroundProgram, as JSON), kept as it was saved because this module
    must not import the runner. `port` is where it listens.
    """

    work_block: str
    port: int
    started_at: datetime
    program: dict[str, object]

    @property
    def endpoint_url(self) -> str:
        """The address to give Claude Code as OTEL_EXPORTER_OTLP_ENDPOINT."""
        return f"http://127.0.0.1:{self.port}"


def make_telemetry_directory(log_directory: Path, work_block_id: str) -> Path:
    """Create the work block's telemetry folder, owner-only, if it is not there, and return it."""
    files = TelemetryFiles(log_directory, work_block_id)
    files.telemetry_folder.mkdir(mode=work_blocks.FOLDER_PERMISSIONS, exist_ok=True)
    files.telemetry_directory.mkdir(mode=work_blocks.FOLDER_PERMISSIONS, exist_ok=True)
    return files.telemetry_directory


def make_bookkeeping_folder(folder: Path) -> None:
    """Create a folder under `work-blocks/`, and `work-blocks/` itself, owner-only, where missing."""
    folder.parent.mkdir(mode=work_blocks.FOLDER_PERMISSIONS, exist_ok=True)
    folder.mkdir(mode=work_blocks.FOLDER_PERMISSIONS, exist_ok=True)


def save_receiver_record(log_directory: Path, receiver_record: ReceiverRecord) -> None:
    """Save the record of a receiver that is listening, replacing any earlier one for its work block.

    Written to a temporary file and renamed into place, so a reader sees the
    old record or the new one, never half of one.
    """
    files = TelemetryFiles(log_directory, receiver_record.work_block)
    make_bookkeeping_folder(files.receivers_folder)
    values = {
        "work_block": receiver_record.work_block,
        "port": receiver_record.port,
        "started_at": work_blocks.format_timestamp(receiver_record.started_at),
        "program": receiver_record.program,
    }
    temporary_file = files.receivers_folder / f".{files.receiver_record.name}.{secrets.token_hex(4)}.tmp"
    work_blocks.write_new_file(temporary_file, json.dumps(values, indent=2) + "\n")
    os.replace(temporary_file, files.receiver_record)


def read_receiver_record(record_file: Path) -> ReceiverRecord | None:
    """Read a saved receiver record; None when it is missing or not one."""
    try:
        values = json.loads(record_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(values, dict):
        return None
    work_block = values.get("work_block")
    port = values.get("port")
    started_at = values.get("started_at")
    program = values.get("program")
    if not isinstance(work_block, str) or not records.RECORD_ID_PATTERN.fullmatch(work_block):
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    if not isinstance(started_at, str) or not records.TIMESTAMP_PATTERN.fullmatch(started_at):
        return None
    if not isinstance(program, dict):
        return None
    return ReceiverRecord(work_block, port, datetime.fromisoformat(started_at), program)


def receiver_record_for(log_directory: Path, work_block_id: str) -> ReceiverRecord | None:
    """The saved record of the receiver for this work block, or None when there is none."""
    return read_receiver_record(TelemetryFiles(log_directory, work_block_id).receiver_record)


def remove_receiver_record(log_directory: Path, work_block_id: str) -> None:
    TelemetryFiles(log_directory, work_block_id).receiver_record.unlink(missing_ok=True)


def receiver_record_files(log_directory: Path) -> list[Path]:
    """Every saved receiver record in the log directory, for finding receivers left running."""
    folder = TelemetryFiles(log_directory, "").receivers_folder
    try:
        return sorted(folder.glob("*.json"))
    except OSError:
        return []


def receiver_is_listening(log_directory: Path, work_block_id: str) -> bool:
    """True while a receiver holds this work block's receipt log locked, which it does exactly while it runs.

    Only looks: the lock, if taken, is released at once, and nothing is
    created. See the module docstring for why this is a sound test.
    """
    receipt_log = TelemetryFiles(log_directory, work_block_id).receipt_log
    try:
        file_descriptor = os.open(receipt_log, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        # Closing releases the lock if this took it.
        os.close(file_descriptor)
    return False


def note_receiver_down(log_directory: Path, work_block_id: str, now: datetime, reason: str) -> bool:
    """Note that the work block's receiver was found not running; True when this is the first note.

    One short line, appended in a single write, as heartbeats are, so the
    hook and a goodall command can note at the same moment.
    """
    files = TelemetryFiles(log_directory, work_block_id)
    make_bookkeeping_folder(files.receiver_down_folder)
    first_note = not files.receiver_down_notes.exists()
    line = json.dumps({"at": work_blocks.format_timestamp(now), "reason": reason}) + "\n"
    descriptor = os.open(
        files.receiver_down_notes, os.O_WRONLY | os.O_APPEND | os.O_CREAT, work_blocks.FILE_PERMISSIONS
    )
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
    return first_note


def receiver_was_noted_down(log_directory: Path, work_block_id: str) -> bool:
    return TelemetryFiles(log_directory, work_block_id).receiver_down_notes.exists()


def notice_receiver_down(log_directory: Path, work_block_id: str, now: datetime) -> bool:
    """What the prompt gate calls: note the receiver down if it is; True only the first time it is noticed.

    A work block with no receiver record is left alone: either goodall
    never started one for it (a work block begun by an older goodall), or
    starting it failed, which `goodall start` noted and said at the time.
    """
    if not TelemetryFiles(log_directory, work_block_id).receiver_record.exists():
        return False
    if receiver_is_listening(log_directory, work_block_id):
        return False
    return note_receiver_down(log_directory, work_block_id, now, NOT_LISTENING)


def stored_request_count(log_directory: Path, work_block_id: str) -> int:
    """How many telemetry requests the receiver stored for this work block: the receipts in its receipt log."""
    receipt_log = TelemetryFiles(log_directory, work_block_id).receipt_log
    return len(telemetry_receipts.read_receipt_log(receipt_log).receipts)


def telemetry_outcome(log_directory: Path, work_block_id: str) -> str | None:
    """What an ended work block's record says about its telemetry: one of records.TELEMETRY_STATES, or None.

    None when goodall never tried to capture telemetry for it: no telemetry
    folder and no note that a receiver was down, as for a work block begun
    by a goodall from before telemetry capture. Call it once the receiver
    has stopped, so every request it acknowledged is counted.
    """
    files = TelemetryFiles(log_directory, work_block_id)
    noted_down = receiver_was_noted_down(log_directory, work_block_id)
    if not files.telemetry_directory.exists() and not noted_down:
        return None
    if stored_request_count(log_directory, work_block_id) == 0:
        return "missing"
    if noted_down:
        return "incomplete"
    return "captured"
