"""Unit tests for goodall.transcripts, the pieces behind transcript sync.

tests/test_transcript_sync.py drives the whole thing through `goodall hook`
and `goodall start` / `status`; these tests pin each piece on its own, with
synthetic files in tmp_path.
"""

import os
import stat
from pathlib import Path

from goodall import transcripts

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"


def test_copy_file_makes_a_byte_identical_owner_only_copy_with_the_sources_modification_time(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b'{"a":1}\n\xff\n')
    os.utime(source, ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))
    destination = tmp_path / "copy" / "source.jsonl"
    destination.parent.mkdir()

    outcome = transcripts.copy_file(source, destination, tmp_path / "superseded" / "source.jsonl")

    assert outcome == transcripts.FileOutcome.COPIED
    assert destination.read_bytes() == b'{"a":1}\n\xff\n'
    assert destination.stat().st_mtime_ns == 1_700_000_000_123_456_789
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def make_projects_folder(tmp_path: Path) -> Path:
    """A synthetic Claude projects folder holding one project folder, and the transcript path inside it."""
    projects_folder = tmp_path / "claude-config" / "projects"
    (projects_folder / "-Users-ada-work").mkdir(parents=True)
    return projects_folder


def test_copy_from_hook_copies_the_named_transcript_into_owner_only_folders_of_the_same_layout(tmp_path: Path) -> None:
    projects_folder = make_projects_folder(tmp_path)
    transcript = projects_folder / "-Users-ada-work" / f"{CLAUDE_SESSION}.jsonl"
    transcript.write_bytes(b"line\n")
    log_directory = tmp_path / "logs"
    log_directory.mkdir()

    transcripts.copy_from_hook(projects_folder, log_directory, CLAUDE_SESSION, transcript)

    copied_project_folder = log_directory / "transcripts" / "-Users-ada-work"
    assert (copied_project_folder / f"{CLAUDE_SESSION}.jsonl").read_bytes() == b"line\n"
    for folder in [log_directory / "transcripts", copied_project_folder]:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
