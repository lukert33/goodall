"""Unit tests for reading what Claude Code sends a hook on standard input (goodall.hook)."""

import json

from goodall.hook import read_hook_input, unreadable_input_category

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"


def test_hook_input_gives_the_claude_session_and_transcript() -> None:
    text = json.dumps(
        {
            "session_id": CLAUDE_SESSION,
            "transcript_path": f"/Users/ada/.claude/projects/-Users-ada-work/{CLAUDE_SESSION}.jsonl",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
    )

    hook_input = read_hook_input(text)

    assert hook_input is not None
    assert hook_input.claude_session == CLAUDE_SESSION
    assert str(hook_input.transcript_path) == f"/Users/ada/.claude/projects/-Users-ada-work/{CLAUDE_SESSION}.jsonl"
    assert hook_input.fields["stop_hook_active"] is False


def test_hook_input_without_a_transcript_path_still_reads() -> None:
    hook_input = read_hook_input(json.dumps({"session_id": CLAUDE_SESSION}))

    assert hook_input is not None
    assert hook_input.transcript_path is None


def test_hook_input_that_is_not_a_json_object_is_unreadable() -> None:
    assert read_hook_input(None) is None
    assert read_hook_input("") is None
    assert read_hook_input("{not json") is None
    assert read_hook_input("[1, 2]") is None


def test_hook_input_without_a_claude_session_id_is_unreadable() -> None:
    # Without the session id goodall cannot tie the Claude session's
    # transcript to the work block, so the input is of no use to the gate.
    assert read_hook_input(json.dumps({"prompt": "hello"})) is None
    assert read_hook_input(json.dumps({"session_id": 42})) is None
    assert read_hook_input(json.dumps({"session_id": "not-a-session-id"})) is None


def test_unreadable_hook_input_is_put_in_one_fixed_category_that_holds_none_of_it() -> None:
    assert unreadable_input_category(None) == "empty"
    assert unreadable_input_category("  \n") == "empty"
    assert unreadable_input_category("{not json") == "not_json"
    assert unreadable_input_category("[1, 2]") == "not_json"
    assert unreadable_input_category(json.dumps({"prompt": "hello"})) == "no_session_id"
    assert unreadable_input_category(json.dumps({"session_id": 42})) == "session_id_not_uuid"
    assert unreadable_input_category(json.dumps({"session_id": "not-a-session-id"})) == "session_id_not_uuid"


def test_readable_hook_input_has_no_category() -> None:
    assert unreadable_input_category(json.dumps({"session_id": CLAUDE_SESSION})) is None
