"""Unit tests for goodall.claude_family: finding Claude sessions, reading their records' times, copying their files.

tests/test_agent_session_copies.py drives the whole thing through `goodall
start` and `goodall stop`; these tests pin each piece on its own. Every
file is synthetic and inside tmp_path; the key names are the ones seen in
the development account on 2026-11-26 (docs/source-applications.md).
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import claude_family
from goodall.source_applications import AgentSession

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
OTHER_SESSION = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"
PROJECT_FOLDER_NAME = "-Users-ada-work"
STARTED_AT = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)
NOW = datetime(2026, 11, 16, 10, 30, 5, tzinfo=UTC)


@dataclass(frozen=True)
class Folders:
    """A synthetic Claude config folder and a log directory, both inside tmp_path."""

    config_folder: Path
    log_directory: Path

    @property
    def project_folder(self) -> Path:
        return self.config_folder / "projects" / PROJECT_FOLDER_NAME

    @property
    def transcript(self) -> Path:
        return self.project_folder / f"{CLAUDE_SESSION}.jsonl"

    @property
    def agent_session(self) -> AgentSession:
        return AgentSession(id=CLAUDE_SESSION, folder=self.project_folder)


@pytest.fixture
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Folders:
    """The folders, with CLAUDE_CONFIG_DIR naming the synthetic config folder, by its real path."""
    config_folder = (tmp_path / "claude-config").resolve()
    made = Folders(config_folder, tmp_path / "logs")
    made.project_folder.mkdir(parents=True)
    made.log_directory.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_folder))
    return made


def record(moment: datetime | None, **other_fields: object) -> bytes:
    """One synthetic transcript record on a line, with a `timestamp` as the Claude family writes it, or without one."""
    fields: dict[str, object] = {"type": "user", **other_fields}
    if moment is not None:
        fields["timestamp"] = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + "250Z"
    return json.dumps(fields).encode("utf-8") + b"\n"


def write_changed_at(path: Path, contents: bytes, changed_at: datetime) -> None:
    """Write a file whose modification time is `changed_at`, not the real clock's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, (changed_at.timestamp(), changed_at.timestamp()))


# ---------------------------------------------------------------------------
# A record's time


def test_a_records_time_is_its_top_level_timestamp() -> None:
    line = (
        b'{"type":"assistant","timestamp":"2026-11-16T09:35:00.123Z","message":{"timestamp":"1999-01-01T00:00:00Z"}}\n'
    )

    assert claude_family.record_time(line) == datetime(2026, 11, 16, 9, 35, 0, 123000, tzinfo=UTC)


@pytest.mark.parametrize(
    "line",
    [
        b'{"type":"custom-title","customTitle":"synthetic"}\n',
        b'{"type":"user","timestamp":1794821700}\n',
        b'{"type":"user","timestamp":"yesterday"}\n',
        # No zone: goodall guesses none.
        b'{"type":"user","timestamp":"2026-11-16T09:35:00"}\n',
        b'["2026-11-16T09:35:00Z"]\n',
        b"{cut short\n",
        b'{"type":"user","timestamp":"2026-11-16T09:35:00Z","text":"\xff"}\n',
        b"\n",
    ],
)
def test_a_line_without_a_time_goodall_can_trust_carries_none(line: bytes) -> None:
    assert claude_family.record_time(line) is None


# ---------------------------------------------------------------------------
# Which Claude sessions belong to a work block


def test_a_claude_session_with_a_record_inside_the_work_block_belongs_to_it(folders: Folders) -> None:
    inside = STARTED_AT + timedelta(minutes=10)
    contents = record(STARTED_AT - timedelta(days=1)) + record(None) + record(inside)
    write_changed_at(folders.transcript, contents, inside)

    assert claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)


def test_a_claude_session_whose_records_are_all_before_or_after_the_work_block_does_not_belong_to_it(
    folders: Folders,
) -> None:
    after = ENDED_AT + timedelta(minutes=10)
    contents = record(STARTED_AT - timedelta(minutes=10)) + record(None) + record(after)
    write_changed_at(folders.transcript, contents, after)

    assert not claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)


def test_a_record_in_a_subagents_transcript_counts_and_one_in_a_tool_result_does_not(folders: Folders) -> None:
    inside = STARTED_AT + timedelta(minutes=10)
    write_changed_at(folders.transcript, record(STARTED_AT - timedelta(hours=2)), inside)
    session_folder = folders.project_folder / CLAUDE_SESSION
    # A tool's saved output can hold anything, a `timestamp` included.
    write_changed_at(session_folder / "tool-results" / "saved.jsonl", record(inside), inside)

    assert not claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)

    write_changed_at(session_folder / "subagents" / "agent-a1b2c3d4.jsonl", record(inside), inside)

    assert claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)


