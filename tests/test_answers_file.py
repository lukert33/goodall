"""Tests for goodall.answers_file: reading and checking an answers file.

An answers file is what the setup page saves, or what a person writes by
hand for unattended setup (docs/answers-file.md). These tests hand the reader
text and check what comes back: the answers, or failures that each say what
to do next and never repeat a value that could be a secret.
"""

from pathlib import Path

from goodall.answers_file import Answers, read_answers_file, read_answers_text
from goodall.terminal import Failure

ANSWERS_FILE = Path("/Users/ada/Downloads/goodall-answers.toml")

NEW_EVAL_ANSWERS = """schema_version = 1
account_mode = "dedicated"
eval = "new"

[new_eval]
name = "goodall-pilot"
description = "Building a product with AI agents."
workstreams = ["engineering", "research"]
construct = "Which parts of the work can be delegated?"

[[new_eval.scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]

[contributor]
id = "ada-lovelace"
display_name = "Ada Lovelace"
main_workstream = "research"

[folders]
record_repo = "~/evals/goodall-pilot-records"
log_directory = "~/evals/goodall-pilot-logs.noindex"

[secrets.references]
ANTHROPIC_API_KEY_RESEARCH = "op://goodall-eval/anthropic-research/credential"
"""

EXISTING_EVAL_ANSWERS = """schema_version = 1
account_mode = "mixed"
eval = "existing"

[contributor]
id = "ada-lovelace"
main_workstream = "research"

[folders]
record_repo = "/Users/ada/evals/goodall-pilot-records"
log_directory = "/Users/ada/evals/goodall-pilot-logs.noindex"
"""


def read(text: str, expected_one_time_code: str | None = None) -> Answers | list[Failure]:
    return read_answers_text(text, ANSWERS_FILE, expected_one_time_code)


def failures_of(text: str, expected_one_time_code: str | None = None) -> list[Failure]:
    reading = read(text, expected_one_time_code)
    assert isinstance(reading, list), "the answers were accepted"
    return reading


def all_text(failures: list[Failure]) -> str:
    text = ""
    for failure in failures:
        text += f"{failure.what_happened} {failure.why} {failure.what_to_do_next} "
    return text


def test_a_new_eval_s_answers_are_read_with_their_defaults() -> None:
    answers = read(NEW_EVAL_ANSWERS)

    assert isinstance(answers, Answers), answers
    assert answers.eval_choice == "new"
    assert answers.new_eval is not None
    assert answers.new_eval.workstreams == ["engineering", "research"]
    assert answers.new_eval.intervention_categories == [
        "policy-mandated",
        "infrastructure",
        "agent-limitation",
        "freely-delegable",
    ]
    assert answers.new_eval.activitywatch is False
    assert answers.contributor_id == "ada-lovelace"
    assert answers.record_repo.as_written == "~/evals/goodall-pilot-records"
    assert answers.record_repo.path.is_absolute()
    assert answers.secrets_manager == "1password"
    assert answers.secret_references == {
        "ANTHROPIC_API_KEY_RESEARCH": "op://goodall-eval/anthropic-research/credential"
    }
    assert answers.goodall_program is None


def test_an_existing_eval_s_answers_need_no_new_eval_and_no_display_name() -> None:
    answers = read(EXISTING_EVAL_ANSWERS)

    assert isinstance(answers, Answers), answers
    assert answers.eval_choice == "existing"
    assert answers.new_eval is None
    assert answers.account_mode == "mixed"
    assert answers.secret_references == {}


def test_a_new_eval_without_its_new_eval_section_is_refused() -> None:
    failures = failures_of(EXISTING_EVAL_ANSWERS.replace('eval = "existing"', 'eval = "new"'))

    assert [failure.kind for failure in failures] == ["answers_key_missing"]
    assert "[new_eval]" in failures[0].what_happened


def test_new_eval_answers_given_for_an_existing_eval_are_refused() -> None:
    text = NEW_EVAL_ANSWERS.replace('eval = "new"', 'eval = "existing"')
    failures = failures_of(text)

    assert "[new_eval]" in all_text(failures)


def test_an_account_mode_goodall_does_not_know_is_refused_with_the_two_it_knows() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS.replace('"dedicated"', '"shared"'))

    assert failures[0].kind == "answers_value_invalid"
    assert '"dedicated"' in failures[0].what_to_do_next
    assert '"mixed"' in failures[0].what_to_do_next


