"""Unit tests for goodall.verbatim_copies: copying one file, or one place, byte for byte.

tests/test_agent_session_copies.py drives the whole thing through `goodall
start` and `goodall stop`; these tests pin each piece on its own, with
synthetic files in tmp_path.
"""

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import verbatim_copies

AGENT_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
NOW = datetime(2026, 11, 17, 10, 15, 0, 123456, tzinfo=UTC)
RELATIVE_PATH = Path("-Users-ada-work") / f"{AGENT_SESSION}.jsonl"


@dataclass(frozen=True)
class Folders:
    """A synthetic folder of a source application's and a log directory, both inside tmp_path."""

    source_folder: Path
    log_directory: Path

    @property
    def mirror(self) -> verbatim_copies.Mirror:
        return verbatim_copies.Mirror(self.source_folder, ("copies",), "superseded-copies")

    @property
    def source(self) -> Path:
        return self.source_folder / RELATIVE_PATH

    @property
    def copy(self) -> Path:
        return self.log_directory / "copies" / RELATIVE_PATH

    def copy_file(self) -> verbatim_copies.FileOutcome:
        return verbatim_copies.copy_file(self.mirror, self.log_directory, RELATIVE_PATH, NOW)


def make_folders(tmp_path: Path) -> Folders:
    source_folder = tmp_path / "application"
    (source_folder / "-Users-ada-work").mkdir(parents=True)
    log_directory = tmp_path / "logs"
    log_directory.mkdir()
    return Folders(source_folder, log_directory)


def test_copy_file_makes_a_byte_identical_owner_only_copy_with_the_sources_modification_time(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b'{"a":1}\n\xff\n')
    os.utime(folders.source, ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))

    outcome = folders.copy_file()

    assert outcome == verbatim_copies.FileOutcome.COPIED
    assert folders.copy.read_bytes() == b'{"a":1}\n\xff\n'
    assert folders.copy.stat().st_mtime_ns == 1_700_000_000_123_456_789
    assert stat.S_IMODE(folders.copy.stat().st_mode) == 0o600
    for folder in [folders.log_directory / "copies", folders.copy.parent]:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_copy_file_leaves_a_copy_alone_when_size_and_modification_time_still_match(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"line\n")
    folders.copy_file()
    inode_before = folders.copy.stat().st_ino

    outcome = folders.copy_file()

    assert outcome == verbatim_copies.FileOutcome.UNCHANGED
    assert folders.copy.stat().st_ino == inode_before


def test_copy_file_replaces_a_copy_that_the_grown_source_starts_with_and_keeps_nothing_aside(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"one\n")
    folders.copy_file()
    with folders.source.open("ab") as source:
        source.write(b"two\n")

    outcome = folders.copy_file()

    assert outcome == verbatim_copies.FileOutcome.COPIED
    assert folders.copy.read_bytes() == b"one\ntwo\n"
    assert not (folders.log_directory / "superseded-copies").exists()


def test_copy_file_keeps_an_earlier_copy_the_source_no_longer_starts_with_under_the_moment_it_was_replaced(
    tmp_path: Path,
) -> None:
    folders = make_folders(tmp_path)
    folders.source.write_bytes(b"one\ntwo\n")
    folders.copy_file()
    # Same length, different bytes: not a shrink, but still not an extension.
    folders.source.write_bytes(b"one\nTWO\n")

    outcome = folders.copy_file()

    assert outcome == verbatim_copies.FileOutcome.COPIED_AND_EARLIER_COPY_KEPT
    assert folders.copy.read_bytes() == b"one\nTWO\n"
    kept_copy = folders.log_directory / "superseded-copies" / "20261117T101500.123456Z" / RELATIVE_PATH
    assert kept_copy.read_bytes() == b"one\ntwo\n"
    assert stat.S_IMODE(kept_copy.parent.stat().st_mode) == 0o700


def test_copy_file_refuses_a_named_pipe_without_waiting_for_a_writer(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    os.mkfifo(folders.source)

    with pytest.raises(verbatim_copies.NotARegularFile) as raised:
        folders.copy_file()

    assert raised.value.filename == str(folders.source)
    assert not folders.copy.exists()


def test_list_files_at_finds_files_nested_far_deeper_than_python_would_recurse_comfortably(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    deep_folder = Path("-Users-ada-work") / AGENT_SESSION / "/".join(["d"] * 200)
    (folders.source_folder / deep_folder).mkdir(parents=True)
    (folders.source_folder / deep_folder / "result.txt").write_bytes(b"deep\n")

    files, skipped = verbatim_copies.list_files_at(folders.source_folder, Path("-Users-ada-work") / AGENT_SESSION)

    assert files == [deep_folder / "result.txt"]
    assert skipped == []


def test_copy_places_copies_a_file_and_a_whole_folder_and_notes_the_links_it_did_not_follow(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    session_folder = folders.source_folder / "-Users-ada-work" / AGENT_SESSION
    (session_folder / "tool-results").mkdir(parents=True)
    folders.source.write_bytes(b"transcript\n")
    (session_folder / "tool-results" / "result.txt").write_bytes(b"result\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not the application's\n")
    (session_folder / "tool-results" / "link.txt").symlink_to(outside)
    places = [RELATIVE_PATH, Path("-Users-ada-work") / AGENT_SESSION, Path("-Users-ada-work") / "nothing-here"]

    copying = verbatim_copies.copy_places(folders.mirror, folders.log_directory, places, NOW)

    copies_folder = folders.log_directory / "copies" / "-Users-ada-work"
    assert folders.copy.read_bytes() == b"transcript\n"
    assert (copies_folder / AGENT_SESSION / "tool-results" / "result.txt").read_bytes() == b"result\n"
    assert not (copies_folder / AGENT_SESSION / "tool-results" / "link.txt").exists()
    assert copying.skipped == [
        f"-Users-ada-work/{AGENT_SESSION}/tool-results/link.txt: a symbolic link, which goodall does not follow"
    ]
    assert copying.copied_anything()


def test_a_note_about_skipped_files_names_the_first_few_and_counts_the_rest() -> None:
    skipped = [f"tool-results/{number}.txt: Permission denied" for number in range(8)]

    sentence = verbatim_copies.describe_skipped(skipped)

    assert sentence.startswith("8 thing(s) were not copied: tool-results/0.txt")
    assert "tool-results/4.txt" in sentence
    assert "tool-results/5.txt" not in sentence
    assert sentence.endswith("; and 3 more")


def test_stale_temporary_files_are_removed_and_fresh_ones_left_for_the_copy_still_running(tmp_path: Path) -> None:
    folders = make_folders(tmp_path)
    copies_folder = folders.log_directory / "copies"
    copies_folder.mkdir()
    stale = copies_folder / ".session.jsonl.1a2b3c4d.goodall-tmp"
    fresh = copies_folder / ".session.jsonl.5e6f7a8b.goodall-tmp"
    for temporary_file in [stale, fresh]:
        temporary_file.write_bytes(b"half a copy")
    an_hour_ago = NOW.timestamp() - 3600
    os.utime(stale, (an_hour_ago, an_hour_ago))
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))

    verbatim_copies.remove_stale_temporary_files(copies_folder, NOW)

    assert not stale.exists()
    assert fresh.exists()
