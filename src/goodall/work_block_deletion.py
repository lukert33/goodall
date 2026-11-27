"""Working out what one work block captured, and deleting it: the engine behind `goodall delete`.

A person may delete what one work block captured (ADR 0021). goodall.delete_command
reads what was typed, says what would happen and what did; this module
decides which files are that work block's (`plan_deletion`) and deletes
them (`carry_out`). Nothing here prints.

Deleting removes whole files and folders and never edits one, so the raw
records that remain are still verbatim (ADR 0003). Per kind of record,
what belongs to the work block, and how goodall can tell:

- Agent sessions. Their copies are tied to no work block on disk: an
  agent session belongs to a work block when one of its records carries a
  time inside it (ADR 0019). So goodall reads the copies in the log
  directory the way it read the originals, and takes each one with a record
  inside the work block, together with every Claude session the work
  block's own record lists and, when its copy is still waiting, every agent
  session in the application with a record inside it. Each is deleted whole
  (its transcript, its folder, its file-edit backups, earlier copies kept
  aside), even when it also has records inside other work blocks: those are
  named, because a transcript is never cut. Each is also put on the list of
  agent sessions never to be copied again (goodall.agent_session_copies),
  because the application still holds it and goodall never touches the
  application's own files.
- Telemetry: the whole folder `telemetry/<work block id>/`, which only that
  work block's receiver wrote to, its receipt log included.
- Heartbeats, and goodall's own bookkeeping about the work block: its file
  in `work-blocks/heartbeats/`, `receivers/`, `receiver-down/`, and its
  note in `transcript-sync/work-blocks-to-copy/`. All are named after its id.
  So is the pause (`work-blocks/pause.json`, `goodall pause`), but only
  when it names this work block: it says which work block was paused, on
  which workstream and when, and would otherwise make the next work block
  say it continues one the person asked to have forgotten. A pause that
  names another work block is left.
- The Scout export in the log directory, which is derived from every
  transcript, so it is deleted whole (only when goodall made it), to be
  rebuilt with `goodall export scout`.
- In the record repo: the work block's record is replaced by a deleted work
  block (goodall.records), and its work block fixes are removed in the
  same commit (goodall.record_repo). Journal entries and interventions that
  name it are the person's own writing, not captured data, and are left.
  So is the record of a work block that names this one in
  `continues_work_block`: records are never rewritten, and the deleted work
  block keeps the id, so that pointer still names a file. The deleted work
  block itself carries neither `account_mode` nor `continues_work_block`.

None of this depends on the account mode (ADR 0021): a work block's files
are found the same way in dedicated and in mixed account mode.

What is left in place, and said so with a next step, because goodall
cannot tell which part of it is this work block's without cutting a raw
record: copies that every agent session of an app family shares (the
Claude family's prompt history and project memory), and ActivityWatch's
copies, each of which covers the stretch between two copies whatever work
blocks fell in it.

Decisions a reader would otherwise reverse-engineer:

- Planning only reads. Anything it cannot read becomes a problem, and then
  nothing is deleted: a deletion that guessed could miss a transcript.
- The raw records go first and the record repo last, because the replaced
  record no longer says when the work block ran. A deletion stopped
  half-way is finished by running the same command again, which can still
  read the times; once the replacement is written there is nothing left.
- The copying lock is held while agent sessions are deleted, so a `goodall
  status` in another terminal can never copy one back in the middle.
"""

import dataclasses
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from goodall import agent_session_copies, records, telemetry_capture, work_blocks
from goodall.activitywatch_copies import COPIES_FOLDER_NAME as ACTIVITYWATCH_COPIES_FOLDER_NAME
from goodall.claude_family import APP_FAMILY as CLAUDE_APP_FAMILY
from goodall.record_repo import RecordWriteOutcome, replace_with_deleted_work_block
from goodall.runner import Runner
from goodall.scout_export import DEFAULT_EXPORT_FOLDER, EXPORT_MARKER_FILE_NAME
from goodall.source_applications import AgentSession, AppFamily
from goodall.terminal import Failure
from goodall.verbatim_copies import describe_error, held_lock

