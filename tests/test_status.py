"""Outside-in tests for `goodall status`, driven through `cli.main`.

Each test runs goodall the way a contributor would, hands it a captured
terminal, and checks only what that contributor would observe: the exit code,
what was printed on standard output and standard error, and the files on disk.

The settings location is always pointed at a temporary folder, through
`GOODALL_CONFIG_DIR` or a temporary home folder, so no test reads the real
home folder of whoever runs the tests.
"""

import io
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from goodall import cli, exit_codes
from goodall.terminal import Terminal


def run_goodall(arguments: list[str]) -> tuple[int, str, str]:
    """Run goodall with a captured terminal.

    Returns the exit code, everything written to standard output, and
    everything written to standard error, in that order.
    """
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal)
    return exit_code, standard_output.getvalue(), standard_error.getvalue()


def files_under(folder: Path) -> list[Path]:
    """List every file and folder inside `folder`, so a test can see if any appeared."""
    return sorted(folder.rglob("*"))


@pytest.fixture
def empty_config_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty folder that GOODALL_CONFIG_DIR points at."""
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    return config_directory


def test_status_without_local_settings_exits_with_error(empty_config_directory: Path) -> None:
    exit_code, _, _ = run_goodall(["status"])
    assert exit_code == exit_codes.ERROR


def test_status_without_local_settings_explains_what_why_and_next_on_standard_error(
    empty_config_directory: Path,
) -> None:
    _, standard_output, standard_error = run_goodall(["status"])

    assert standard_output == ""
    assert standard_error.startswith("FAIL  No local settings were found.")
    assert "\nWhy   " in standard_error
    assert "\nNext  Run goodall onboard" in standard_error


def test_status_without_local_settings_names_where_it_looked(empty_config_directory: Path) -> None:
    _, _, standard_error = run_goodall(["status"])

    # The path is never split across lines, so a contributor can copy it.
    expected_settings_file = empty_config_directory / "local-settings.toml"
    assert str(expected_settings_file) in standard_error
    assert "GOODALL_CONFIG_DIR" in standard_error


def test_status_without_local_settings_creates_no_files(empty_config_directory: Path) -> None:
    run_goodall(["status"])
    assert files_under(empty_config_directory) == []


def test_status_without_override_looks_in_home_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_folder = tmp_path / "home"
    home_folder.mkdir()
    monkeypatch.delenv("GOODALL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home_folder))

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert str(home_folder / ".config" / "goodall" / "local-settings.toml") in standard_error
    assert "home folder" in standard_error
    assert files_under(home_folder) == []


def test_empty_override_counts_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `export GOODALL_CONFIG_DIR=` is a common way to "clear" a variable;
    # treating it as the current folder would make goodall's answer depend on
    # where the command was run.
    home_folder = tmp_path / "home"
    home_folder.mkdir()
    monkeypatch.setenv("GOODALL_CONFIG_DIR", "")
    monkeypatch.setenv("HOME", str(home_folder))

    _, _, standard_error = run_goodall(["status"])

    assert str(home_folder / ".config" / "goodall" / "local-settings.toml") in standard_error


def test_relative_override_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A relative folder would be looked up from whatever folder a command or
    # hook happens to run in, so two commands could find different evals.
    monkeypatch.setenv("GOODALL_CONFIG_DIR", "relative/goodall-config")
    monkeypatch.chdir(tmp_path)

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  GOODALL_CONFIG_DIR is set to relative/goodall-config")
    assert "absolute" in standard_error
    assert files_under(tmp_path) == []


def test_status_json_reports_the_same_failure_on_standard_output(empty_config_directory: Path) -> None:
    text_exit_code, _, text_error = run_goodall(["status"])
    json_exit_code, json_output, json_error = run_goodall(["status", "--json"])

    assert json_exit_code == text_exit_code == exit_codes.ERROR
    assert json_error == ""

    document = json.loads(json_output)
    # One stable shape whatever went wrong: an agent reads work_block_active
    # to decide whether to proceed, and failures is always a list, because
    # a settings file can have several things wrong with it at once.
    assert set(document) == {"work_block_active", "failures"}
    assert document["work_block_active"] is False
    assert len(document["failures"]) == 1
    failure = document["failures"][0]
    assert failure["kind"] == "local_settings_missing"
    assert str(empty_config_directory / "local-settings.toml") in failure["what_happened"]
    assert "goodall onboard" in failure["what_to_do_next"]
    assert set(failure) == {"kind", "what_happened", "why", "what_to_do_next"}

    # The text screen is a rendering of the same record, so the opening words
    # of each part appear in both.
    assert failure["what_happened"].split(".")[0] in text_error
    assert failure["why"].split(" ")[0] in text_error


def test_status_json_is_exactly_one_document(empty_config_directory: Path) -> None:
    _, json_output, _ = run_goodall(["status", "--json"])
    # json.loads rejects trailing data, so this fails if anything else was printed.
    json.loads(json_output)
    assert json_output.endswith("}\n")


def test_status_without_local_settings_uses_no_colour_when_not_a_terminal(empty_config_directory: Path) -> None:
    _, _, standard_error = run_goodall(["status"])
    assert "\x1b[" not in standard_error


def test_status_with_local_settings_present_says_reading_them_is_not_built(empty_config_directory: Path) -> None:
    # Reading local settings comes in the next ticket. Until then, finding the
    # file must not be mistaken for "everything is fine".
    settings_file = empty_config_directory / "local-settings.toml"
    settings_file.write_text("schema_version = 1\n")

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  Local settings were found")
    assert str(settings_file) in standard_error
    assert "not built yet" in standard_error
    assert settings_file.read_text() == "schema_version = 1\n"


@pytest.fixture
def remove_permissions() -> Iterator[Callable[[Path], None]]:
    """Take every permission away from a file or folder, and give them back afterwards.

    Permissions are restored even when the test fails, so pytest can still
    delete its temporary folders. Root ignores permissions, so these tests
    are skipped when run as root.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read anything, so permissions cannot be taken away")
    locked_paths: list[tuple[Path, int]] = []

    def lock(path: Path) -> None:
        locked_paths.append((path, path.stat().st_mode))
        path.chmod(0)

    yield lock
    for path, original_mode in reversed(locked_paths):
        path.chmod(original_mode)


