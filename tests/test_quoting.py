"""Unit tests for goodall.quoting, which decides whether text from a settings or env file may be shown back.

The outside-in tests in tests/test_status.py and tests/test_doctor.py prove
that no command shows a pasted secret; these pin down the rule itself.
"""

import pytest

from goodall.quoting import can_show_name, key_format_in, looks_like_a_random_token, toml_error_as_shown


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


# One synthetic example of each recognised key format, and the issuer goodall
# should name for it. Built from two pieces, so a secret scanner reading this
# file does not mistake it for a leaked key.
KEYS_BY_ISSUER = [
    ("sk-" + "ant-api03-SYNTHETICsyntheticSYNTHETIC", "Anthropic"),
    ("sk-" + "proj-SYNTHETICsyntheticSYNTHETIC0123", "OpenAI"),
    ("sk-" + "SYNTHETICsyntheticSYNTHETICsynthetic0123", "OpenAI"),
    ("ghp_" + "SYNTHETICsyntheticSYNTHETICsynthetic", "GitHub"),
    ("gho_" + "SYNTHETICsyntheticSYNTHETICsynthetic", "GitHub"),
    ("ghu_" + "SYNTHETICsyntheticSYNTHETICsynthetic", "GitHub"),
    ("ghs_" + "SYNTHETICsyntheticSYNTHETICsynthetic", "GitHub"),
    ("ghr_" + "SYNTHETICsyntheticSYNTHETICsynthetic", "GitHub"),
    ("github_pat_" + "SYNTHETICsynthetic_SYNTHETICsynthetic", "GitHub"),
    ("AKIA" + "SYNTHETIC0000000", "AWS"),
    ("ASIA" + "SYNTHETIC0000000", "AWS"),
    ("xoxb-" + "0000000000-SYNTHETICsynthetic", "Slack"),
    ("xoxp-" + "0000000000-SYNTHETICsynthetic", "Slack"),
    ("xoxa-" + "0000000000-SYNTHETICsynthetic", "Slack"),
    ("AIza" + "SyntheticSYNTHETICsyntheticSYNTHETI", "Google"),
    ("ops_" + "SYNTHETICsynthetic" * 3, "1Password"),
    ("-----BEGIN " + "PRIVATE KEY-----", "whoever issued it"),
    ("-----BEGIN " + "OPENSSH PRIVATE KEY-----", "whoever issued it"),
    ("-----BEGIN " + "EC PRIVATE KEY-----", "whoever issued it"),
    ("eyJ" + "SYNTHETICsynthetic.eyJSYNTHETICsynthetic.SYNTHETICsynthetic", "whoever issued it"),
]


@pytest.mark.parametrize(("key", "issuer"), KEYS_BY_ISSUER)
def test_each_recognised_key_format_is_found_and_names_its_issuer(key: str, issuer: str) -> None:
    key_format = key_format_in(key)

    assert key_format is not None
    assert key_format.issuer == issuer


@pytest.mark.parametrize(("key", "issuer"), KEYS_BY_ISSUER)
def test_a_recognised_key_inside_a_longer_value_is_found(key: str, issuer: str) -> None:
    assert key_format_in(f"Authorization=Bearer {key}") is not None


@pytest.mark.parametrize(
    "text",
    [
        # Long identifiers that are not keys: a UUID and a 40-character git commit id.
        "123e4567-e89b-12d3-a456-426614174000",
        "9fceb02d0ae598e95dc970b74767f19372d61af8",
        # Ordinary words and settings.
        "otlp",
        "http/json",
        "skeleton",
        "ops_team",
        "http://127.0.0.1:4318",
        # A prefix with too little after it to be a key.
        "sk-ant-short",
        "sk-proj-short",
        "sk-learn-something",
        "ghp_short",
        "github_pat_short",
        "xoxb-short",
        "AIzaShort",
        # AWS key ids are exactly 16 characters after the prefix.
        "AKIA" + "SYNTHETIC000000",
        "AKIA" + "RECOGNISABLEFAKEVALUE7",
        # A prefix that is part of a longer word does not count.
        "task-" + "ant-api03-SYNTHETICsyntheticSYNTHETIC",
        "BEGIN PRIVATE KEY",
        "eyJhbGciOiJIUzI1NiJ9",
    ],
)
def test_identifiers_ordinary_words_and_short_prefixes_are_not_recognised_as_keys(text: str) -> None:
    assert key_format_in(text) is None


def test_a_name_in_a_recognised_key_format_is_not_shown() -> None:
    # No digit in it, so the random-token rule alone would let it through.
    assert not can_show_name("ghp_" + "SYNTHETICsyntheticSYNTHETICsynthetic")
