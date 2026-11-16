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
from dataclasses import dataclass
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


# Reading local settings. These tests build a pretend eval account inside
# tmp_path: a home folder (HOME points at it, so `~` means it), a settings
# folder that GOODALL_CONFIG_DIR names, a record repo folder and a log
# directory. Each test then breaks one thing.


@dataclass
class EvalAccount:
    home_folder: Path
    config_directory: Path
    record_repo: Path
    log_directory: Path

    @property
    def settings_file(self) -> Path:
        return self.config_directory / "local-settings.toml"

    def valid_settings_text(self) -> str:
        """Local settings in the agreed shape, pointing at this account's folders."""
        return (
            "schema_version = 1\n"
            'contributor = "ada-lovelace"\n'
            'main_workstream = "research"\n'
            'record_repo = "~/evals/goodall-pilot-records"\n'
            f'log_directory = "{self.log_directory}"\n'
            'secrets_manager = "1password"\n'
        )

    def write_settings(self, text: str) -> None:
        self.settings_file.write_text(text)


@pytest.fixture
def eval_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvalAccount:
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "evals" / "goodall-pilot-records"
    log_directory = tmp_path / "EvalLogs" / "goodall-pilot"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    return EvalAccount(home_folder, config_directory, record_repo, log_directory)


def failure_kinds() -> list[str]:
    """Run `goodall status --json` and return the kind of every failure, in order."""
    _, standard_output, _ = run_goodall(["status", "--json"])
    failures = json.loads(standard_output)["failures"]
    return [failure["kind"] for failure in failures]


def test_settings_that_are_not_valid_toml_name_the_file_and_the_line(eval_account: EvalAccount) -> None:
    # A hand-edit that forgot the quotes around a value, on line 2.
    eval_account.write_settings(eval_account.valid_settings_text().replace('"ada-lovelace"', "ada-lovelace"))

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  Local settings at")
    assert str(eval_account.settings_file) in standard_error
    assert "line 2" in standard_error
    assert failure_kinds() == ["local_settings_not_toml"]


def test_settings_that_are_not_text_are_reported_not_crashed_on(eval_account: EvalAccount) -> None:
    eval_account.settings_file.write_bytes(b'schema_version = 1\ncontributor = "\xff\xfe"\n')

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert str(eval_account.settings_file) in standard_error
    assert failure_kinds() == ["local_settings_not_toml"]


def test_missing_required_key_is_named(eval_account: EvalAccount) -> None:
    eval_account.write_settings(eval_account.valid_settings_text().replace('main_workstream = "research"\n', ""))

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_error.startswith("FAIL  Local settings at")
    assert "main_workstream" in standard_error
    assert failure_kinds() == ["local_settings_key_missing"]


def test_unknown_key_is_named_and_a_close_match_suggested(eval_account: EvalAccount) -> None:
    # A misspelled key is both unknown and leaves the real key missing; both
    # are reported, so the contributor sees the whole picture at once.
    eval_account.write_settings(eval_account.valid_settings_text().replace("contributor =", "contributer ="))

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert "contributer" in standard_error
    assert "Did you mean contributor?" in " ".join(standard_error.split())
    assert sorted(failure_kinds()) == ["local_settings_key_missing", "local_settings_key_unknown"]


def test_unknown_key_is_reported_even_when_everything_else_is_valid(eval_account: EvalAccount) -> None:
    eval_account.write_settings(eval_account.valid_settings_text() + 'favourite_colour = "green"\n')

    _, _, standard_error = run_goodall(["status"])

    assert "favourite_colour" in standard_error
    assert failure_kinds() == ["local_settings_key_unknown"]


def test_several_problems_are_all_shown_one_after_another(eval_account: EvalAccount) -> None:
    # Five required keys missing, plus one unknown key.
    eval_account.write_settings('schema_version = 1\nfavourite_colour = "green"\n')

    _, standard_output, standard_error = run_goodall(["status"])

    assert standard_output == ""
    assert standard_error.count("FAIL  ") == 6
    assert standard_error.count("\nWhy   ") == 6
    assert standard_error.count("\nNext  ") == 6
    assert failure_kinds().count("local_settings_key_missing") == 5


