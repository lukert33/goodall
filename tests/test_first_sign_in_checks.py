"""Outside-in tests for `goodall doctor`'s checks of first sign-in setup (ADR 0022).

The administrator install stages a first-run script, an answers file and
two LaunchAgents where only root can change them (goodall.system_places).
`goodall doctor` checks, and only checks, that they are still so. Each test
lays the files out in the isolated system folders of tests/conftest.py, as
the administrator install would, and runs `goodall doctor --json` through
`cli.main`. A test cannot make a file owned by root, so files the test
makes stand for root's (`ROOT_USER_ID`), except in the test about an owner
that is not root.
"""

import io
import json
import os
import plistlib
from pathlib import Path

import pytest

from goodall import cli, exit_codes, system_places
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

CHECK_IDS = [
    "first_sign_in.goodall_for_every_account",
    "first_sign_in.staged_files_protected",
    "first_sign_in.launch_agents_in_place",
]


@pytest.fixture(autouse=True)
def files_made_by_this_test_count_as_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_places, "ROOT_USER_ID", os.getuid())


def write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def stage_first_sign_in_setup() -> None:
    """Lay out every file as the administrator install leaves it."""
    write(system_places.installed_goodall(), "#!/bin/sh\n", 0o755)
    system_places.STAGING_FOLDER.mkdir(parents=True)
    system_places.STAGING_FOLDER.chmod(0o755)
    write(system_places.staged_answers_file(), '[eval_account]\ncreate = true\nname = "goodall-eval"\n', 0o644)
    write(system_places.first_run_script(), system_places.first_run_script_text(), 0o755)
    write(system_places.first_run_agent_file(), system_places.first_run_agent_text(), 0o644)
    write(system_places.menu_bar_icon_agent_file(), system_places.menu_bar_icon_agent_text(), 0o644)


def doctor() -> tuple[int, dict[str, dict[str, object]], str]:
    """Run `goodall doctor --json`; return its exit code, this module's checks by id, and every word they say."""
    standard_output = io.StringIO()
    terminal = Terminal(standard_output, io.StringIO())
    runner = FakeRunner()
    runner.answer(["/usr/bin/id", "-Gn"], standard_output="staff\n")
    exit_code = cli.main(["doctor", "--json"], terminal=terminal, runner=runner)
    document = json.loads(standard_output.getvalue())
    checks = {}
    words = ""
    for check in document["checks"]:
        if check["id"] in CHECK_IDS:
            checks[check["id"]] = check
            words += json.dumps(check) + " "
    return exit_code, checks, words


def status_of(checks: dict[str, dict[str, object]]) -> dict[str, object]:
    found = {}
    for check_id, check in checks.items():
        found[check_id] = check["status"]
    return found


def test_a_mac_with_no_first_sign_in_setup_passes_and_says_so() -> None:
    _, checks, words = doctor()

    assert status_of(checks) == dict.fromkeys(CHECK_IDS, "pass")
    assert "set up by hand" in words


def test_first_sign_in_setup_as_the_administrator_install_leaves_it_passes() -> None:
    stage_first_sign_in_setup()

    _, checks, _ = doctor()

    assert status_of(checks) == dict.fromkeys(CHECK_IDS, "pass")


def test_a_first_run_script_another_account_can_change_fails_with_a_next_step() -> None:
    stage_first_sign_in_setup()
    system_places.first_run_script().chmod(0o777)

    exit_code, checks, words = doctor()

    assert exit_code == exit_codes.ERROR
    assert checks["first_sign_in.staged_files_protected"]["status"] == "fail"
    assert str(system_places.first_run_script()) in words
    assert f"sudo chmod 755 '{system_places.first_run_script()}'" in words


def test_a_first_run_script_not_owned_by_root_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    stage_first_sign_in_setup()
    monkeypatch.setattr(system_places, "ROOT_USER_ID", 0)

    exit_code, checks, words = doctor()

    assert exit_code == exit_codes.ERROR
    assert checks["first_sign_in.staged_files_protected"]["status"] == "fail"
    assert "not owned by root" in words
    assert "sudo chown root:wheel" in words


def test_a_staged_answers_file_others_can_change_fails() -> None:
    stage_first_sign_in_setup()
    system_places.staged_answers_file().chmod(0o666)

    _, checks, words = doctor()

    assert checks["first_sign_in.staged_files_protected"]["status"] == "fail"
    assert str(system_places.staged_answers_file()) in words


def test_a_missing_first_run_launch_agent_fails() -> None:
    stage_first_sign_in_setup()
    system_places.first_run_agent_file().unlink()

    exit_code, checks, words = doctor()

    assert exit_code == exit_codes.ERROR
    assert checks["first_sign_in.launch_agents_in_place"]["status"] == "fail"
    assert str(system_places.first_run_agent_file()) in words


def test_a_launch_agent_that_runs_something_else_fails() -> None:
    stage_first_sign_in_setup()
    agent = plistlib.loads(system_places.first_run_agent_file().read_bytes())
    agent["ProgramArguments"] = ["/tmp/goodall-something-else"]
    system_places.first_run_agent_file().write_bytes(plistlib.dumps(agent))

    _, checks, words = doctor()

    assert checks["first_sign_in.launch_agents_in_place"]["status"] == "fail"
    assert str(system_places.first_run_script()) in words


def test_a_menu_bar_icon_agent_that_would_undo_quit_goodall_fails() -> None:
    stage_first_sign_in_setup()
    agent = plistlib.loads(system_places.menu_bar_icon_agent_file().read_bytes())
    agent["KeepAlive"] = True
    system_places.menu_bar_icon_agent_file().write_bytes(plistlib.dumps(agent))

    _, checks, words = doctor()

    assert checks["first_sign_in.launch_agents_in_place"]["status"] == "fail"
    assert "Quit goodall" in words


def test_a_launch_agent_others_can_change_fails() -> None:
    stage_first_sign_in_setup()
    system_places.menu_bar_icon_agent_file().chmod(0o666)

    _, checks, _ = doctor()

    assert checks["first_sign_in.launch_agents_in_place"]["status"] == "fail"


def test_staged_setup_without_goodall_installed_for_every_account_fails_with_where_to_install_it() -> None:
    stage_first_sign_in_setup()
    system_places.installed_goodall().unlink()

    _, checks, words = doctor()

    assert checks["first_sign_in.goodall_for_every_account"]["status"] == "fail"
    assert str(system_places.installed_goodall()) in words
    assert "docs/administrator-install.md" in words


def test_doctor_changes_nothing_in_the_first_sign_in_setup() -> None:
    stage_first_sign_in_setup()
    system_places.first_run_script().chmod(0o777)
    before = {}
    for folder in [system_places.STAGING_FOLDER, system_places.LAUNCH_AGENTS_FOLDER]:
        for path in folder.iterdir():
            before[path] = (path.stat().st_mode, path.read_bytes())

    doctor()

    after = {}
    for folder in [system_places.STAGING_FOLDER, system_places.LAUNCH_AGENTS_FOLDER]:
        for path in folder.iterdir():
            after[path] = (path.stat().st_mode, path.read_bytes())
    assert after == before
