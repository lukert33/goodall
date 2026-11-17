"""Unit tests for goodall.quoting, which decides whether text from a settings or env file may be shown back.

The outside-in tests in tests/test_status.py and tests/test_doctor.py prove
that no command shows a pasted secret; these pin down the rule itself.
"""

import pytest

from goodall.quoting import can_show_name, looks_like_a_random_token, toml_error_as_shown


@pytest.mark.parametrize(
    "name",
    [
        "contributor",
        "contributer",
        "main_workstream",
        "display-name",
        "ANTHROPIC_API_KEY_ENGINEERING",
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    ],
)
def test_short_names_a_person_would_type_can_be_shown(name: str) -> None:
    assert can_show_name(name)


@pytest.mark.parametrize(
    "text",
    [
        # Synthetic stand-ins for a pasted key, a base64 token and a long hex id.
        "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC",
        "SYNTHETICsyntheticSYNTHETIC0123456789x9",
        "0123456789abcdef0123456789abcdef",
        # Anything that is not shaped like a name.
        "op://Eval/item/field",
        "has a space",
        "",
        # Too long to be a name a person typed.
        "a" * 65,
    ],
)
def test_text_that_could_be_a_secret_or_is_not_a_name_is_not_shown(text: str) -> None:
    assert not can_show_name(text)


def test_a_toml_error_that_repeats_a_key_keeps_only_the_place() -> None:
    message = "Cannot declare ('sk-ant-api03-SYNTHETICsyntheticSYNTHETIC',) twice (at line 3, column 4)"

    assert toml_error_as_shown(message) == "A table or key is declared twice (at line 3, column 4)"


def test_a_toml_error_that_repeats_nothing_from_the_file_is_kept_as_it_is() -> None:
    message = "Expected '=' after a key in a key/value pair (at line 2, column 9)"

    assert toml_error_as_shown(message) == message


def test_an_unfamiliar_toml_error_holding_a_token_is_hidden() -> None:
    # A later Python may word a message differently and still repeat a key.
    shown = toml_error_as_shown("Key SYNTHETICsyntheticSYNTHETIC0123456789x9 is odd (at line 1, column 1)")

    assert "SYNTHETICsynthetic" not in shown
    assert shown.endswith("(at line 1, column 1)")


def test_a_token_inside_a_longer_value_is_found() -> None:
    assert looks_like_a_random_token("Bearer SYNTHETICsyntheticSYNTHETIC0123456789x9")
    assert not looks_like_a_random_token("http/json")