def test_newer_schema_version_says_to_upgrade_goodall(eval_account: EvalAccount) -> None:
    # A newer goodall may have renamed or added keys, so goodall reports
    # nothing else: every other complaint would be noise.
    eval_account.write_settings(
        eval_account.valid_settings_text().replace("schema_version = 1", "schema_version = 2")
        + 'added_in_version_two = "yes"\n'
    )

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert "schema_version 2" in standard_error
    assert "upgrade goodall" in standard_error.lower()
    assert failure_kinds() == ["local_settings_schema_too_new"]


@pytest.mark.parametrize(
    "schema_version_line",
    ['schema_version = "1"', "schema_version = 0", "schema_version = true", "schema_version = 1.0"],
)
def test_schema_version_that_is_not_a_known_whole_number_is_reported(
    eval_account: EvalAccount, schema_version_line: str
) -> None:
    eval_account.write_settings(eval_account.valid_settings_text().replace("schema_version = 1", schema_version_line))
    assert failure_kinds() == ["local_settings_value_invalid"]


@pytest.mark.parametrize(
    "broken_line",
    ["contributor = 5", 'contributor = ""', 'contributor = ["ada-lovelace"]'],
)
def test_text_setting_with_the_wrong_kind_of_value_is_reported(eval_account: EvalAccount, broken_line: str) -> None:
    eval_account.write_settings(eval_account.valid_settings_text().replace('contributor = "ada-lovelace"', broken_line))

    _, _, standard_error = run_goodall(["status"])

    assert "contributor" in standard_error
    assert failure_kinds() == ["local_settings_value_invalid"]


def test_secrets_manager_other_than_1password_is_reported(eval_account: EvalAccount) -> None:
    eval_account.write_settings(eval_account.valid_settings_text().replace('"1password"', '"bitwarden"'))

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert "secrets_manager" in standard_error
    assert '"1password"' in standard_error
    assert failure_kinds() == ["local_settings_secrets_manager_unsupported"]


def test_a_value_in_the_wrong_place_is_never_echoed(eval_account: EvalAccount) -> None:
    # Terminal output can end up in a transcript an agent reads, and a value
    # in the wrong place may be a secret pasted by mistake (ADR 0006).
    pasted_secret = "sk-ant-api03-not-a-real-key"
    eval_account.write_settings(
        eval_account.valid_settings_text()
        .replace('"1password"', f'"{pasted_secret}"')
        .replace('"research"', f'["{pasted_secret}"]')
    )

    _, standard_output, standard_error = run_goodall(["status"])
    _, json_output, _ = run_goodall(["status", "--json"])

    assert pasted_secret not in standard_output + standard_error + json_output
    assert sorted(failure_kinds()) == ["local_settings_secrets_manager_unsupported", "local_settings_value_invalid"]


def test_secrets_manager_is_matched_exactly(eval_account: EvalAccount) -> None:
    eval_account.write_settings(eval_account.valid_settings_text().replace('"1password"', '"1Password"'))
    assert failure_kinds() == ["local_settings_secrets_manager_unsupported"]


def test_relative_folder_setting_is_reported(eval_account: EvalAccount) -> None:
    # A relative folder would be looked up from wherever a command happened
    # to start, so the same settings could point at different folders.
    eval_account.write_settings(
        eval_account.valid_settings_text().replace('"~/evals/goodall-pilot-records"', '"evals/goodall-pilot-records"')
    )

    _, _, standard_error = run_goodall(["status"])

    assert "record_repo" in standard_error
    assert "evals/goodall-pilot-records" in standard_error
    assert failure_kinds() == ["local_settings_value_invalid"]


def test_tilde_of_another_account_is_not_expanded(eval_account: EvalAccount) -> None:
    # Only `~` and `~/...` mean this account's home. `~someone/...` would
    # point into another account's home, which an eval account must not use.
    eval_account.write_settings(
        eval_account.valid_settings_text().replace('"~/evals/goodall-pilot-records"', '"~root/records"')
    )
    assert failure_kinds() == ["local_settings_value_invalid"]


