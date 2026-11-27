"""Unit tests for goodall.timeline_claude: which entries a copied Claude transcript gives.

The transcripts are the synthetic fixtures goodall's other rules are tested
with, placed where goodall.claude_family copies a Claude session to.
"""

import json
from pathlib import Path

import pytest

from goodall import timeline_claude
from goodall.timeline_entries import TimelineEntry

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_transcripts"
PROJECT_FOLDER_NAME = "-Users-ada-work"
CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"


@pytest.fixture
def log_directory(tmp_path: Path) -> Path:
    folder = tmp_path / "logs"
    (folder / "transcripts" / PROJECT_FOLDER_NAME).mkdir(parents=True)
    return folder


def copy_fixture(log_directory: Path, fixture_name: str, claude_session: str = CLAUDE_SESSION) -> Path:
    """Place one fixture transcript where a copied Claude session's transcript is; return its path there."""
    transcript = log_directory / "transcripts" / PROJECT_FOLDER_NAME / f"{claude_session}.jsonl"
    transcript.write_bytes((FIXTURES / fixture_name).read_bytes())
    return transcript


def in_brief(entries: list[TimelineEntry]) -> list[tuple[int | None, str | None, str, str | None, str | None]]:
    """Each entry as (line, actor, kind, tool, outcome), in line order."""
    brief = []
    for entry in sorted(entries, key=lambda entry: entry.position or 0):
        brief.append((entry.position, entry.actor, entry.kind, entry.tool, entry.outcome))
    return brief


def test_a_tool_result_that_carries_the_persons_decision_appears_as_that_decision(log_directory: Path) -> None:
    copy_fixture(log_directory, "permission_decisions.jsonl")

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    assert gathered.problems == []
    assert in_brief(gathered.entries)[:4] == [
        (1, "ai", "tool_call", "Bash", None),
        (2, "human", "tool_call_approval_once", "Bash", None),
        (3, "ai", "tool_call", "Edit", None),
        (4, "human", "tool_call_approval_always", "Edit", None),
    ]


def test_a_prompt_sent_through_the_sdk_has_no_actor_because_whether_it_is_the_person_is_undecided(
    log_directory: Path,
) -> None:
    copy_fixture(log_directory, "sdk_prompts.jsonl")

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    sdk_prompts = [entry for entry in gathered.entries if entry.kind == "sdk_prompt"]
    assert sdk_prompts != []
    for entry in sdk_prompts:
        assert entry.actor is None
        assert entry.category == "undecided"


def test_a_line_with_no_time_is_in_no_entry_and_is_counted(log_directory: Path) -> None:
    transcript = copy_fixture(log_directory, "titles_and_compaction.jsonl")
    timed_lines = 0
    for line in transcript.read_text().splitlines():
        if "timestamp" in json.loads(line):
            timed_lines += 1

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    assert gathered.without_a_time == 2
    assert len({entry.position for entry in gathered.entries}) == timed_lines == 6


def test_malformed_lines_and_unknown_types_never_stop_the_rest_and_keep_none_of_their_text(log_directory: Path) -> None:
    copy_fixture(log_directory, "unfamiliar_and_malformed.jsonl")

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    assert gathered.problems == []
    unfamiliar = [entry for entry in gathered.entries if entry.kind == "unfamiliar_entry"]
    for entry in unfamiliar:
        assert (entry.actor, entry.entry_type, entry.label_rule) == (None, None, None)
    assert len(gathered.entries) + gathered.without_a_time >= 8


def test_a_subagents_transcript_is_read_and_marked_and_a_tools_saved_output_is_not_a_transcript(
    log_directory: Path,
) -> None:
    copy_fixture(log_directory, "session-u.jsonl")
    session_folder = log_directory / "transcripts" / PROJECT_FOLDER_NAME / CLAUDE_SESSION
    (session_folder / "subagents").mkdir(parents=True)
    subagent_fixture = FIXTURES / "session-u" / "subagents" / "agent-example.jsonl"
    (session_folder / "subagents" / "agent-example.jsonl").write_bytes(subagent_fixture.read_bytes())
    (session_folder / "tool-results").mkdir()
    saved_output = '{"type":"user","timestamp":"2026-03-04T09:00:00.000Z","message":{"content":"SYNTHETIC"}}\n'
    (session_folder / "tool-results" / "saved.jsonl").write_text(saved_output)

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    paths = {str(entry.raw_record.path) for entry in gathered.entries}
    assert paths == {
        f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}.jsonl",
        f"transcripts/{PROJECT_FOLDER_NAME}/{CLAUDE_SESSION}/subagents/agent-example.jsonl",
    }
    for entry in gathered.entries:
        assert entry.in_subagent == ("subagents" in entry.raw_record.path.parts)


def test_earlier_copies_kept_aside_are_not_read_so_nothing_counts_twice(log_directory: Path) -> None:
    copy_fixture(log_directory, "typed_prompts.jsonl")
    kept_aside = log_directory / "superseded-transcripts" / "20261117T101500.123456Z" / PROJECT_FOLDER_NAME
    kept_aside.mkdir(parents=True)
    (kept_aside / f"{CLAUDE_SESSION}.jsonl").write_bytes((FIXTURES / "typed_prompts.jsonl").read_bytes())

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    assert {entry.raw_record.path.parts[0] for entry in gathered.entries} == {"transcripts"}


def test_a_line_with_several_content_blocks_gives_an_entry_for_each_in_the_lines_order(log_directory: Path) -> None:
    transcript = log_directory / "transcripts" / PROJECT_FOLDER_NAME / f"{CLAUDE_SESSION}.jsonl"
    content = [
        {"type": "thinking", "thinking": "SYNTHETIC", "signature": "synthetic"},
        {"type": "text", "text": "SYNTHETIC"},
        {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/SYNTHETIC"}},
        {"type": "tool_use", "id": "toolu_2", "name": "SyntheticToolSomeoneNamed", "input": {}},
    ]
    line = {
        "type": "assistant",
        "uuid": "a-1",
        "timestamp": "2026-11-16T09:35:00.000Z",
        "message": {"role": "assistant", "model": "example-model", "content": content},
    }
    transcript.write_text(json.dumps(line) + "\n")

    gathered = timeline_claude.entries_from_transcripts(log_directory)

    assert [(entry.kind, entry.tool, entry.actor) for entry in gathered.entries] == [
        ("thinking", None, "ai"),
        ("message_text", None, "ai"),
        ("tool_call", "Read", "ai"),
        ("tool_call", "other_tool", "ai"),
    ]
    assert {entry.position for entry in gathered.entries} == {1}


def test_a_transcript_that_cannot_be_read_is_a_problem_naming_the_file_and_the_others_are_still_read(
    log_directory: Path,
) -> None:
    copy_fixture(log_directory, "typed_prompts.jsonl")
    unreadable = copy_fixture(log_directory, "rejections.jsonl", "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3")
    unreadable.chmod(0o000)
    try:
        gathered = timeline_claude.entries_from_transcripts(log_directory)
    finally:
        unreadable.chmod(0o600)

    assert gathered.problems == [
        f"The copied transcript transcripts/{PROJECT_FOLDER_NAME}/5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3.jsonl: "
        "Permission denied."
    ]
    assert gathered.entries != []
