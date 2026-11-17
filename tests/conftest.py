"""Settings shared by every test.

Any test that runs a goodall command could otherwise read the local settings
of whoever is running the tests, because goodall looks in the home folder by
default. Pointing GOODALL_CONFIG_DIR at a fresh empty folder for every test
keeps the developer's real home folder out of reach (AGENTS.md, "Testing").
A test that needs a different location sets or removes the variable itself.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config_directory = tmp_path_factory.mktemp("isolated-goodall-config")
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(Path(config_directory)))
