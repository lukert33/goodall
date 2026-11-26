"""Unit tests for goodall.agent_session_copies: the notes of work blocks to copy, and copying for one.

tests/test_agent_session_copies.py drives the baseline through `goodall
start` and `goodall stop` with the Claude family. These tests pin the
baseline's own pieces, and put a second, made-up app family through it to
show that the seam (goodall.source_applications) asks nothing of a family
that the Claude family alone could give. Every file is synthetic.
"""

import fcntl
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import agent_session_copies, verbatim_copies
from goodall.source_applications import AgentSession, AppFamily, FoundAgentSessions, is_inside_work_block

WORK_BLOCK = "20261116T093000Z-ada-lovelace-7f3a9c2e"
STARTED_AT = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)
NOW = datetime(2026, 11, 16, 10, 30, 5, tzinfo=UTC)


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    folder.mkdir()
    return folder


def note_file(log_directory: Path) -> Path:
    return log_directory / "transcript-sync" / "work-blocks-to-copy" / f"{WORK_BLOCK}.json"


# ---------------------------------------------------------------------------
# A made-up app family with a layout unlike the Claude family's: one folder
# for each conversation, holding a database file and a file of times.


def make_notebook_family(application_folder: Path) -> AppFamily:
    """An app family that keeps each agent session as `conversations/<id>/` with `state.db` and `times.txt`."""

    def find_agent_sessions(log_directory: Path) -> FoundAgentSessions:
        agent_sessions = []
        for conversation_folder in sorted((application_folder / "conversations").iterdir()):
            agent_sessions.append(AgentSession(id=conversation_folder.name, folder=conversation_folder))
        return FoundAgentSessions(agent_sessions, [])

    def has_record_inside(agent_session: AgentSession, started_at: datetime, ended_at: datetime) -> bool:
        for line in (agent_session.folder / "times.txt").read_text().splitlines():
            if is_inside_work_block(datetime.fromisoformat(line), started_at, ended_at):
                return True
        return False

    def copy_agent_session(agent_session: AgentSession, log_directory: Path, now: datetime) -> verbatim_copies.Copying:
        mirror = verbatim_copies.Mirror(application_folder, ("notebook",), "superseded-notebook")
        return verbatim_copies.copy_places(mirror, log_directory, [Path("conversations") / agent_session.id], now)

    def find_copied_agent_sessions(log_directory: Path) -> FoundAgentSessions:
        return FoundAgentSessions([], [])

    def copied_places(log_directory: Path, agent_session_id: str) -> list[Path]:
        return [log_directory / "notebook" / "conversations" / agent_session_id]

    return AppFamily(
        name="Notebook",
        copies_folder_names=("notebook",),
        find_agent_sessions=find_agent_sessions,
        has_record_inside=has_record_inside,
        copy_agent_session=copy_agent_session,
        find_copied_agent_sessions=find_copied_agent_sessions,
        copied_places=copied_places,
        shared_copies_left="",
    )


def write_conversation(application_folder: Path, conversation: str, moments: list[datetime]) -> None:
    conversation_folder = application_folder / "conversations" / conversation
    conversation_folder.mkdir(parents=True)
    (conversation_folder / "state.db").write_bytes(b"synthetic database \x00\xff " + conversation.encode())
    (conversation_folder / "times.txt").write_text("".join(moment.isoformat() + "\n" for moment in moments))


@pytest.fixture
def notebook_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The made-up application's folder, with its family the only one the baseline knows."""
    application_folder = tmp_path / "notebook-application"
    (application_folder / "conversations").mkdir(parents=True)
    monkeypatch.setattr(agent_session_copies, "APP_FAMILIES", [make_notebook_family(application_folder)])
    return application_folder


