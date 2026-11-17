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
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_local_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config_directory = tmp_path_factory.mktemp("isolated-goodall-config")
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(Path(config_directory)))
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
