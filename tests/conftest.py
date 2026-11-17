"""Settings shared by every test.

Any test that runs a goodall command could otherwise read the local settings
of whoever is running the tests, because goodall looks in the home folder by
default. Pointing GOODALL_CONFIG_DIR at a fresh empty folder for every test
keeps the developer's real home folder out of reach (AGENTS.md, "Testing").
A test that needs a different location sets or removes the variable itself.

CLAUDE_CONFIG_DIR is pointed at a fresh empty folder for the same reason:
goodall copies transcripts out of Claude Code's projects folder, which is
inside the home folder by default and holds a person's private prompts.
HOME is pointed at a fresh empty folder too, because the sweep also looks
in the default `~/.claude/projects` whatever CLAUDE_CONFIG_DIR says.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config_directory = tmp_path_factory.mktemp("isolated-goodall-config")
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(Path(config_directory)))


@pytest.fixture(autouse=True)
def isolated_claude_projects_folder(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    claude_config_directory = tmp_path_factory.mktemp("isolated-claude-config")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path(claude_config_directory)))


@pytest.fixture(autouse=True)
def not_inside_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test as a person at a terminal, even when Claude Code is running the tests.

    `goodall journal` and `goodall intervene` refuse when CLAUDECODE=1
    (goodall.caller); tests of that refusal set the variable themselves.
    """
    monkeypatch.delenv("CLAUDECODE", raising=False)


@pytest.fixture(autouse=True)
def isolated_home_folder(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home_folder = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(Path(home_folder)))