# The kinds of thing deleted, each a fixed word for `--json`, with how a
# person reads it.
AGENT_SESSION_COPIES = "agent_session_copies"
TELEMETRY = "telemetry"
HEARTBEATS = "heartbeats"
BOOKKEEPING = "bookkeeping"
SCOUT_EXPORT = "scout_export"
WORK_BLOCK_FIXES = "work_block_fixes"
KIND_LABELS = {
    AGENT_SESSION_COPIES: "Agent sessions",
    TELEMETRY: "Telemetry",
    HEARTBEATS: "Heartbeats",
    BOOKKEEPING: "Bookkeeping",
    SCOUT_EXPORT: "Scout export",
    WORK_BLOCK_FIXES: "Work block fixes",
}


@dataclass(frozen=True)
class WorkBlockToDelete:
    """The work block being deleted, as its record gives it.

    `record_file` is its record in the record repo, or None when the record
    is still waiting in the log directory, in `waiting_file`.
    """

    record: records.WorkBlockRecord
    record_file: Path | None
    waiting_file: Path | None

    @property
    def id(self) -> str:
        return self.record.id


@dataclass(frozen=True)
class AgentSessionToDelete:
    """One agent session of the work block: its family, its id, and its copies in the log directory (maybe none)."""

    app_family: AppFamily
    id: str
    places: list[Path]


@dataclass(frozen=True)
class KindToDelete:
    """The files and folders of one kind to delete, and how many files they hold."""

    kind: str
    places: list[Path]
    file_count: int


@dataclass
class DeletionPlan:
    """Everything `goodall delete` would delete for one work block, and what it would leave and why.

    `problems` holds a plain sentence for each thing goodall could not
    read while planning; while there is one, nothing is deleted.
    """

    work_block: WorkBlockToDelete
    agent_sessions: list[AgentSessionToDelete] = field(default_factory=list)
    kinds: list[KindToDelete] = field(default_factory=list)
    fix_files: list[Path] = field(default_factory=list)
    other_work_blocks_lost_transcripts: list[str] = field(default_factory=list)
    left_in_place: list[Failure] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Finding the work block


@dataclass(frozen=True)
class FoundWorkBlock:
    """What goodall found under a work block id: the work block, a deleted work block, or a failure."""

    work_block: WorkBlockToDelete | None = None
    already_deleted: records.DeletedWorkBlock | None = None
    failure: Failure | None = None


def find_work_block(record_repo: Path, log_directory: Path, work_block_id: str) -> FoundWorkBlock:
    """Find the work block with this id: its record in the record repo, or the one still waiting in the log directory.

    `work_block_id` has already matched records.RECORD_ID_PATTERN, so it
    cannot name a file outside the folders it is looked for in.
    """
    record_file = record_repo / records.WORK_BLOCKS_FOLDER / f"{work_block_id}.json"
    waiting_file = work_blocks.WorkBlockFiles(log_directory).unrecorded_file(work_block_id)
    for candidate in [record_file, waiting_file]:
        try:
            text = candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            return FoundWorkBlock(failure=record_unreadable_failure(candidate))
        if records.json_record_type(text) == records.DELETED_WORK_BLOCK_RECORD_TYPE:
            deleted = records.parse_deleted_work_block(text).record
            if deleted is not None and deleted.id == work_block_id:
                return FoundWorkBlock(already_deleted=deleted)
        record = records.parse_work_block_record(text).record
        if record is None or record.id != work_block_id or record.ended_at is None:
            return FoundWorkBlock(failure=record_unreadable_failure(candidate))
        if candidate == record_file:
            return FoundWorkBlock(work_block=WorkBlockToDelete(record, record_file, None))
        return FoundWorkBlock(work_block=WorkBlockToDelete(record, None, waiting_file))
    return FoundWorkBlock()


def read_deleted_work_block(record_repo: Path, work_block_id: str) -> records.DeletedWorkBlock | None:
    """The deleted work block with this id in the record repo, or None when the id names no deleted work block.

    For the commands that take a work block's id (`goodall timeline`,
    `goodall fixblock`), so that they can say the work block was deleted
    instead of saying they cannot find it. `work_block_id` has already
    matched records.RECORD_ID_PATTERN, so it cannot name a file outside the
    work-blocks folder.
    """
    record_file = record_repo / records.WORK_BLOCKS_FOLDER / f"{work_block_id}.json"
    try:
        text = record_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if records.json_record_type(text) != records.DELETED_WORK_BLOCK_RECORD_TYPE:
        return None
    deleted = records.parse_deleted_work_block(text).record
    if deleted is None or deleted.id != work_block_id:
        return None
    return deleted


