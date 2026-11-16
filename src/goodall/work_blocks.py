"""The live state of a work block, kept as files in the log directory.

`goodall start`, `goodall stop` and `goodall status` (goodall.work_block_commands
and goodall.status) use this module, and so will the Claude Code hook: the
prompt gate asks `active_work_block` whether a work block is active, and
leaves a heartbeat with `record_heartbeat` each time it fires. That is why
this module imports only the standard library and goodall.records, which
does too (ADR 0005), and why its two hook functions read a file or two and
never run a program.

Where the state lives, and why there (ADR 0008's reasoning): every file is
inside the log directory, under `work-blocks/`. When the log directory
cannot be reached, goodall cannot record, so there must be no way to look
"recording" without it; a marker kept anywhere else could say a work block
is active while nothing can be written.

    <log directory>/work-blocks/
        active-work-block.json      the marker: present exactly while a work block runs
        heartbeats/<id>.jsonl       one line per heartbeat of that work block
        unrecorded/<id>.json        an ended work block's record, until it is in the record repo
        damaged-markers/...         markers goodall could not read, moved aside and kept

Decisions a reader would otherwise reverse-engineer (all PROVISIONAL):

- The marker holds the work block record as it stands while running (the
  same JSON as the record repo's `work-blocks/` files, with `ended_at` and
  `end_state` null), so goodall.records reads and checks it, and the record
  written at the end is the marker plus how it ended.
- The marker is never rewritten after it is created. Heartbeats go to a
  file of their own, appended one line at a time, so a hook firing while a
  person runs `goodall stop` can never leave a half-written marker.
- Creating the marker is atomic, so two `goodall start` commands racing
  cannot both win: the record is written in full to a temporary file, then
  hard-linked to the marker's name. A link fails if the name is taken, and
  the marker appears whole or not at all. Trap: some disks (exFAT, used for
  drives shared with Windows) cannot make hard links; there goodall falls
  back to creating the marker with O_EXCL, which is just as exclusive, but a
  reader can, for an instant, see it empty.
- Files are created readable by this macOS user account only (0600, and
  0700 for folders), because they name the contributor and the machine.
  Disks that do not keep permissions (exFAT again) ignore this.
- A work block whose last heartbeat is more than IDLE_LIMIT ago is overdue:
  nobody stopped it, so it is no longer treated as active. The next
  `goodall start`, `stop` or `status` closes it at its last heartbeat with
  the end state `inferred` (goodall.work_block_commands). A heartbeat never
  extends an overdue work block, so a hook firing the next morning cannot
  stretch a forgotten work block over the night.
- An ended work block's record is first saved in `unrecorded/`, and only
  then is the marker removed, so the work is never lost if the record repo
  cannot take it yet. A marker whose work block already has an unrecorded
  record has ended: it is not active.
"""

import errno
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from goodall import records

# How long a work block may go without a heartbeat before goodall treats it
# as forgotten and closes it at its last heartbeat. PROVISIONAL, and the
# project owner's to set: longer than a lunch break or a long meeting spent
# away from Claude Code, shorter than a night, so a forgotten `goodall stop`
# does not count the night as work.
IDLE_LIMIT = timedelta(hours=3)

# The folder inside the log directory that holds everything in this module.
WORK_BLOCKS_FOLDER_NAME = "work-blocks"
MARKER_FILE_NAME = "active-work-block.json"
HEARTBEATS_FOLDER_NAME = "heartbeats"
UNRECORDED_FOLDER_NAME = "unrecorded"
DAMAGED_MARKERS_FOLDER_NAME = "damaged-markers"

# Readable and writable by this macOS user account only.
FILE_PERMISSIONS = 0o600
FOLDER_PERMISSIONS = 0o700

# How much of the end of a heartbeat file is read to find the last
# heartbeat. A heartbeat line is under 100 bytes, so this holds dozens of
# them, and the hook never reads the whole file however long the day was.
HEARTBEAT_TAIL_BYTES = 4096

# The errors `os.link` gives on a disk that cannot make hard links.
LINK_NOT_SUPPORTED_ERRORS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}


@dataclass(frozen=True)
class WorkBlockFiles:
    """Where each file about work blocks lives, inside one log directory."""

    log_directory: Path

    @property
    def folder(self) -> Path:
        return self.log_directory / WORK_BLOCKS_FOLDER_NAME

    @property
    def marker(self) -> Path:
        return self.folder / MARKER_FILE_NAME

    @property
    def heartbeats_folder(self) -> Path:
        return self.folder / HEARTBEATS_FOLDER_NAME

    @property
    def unrecorded_folder(self) -> Path:
        return self.folder / UNRECORDED_FOLDER_NAME

    @property
    def damaged_markers_folder(self) -> Path:
        return self.folder / DAMAGED_MARKERS_FOLDER_NAME

    def heartbeat_file(self, work_block_id: str) -> Path:
        return self.heartbeats_folder / f"{work_block_id}.jsonl"

    def unrecorded_file(self, work_block_id: str) -> Path:
        return self.unrecorded_folder / f"{work_block_id}.json"


