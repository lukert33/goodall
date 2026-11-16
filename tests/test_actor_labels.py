"""Tests for goodall.actor_labels: an actor label for every transcript entry.

Each test hands a hand-written transcript from tests/fixtures/ to
`label_transcript`, the module's entry point, and checks what comes back.
Every fixture is synthetic: invented text, placeholder paths and ids, shaped
like the entries Claude Code writes. Real transcripts hold a contributor's
private prompts and must never be copied into fixtures.
"""

import subprocess
import sys
from pathlib import Path

from goodall.actor_labels import (
    ACTOR_LABEL_RULES,
    ACTOR_LABEL_RULES_VERSION,
    EntryLabel,
    LabelSummary,
    UnmatchedEntry,
    label_transcript,
    summarise_labels,
)
from goodall.human_actions import parse_human_actions

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_transcripts"


def test_every_entry_gets_one_label_and_the_rule_that_decided_it() -> None:
    result = label_transcript(FIXTURES / "actor_labels.jsonl")

    assert result.labels == [
        EntryLabel(1, None, "file-history-snapshot", "harness", "bookkeeping_entry", mixed=False),
        EntryLabel(2, "u-0002", "user", "human", "person_typed_prompt", mixed=False),
        # The harness put a reminder in front of what the person typed: the
        # person is the primary producer, and the entry is marked mixed.
        EntryLabel(3, "u-0003", "user", "human", "person_typed_prompt_behind_harness_context", mixed=True),
        EntryLabel(4, "a-0004", "assistant", "ai", "model_wrote_assistant_entry", mixed=False),
        EntryLabel(5, "a-0005", "assistant", "ai", "model_wrote_assistant_entry", mixed=False),
        EntryLabel(6, "u-0006", "user", "harness", "harness_returned_tool_results", mixed=False),
        EntryLabel(7, "a-0007", "assistant", "ai", "model_wrote_assistant_entry", mixed=False),
        EntryLabel(8, "u-0008", "user", "human", "person_rejected_tool_call", mixed=True),
        EntryLabel(9, "u-0009", "user", "harness", "interruption_marker_closing_a_rejection", mixed=False),
        EntryLabel(10, "u-0010", "user", "human", "person_interrupted", mixed=True),
        EntryLabel(11, "u-0011", "user", "harness", "harness_injected_user_entry", mixed=False),
        EntryLabel(12, "u-0012", "user", "harness", "harness_injected_user_entry", mixed=False),
        EntryLabel(13, "u-0013", "user", "harness", "harness_injected_user_entry", mixed=False),
        EntryLabel(14, "a-0014", "assistant", "harness", "harness_wrote_assistant_entry", mixed=False),
        EntryLabel(15, "u-0015", "user", "ai", "agent_instructed_subagent", mixed=False),
        EntryLabel(16, "a-0016", "assistant", "ai", "model_wrote_assistant_entry", mixed=False),
        EntryLabel(17, "t-0017", "attachment", "harness", "bookkeeping_entry", mixed=False),
        EntryLabel(18, "s-0018", "system", "harness", "bookkeeping_entry", mixed=False),
        EntryLabel(23, "u-0023", "user", "harness", "harness_injected_user_entry", mixed=False),
    ]
    # Line 21 is blank: it holds no entry, so it is neither labelled nor
    # reported. Everything else that no rule decided is reported, not guessed.
    assert result.unmatched == [
        UnmatchedEntry(19, None, None, "line is not valid JSON"),
        UnmatchedEntry(20, "x-0020", "example-future-entry", "unknown entry type 'example-future-entry'"),
        UnmatchedEntry(22, "u-0022", "user", "no rule matched this user entry (origin kind 'peer')"),
        UnmatchedEntry(24, None, None, "line is JSON but not an object"),
    ]


def test_the_result_says_which_rules_version_produced_it() -> None:
    result = label_transcript(FIXTURES / "actor_labels.jsonl")

    assert result.rules_version == ACTOR_LABEL_RULES_VERSION
    assert result.rules_version == "1"


def test_the_module_loads_nothing_outside_the_standard_library() -> None:
    # A fresh interpreter, so modules pytest itself imported do not count.
    # Only modules the import adds are checked: the interpreter's own start-up
    # (a virtual environment's helper, for example) is not goodall's doing.
    probe = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import goodall.actor_labels\n"
        "outside = set()\n"
        "for name in set(sys.modules) - before:\n"
        "    top = name.split('.')[0]\n"
        "    if top not in sys.stdlib_module_names and top != 'goodall':\n"
        "        outside.add(top)\n"
        "print(sorted(outside))\n"
    )
    completed = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True)

    assert completed.stdout.strip() == "[]"


def test_every_rule_has_a_unique_name_and_a_one_sentence_description() -> None:
    names = [rule.name for rule in ACTOR_LABEL_RULES]
    assert len(names) == len(set(names))
    for rule in ACTOR_LABEL_RULES:
        assert rule.description.endswith(".")
        assert rule.description.count(". ") == 0, rule.name


def test_human_labels_fall_on_exactly_the_lines_holding_a_human_action() -> None:
    # The person is decided by goodall.human_actions, so on its own fixtures
    # the human labels and the human actions must sit on the same lines.
    for fixture_name in ("typed_prompts.jsonl", "interruptions.jsonl", "rejections.jsonl"):
        labels = label_transcript(FIXTURES / fixture_name).labels
        human_lines = [label.line_number for label in labels if label.actor_label == "human"]
        action_lines = sorted({action.line_number for action in parse_human_actions(FIXTURES / fixture_name).actions})
        assert human_lines == action_lines, fixture_name


def test_the_summary_shows_how_a_transcript_divides() -> None:
    summary = summarise_labels(label_transcript(FIXTURES / "actor_labels.jsonl"))

    assert summary == LabelSummary(
        rules_version="1",
        entries_by_label={"human": 4, "ai": 5, "harness": 10},
        entries_by_rule={
            "person_typed_prompt": 1,
            "person_typed_prompt_behind_harness_context": 1,
            "person_rejected_tool_call": 1,
            "person_interrupted": 1,
            "harness_wrote_assistant_entry": 1,
            "model_wrote_assistant_entry": 4,
            "harness_returned_tool_results": 1,
            "interruption_marker_closing_a_rejection": 1,
            "harness_injected_user_entry": 4,
            "agent_instructed_subagent": 1,
            "bookkeeping_entry": 3,
        },
        mixed_entries=3,
        unmatched_entries=4,
    )


def test_the_summary_lists_every_label_and_rule_even_when_unused() -> None:
    # A zero is information: it says the rule ran and matched nothing.
    summary = summarise_labels(label_transcript(FIXTURES / "typed_prompts.jsonl"))

    assert summary.entries_by_label["harness"] == 0
    assert summary.entries_by_rule["person_rejected_tool_call"] == 0
