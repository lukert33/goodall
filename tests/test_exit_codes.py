"""Tests that the exit codes keep the values documented in AGENTS.md.

Agents and scripts branch on these numbers, so changing one is a breaking
change and must fail a test.
"""

from goodall import exit_codes


def test_exit_codes_have_documented_values() -> None:
    assert exit_codes.SUCCESS == 0
    assert exit_codes.ERROR == 1
    assert exit_codes.USAGE_ERROR == 2
    assert exit_codes.SESSION_GATE_CLOSED == 3