def test_every_problem_is_reported_at_once() -> None:
    text = NEW_EVAL_ANSWERS.replace('id = "ada-lovelace"', 'id = "Ada Lovelace"').replace(
        'log_directory = "~/evals/goodall-pilot-logs.noindex"', 'log_directory = "logs"'
    )
    failures = failures_of(text)

    assert len(failures) == 2
    assert "[contributor] id" in failures[0].what_happened
    assert "[folders] log_directory" in failures[1].what_happened


def test_a_main_workstream_that_is_not_one_of_the_new_eval_s_workstreams_is_refused() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS.replace('main_workstream = "research"', 'main_workstream = "design"'))

    assert "main_workstream" in failures[0].what_happened
    assert "engineering, research" in all_text(failures)


def test_a_raw_key_in_place_of_a_reference_is_refused_and_never_repeated() -> None:
    raw_key = "sk-ant-api03-" + "x7" * 30
    failures = failures_of(NEW_EVAL_ANSWERS.replace("op://goodall-eval/anthropic-research/credential", raw_key))

    assert raw_key not in all_text(failures)
    assert failures[0].kind == "answers_reference_invalid"
    assert "Anthropic" in all_text(failures)


def test_a_raw_key_anywhere_else_is_refused_and_never_repeated() -> None:
    raw_key = "ghp_" + "b2" * 20
    failures = failures_of(NEW_EVAL_ANSWERS.replace("Building a product with AI agents.", raw_key))

    assert raw_key not in all_text(failures)
    assert failures[0].kind == "answers_secret_value"
    assert "[new_eval] description" in failures[0].what_happened


def test_an_unknown_key_that_could_be_a_secret_is_not_named() -> None:
    pasted_key = "sk-ant-api03-" + "y8" * 30
    failures = failures_of(NEW_EVAL_ANSWERS + f'\n[claude_code]\n{pasted_key} = "x"\n')

    assert pasted_key not in all_text(failures)
    assert failures[0].kind == "answers_key_unknown"


def test_a_secret_reference_outside_the_references_is_refused() -> None:
    text = NEW_EVAL_ANSWERS.replace("Building a product with AI agents.", "op://goodall-eval/item/field")
    failures = failures_of(text)

    assert failures[0].kind == "answers_value_invalid"
    assert "[secrets.references]" in failures[0].what_to_do_next


def test_an_email_address_in_what_the_eval_config_will_hold_is_refused() -> None:
    failures = failures_of(
        NEW_EVAL_ANSWERS.replace('display_name = "Ada Lovelace"', 'display_name = "ada@example.org"')
    )

    assert "email" in all_text(failures)
    assert "ada@example.org" not in all_text(failures)


def test_the_service_account_token_is_refused_even_as_a_reference() -> None:
    text = NEW_EVAL_ANSWERS.replace("ANTHROPIC_API_KEY_RESEARCH", "OP_SERVICE_ACCOUNT_TOKEN")
    failures = failures_of(text)

    assert "OP_SERVICE_ACCOUNT_TOKEN" in failures[0].what_happened


def test_the_default_activitywatch_port_is_refused() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS + '\n[activitywatch]\nport = 5600\ndevice_id = "not-an-id"\n')

    assert len(failures) == 2
    assert "5600" in failures[0].what_happened
    assert "[activitywatch] device_id" in failures[1].what_happened


def test_a_goodall_program_must_be_a_full_path() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS + '\n[claude_code]\ngoodall_program = "goodall"\n')

    assert "[claude_code] goodall_program" in failures[0].what_happened


def test_a_one_time_code_is_checked_only_when_one_is_expected() -> None:
    with_code = 'one_time_code = "goodall-4f9c2a"\n' + NEW_EVAL_ANSWERS

    assert isinstance(read(with_code), Answers)
    assert isinstance(read(with_code, expected_one_time_code="goodall-4f9c2a"), Answers)
    wrong_code_failures = failures_of(with_code, expected_one_time_code="goodall-0000aa")
    assert [failure.kind for failure in wrong_code_failures] == ["answers_one_time_code_mismatch"]
    assert "goodall-4f9c2a" not in all_text(wrong_code_failures)
    missing_code_failures = failures_of(NEW_EVAL_ANSWERS, expected_one_time_code="goodall-4f9c2a")
    assert [failure.kind for failure in missing_code_failures] == ["answers_one_time_code_mismatch"]


