"""Settings shared by every test.

Any test that runs a goodall command could otherwise read the local settings
of whoever is running the tests, because goodall looks in the home folder by
default. Pointing GOODALL_CONFIG_DIR at a fresh empty folder for every test
keeps the developer's real home folder out of reach (AGENTS.md, "Testing").
A test that needs a different location sets or removes the variable itself.

For the same reason every test starts without OP_SERVICE_ACCOUNT_TOKEN: a
developer whose own Mac signs op in with a 1Password service account has
a real token there, which would change what doctor reports and would reach
every program a test runs for real. A test about service accounts sets a
synthetic token itself.

CLAUDE_CONFIG_DIR is pointed at a fresh empty folder for the same reason:
goodall copies transcripts out of Claude Code's projects folder, which is
inside the home folder by default and holds a person's private prompts.
HOME is pointed at a fresh empty folder too, because the sweep also looks
in the default `~/.claude/projects` whatever CLAUDE_CONFIG_DIR says.

The folder goodall looks in for the menu bar icon's helper is pointed at a
fresh empty folder as well. The real one is `/Applications`, shared by the
whole Mac, and a developer who has the helper installed there would
otherwise have every `goodall start` in a test try to start it.
"""

from pathlib import Path

import pytest

from goodall import menu_bar_icon


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config_directory = tmp_path_factory.mktemp("isolated-goodall-config")
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(Path(config_directory)))
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)


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


@pytest.fixture(autouse=True)
def isolated_applications_folder(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    applications_folder = tmp_path_factory.mktemp("isolated-applications")
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", Path(applications_folder))
