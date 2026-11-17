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

    model = ("model:example-model",)
    assert result.labels == [
        EntryLabel(1, None, "file-history-snapshot", "harness", "bookkeeping_entry", False, initiated_by="harness"),
        # The label says a person; which person is not in a transcript, so
        # no identity is claimed.
        EntryLabel(2, "u-0002", "user", "human", "person_typed_prompt", False, initiated_by="human"),
        # The harness put a reminder in front of what the person typed: the
        # person is the primary producer, and the entry is marked mixed.
        EntryLabel(
            3, "u-0003", "user", "human", "person_typed_prompt_behind_harness_context", True, initiated_by="human"
        ),
        # What prompted a model's reply is not decided here, so it is empty.
        EntryLabel(4, "a-0004", "assistant", "ai", "model_wrote_assistant_entry", False, identity=model),
        EntryLabel(5, "a-0005", "assistant", "ai", "model_wrote_assistant_entry", False, identity=model),
        EntryLabel(6, "u-0006", "user", "harness", "harness_returned_tool_results", False, initiated_by="ai"),
        EntryLabel(7, "a-0007", "assistant", "ai", "model_wrote_assistant_entry", False, identity=model),
        EntryLabel(8, "u-0008", "user", "human", "person_rejected_tool_call", True, initiated_by="human"),
        EntryLabel(
            9, "u-0009", "user", "harness", "interruption_marker_closing_a_rejection", False, initiated_by="human"
        ),
        EntryLabel(10, "u-0010", "user", "human", "person_interrupted", True, initiated_by="human"),
        EntryLabel(11, "u-0011", "user", "harness", "harness_injected_user_entry", False, initiated_by="harness"),
        # A slash command the person typed, recorded in harness tags.
        EntryLabel(12, "u-0012", "user", "human", "person_typed_slash_command", True, initiated_by="human"),
        EntryLabel(13, "u-0013", "user", "harness", "harness_injected_user_entry", False, initiated_by="harness"),
        EntryLabel(
            14, "a-0014", "assistant", "harness", "harness_wrote_assistant_entry", False, initiated_by="harness"
        ),
        EntryLabel(15, "u-0015", "user", "ai", "agent_instructed_subagent", False, initiated_by="ai"),
        # A subagent's reply names both the subagent and its model.
        EntryLabel(
            16,
            "a-0016",
            "assistant",
            "ai",
            "model_wrote_assistant_entry",
            False,
            identity=("model:example-model", "subagent:agent-example"),
        ),
        EntryLabel(17, "t-0017", "attachment", "harness", "bookkeeping_entry", False, initiated_by="harness"),
        EntryLabel(18, "s-0018", "system", "harness", "bookkeeping_entry", False, initiated_by="harness"),
        # Another agent's message: an agent is an agent, whichever one it
        # was. The entry names it, so the identity says which.
        EntryLabel(
            22, "u-0022", "user", "ai", "another_agent_sent_message", False, ("peer:example-peer",), initiated_by="ai"
        ),
        # A compaction summary is the model's words. No boundary before it
        # says what started it, so who asked is empty.
        EntryLabel(23, "u-0023", "user", "ai", "model_summarised_conversation", False),
        # A coordinating agent's message names no agent, so no identity.
        EntryLabel(25, "u-0025", "user", "ai", "another_agent_sent_message", False, initiated_by="ai"),
    ]
    # Line 21 is blank: it holds no entry, so it is neither labelled nor
    # reported. Everything else that no rule decided is reported, not guessed.
    assert result.unmatched == [
        UnmatchedEntry(19, None, None, "line is not valid JSON"),
        UnmatchedEntry(20, "x-0020", "example-future-entry", "unknown entry type 'example-future-entry'"),
        UnmatchedEntry(24, None, None, "line is JSON but not an object"),
    ]


def test_the_result_says_which_rules_version_produced_it() -> None:
    result = label_transcript(FIXTURES / "actor_labels.jsonl")

    assert result.rules_version == ACTOR_LABEL_RULES_VERSION
    assert result.rules_version == "2"


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
    fixture_names = (
        "typed_prompts.jsonl",
        "interruptions.jsonl",
        "rejections.jsonl",
        "shell_commands.jsonl",
        "slash_commands.jsonl",
        "permission_decisions.jsonl",
        "questions_and_plans.jsonl",
        "queued_prompts.jsonl",
        "blocked_prompts.jsonl",
        "permission_modes.jsonl",
    )
    for fixture_name in fixture_names:
        labels = label_transcript(FIXTURES / fixture_name).labels
        human_lines = [label.line_number for label in labels if label.actor_label == "human"]
        action_lines = set()
        for action in parse_human_actions(FIXTURES / fixture_name).actions:
            if action.kind != "sdk_prompt":
                action_lines.add(action.line_number)
        assert human_lines == sorted(action_lines), fixture_name