def files_and_bytes(folder: Path) -> dict[str, bytes]:
    """Every file under `folder`, by its path relative to `folder`, with its exact contents."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(folder))] = path.read_bytes()
    return found


# ---------------------------------------------------------------------------
# Notes


def test_a_note_says_when_the_work_block_started_and_ended_and_a_second_note_for_it_replaces_the_first(
    log_directory: Path,
) -> None:
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)

    assert json.loads(note_file(log_directory).read_text()) == {
        "work_block": WORK_BLOCK,
        "started_at": "2026-11-16T09:30:00Z",
        "ended_at": "2026-11-16T10:30:00Z",
    }
    assert agent_session_copies.work_blocks_to_copy(log_directory) == [WORK_BLOCK]

    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT + timedelta(hours=1))

    assert json.loads(note_file(log_directory).read_text())["ended_at"] == "2026-11-16T11:30:00Z"
    assert agent_session_copies.work_blocks_to_copy(log_directory) == [WORK_BLOCK]


def test_with_no_note_there_is_nothing_to_copy_and_no_folder_is_made(
    log_directory: Path, notebook_folder: Path
) -> None:
    write_conversation(notebook_folder, "c-1", [STARTED_AT + timedelta(minutes=5)])

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    assert (result.agent_sessions_copied, result.problems) == (0, [])
    assert list(log_directory.iterdir()) == []


def test_a_note_goodall_cannot_read_is_a_problem_and_is_kept(log_directory: Path, notebook_folder: Path) -> None:
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)
    note_file(log_directory).write_text('{"work_block": "cut sh')

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    [problem] = result.problems
    assert "does not say when the work block started and ended" in problem
    assert note_file(log_directory).exists()


# ---------------------------------------------------------------------------
# Copying for one work block


def test_a_second_app_family_is_copied_through_the_same_baseline_in_its_own_layout(
    log_directory: Path, notebook_folder: Path
) -> None:
    write_conversation(notebook_folder, "c-inside", [STARTED_AT - timedelta(days=1), STARTED_AT + timedelta(minutes=5)])
    write_conversation(notebook_folder, "c-before", [STARTED_AT - timedelta(days=1)])
    write_conversation(notebook_folder, "c-after", [ENDED_AT + timedelta(minutes=5)])
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    assert (result.agent_sessions_copied, result.problems) == (1, [])
    expected = {}
    for path, contents in files_and_bytes(notebook_folder).items():
        if path.startswith("conversations/c-inside/"):
            expected[path] = contents
    assert sorted(expected) == ["conversations/c-inside/state.db", "conversations/c-inside/times.txt"]
    assert files_and_bytes(log_directory / "notebook") == expected
    # Everything was copied, so the note is gone and the next command has nothing to do.
    assert agent_session_copies.work_blocks_to_copy(log_directory) == []


def test_an_agent_session_that_cannot_be_read_is_a_problem_the_rest_is_copied_and_the_note_stays(
    log_directory: Path, notebook_folder: Path
) -> None:
    write_conversation(notebook_folder, "c-broken", [STARTED_AT + timedelta(minutes=5)])
    write_conversation(notebook_folder, "c-fine", [STARTED_AT + timedelta(minutes=6)])
    (notebook_folder / "conversations" / "c-broken" / "times.txt").unlink()
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    assert result.agent_sessions_copied == 1
    assert result.problems == ["Notebook agent session c-broken: No such file or directory."]
    assert (log_directory / "notebook" / "conversations" / "c-fine" / "state.db").exists()
    assert agent_session_copies.work_blocks_to_copy(log_directory) == [WORK_BLOCK]

    # Once the application's file is back, the next try copies it and removes the note.
    (notebook_folder / "conversations" / "c-broken" / "times.txt").write_text(
        (STARTED_AT + timedelta(minutes=5)).isoformat() + "\n"
    )
    second_result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW + timedelta(minutes=1))

    assert (second_result.agent_sessions_copied, second_result.problems) == (2, [])
    assert agent_session_copies.work_blocks_to_copy(log_directory) == []


def test_a_file_goodall_had_to_skip_is_a_problem_though_the_rest_of_the_agent_session_is_copied(
    log_directory: Path, notebook_folder: Path, tmp_path: Path
) -> None:
    write_conversation(notebook_folder, "c-1", [STARTED_AT + timedelta(minutes=5)])
    outside = tmp_path / "outside.txt"
    outside.write_text("not the application's\n")
    (notebook_folder / "conversations" / "c-1" / "link.txt").symlink_to(outside)
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    assert result.agent_sessions_copied == 1
    assert result.problems == [
        "Notebook agent session c-1: 1 thing(s) were not copied: conversations/c-1/link.txt: a symbolic link, "
        "which goodall does not follow."
    ]
    assert not (log_directory / "notebook" / "conversations" / "c-1" / "link.txt").exists()
    assert agent_session_copies.work_blocks_to_copy(log_directory) == [WORK_BLOCK]


def test_an_agent_session_whose_copies_a_person_deleted_is_never_copied_again(
    log_directory: Path, notebook_folder: Path
) -> None:
    write_conversation(notebook_folder, "c-deleted", [STARTED_AT + timedelta(minutes=5)])
    write_conversation(notebook_folder, "c-kept", [STARTED_AT + timedelta(minutes=6)])
    agent_session_copies.note_agent_sessions_deleted(log_directory, [("Notebook", "c-deleted")])
    agent_session_copies.note_agent_sessions_deleted(log_directory, [("Notebook", "c-deleted"), ("Other", "c-kept")])
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    assert (result.agent_sessions_copied, result.problems) == (1, [])
    assert sorted(path.name for path in (log_directory / "notebook" / "conversations").iterdir()) == ["c-kept"]
    assert agent_session_copies.read_deleted_agent_sessions(log_directory) == {
        ("Notebook", "c-deleted"),
        ("Other", "c-kept"),
    }


def test_a_list_of_deleted_agent_sessions_goodall_cannot_read_stops_every_copy_and_keeps_the_note(
    log_directory: Path, notebook_folder: Path
) -> None:
    write_conversation(notebook_folder, "c-1", [STARTED_AT + timedelta(minutes=5)])
    agent_session_copies.note_agent_sessions_deleted(log_directory, [("Notebook", "c-deleted")])
    (log_directory / "transcript-sync" / "deleted-agent-sessions.json").write_text("{cut sh")
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    [problem] = result.problems
    assert "deleted" in problem
    assert not (log_directory / "notebook").exists()
    assert agent_session_copies.work_blocks_to_copy(log_directory) == [WORK_BLOCK]


def test_each_ended_work_block_gets_the_agent_sessions_with_a_record_inside_it(
    log_directory: Path, notebook_folder: Path
) -> None:
    afternoon = STARTED_AT + timedelta(hours=5)
    write_conversation(notebook_folder, "c-morning", [STARTED_AT + timedelta(minutes=5)])
    write_conversation(notebook_folder, "c-afternoon", [afternoon + timedelta(minutes=5)])
    write_conversation(notebook_folder, "c-lunch", [STARTED_AT + timedelta(hours=3)])
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)
    agent_session_copies.note_work_block_to_copy(
        log_directory, "20261116T143000Z-ada-lovelace-1b2c3d4e", afternoon, afternoon + timedelta(hours=1)
    )

    result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW + timedelta(hours=6))

    assert (result.agent_sessions_copied, result.problems) == (2, [])
    assert sorted(path.name for path in (log_directory / "notebook" / "conversations").iterdir()) == [
        "c-afternoon",
        "c-morning",
    ]


def test_while_another_goodall_is_copying_this_one_reports_it_and_keeps_the_note(
    log_directory: Path, notebook_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_conversation(notebook_folder, "c-1", [STARTED_AT + timedelta(minutes=5)])
    agent_session_copies.note_work_block_to_copy(log_directory, WORK_BLOCK, STARTED_AT, ENDED_AT)
    monkeypatch.setattr(verbatim_copies, "LOCK_WAIT", timedelta(milliseconds=50))
    lock_folder = log_directory / "transcript-sync" / "locks"
    lock_folder.mkdir()
    with (lock_folder / "copying-agent-sessions.lock").open("w") as held_by_another_goodall:
        fcntl.flock(held_by_another_goodall, fcntl.LOCK_EX)

        result = agent_session_copies.copy_ended_work_blocks(log_directory, NOW)

    assert result.agent_sessions_copied == 0
    assert result.problems == [
        "Copying agent sessions did not start: another goodall command copying agent sessions took too long."
    ]
    assert not (log_directory / "notebook").exists()
    assert agent_session_copies.work_blocks_to_copy(log_directory) == [WORK_BLOCK]
