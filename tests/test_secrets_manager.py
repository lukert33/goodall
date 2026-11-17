"""Unit tests for what goodall.secrets_manager accepts as a well-formed secret reference.

The 1Password form follows 1Password's documented secret reference syntax
(https://www.1password.dev/cli/secret-reference-syntax/, read 2026-11-16):
op://vault/item/[section/]field, where each name is letters, digits,
hyphens, underscores, periods and spaces, and any other name is replaced by
its id.
"""

import pytest

from goodall.secrets_manager import is_well_formed_secret_reference


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
        "sk-ant-api03-RECOGNISABLEFAKEVALUE",
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
