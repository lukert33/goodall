"""Unit tests for goodall.transcripts, the pieces behind transcript sync.

tests/test_transcript_sync.py drives the whole thing through `goodall hook`
and `goodall start` / `status`; these tests pin each piece on its own, with
synthetic files in tmp_path.
"""

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import transcripts

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
NOW = datetime(2026, 11, 17, 10, 15, 0, 123456, tzinfo=UTC)
RELATIVE_PATH = Path("-Users-ada-work") / f"{CLAUDE_SESSION}.jsonl"


@dataclass(frozen=True)
class Folders:
    """A synthetic Claude projects folder and a log directory, both inside tmp_path."""

    projects_folder: Path
    log_directory: Path

    @property
    def source(self) -> Path:
        return self.projects_folder / RELATIVE_PATH

    @property
    def copy(self) -> Path:
        return self.log_directory / "transcripts" / RELATIVE_PATH

    def copy_file(self) -> transcripts.FileOutcome:
        return transcripts.copy_file(self.projects_folder, self.log_directory, RELATIVE_PATH, NOW)


def make_folders(tmp_path: Path) -> Folders:
    projects_folder = tmp_path / "claude-config" / "projects"
    (projects_folder / "-Users-ada-work").mkdir(parents=True)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    return Folders(projects_folder, log_directory)


def test_copy_file_makes_a_byte_identical_owner_only_copy_with_the_sources_modification_time(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b'{"a":1}\n\xff\n')
    os.utime(folders.source, ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))

    outcome = folders.copy_file()

    assert outcome == transcripts.FileOutcome.COPIED
    assert folders.copy.read_bytes() == b'{"a":1}\n\xff\n'
    assert folders.copy.stat().st_mtime_ns == 1_700_000_000_123_456_789
    assert stat.S_IMODE(folders.copy.stat().st_mode) == 0o600
    for folder in [folders.log_directory / "transcripts", folders.copy.parent]:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_copy_file_leaves_a_copy_alone_when_size_and_modification_time_still_match(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"line\n")
    folders.copy_file()
    inode_before = folders.copy.stat().st_ino

    outcome = folders.copy_file()

    assert outcome == transcripts.FileOutcome.UNCHANGED
    assert folders.copy.stat().st_ino == inode_before


def test_copy_file_replaces_a_copy_that_the_grown_source_starts_with_and_keeps_nothing_aside(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"one\n")
    folders.copy_file()
    with folders.source.open("ab") as source:
        source.write(b"two\n")

    outcome = folders.copy_file()

    assert outcome == transcripts.FileOutcome.COPIED
    assert folders.copy.read_bytes() == b"one\ntwo\n"
    assert not (folders.log_directory / "superseded-transcripts").exists()


def test_copy_file_keeps_an_earlier_copy_the_source_no_longer_starts_with_under_the_moment_it_was_replaced(
    tmp_path: Path,
) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"one\ntwo\n")
    folders.copy_file()
    # Same length, different bytes: not a shrink, but still not an extension.
    folders.source.write_bytes(b"one\nTWO\n")

    outcome = folders.copy_file()

    assert outcome == transcripts.FileOutcome.COPIED_AND_EARLIER_COPY_KEPT
    assert folders.copy.read_bytes() == b"one\nTWO\n"
    kept_copy = folders.log_directory / "superseded-transcripts" / "20261117T101500.123456Z" / RELATIVE_PATH
    assert kept_copy.read_bytes() == b"one\ntwo\n"
    assert stat.S_IMODE(kept_copy.parent.stat().st_mode) == 0o700


def test_sweep_reports_a_project_folder_it_cannot_read_and_copies_the_rest(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read anything, so permissions cannot be taken away")
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"line\n")
    unreadable_project_folder = folders.projects_folder / "-Users-ada-private"
    unreadable_project_folder.mkdir()
    unreadable_project_folder.chmod(0)
    try:
        result = transcripts.sweep(folders.projects_folder, folders.log_directory, NOW)
    finally:
        unreadable_project_folder.chmod(0o700)

    assert result.claude_sessions_copied == 1
    assert folders.copy.read_bytes() == b"line\n"
    assert len(result.problems) == 1
    assert "-Users-ada-private" in result.problems[0]


def test_sweep_skips_symbolic_links_among_subagent_files(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"line\n")
    subagents_folder = folders.projects_folder / "-Users-ada-work" / CLAUDE_SESSION / "subagents"
    subagents_folder.mkdir(parents=True)
    (subagents_folder / "agent-a1.jsonl").write_bytes(b"subagent\n")
    private_file = tmp_path / "private.txt"
    private_file.write_bytes(b"private\n")
    (subagents_folder / "agent-a2.jsonl").symlink_to(private_file)

    transcripts.sweep(folders.projects_folder, folders.log_directory, NOW)

    copied_subagents_folder = folders.log_directory / "transcripts" / "-Users-ada-work" / CLAUDE_SESSION / "subagents"
    assert sorted(path.name for path in copied_subagents_folder.iterdir()) == ["agent-a1.jsonl"]


def test_copy_file_refuses_a_named_pipe_without_waiting_for_a_writer(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    os.mkfifo(folders.source)

    with pytest.raises(transcripts.NotARegularFile) as raised:
        folders.copy_file()

    assert raised.value.filename == str(folders.source)
    assert not folders.copy.exists()


def test_list_files_inside_finds_files_nested_far_deeper_than_python_would_recurse_comfortably(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    deep_folder = Path("-Users-ada-work") / CLAUDE_SESSION / "/".join(["d"] * 200)
    (folders.projects_folder / deep_folder).mkdir(parents=True)
    (folders.projects_folder / deep_folder / "result.txt").write_bytes(b"deep\n")

    files, skipped = transcripts.list_files_inside(folders.projects_folder, Path("-Users-ada-work") / CLAUDE_SESSION)

    assert files == [deep_folder / "result.txt"]
    assert skipped == []


def test_sweep_copies_the_rest_of_a_claude_session_and_reports_what_it_skipped(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"line\n")
    tool_results = folders.projects_folder / "-Users-ada-work" / CLAUDE_SESSION / "tool-results"
    tool_results.mkdir(parents=True)
    (tool_results / "a.txt").write_bytes(b"result\n")
    os.mkfifo(tool_results / "b.txt")

    result = transcripts.sweep(folders.projects_folder, folders.log_directory, NOW)

    assert result.claude_sessions_copied == 1
    copied_result = (
        folders.log_directory / "transcripts" / "-Users-ada-work" / CLAUDE_SESSION / "tool-results" / "a.txt"
    )
    assert copied_result.read_bytes() == b"result\n"
    assert len(result.problems) == 1
    assert CLAUDE_SESSION in result.problems[0]
    assert "tool-results/b.txt: a special file" in result.problems[0]


def test_a_note_about_skipped_files_names_the_first_few_and_counts_the_rest() -> None:
    skipped = [f"tool-results/{number}.txt: Permission denied" for number in range(8)]

    sentence = transcripts.describe_skipped(skipped)

    assert sentence.startswith("8 thing(s) in the Claude session's folder were not copied: tool-results/0.txt")
    assert "tool-results/4.txt" in sentence
    assert "tool-results/5.txt" not in sentence
    assert sentence.endswith("; and 3 more")