def is_active(log_directory: Path, work_block_id: str) -> bool:
    """True while the marker names this work block, even one nobody has stopped: it may still be recording."""
    marker = work_blocks.read_marker(log_directory)
    if isinstance(marker, work_blocks.RunningWorkBlock):
        return marker.id == work_block_id
    if isinstance(marker, work_blocks.DamagedMarker):
        return marker.values.get("id") == work_block_id
    return False


# ---------------------------------------------------------------------------
# Planning: what belongs to the work block


def plan_deletion(record_repo: Path, log_directory: Path, work_block: WorkBlockToDelete, now: datetime) -> DeletionPlan:
    """Work out, reading only, every file of the work block's to delete and what is left in place."""
    plan = DeletionPlan(work_block=work_block)
    plan_agent_sessions(plan, record_repo, log_directory, now)
    session_places = []
    for agent_session in plan.agent_sessions:
        session_places.extend(agent_session.places)
    add_kind(plan, AGENT_SESSION_COPIES, session_places)

    telemetry_files = telemetry_capture.TelemetryFiles(log_directory, work_block.id)
    add_kind(plan, TELEMETRY, [telemetry_files.telemetry_directory])
    add_kind(plan, HEARTBEATS, [work_blocks.WorkBlockFiles(log_directory).heartbeat_file(work_block.id)])
    bookkeeping = [
        telemetry_files.receiver_record,
        telemetry_files.receiver_down_notes,
        agent_session_copies.note_file(log_directory, work_block.id),
    ]
    pause = work_blocks.read_pause(log_directory)
    if pause is not None and pause.work_block_id == work_block.id:
        # The pause names this work block. A pause for another work block,
        # or one goodall cannot read, is not this work block's and stays.
        bookkeeping.append(work_blocks.WorkBlockFiles(log_directory).pause)
    add_kind(plan, BOOKKEEPING, bookkeeping)
    scout_export = log_directory / DEFAULT_EXPORT_FOLDER
    if (scout_export / EXPORT_MARKER_FILE_NAME).is_file():
        add_kind(plan, SCOUT_EXPORT, [scout_export])

    plan.fix_files = fix_files_of(record_repo, work_block.id)
    if plan.fix_files:
        plan.kinds.append(KindToDelete(WORK_BLOCK_FIXES, plan.fix_files, len(plan.fix_files)))

    plan.left_in_place.extend(what_is_left_in_place(plan, record_repo, log_directory))
    return plan


def plan_agent_sessions(plan: DeletionPlan, record_repo: Path, log_directory: Path, now: datetime) -> None:
    """Find the work block's agent sessions, their copies, and the other work blocks that share them."""
    record = plan.work_block.record
    assert record.ended_at is not None
    copy_still_waiting = agent_session_copies.note_file(log_directory, record.id).exists()
    found_by_family: dict[str, dict[str, AgentSession | None]] = {}
    for app_family in agent_session_copies.APP_FAMILIES:
        found: dict[str, AgentSession | None] = {}
        copied = app_family.find_copied_agent_sessions(log_directory)
        plan.problems.extend(copied.problems)
        add_sessions_inside(plan, app_family, copied.agent_sessions, record, found)
        if copy_still_waiting:
            originals = app_family.find_agent_sessions(log_directory)
            plan.problems.extend(originals.problems)
            add_sessions_inside(plan, app_family, originals.agent_sessions, record, found)
        if app_family.name == CLAUDE_APP_FAMILY.name:
            # The Claude sessions the work block's heartbeats named, whose
            # copies may not hold a record inside it (a copy that failed).
            for claude_session in record.claude_sessions:
                if claude_session not in found:
                    found[claude_session] = copied_session_named(copied.agent_sessions, claude_session)
        found_by_family[app_family.name] = found

    other_work_blocks = other_ended_work_blocks(record_repo, log_directory, record.id, now)
    lost: set[str] = set()
    for app_family in agent_session_copies.APP_FAMILIES:
        for agent_session_id, agent_session in sorted(found_by_family[app_family.name].items()):
            try:
                places = app_family.copied_places(log_directory, agent_session_id)
            except OSError as error:
                plan.problems.append(
                    f"The copies of {app_family.name} agent session {agent_session_id}: {describe_error(error)}."
                )
                places = []
            plan.agent_sessions.append(AgentSessionToDelete(app_family, agent_session_id, places))
            for other in other_work_blocks:
                if shares_agent_session(plan, app_family, agent_session_id, agent_session, other):
                    lost.add(other.id)
    plan.other_work_blocks_lost_transcripts = sorted(lost)