@dataclass(frozen=True)
class RunningWorkBlock:
    """A work block whose marker is in place, as read from the marker and its heartbeats.

    `record` is the work block record as it stands while running (no end
    yet). `last_heartbeat_at` is the latest moment there is evidence it was
    active: its last heartbeat, or its start when it has none.
    """

    record: records.WorkBlockRecord
    last_heartbeat_at: datetime

    @property
    def id(self) -> str:
        return self.record.id

    def is_overdue(self, now: datetime) -> bool:
        """True when nobody has been seen working in it for longer than IDLE_LIMIT."""
        return now - self.last_heartbeat_at > IDLE_LIMIT


@dataclass(frozen=True)
class DamagedMarker:
    """A marker goodall could not read as a running work block.

    `problem` says why, in words for a person. `values` holds whatever the
    file did contain as a JSON object (empty when it held none), so goodall
    can recover what is left. `modified_at` is when the file last changed;
    a marker is never rewritten, so that is when the work block started.
    """

    problem: str
    values: dict[str, object]
    modified_at: datetime | None


def read_marker(log_directory: Path) -> RunningWorkBlock | DamagedMarker | None:
    """Read the marker: the running work block, a damaged marker, or None when no work block is running.

    A marker whose work block already has an unrecorded record is None: that
    work block has ended, and only the marker's removal was interrupted.
    Never raises for anything on the disk.
    """
    files = WorkBlockFiles(log_directory)
    try:
        marker_text = files.marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as read_error:
        return DamagedMarker(problem=f"it could not be read ({read_error})", values={}, modified_at=None)

    parse_result = records.parse_work_block_record(marker_text)
    running_record = parse_result.record
    problems = list(parse_result.problems)
    if running_record is not None:
        problems.extend(records.validate_record(running_record))
        if running_record.ended_at is not None or running_record.end_state is not None:
            problems.append(records.RecordProblem("end_state", "a running work block cannot have ended"))
    if running_record is None or problems:
        return damaged_marker(files.marker, marker_text, problems)

    if files.unrecorded_file(running_record.id).exists():
        return None
    last_heartbeat_at = read_last_heartbeat_at(files.heartbeat_file(running_record.id), running_record.started_at)
    return RunningWorkBlock(record=running_record, last_heartbeat_at=last_heartbeat_at)