def test_each_new_kind_of_human_action_has_its_own_rule() -> None:
    # The rule name says which kind of action made the entry the person's,
    # so labels can be regrouped along with the actions.
    rule_names_by_fixture = {
        "shell_commands.jsonl": {"person_ran_shell_command", "person_typed_prompt"},
        "slash_commands.jsonl": {"person_typed_slash_command", "person_typed_prompt"},
        "permission_decisions.jsonl": {"person_approved_tool_call", "person_dismissed_permission_prompt"},
        "questions_and_plans.jsonl": {"person_answered_question", "person_rejected_tool_call", "person_approved_plan"},
        "queued_prompts.jsonl": {"person_typed_prompt", "person_queued_prompt"},
        "blocked_prompts.jsonl": {"person_typed_prompt", "person_prompt_blocked_by_hook"},
    }
    for fixture_name, expected_rule_names in rule_names_by_fixture.items():
        labels = label_transcript(FIXTURES / fixture_name).labels
        rule_names = set()
        for label in labels:
            if label.actor_label == "human":
                rule_names.add(label.rule_name)
        assert rule_names == expected_rule_names, fixture_name


def test_titles_and_summaries_are_the_models_words_and_say_who_asked_for_them() -> None:
    # Chosen direction, open to revision: the label says who chose the
    # words, and `initiated_by` says who asked for them, so harness-initiated
    # model text can be left out of counts of task work.
    result = label_transcript(FIXTURES / "titles_and_compaction.jsonl")

    assert result.labels == [
        # The person asked for a compaction: that request is their action.
        EntryLabel(1, "u-0001", "user", "human", "person_typed_slash_command", mixed=True, initiated_by="human"),
        EntryLabel(2, "s-0002", "system", "harness", "bookkeeping_entry", mixed=False, initiated_by="harness"),
        # The summary that followed is the model's, asked for by the person.
        EntryLabel(
            3, "u-0003", "user", "ai", "model_summarised_conversation_when_asked", mixed=False, initiated_by="human"
        ),
        EntryLabel(4, "s-0004", "system", "harness", "bookkeeping_entry", mixed=False, initiated_by="harness"),
        # Claude Code compacted on its own when the context filled.
        EntryLabel(
            5, "u-0005", "user", "ai", "model_summarised_conversation_unprompted", mixed=False, initiated_by="harness"
        ),
        # No boundary says what started it, so who asked is left empty.
        EntryLabel(6, "u-0006", "user", "ai", "model_summarised_conversation", mixed=False, initiated_by=None),
        EntryLabel(7, None, "ai-title", "ai", "model_titled_session", mixed=False, initiated_by="harness"),
        # A custom title may be one the person typed or one an app chose.
        EntryLabel(8, None, "custom-title", "ai", "session_title_recorded", mixed=False, initiated_by=None),
    ]
    assert result.unmatched == []


def test_a_prompt_a_program_sent_is_left_unmatched_while_that_is_undecided() -> None:
    result = label_transcript(FIXTURES / "sdk_prompts.jsonl")

    undecided_lines = []
    for unmatched in result.unmatched:
        if unmatched.reason == "prompt sent through the SDK: whether that is the person is undecided":
            undecided_lines.append(unmatched.line_number)
    assert undecided_lines == [2, 9]


def test_the_summary_shows_how_a_transcript_divides() -> None:
    summary = summarise_labels(label_transcript(FIXTURES / "actor_labels.jsonl"))

    assert summary == LabelSummary(
        rules_version="2",
        entries_by_label={"human": 5, "ai": 8, "harness": 8},
        entries_by_rule={
            "person_typed_prompt": 1,
            "person_typed_prompt_behind_harness_context": 1,
            "person_rejected_tool_call": 1,
            "person_interrupted": 1,
            "person_ran_shell_command": 0,
            "person_typed_slash_command": 1,
            "person_approved_tool_call": 0,
            "person_dismissed_permission_prompt": 0,
            "person_answered_question": 0,
            "person_approved_plan": 0,
            "person_queued_prompt": 0,
            "person_prompt_blocked_by_hook": 0,
            "another_agent_sent_message": 2,
            "model_summarised_conversation_when_asked": 0,
            "model_summarised_conversation_unprompted": 0,
            "model_summarised_conversation": 1,
            "model_titled_session": 0,
            "session_title_recorded": 0,
            "harness_wrote_assistant_entry": 1,
            "model_wrote_assistant_entry": 4,
            "harness_returned_tool_results": 1,
            "interruption_marker_closing_a_rejection": 1,
            "harness_injected_user_entry": 2,
            "agent_instructed_subagent": 1,
            "bookkeeping_entry": 3,
        },
        mixed_entries=4,
        unmatched_entries=3,
    )


def test_the_summary_lists_every_label_and_rule_even_when_unused() -> None:
    # A zero is information: it says the rule ran and matched nothing.
    summary = summarise_labels(label_transcript(FIXTURES / "typed_prompts.jsonl"))

    assert summary.entries_by_label["harness"] == 0
    assert summary.entries_by_rule["person_rejected_tool_call"] == 0