def test_a_transcript_last_changed_before_the_work_block_began_is_not_opened(folders: Folders) -> None:
    write_changed_at(folders.transcript, record(STARTED_AT - timedelta(hours=1)), STARTED_AT - timedelta(hours=1))
    # Unreadable, so opening it would raise.
    folders.transcript.chmod(0o000)
    try:
        assert not claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)
    finally:
        folders.transcript.chmod(0o600)


def test_a_transcript_that_cannot_be_read_raises_rather_than_leave_the_session_out_quietly(folders: Folders) -> None:
    inside = STARTED_AT + timedelta(minutes=10)
    write_changed_at(folders.transcript, record(inside), inside)
    folders.transcript.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)
    finally:
        folders.transcript.chmod(0o600)


def test_a_claude_session_with_a_folder_but_no_transcript_has_no_record_inside(folders: Folders) -> None:
    (folders.project_folder / CLAUDE_SESSION).mkdir()

    assert not claude_family.has_record_inside(folders.agent_session, STARTED_AT, ENDED_AT)


# ---------------------------------------------------------------------------
# Finding Claude sessions


def test_every_claude_session_in_every_project_folder_is_found_and_other_files_are_not(folders: Folders) -> None:
    folders.transcript.write_bytes(b"")
    other_project_folder = folders.config_folder / "projects" / "-Users-ada-elsewhere"
    # A Claude session that has only its folder so far.
    (other_project_folder / OTHER_SESSION).mkdir(parents=True)
    (other_project_folder / "notes.jsonl").write_bytes(b"{}\n")
    (other_project_folder / "memory").mkdir()

    found = claude_family.find_agent_sessions(folders.log_directory)

    assert found.problems == []
    assert found.agent_sessions == [
        AgentSession(id=OTHER_SESSION, folder=other_project_folder),
        AgentSession(id=CLAUDE_SESSION, folder=folders.project_folder),
    ]


def test_a_claude_session_is_found_once_however_many_ways_its_config_folder_is_known(
    folders: Folders, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folders.transcript.write_bytes(b"")
    linked_config_folder = tmp_path / "linked-claude-config"
    linked_config_folder.symlink_to(folders.config_folder)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(linked_config_folder))
    claude_family.remember_claude_config_folder(folders.log_directory, folders.config_folder)
    claude_family.remember_claude_config_folder(folders.log_directory, tmp_path / "gone")

    found = claude_family.find_agent_sessions(folders.log_directory)

    assert found.problems == []
    assert found.agent_sessions == [AgentSession(id=CLAUDE_SESSION, folder=folders.project_folder)]


