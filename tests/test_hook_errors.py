"""What `goodall hook` does when goodall itself goes wrong (goodall.hook).

An unexpected error cannot be caused from outside a fresh process, so these
tests run the hook in this process, with a captured terminal, and make the
heartbeat raise. The prompt gate must fail closed; every other event must
fail silently.
"""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import hook, work_blocks
from goodall.terminal import Terminal

CLAUDE_SESSION = "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5"
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)


def write_local_settings(config_directory: Path, log_directory: Path) -> None:
    log_directory.mkdir()
    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        f'record_repo = "{log_directory.parent}"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )


def run_hook_in_process(event_name: str) -> tuple[int, str, str]:
    """Run the hook with Claude Code's input on a captured terminal; return the exit code, output and errors."""
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    standard_input = io.StringIO(json.dumps({"session_id": CLAUDE_SESSION, "hook_event_name": event_name}))
    terminal = Terminal(standard_output, standard_error, standard_input)
    exit_code = hook.run_hook(event_name, terminal, lambda: NOW)
    return exit_code, standard_output.getvalue(), standard_error.getvalue()


@pytest.fixture
def broken_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid local settings, and a heartbeat that raises as a bug in goodall would."""
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    write_local_settings(config_directory, tmp_path / "logs")
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))

    def raise_unexpectedly(log_directory: Path, now: datetime, claude_session: str | None = None) -> None:
        raise RuntimeError("a bug in goodall")

    monkeypatch.setattr(work_blocks, "record_heartbeat", raise_unexpectedly)


@pytest.mark.usefixtures("broken_heartbeat")
def test_an_unexpected_error_refuses_the_prompt() -> None:
    exit_code, output, errors = run_hook_in_process("UserPromptSubmit")

    assert exit_code == 2
    assert output == ""
    assert errors == (
        "Goodall hit an unexpected problem while checking this prompt, so it was not sent. "
        "Run goodall status in a terminal to see what is wrong, and if it shows nothing, "
        "tell your eval's researcher.\n"
    )


@pytest.mark.usefixtures("broken_heartbeat")
def test_an_unexpected_error_in_any_other_event_is_silent() -> None:
    for event_name in ["SessionStart", "PostToolUse", "Stop", "SessionEnd"]:
        assert run_hook_in_process(event_name) == (0, "", "")


@pytest.mark.usefixtures("broken_heartbeat")
def test_in_mixed_account_mode_an_unexpected_error_lets_the_prompt_through_silently(tmp_path: Path) -> None:
    # The gate is off (ADR 0021), so a bug in goodall must not stop a prompt
    # that may have nothing to do with the eval.
    with (tmp_path / "goodall-config" / "local-settings.toml").open("a") as settings_file:
        settings_file.write('account_mode = "mixed"\n')

    assert run_hook_in_process("UserPromptSubmit") == (0, "", "")