def add_sessions_inside(
    plan: DeletionPlan,
    app_family: AppFamily,
    candidates: list[AgentSession],
    record: records.WorkBlockRecord,
    found: dict[str, AgentSession | None],
) -> None:
    """Add to `found` each candidate with a record inside the work block; one that cannot be read is a problem."""
    assert record.ended_at is not None
    for candidate in candidates:
        if candidate.id in found:
            continue
        try:
            if app_family.has_record_inside(candidate, record.started_at, record.ended_at):
                found[candidate.id] = candidate
        except OSError as error:
            plan.problems.append(f"{app_family.name} agent session {candidate.id}: {describe_error(error)}.")


def copied_session_named(copied: list[AgentSession], agent_session_id: str) -> AgentSession | None:
    """The copied agent session with this id, or None when it has no copy."""
    for agent_session in copied:
        if agent_session.id == agent_session_id:
            return agent_session
    return None


def shares_agent_session(
    plan: DeletionPlan,
    app_family: AppFamily,
    agent_session_id: str,
    agent_session: AgentSession | None,
    other: records.WorkBlockRecord,
) -> bool:
    """True when another work block also holds this agent session: its record lists it, or a record is inside it."""
    if app_family.name == CLAUDE_APP_FAMILY.name and agent_session_id in other.claude_sessions:
        return True
    if agent_session is None or other.ended_at is None:
        return False
    try:
        return app_family.has_record_inside(agent_session, other.started_at, other.ended_at)
    except OSError as error:
        plan.problems.append(f"{app_family.name} agent session {agent_session_id}: {describe_error(error)}.")
        return False