def damaged_marker(marker: Path, marker_text: str, problems: list[records.RecordProblem]) -> DamagedMarker:
    """Describe a marker that is not a running work block, keeping whatever it held."""
    values: dict[str, object] = {}
    try:
        parsed = json.loads(marker_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        values = parsed
    modified_at = None
    try:
        modified_at = datetime.fromtimestamp(marker.stat().st_mtime, UTC).replace(microsecond=0)
    except OSError:
        pass
    problem = "it is empty"
    if problems:
        problem = problems[0].message
    if marker_text.strip() != "" and not problems:
        problem = "it does not hold a running work block"
    return DamagedMarker(problem=problem, values=values, modified_at=modified_at)


def active_work_block(log_directory: Path, now: datetime) -> RunningWorkBlock | None:
    """The work block active now, or None: what the prompt gate asks before every prompt.

    None when there is no marker, when the marker is damaged, when the log
    directory cannot be reached, and when the work block is overdue (no
    heartbeat for longer than IDLE_LIMIT). Cheap: it reads the marker and the
    end of one heartbeat file, and never runs a program.
    """
    marker = read_marker(log_directory)
    if not isinstance(marker, RunningWorkBlock):
        return None
    if marker.is_overdue(now):
        return None
    return marker


def record_heartbeat(log_directory: Path, now: datetime, claude_session: str | None = None) -> RunningWorkBlock | None:
    """Leave evidence that the active work block is still active now, and return it; None when none is.

    The hook calls this each time it fires, with the Claude Code session id
    it was given, so the work block's record can list the Claude sessions
    that fell inside it. A session id that is not one (a lowercase UUID) is
    left out rather than stored, because it would make the record invalid.
    Nothing is recorded when no work block is active, including an overdue
    one: a heartbeat must never revive a forgotten work block.

    Safe to call very often and from several processes at once: each call
    appends one short line in a single write, and never touches the marker.
    """
    work_block = active_work_block(log_directory, now)
    if work_block is None:
        return None
    heartbeat: dict[str, str | None] = {"at": format_timestamp(now), "claude_session": None}
    if claude_session is not None and records.CLAUDE_SESSION_ID_PATTERN.fullmatch(claude_session):
        heartbeat["claude_session"] = claude_session
    line = json.dumps(heartbeat) + "\n"
    heartbeat_file = WorkBlockFiles(log_directory).heartbeat_file(work_block.id)
    try:
        descriptor = os.open(heartbeat_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_PERMISSIONS)
    except OSError:
        return None
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
    return RunningWorkBlock(record=work_block.record, last_heartbeat_at=max(now, work_block.last_heartbeat_at))


def read_last_heartbeat_at(heartbeat_file: Path, started_at: datetime) -> datetime:
    """The latest heartbeat in `heartbeat_file`, or `started_at` when there is none later.

    Reads only the end of the file. A line cut short by a crash, or
    otherwise unreadable, is skipped.
    """
    try:
        with heartbeat_file.open("rb") as opened_file:
            size = opened_file.seek(0, os.SEEK_END)
            opened_file.seek(max(0, size - HEARTBEAT_TAIL_BYTES))
            tail = opened_file.read()
    except OSError:
        return started_at
    latest = started_at
    for line in tail.decode("utf-8", errors="replace").splitlines():
        heartbeat_at = parse_heartbeat_line(line)[0]
        if heartbeat_at is not None and heartbeat_at > latest:
            latest = heartbeat_at
    return latest


def read_claude_sessions(log_directory: Path, work_block_id: str) -> tuple[str, ...]:
    """Every Claude session id the heartbeats of one work block carried, in the order first seen."""
    heartbeat_file = WorkBlockFiles(log_directory).heartbeat_file(work_block_id)
    try:
        text = heartbeat_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    claude_sessions: list[str] = []
    for line in text.splitlines():
        claude_session = parse_heartbeat_line(line)[1]
        if claude_session is not None and claude_session not in claude_sessions:
            claude_sessions.append(claude_session)
    return tuple(claude_sessions)


def parse_heartbeat_line(line: str) -> tuple[datetime | None, str | None]:
    """The moment and Claude session id in one heartbeat line; None for any part that is not readable."""
    try:
        values = json.loads(line)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(values, dict):
        return None, None
    heartbeat_at = None
    at_text = values.get("at")
    if isinstance(at_text, str) and records.TIMESTAMP_PATTERN.fullmatch(at_text):
        heartbeat_at = datetime.fromisoformat(at_text)
    claude_session = values.get("claude_session")
    if not isinstance(claude_session, str) or not records.CLAUDE_SESSION_ID_PATTERN.fullmatch(claude_session):
        claude_session = None
    return heartbeat_at, claude_session


def create_marker(log_directory: Path, running_record: records.WorkBlockRecord) -> bool:
    """Create the marker for a work block that is starting; False when another marker is already there.

    Atomic: of two goodall commands creating a marker at the same moment,
    exactly one gets True. Raises OSError only for a disk that cannot be
    written, which the caller reports.
    """
    files = WorkBlockFiles(log_directory)
    make_folders(files)
    text = records.render_record(running_record)
    temporary_file = files.folder / f".{MARKER_FILE_NAME}.{secrets.token_hex(4)}.tmp"
    write_new_file(temporary_file, text)
    try:
        os.link(temporary_file, files.marker)
        return True
    except FileExistsError:
        return False
    except OSError as link_error:
        if link_error.errno not in LINK_NOT_SUPPORTED_ERRORS:
            raise
        return create_marker_without_link(files.marker, text)
    finally:
        temporary_file.unlink(missing_ok=True)


def create_marker_without_link(marker: Path, text: str) -> bool:
    """Create the marker with O_EXCL, on a disk that cannot make hard links; False when one is there."""
    try:
        write_new_file(marker, text)
    except FileExistsError:
        return False
    return True


def write_new_file(path: Path, text: str) -> None:
    """Create `path`, owner-only, holding `text`; raises FileExistsError when something is already there.

    The permissions are set as the file is created, so it is never readable
    by others even for an instant.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS)
    with os.fdopen(descriptor, "w", encoding="utf-8") as opened_file:
        opened_file.write(text)
        opened_file.flush()
        os.fsync(opened_file.fileno())


def make_folders(files: WorkBlockFiles) -> None:
    """Create the work-blocks folders in the log directory, owner-only, where they are missing."""
    for folder in [files.folder, files.heartbeats_folder, files.unrecorded_folder]:
        folder.mkdir(mode=FOLDER_PERMISSIONS, exist_ok=True)


def format_timestamp(moment: datetime) -> str:
    """Write a moment in UTC as records do: `2026-11-16T09:30:00Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