def test_claude_sessions_are_found_in_the_default_folder_and_in_one_a_hook_remembered(
    folders: Folders, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A terminal where CLAUDE_CONFIG_DIR is not set at all.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    folders.transcript.write_bytes(b"")
    claude_family.remember_claude_config_folder(folders.log_directory, folders.config_folder)
    default_project_folder = Path(os.environ["HOME"]).resolve() / ".claude" / "projects" / PROJECT_FOLDER_NAME
    default_project_folder.mkdir(parents=True)
    (default_project_folder / f"{OTHER_SESSION}.jsonl").write_bytes(b"")

    found = claude_family.find_agent_sessions(folders.log_directory)

    assert found.problems == []
    assert found.agent_sessions == [
        AgentSession(id=OTHER_SESSION, folder=default_project_folder),
        AgentSession(id=CLAUDE_SESSION, folder=folders.project_folder),
    ]


def test_a_project_folder_that_cannot_be_read_is_reported_and_the_rest_still_found(folders: Folders) -> None:
    folders.transcript.write_bytes(b"")
    locked_project_folder = folders.config_folder / "projects" / "-Users-ada-locked"
    locked_project_folder.mkdir()
    locked_project_folder.chmod(0o000)
    try:
        found = claude_family.find_agent_sessions(folders.log_directory)
    finally:
        locked_project_folder.chmod(0o700)

    assert found.agent_sessions == [AgentSession(id=CLAUDE_SESSION, folder=folders.project_folder)]
    assert found.problems == ["The Claude project folder -Users-ada-locked: Permission denied."]


def test_a_projects_folder_that_is_a_symbolic_link_is_reported_and_not_followed(
    folders: Folders, tmp_path: Path
) -> None:
    folders.transcript.write_bytes(b"")
    moved_projects_folder = tmp_path / "moved-projects"
    (folders.config_folder / "projects").rename(moved_projects_folder)
    (folders.config_folder / "projects").symlink_to(moved_projects_folder)

    found = claude_family.find_agent_sessions(folders.log_directory)

    assert found.agent_sessions == []
    [problem] = found.problems
    assert "is a symbolic link, which goodall does not follow" in problem


# ---------------------------------------------------------------------------
# Copying one Claude session


def files_and_bytes(folder: Path) -> dict[str, bytes]:
    """Every file under `folder`, by its path relative to `folder`, with its exact contents."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            found[str(path.relative_to(folder))] = path.read_bytes()
    return found


def test_everything_the_family_keeps_for_a_claude_session_is_copied_in_its_own_layout_and_nothing_else(
    folders: Folders,
) -> None:
    in_projects = {
        f"{CLAUDE_SESSION}.jsonl": b'{"type":"user"}\n\xff\n',
        f"{CLAUDE_SESSION}/custom-title.json": b'{"customTitle":"synthetic"}\n',
        f"{CLAUDE_SESSION}/subagents/agent-a1b2c3d4.jsonl": b'{"type":"assistant"}\n',
        f"{CLAUDE_SESSION}/subagents/agent-a1b2c3d4.meta.json": b"{}\n",
        f"{CLAUDE_SESSION}/tool-results/toolu_01.txt": b"3 passed\n",
        f"{CLAUDE_SESSION}/tool-results/pdf-01/page-1.png": b"\x89PNG synthetic",
        "MEMORY.md": b"# Memory\n",
        "memory/notes.md": b"synthetic note\n",
    }
    for relative_path, contents in in_projects.items():
        path = folders.project_folder / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    # Another Claude session in the same project, which is not the one copied.
    (folders.project_folder / f"{OTHER_SESSION}.jsonl").write_bytes(b'{"type":"user"}\n')
    in_config_folder = {
        "history.jsonl": b'{"display":"synthetic prompt"}\n',
        f"file-history/{CLAUDE_SESSION}/a1b2c3@v1": b"before the edit\n",
    }
    not_copied = {
        "settings.json": b'{"env":{}}\n',
        ".credentials.json": b'{"synthetic":"not a real credential"}\n',
        f"file-history/{OTHER_SESSION}/d4e5f6@v1": b"another session's backup\n",
        f"session-env/{CLAUDE_SESSION}/env": b"synthetic\n",
    }
    for relative_path, contents in {**in_config_folder, **not_copied}.items():
        path = folders.config_folder / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    copying = claude_family.copy_agent_session(folders.agent_session, folders.log_directory, NOW)

    assert copying.skipped == []
    assert files_and_bytes(folders.log_directory / "transcripts") == {
        f"{PROJECT_FOLDER_NAME}/{relative_path}": contents for relative_path, contents in in_projects.items()
    }
    config_copies_name = claude_family.mirror_folder_name(folders.config_folder)
    assert files_and_bytes(folders.log_directory / "claude-config") == {
        f"{config_copies_name}/{relative_path}": contents for relative_path, contents in in_config_folder.items()
    }


def test_copied_claude_sessions_are_found_in_the_log_directory_and_read_as_the_originals_are(
    folders: Folders,
) -> None:
    write_changed_at(folders.transcript, record(STARTED_AT + timedelta(minutes=5)), STARTED_AT + timedelta(minutes=5))
    claude_family.copy_agent_session(folders.agent_session, folders.log_directory, NOW)

    found = claude_family.find_copied_agent_sessions(folders.log_directory)

    copies_project_folder = folders.log_directory / "transcripts" / PROJECT_FOLDER_NAME
    assert found.agent_sessions == [AgentSession(id=CLAUDE_SESSION, folder=copies_project_folder)]
    assert found.problems == []
    assert claude_family.has_record_inside(found.agent_sessions[0], STARTED_AT, ENDED_AT)


def test_with_nothing_copied_no_copied_claude_session_is_found(folders: Folders) -> None:
    assert claude_family.find_copied_agent_sessions(folders.log_directory).agent_sessions == []


def test_the_copied_places_of_a_claude_session_are_its_own_copies_and_never_a_shared_one(folders: Folders) -> None:
    log_directory = folders.log_directory
    config_copies_name = claude_family.mirror_folder_name(folders.config_folder)
    owned = [
        f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl",
        f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/tool-results/a.txt",
        f"superseded-transcripts/20261116T120000.000000Z/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl",
        f"claude-config/{config_copies_name}/file-history/{CLAUDE_SESSION}/a@v1",
        f"superseded-claude-config/20261116T120000.000000Z/{config_copies_name}/file-history/{CLAUDE_SESSION}/a@v1",
    ]
    shared_or_another_sessions = [
        f"transcripts/{PROJECT_FOLDER_NAME}/{OTHER_SESSION}.jsonl",
        f"transcripts/{PROJECT_FOLDER_NAME}/MEMORY.md",
        f"claude-config/{config_copies_name}/history.jsonl",
        f"claude-config/{config_copies_name}/file-history/{OTHER_SESSION}/b@v1",
    ]
    for relative_path in owned + shared_or_another_sessions:
        path = log_directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic\n")

    places = claude_family.copied_places(log_directory, CLAUDE_SESSION)

    assert sorted(str(place.relative_to(log_directory)) for place in places) == [
        f"claude-config/{config_copies_name}/file-history/{CLAUDE_SESSION}",
        f"superseded-claude-config/20261116T120000.000000Z/{config_copies_name}/file-history/{CLAUDE_SESSION}",
        f"superseded-transcripts/20261116T120000.000000Z/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl",
        f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}",
        f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl",
    ]


def test_an_id_that_is_not_a_claude_session_id_has_no_copied_places(folders: Folders) -> None:
    (folders.log_directory / "transcripts" / PROJECT_FOLDER_NAME).mkdir(parents=True)

    assert claude_family.copied_places(folders.log_directory, "..") == []


def test_the_allow_list_for_the_claude_config_folder_names_only_the_prompt_history_and_file_edit_backups() -> None:
    # Widening this list means reading more of a folder that holds credentials:
    # it takes a decision, and this test changing with it.
    assert claude_family.CLAUDE_CONFIG_FOLDER_ALLOW_LIST == ["history.jsonl", "file-history/<session id>"]


def test_the_family_tells_the_baseline_its_name_and_the_folders_that_hold_its_copies() -> None:
    assert claude_family.APP_FAMILY.name == "Claude"
    assert claude_family.APP_FAMILY.copies_folder_names == ("transcripts", "claude-config")


# ---------------------------------------------------------------------------
# Remembering where hooks found Claude Code


def remembered_folders(log_directory: Path) -> list[str]:
    remembered_file = log_directory / "transcript-sync" / "claude-config-folders.json"
    remembered: list[str] = json.loads(remembered_file.read_text())["claude_config_folders"]
    return remembered


def test_remembering_a_folder_already_remembered_leaves_the_file_untouched(folders: Folders) -> None:
    claude_family.remember_claude_config_folder(folders.log_directory, folders.config_folder)
    remembered_file = folders.log_directory / "transcript-sync" / "claude-config-folders.json"
    inode_before = remembered_file.stat().st_ino

    claude_family.remember_claude_config_folder(folders.log_directory, folders.config_folder)

    assert remembered_folders(folders.log_directory) == [str(folders.config_folder)]
    assert remembered_file.stat().st_ino == inode_before


def test_remembered_folders_never_grow_past_the_limit_and_the_oldest_is_forgotten_first(
    folders: Folders, tmp_path: Path
) -> None:
    folder_count = claude_family.REMEMBERED_FOLDERS_LIMIT + 3
    for number in range(folder_count):
        claude_family.remember_claude_config_folder(folders.log_directory, tmp_path / f"claude-{number}")

    remembered = remembered_folders(folders.log_directory)

    assert len(remembered) == claude_family.REMEMBERED_FOLDERS_LIMIT
    assert remembered[0] == str(tmp_path / "claude-3")
    assert remembered[-1] == str(tmp_path / f"claude-{folder_count - 1}")


def test_a_remembered_folders_file_that_cannot_be_read_as_json_is_replaced(folders: Folders) -> None:
    sync_folder = folders.log_directory / "transcript-sync"
    sync_folder.mkdir()
    (sync_folder / "claude-config-folders.json").write_text('{"claude_config_folders": ["cut sh')

    claude_family.remember_claude_config_folder(folders.log_directory, folders.config_folder)

    assert remembered_folders(folders.log_directory) == [str(folders.config_folder)]


def test_hooks_remembering_different_folders_at_the_same_moment_lose_none_of_them(
    folders: Folders, tmp_path: Path
) -> None:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from goodall import claude_family\n"
        "claude_family.remember_claude_config_folder(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    )
    processes = []
    for number in range(8):
        config_folder = tmp_path / f"claude-{number}"
        processes.append(
            subprocess.Popen([sys.executable, "-c", script, str(folders.log_directory), str(config_folder)])
        )
    for process in processes:
        assert process.wait(timeout=30) == 0

    expected = [str(tmp_path / f"claude-{number}") for number in range(8)]
    assert sorted(remembered_folders(folders.log_directory)) == sorted(expected)