def test_status_with_unreadable_config_folder_fails_instead_of_crashing(
    empty_config_directory: Path, remove_permissions: Callable[[Path], None]
) -> None:
    (empty_config_directory / "local-settings.toml").write_text("schema_version = 1\n")
    remove_permissions(empty_config_directory)

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  Local settings at")
    assert str(empty_config_directory / "local-settings.toml") in standard_error
    assert "could not be read" in standard_error
    assert "Permission denied" in standard_error
    assert "\nNext  " in standard_error
    assert "permissions" in standard_error


def test_status_with_unreadable_settings_file_fails_instead_of_crashing(
    empty_config_directory: Path, remove_permissions: Callable[[Path], None]
) -> None:
    settings_file = empty_config_directory / "local-settings.toml"
    settings_file.write_text("schema_version = 1\n")
    remove_permissions(settings_file)

    exit_code, _, standard_error = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    assert standard_error == ""


def test_status_json_with_unreadable_settings_file_names_the_problem(
    empty_config_directory: Path, remove_permissions: Callable[[Path], None]
) -> None:
    settings_file = empty_config_directory / "local-settings.toml"
    settings_file.write_text("schema_version = 1\n")
    remove_permissions(settings_file)

    _, standard_output, _ = run_goodall(["status", "--json"])

    failures = json.loads(standard_output)["failures"]
    assert [failure["kind"] for failure in failures] == ["local_settings_unreadable"]
    assert str(settings_file) in failures[0]["what_happened"]