def test_valid_settings_end_in_saying_the_eval_config_cannot_be_read_yet(eval_account: EvalAccount) -> None:
    # The record repo is written as ~/evals/goodall-pilot-records and only
    # exists under the pretend home folder, so reaching this point also
    # proves that a leading ~ means this account's home.
    eval_account.write_settings(eval_account.valid_settings_text())

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  Local settings are valid")
    assert "eval config" in standard_error
    assert str(eval_account.record_repo) in standard_error
    assert failure_kinds() == ["eval_config_not_read_yet"]
    assert eval_account.settings_file.read_text() == eval_account.valid_settings_text()


def test_missing_record_repo_folder_is_shown_as_written(eval_account: EvalAccount) -> None:
    eval_account.write_settings(
        eval_account.valid_settings_text().replace("~/evals/goodall-pilot-records", "~/evals/not-cloned-yet")
    )

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_error.startswith("FAIL  The record repo folder ~/evals/not-cloned-yet does not exist.")
    assert str(eval_account.home_folder / "evals" / "not-cloned-yet") in standard_error
    assert failure_kinds() == ["record_repo_missing"]


def test_record_repo_that_is_a_file_is_reported(eval_account: EvalAccount) -> None:
    (eval_account.home_folder / "evals" / "a-file").write_text("not a folder\n")
    eval_account.write_settings(
        eval_account.valid_settings_text().replace("~/evals/goodall-pilot-records", "~/evals/a-file")
    )
    assert failure_kinds() == ["record_repo_missing"]


def test_unreachable_log_directory_is_reported_as_not_recording(eval_account: EvalAccount) -> None:
    # An unplugged drive is the everyday case: the folder is simply not there.
    eval_account.write_settings(
        eval_account.valid_settings_text().replace(str(eval_account.log_directory), "/Volumes/NotPluggedIn/goodall")
    )

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  Not recording:")
    assert "/Volumes/NotPluggedIn/goodall" in standard_error
    assert "no work block" in standard_error

    _, json_output, _ = run_goodall(["status", "--json"])
    document = json.loads(json_output)
    assert document["work_block_active"] is False
    assert [failure["kind"] for failure in document["failures"]] == ["log_directory_unreachable"]


def test_log_directory_without_permission_is_reported_as_not_recording(
    eval_account: EvalAccount, remove_permissions: Callable[[Path], None]
) -> None:
    eval_account.write_settings(eval_account.valid_settings_text())
    remove_permissions(eval_account.log_directory)

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_error.startswith("FAIL  Not recording:")
    assert failure_kinds() == ["log_directory_unreachable"]


@pytest.mark.parametrize(
    "replace_from, replace_to",
    [
        ("schema_version = 1", "schema_version = = 1"),
        ('main_workstream = "research"', ""),
        ('main_workstream = "research"', 'main_workstream = "research"\nmain_workstreem = "x"'),
        ("schema_version = 1", "schema_version = 99"),
        ('"1password"', '"keychain"'),
        ("~/evals/goodall-pilot-records", "~/evals/missing"),
        ('"~/evals/goodall-pilot-records"', '"records"'),
        ("log_directory = ", 'log_directory = "/Volumes/Gone"\nunused = '),
    ],
)
def test_every_failure_says_what_why_and_next_and_exits_1(
    eval_account: EvalAccount, replace_from: str, replace_to: str
) -> None:
    eval_account.write_settings(eval_account.valid_settings_text().replace(replace_from, replace_to))

    exit_code, standard_output, _ = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    failures = json.loads(standard_output)["failures"]
    assert failures != []
    for failure in failures:
        assert set(failure) == {"kind", "what_happened", "why", "what_to_do_next"}
        for part in ["what_happened", "why", "what_to_do_next"]:
            assert failure[part].strip() != "", f"{failure['kind']} has an empty {part}"