def other_ended_work_blocks(
    record_repo: Path, log_directory: Path, work_block_id: str, now: datetime
) -> list[records.WorkBlockRecord]:
    """Every other work block goodall knows of, with an end: the active one counts as ending now.

    Read from the record repo, from the records still waiting in the log
    directory, and from the marker. A file that is not a work block record
    (a deleted work block, say) is passed over.
    """
    candidates = sorted((record_repo / records.WORK_BLOCKS_FOLDER).glob("*.json"))
    candidates.extend(work_blocks.unrecorded_files(log_directory))
    found = []
    for candidate in candidates:
        try:
            record = records.parse_work_block_record(candidate.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            continue
        if record is not None and record.id != work_block_id and record.ended_at is not None:
            found.append(record)
    marker = work_blocks.read_marker(log_directory)
    if isinstance(marker, work_blocks.RunningWorkBlock) and marker.id != work_block_id:
        found.append(dataclasses.replace(marker.record, ended_at=now))
    return found


def add_kind(plan: DeletionPlan, kind: str, places: list[Path]) -> None:
    """Add the places of one kind that are there, with how many files they hold; nothing when none is there."""
    present = []
    file_count = 0
    for place in places:
        place_status = status_or_none(place)
        if place_status is None:
            continue
        present.append(place)
        if stat.S_ISDIR(place_status.st_mode):
            file_count += count_files(place)
        else:
            file_count += 1
    if present:
        plan.kinds.append(KindToDelete(kind, present, file_count))


def count_files(folder: Path) -> int:
    """How many files, of any sort, are under `folder` at any depth, without following links."""
    file_count = 0
    for _, _, file_names in os.walk(folder):
        file_count += len(file_names)
    return file_count


def status_or_none(path: Path) -> os.stat_result | None:
    """The path's own status (a link is not followed), or None when nothing is there."""
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None


def fix_files_of(record_repo: Path, work_block_id: str) -> list[Path]:
    """The work block fixes in the record repo that name this work block, oldest first."""
    fix_files = []
    for fix_file in sorted((record_repo / records.WORK_BLOCK_FIXES_FOLDER).glob("*.json")):
        try:
            fix = records.parse_work_block_fix(fix_file.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            continue
        if fix is not None and fix.work_block == work_block_id:
            fix_files.append(fix_file)
    return fix_files


def entries_naming(record_repo: Path, work_block_id: str) -> int:
    """How many journal entries and interventions in the record repo name this work block."""
    count = 0
    for entry_file in sorted((record_repo / records.JOURNAL_FOLDER).glob("*.md")):
        try:
            entry = records.parse_journal_entry(entry_file.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            continue
        if entry is not None and entry.work_block == work_block_id:
            count += 1
    for intervention_file in sorted((record_repo / records.INTERVENTIONS_FOLDER).glob("*.md")):
        try:
            intervention = records.parse_intervention(intervention_file.read_text(encoding="utf-8")).record
        except (OSError, UnicodeDecodeError):
            continue
        if intervention is not None and intervention.work_block == work_block_id:
            count += 1
    return count


# ---------------------------------------------------------------------------
# What is left in place, said so with a next step


def what_is_left_in_place(plan: DeletionPlan, record_repo: Path, log_directory: Path) -> list[Failure]:
    """A warning for each thing of the work block's that `goodall delete` leaves, saying why and what to do."""
    left = []
    families_with_sessions: list[AppFamily] = []
    for agent_session in plan.agent_sessions:
        if agent_session.app_family not in families_with_sessions:
            families_with_sessions.append(agent_session.app_family)
    for app_family in families_with_sessions:
        session_count = 0
        for agent_session in plan.agent_sessions:
            if agent_session.app_family is app_family:
                session_count += 1
        left.append(application_keeps_its_own_failure(app_family, session_count))
        if app_family.shared_copies_left:
            left.append(shared_copies_left_failure(app_family))
    if (log_directory / ACTIVITYWATCH_COPIES_FOLDER_NAME).exists():
        left.append(activitywatch_left_failure())
    entry_count = entries_naming(record_repo, plan.work_block.id)
    if entry_count > 0:
        left.append(entries_left_failure(entry_count))
    if plan.agent_sessions:
        left.append(exports_elsewhere_failure())
    return left


def application_keeps_its_own_failure(app_family: AppFamily, session_count: int) -> Failure:
    """Said for every app family that had agent sessions deleted: its applications still hold their own files."""
    return Failure(
        kind="source_application_keeps_its_own_files",
        what_happened=(
            f"The {app_family.name} family's applications still keep their own files for these {session_count} "
            "agent session(s). Only goodall's copies in the log directory are deleted."
        ),
        why=(
            "Goodall never changes another application's files. It lists the agent sessions as deleted, so it "
            "never copies them again, even when one is used in a later work block."
        ),
        what_to_do_next=(
            "To remove them from this Mac altogether, delete them in the application itself. goodall delete --json "
            "lists their ids."
        ),
    )


def shared_copies_left_failure(app_family: AppFamily) -> Failure:
    """Said when an app family keeps copies its agent sessions share: they are left, and why they come back."""
    return Failure(
        kind="shared_copies_left",
        what_happened=f"Left in place: {app_family.shared_copies_left}.",
        why=(
            f"Every {app_family.name} agent session shares them, so deleting them would delete other work blocks' "
            "records too, and a raw record is deleted whole or not at all. They also hold what was typed in the "
            "deleted agent sessions, and since the application keeps its own, the next work block that copies any "
            f"{app_family.name} agent session copies them again."
        ),
        what_to_do_next=(
            "Leave them, or, to remove the deleted agent sessions' prompts for good, delete those lines in the "
            "application's own files yourself and then delete these copies; goodall never edits either."
        ),
    )


def activitywatch_left_failure() -> Failure:
    """Said when the log directory holds ActivityWatch copies, which are never per work block."""
    return Failure(
        kind="activitywatch_copies_left",
        what_happened="ActivityWatch's copies in activitywatch/ in the log directory are left in place.",
        why=(
            "Each copy holds everything ActivityWatch recorded between two copies, whatever work blocks fell in it, "
            "so goodall cannot take out this work block's part without cutting a raw record, and it does not guess."
        ),
        what_to_do_next=(
            "If you need that time gone, delete the folders under activitywatch/ whose dates cover this work block "
            "yourself, knowing they hold other times too. ActivityWatch keeps its own record in its own data "
            "folder, which goodall never changes."
        ),
    )


def entries_left_failure(entry_count: int) -> Failure:
    """Said when journal entries or interventions name the work block: they are the person's writing, and stay."""
    return Failure(
        kind="entries_left",
        what_happened=f"{entry_count} journal entry or intervention file(s) in the record repo name this work block.",
        why="They are what you wrote on purpose, not what goodall captured, so goodall leaves them as they are.",
        what_to_do_next="If you want one gone, delete its file from the record repo yourself and commit.",
    )


def exports_elsewhere_failure() -> Failure:
    """Said when transcripts are deleted: an export made somewhere else was built from them."""
    return Failure(
        kind="exports_elsewhere",
        what_happened="An export you made into another folder with --output was built from these transcripts too.",
        why="Goodall does not keep a list of where exports were made, so it cannot delete them.",
        what_to_do_next="Delete any such export yourself, or run goodall export scout again to rebuild it.",
    )


def record_unreadable_failure(record_file: Path) -> Failure:
    """The failure for a work block record goodall cannot read, so cannot tell when the work block ran."""
    return Failure(
        kind="delete_record_unreadable",
        what_happened=f"Goodall could not read the work block record {record_file}. Nothing was deleted.",
        why=(
            "Which agent sessions belong to a work block is worked out from when it started and ended, so goodall "
            "deletes nothing it cannot read the times of, rather than guess."
        ),
        what_to_do_next="Run goodall doctor, which says what is wrong with the file, then run this command again.",
    )


# ---------------------------------------------------------------------------
# Carrying the plan out


@dataclass(frozen=True)
class DeletionOutcome:
    """What carrying out a plan did: the replaced record's outcome, or a failure that stopped it first."""

    record: RecordWriteOutcome | None
    failure: Failure | None = None


def carry_out(
    runner: Runner, record_repo: Path, log_directory: Path, plan: DeletionPlan, now: datetime, contributor: str
) -> DeletionOutcome:
    """Delete everything in the plan, then replace the work block's record in the record repo.

    The raw records and bookkeeping go first, under the copying lock, after
    the agent sessions are listed as never to be copied again; the record
    repo goes last (see the module docstring for why). The waiting record,
    if any, is removed only once its replacement is written, so a stopped
    deletion can always be run again.
    """
    try:
        with held_lock(
            log_directory, agent_session_copies.COPYING_LOCK_NAME, "another goodall command copying agent sessions"
        ):
            deleted_sessions = []
            for agent_session in plan.agent_sessions:
                deleted_sessions.append((agent_session.app_family.name, agent_session.id))
            if deleted_sessions:
                agent_session_copies.note_agent_sessions_deleted(log_directory, deleted_sessions)
            for kind in plan.kinds:
                if kind.kind == WORK_BLOCK_FIXES:
                    continue
                for place in kind.places:
                    remove_place(place)
    except (OSError, TimeoutError) as error:
        return DeletionOutcome(None, deletion_stopped_failure(error))

    deleted = records.build_deleted_work_block(
        work_block=plan.work_block.id,
        contributor=contributor,
        deleted_at=now,
        other_work_blocks_lost_transcripts=plan.other_work_blocks_lost_transcripts,
    )
    outcome = replace_with_deleted_work_block(runner, record_repo, deleted, plan.fix_files)
    if outcome.written and plan.work_block.waiting_file is not None:
        plan.work_block.waiting_file.unlink(missing_ok=True)
    return DeletionOutcome(outcome)


def remove_place(place: Path) -> None:
    """Delete a file, or a folder with everything in it; a link is removed itself, never followed."""
    place_status = status_or_none(place)
    if place_status is None:
        return
    if stat.S_ISDIR(place_status.st_mode):
        shutil.rmtree(place)
    else:
        place.unlink(missing_ok=True)


def deletion_stopped_failure(error: OSError | TimeoutError) -> Failure:
    """The failure for a deletion that stopped part-way, which running the command again finishes."""
    problem = str(error)
    if isinstance(error, OSError):
        problem = describe_error(error)
    return Failure(
        kind="delete_stopped",
        what_happened=f"Goodall stopped deleting part-way: {problem}. What was deleted stays deleted.",
        why="Goodall deletes raw records before it replaces the work block's record, so nothing is half-written.",
        what_to_do_next="Fix what is named above if it is yours to fix, then run the same command again to finish.",
    )