def test_answers_written_for_a_newer_goodall_get_only_upgrade_goodall() -> None:
    failures = failures_of(
        NEW_EVAL_ANSWERS.replace("schema_version = 1", "schema_version = 2") + "\nsomething_new = 1\n"
    )

    assert [failure.kind for failure in failures] == ["answers_schema_too_new"]


def test_text_that_is_not_toml_says_where_the_mistake_is() -> None:
    failures = failures_of('schema_version = 1\naccount_mode = "dedicated\n')

    assert failures[0].kind == "answers_file_not_toml"
    assert "line 2" in failures[0].what_happened


def test_a_missing_answers_file_says_which_path_goodall_looked_at(tmp_path: Path) -> None:
    reading = read_answers_file(tmp_path / "missing.toml")

    assert isinstance(reading, list)
    assert reading[0].kind == "answers_file_missing"
    assert str(tmp_path / "missing.toml") in reading[0].what_happened


# The [eval_account] table: whether the administrator install creates the
# eval account, and its name (ADR 0022).

EVAL_ACCOUNT_TABLE = """
[eval_account]
create = true
name = "goodall-eval"
"""


def test_an_eval_account_to_create_is_read_with_its_name() -> None:
    answers = read(NEW_EVAL_ANSWERS + EVAL_ACCOUNT_TABLE)

    assert isinstance(answers, Answers), answers
    assert answers.eval_account is not None
    assert answers.eval_account.create is True
    assert answers.eval_account.name == "goodall-eval"


def test_answers_without_an_eval_account_table_create_no_account() -> None:
    answers = read(NEW_EVAL_ANSWERS)

    assert isinstance(answers, Answers), answers
    assert answers.eval_account is None


def test_declining_to_create_an_eval_account_needs_no_name() -> None:
    answers = read(NEW_EVAL_ANSWERS + "\n[eval_account]\ncreate = false\n")

    assert isinstance(answers, Answers), answers
    assert answers.eval_account is not None
    assert answers.eval_account.create is False
    assert answers.eval_account.name is None


def test_a_name_given_while_declining_is_refused_because_it_would_have_no_effect() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS + '\n[eval_account]\ncreate = false\nname = "goodall-eval"\n')

    assert [failure.kind for failure in failures] == ["answers_value_invalid"]
    assert "create = true" in all_text(failures)


def test_an_eval_account_to_create_needs_a_name() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS + "\n[eval_account]\ncreate = true\n")

    assert [failure.kind for failure in failures] == ["answers_key_missing"]
    assert "[eval_account] name" in all_text(failures)


def test_create_must_be_true_or_false() -> None:
    failures = failures_of(NEW_EVAL_ANSWERS + '\n[eval_account]\ncreate = "yes"\nname = "goodall-eval"\n')

    assert [failure.kind for failure in failures] == ["answers_value_invalid"]
    assert "[eval_account] create" in all_text(failures)


def test_an_account_name_macos_would_not_take_is_refused_without_being_repeated() -> None:
    for bad_name in ["Goodall Eval", "-goodall", "1goodall", "_goodall", "goodall/eval", ""]:
        failures = failures_of(NEW_EVAL_ANSWERS + f'\n[eval_account]\ncreate = true\nname = "{bad_name}"\n')

        assert [failure.kind for failure in failures] == ["answers_value_invalid"], bad_name
        assert "[eval_account] name" in all_text(failures)
        if bad_name != "":
            assert bad_name not in all_text(failures)


def test_an_eval_account_is_created_only_for_dedicated_account_mode() -> None:
    mixed = NEW_EVAL_ANSWERS.replace('account_mode = "dedicated"', 'account_mode = "mixed"')

    failures = failures_of(mixed + EVAL_ACCOUNT_TABLE)

    assert [failure.kind for failure in failures] == ["answers_eval_account_needs_dedicated_mode"]
    assert 'account_mode = "dedicated"' in all_text(failures)


def test_a_new_eval_account_cannot_join_an_existing_eval_on_this_mac() -> None:
    existing = EXISTING_EVAL_ANSWERS.replace('account_mode = "mixed"', 'account_mode = "dedicated"')

    failures = failures_of(existing + EVAL_ACCOUNT_TABLE)

    assert [failure.kind for failure in failures] == ["answers_eval_account_needs_new_eval"]
    assert 'eval = "new"' in all_text(failures)
