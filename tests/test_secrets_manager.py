"""Unit tests for what goodall.secrets_manager accepts as a well-formed secret reference.

The 1Password form follows 1Password's documented secret reference syntax
(https://www.1password.dev/cli/secret-reference-syntax/, read 2026-11-16):
op://vault/item/[section/]field, where each name is letters, digits,
hyphens, underscores, periods and spaces, and any other name is replaced by
its id.
"""

import pytest

from goodall.secrets_manager import (
    is_service_account_token_variable,
    is_well_formed_secret_reference,
    service_account_token_is_set,
)


@pytest.mark.parametrize(
    "reference",
    [
        "op://Eval/anthropic-engineering/credential",
        "op://Eval/anthropic-engineering/API keys/credential",
        "op://My Vault/Anthropic engineering/credential",
        "op://dev/Stripe/publishable-key",
        "op://ab12cd34ef56gh78ij90kl12mn/xy98wv76ut54sr32qp10on98ml/credential",
        "op://Eval/item.v2/field_name",
    ],
)
def test_1password_references_in_the_documented_form_are_well_formed(reference: str) -> None:
    assert is_well_formed_secret_reference("1password", reference)


@pytest.mark.parametrize(
    "value",
    [
        # Raw keys pasted by mistake.
        "sk-ant-api03-RECOGNIZABLEFAKEVALUE",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        # Too few or too many parts.
        "op://Eval/credential",
        "op://Eval/item/section/subsection/field",
        # An empty part, or one that starts or ends with a space.
        "op://Eval//credential",
        "op://Eval/ /credential",
        "op://Eval/item/credential/",
        "op://Eval/ item/credential",
        # Characters 1Password's syntax does not allow in a name.
        "op://Eval/item (copy)/credential",
        "op://Eval/item/credential?attribute=otp",
        "op://Eval/it\tem/credential",
        # Not the op:// scheme, in any spelling.
        "OP://Eval/item/credential",
        "op:/Eval/item/credential",
        "https://Eval/item/credential",
        "",
    ],
)
def test_anything_else_is_not_a_well_formed_1password_reference(value: str) -> None:
    assert not is_well_formed_secret_reference("1password", value)


def test_a_secrets_manager_goodall_does_not_know_accepts_nothing() -> None:
    # Local settings refuse any other manager, so this never happens in use;
    # if it did, nothing must pass by default.
    assert not is_well_formed_secret_reference("another-manager", "op://Eval/item/credential")


@pytest.mark.parametrize(("value", "is_set"), [("ops_" + "SYNTHETIC" * 6, True), ("", False)])
def test_a_service_account_token_counts_as_set_only_when_it_is_not_empty(
    monkeypatch: pytest.MonkeyPatch, value: str, is_set: bool
) -> None:
    # An empty variable leaves op signing in through the app.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", value)

    assert service_account_token_is_set("1password") is is_set


def test_no_service_account_token_when_the_variable_is_absent() -> None:
    # tests/conftest.py removes the variable before every test.
    assert not service_account_token_is_set("1password")
    assert not service_account_token_is_set("another-manager")


@pytest.mark.parametrize(
    ("name", "is_the_variable"),
    [("OP_SERVICE_ACCOUNT_TOKEN", True), ("OP_CONNECT_TOKEN", False), ("ANTHROPIC_API_KEY", False)],
)
def test_the_service_account_token_variable_is_recognized_by_its_exact_name(name: str, is_the_variable: bool) -> None:
    assert is_service_account_token_variable(name) is is_the_variable
