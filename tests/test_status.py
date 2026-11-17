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


# An obviously made-up key, used wherever a test checks that goodall never repeats a value.
SYNTHETIC_SECRET = "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC"


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
    assert standard_error.startswith("FAIL  GOODALL_CONFIG_DIR is not an absolute path")
    assert "absolute" in standard_error
    assert files_under(tmp_path) == []


@pytest.mark.parametrize("extra_arguments", [[], ["--json"]])
def test_relative_override_is_never_repeated_back_because_it_may_be_a_pasted_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_arguments: list[str]
) -> None:
    # Someone who pastes a key into the wrong variable must not see goodall
    # print it (ADR 0006), so the message names the variable and nothing else.
    monkeypatch.setenv("GOODALL_CONFIG_DIR", SYNTHETIC_SECRET)
    monkeypatch.chdir(tmp_path)

    exit_code, standard_output, standard_error = run_goodall(["status", *extra_arguments])

    assert exit_code == exit_codes.ERROR
    assert "SYNTHETIC" not in standard_output + standard_error
    message = " ".join((standard_output + standard_error).split())
    assert "GOODALL_CONFIG_DIR" in message
    assert "full path starting with /" in message
    assert "for example /Users/you/goodall-config" in message
    assert "unset it" in message


def test_status_json_reports_the_same_failure_on_standard_output(empty_config_directory: Path) -> None:
    text_exit_code, _, text_error = run_goodall(["status"])
    json_exit_code, json_output, json_error = run_goodall(["status", "--json"])

    assert json_exit_code == text_exit_code == exit_codes.ERROR
    assert json_error == ""

    document = json.loads(json_output)
    # One stable shape whatever went wrong: an agent reads work_block_active
    # to decide whether to proceed, and failures is always a list, because
    # a settings file can have several things wrong with it at once. The
    # eval and the contributor are null until goodall has read them.
    assert set(document) == {"work_block_active", "eval", "contributor", "failures"}
    assert document["work_block_active"] is False
    assert document["eval"] is None
    assert document["contributor"] is None
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

# The eval config in the agreed shape (goodall-xes.8), with two contributors
# so the roster holds someone other than this account.
VALID_EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "Building a SaaS product to first recurring revenue with AI."
workstreams = ["engineering", "research", "go-to-market"]
intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[contributors]]
id = "grace-hopper"
display_name = "Grace Hopper"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""


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

    @property
    def eval_config_file(self) -> Path:
        return self.record_repo / "eval.toml"

    def write_eval_config(self, text: str) -> None:
        self.eval_config_file.write_text(text)

    def make_valid(self) -> None:
        """Write valid local settings and a valid eval config, so a test can break one thing."""
        self.write_settings(self.valid_settings_text())
        self.write_eval_config(VALID_EVAL_CONFIG_TEXT)


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
    assert "full path" in standard_error
    assert failure_kinds() == ["local_settings_value_invalid"]


def test_a_folder_setting_that_is_not_a_path_is_never_echoed(eval_account: EvalAccount) -> None:
    # A value that is not a full path may be a key pasted into the wrong
    # setting, so it is shown only once it is known to be a path.
    pasted_secret = "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC"
    eval_account.write_settings(
        eval_account.valid_settings_text().replace('"~/evals/goodall-pilot-records"', f'"{pasted_secret}"')
    )

    printed = everything_goodall_printed()

    assert "SYNTHETICsynthetic" not in printed
    assert "record_repo" in printed
    assert failure_kinds() == ["local_settings_value_invalid"]


@pytest.mark.parametrize("setting_name", ["record_repo", "log_directory"])
def test_a_folder_setting_with_a_null_character_is_a_failure_not_a_crash(
    eval_account: EvalAccount, setting_name: str
) -> None:
    # TOML can hold a null character (written \u0000), which no path on macOS
    # can contain; asking the disk about such a path raises ValueError.
    settings_lines = []
    for line in eval_account.valid_settings_text().splitlines():
        if line.startswith(setting_name):
            line = f'{setting_name} = "/Users/SYNTHETICsynthetic\\u0000/records"'
        settings_lines.append(line)
    eval_account.write_settings("\n".join(settings_lines) + "\n")
    eval_account.write_eval_config(VALID_EVAL_CONFIG_TEXT)

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_error.startswith("FAIL")
    assert setting_name in standard_error
    assert "SYNTHETICsynthetic" not in everything_goodall_printed()
    assert failure_kinds() == ["local_settings_value_invalid"]


def test_tilde_of_another_account_is_not_expanded(eval_account: EvalAccount) -> None:
    # Only `~` and `~/...` mean this account's home. `~someone/...` would
    # point into another account's home, which an eval account must not use.
    eval_account.write_settings(
        eval_account.valid_settings_text().replace('"~/evals/goodall-pilot-records"', '"~root/records"')
    )
    assert failure_kinds() == ["local_settings_value_invalid"]


def test_valid_eval_reports_the_eval_the_contributor_and_no_work_block(eval_account: EvalAccount) -> None:
    # The record repo is written as ~/evals/goodall-pilot-records and only
    # exists under the pretend home folder, so reaching this point also
    # proves that a leading ~ means this account's home.
    eval_account.make_valid()

    exit_code, standard_output, standard_error = run_goodall(["status"])

    # Exit 0 is reserved for an active work block, which cannot exist yet.
    assert exit_code == exit_codes.ERROR
    assert standard_error == ""
    assert standard_output == (
        "Eval         goodall-pilot\n"
        "Contributor  Ada Lovelace (ada-lovelace)\n"
        "Workstream   research, your main workstream\n"
        "\n"
        "No work block is active, so nothing is being recorded.\n"
    )


def test_valid_eval_as_json_gives_the_same_facts(eval_account: EvalAccount) -> None:
    eval_account.make_valid()

    exit_code, standard_output, standard_error = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    assert standard_error == ""
    assert json.loads(standard_output) == {
        "work_block_active": False,
        "eval": {"name": "goodall-pilot"},
        "contributor": {"id": "ada-lovelace", "display_name": "Ada Lovelace", "main_workstream": "research"},
        "failures": [],
    }


def test_reading_the_eval_changes_no_files(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    files_before = files_under(eval_account.home_folder)

    run_goodall(["status"])
    run_goodall(["status", "--json"])

    assert files_under(eval_account.home_folder) == files_before
    assert eval_account.eval_config_file.read_text() == VALID_EVAL_CONFIG_TEXT
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


# What goes wrong with the eval config. Local settings are valid in every
# test below; each test breaks one thing in eval.toml, or makes local
# settings disagree with it.


def break_eval_config(eval_account: EvalAccount, replace_from: str, replace_to: str) -> None:
    """Write valid files, then change one piece of text in the eval config."""
    eval_account.make_valid()
    assert replace_from in VALID_EVAL_CONFIG_TEXT, f"{replace_from!r} is not in the eval config"
    eval_account.write_eval_config(VALID_EVAL_CONFIG_TEXT.replace(replace_from, replace_to))


def everything_goodall_printed() -> str:
    """Run goodall status both ways and return everything it printed, on both streams."""
    _, text_output, text_error = run_goodall(["status"])
    _, json_output, json_error = run_goodall(["status", "--json"])
    return text_output + text_error + json_output + json_error


def test_missing_eval_config_names_the_file_and_the_fix(eval_account: EvalAccount) -> None:
    eval_account.write_settings(eval_account.valid_settings_text())

    exit_code, standard_output, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    assert standard_output == ""
    assert standard_error.startswith("FAIL  No eval config was found")
    assert str(eval_account.eval_config_file) in standard_error
    # The fix is either a wrong record_repo in local settings or a record
    # repo that is not up to date, so both files are named.
    assert str(eval_account.settings_file) in standard_error
    assert failure_kinds() == ["eval_config_missing"]


def test_eval_config_failure_as_json_keeps_the_stable_shape(eval_account: EvalAccount) -> None:
    eval_account.write_settings(eval_account.valid_settings_text())

    exit_code, standard_output, standard_error = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    assert standard_error == ""
    document = json.loads(standard_output)
    assert document["work_block_active"] is False
    assert document["eval"] is None
    assert document["contributor"] is None
    assert [failure["kind"] for failure in document["failures"]] == ["eval_config_missing"]


def test_unreadable_eval_config_fails_instead_of_crashing(
    eval_account: EvalAccount, remove_permissions: Callable[[Path], None]
) -> None:
    eval_account.make_valid()
    remove_permissions(eval_account.eval_config_file)

    _, _, standard_error = run_goodall(["status"])

    assert "Permission denied" in standard_error
    assert failure_kinds() == ["eval_config_unreadable"]


def test_eval_config_that_is_not_valid_toml_names_the_file_and_the_line(eval_account: EvalAccount) -> None:
    # A hand-edit that forgot the quotes around the eval's name, on line 2.
    break_eval_config(eval_account, 'name = "goodall-pilot"', "name = goodall-pilot")

    _, _, standard_error = run_goodall(["status"])

    assert standard_error.startswith("FAIL  The eval config at")
    assert str(eval_account.eval_config_file) in standard_error
    assert "line 2" in standard_error
    assert failure_kinds() == ["eval_config_not_toml"]


def test_unknown_key_in_eval_config_is_named_with_a_close_match(eval_account: EvalAccount) -> None:
    break_eval_config(eval_account, "workstreams =", "workstream =")

    _, _, standard_error = run_goodall(["status"])

    assert "Did you mean workstreams?" in " ".join(standard_error.split())
    assert str(eval_account.eval_config_file) in standard_error
    assert sorted(failure_kinds()) == ["eval_config_key_missing", "eval_config_key_unknown"]


def test_unknown_key_in_a_contributor_block_is_named(eval_account: EvalAccount) -> None:
    break_eval_config(eval_account, 'display_name = "Grace Hopper"', 'display_name = "Grace Hopper"\nteam = "x"')

    _, _, standard_error = run_goodall(["status"])

    assert "team" in standard_error
    assert failure_kinds() == ["eval_config_key_unknown"]


def test_newer_eval_config_schema_says_to_upgrade_goodall_and_nothing_else(eval_account: EvalAccount) -> None:
    break_eval_config(eval_account, "schema_version = 1", 'schema_version = 2\nadded_later = "yes"')

    _, _, standard_error = run_goodall(["status"])

    assert "schema_version 2" in standard_error
    assert "upgrade goodall" in standard_error.lower()
    assert failure_kinds() == ["eval_config_schema_too_new"]


@pytest.mark.parametrize(
    "replace_from, replace_to",
    [
        ('name = "goodall-pilot"\n', ""),
        ('description = "Building a SaaS product to first recurring revenue with AI."\n', ""),
        ("schema_version = 1\n", ""),
    ],
)
def test_missing_eval_config_key_is_named(eval_account: EvalAccount, replace_from: str, replace_to: str) -> None:
    break_eval_config(eval_account, replace_from, replace_to)
    assert failure_kinds() == ["eval_config_key_missing"]


@pytest.mark.parametrize(
    "replace_from, replace_to",
    [
        ('name = "goodall-pilot"', 'name = ""'),
        ('name = "goodall-pilot"', "name = 5"),
        ('workstreams = ["engineering", "research", "go-to-market"]', "workstreams = []"),
        ('workstreams = ["engineering", "research", "go-to-market"]', 'workstreams = "research"'),
        ('"go-to-market"]', '"Go To Market"]'),
        ('"freely-delegable"]', '"freely delegable"]'),
        ('display_name = "Grace Hopper"', 'display_name = ""'),
        ('paths = ["**"]', "paths = []"),
        ('id = "grace-hopper"', 'id = "Grace_Hopper"'),
        ('id = "grace-hopper"', 'id = "-grace"'),
    ],
)
def test_eval_config_value_of_the_wrong_form_is_reported(
    eval_account: EvalAccount, replace_from: str, replace_to: str
) -> None:
    break_eval_config(eval_account, replace_from, replace_to)
    assert failure_kinds() == ["eval_config_value_invalid"]


def test_contributor_id_rule_is_explained(eval_account: EvalAccount) -> None:
    break_eval_config(eval_account, 'id = "grace-hopper"', 'id = "Grace Hopper"')

    _, _, standard_error = run_goodall(["status"])

    assert "lowercase letters, digits and hyphens" in " ".join(standard_error.split())


def test_eval_config_without_contributors_or_scope_is_reported(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    first_block = VALID_EVAL_CONFIG_TEXT.index("[[contributors]]")
    eval_account.write_eval_config(VALID_EVAL_CONFIG_TEXT[:first_block])

    assert failure_kinds() == ["eval_config_key_missing", "eval_config_key_missing"]


def test_duplicate_contributor_ids_are_reported(eval_account: EvalAccount) -> None:
    break_eval_config(eval_account, 'id = "grace-hopper"', 'id = "ada-lovelace"')

    _, _, standard_error = run_goodall(["status"])

    assert "ada-lovelace" in standard_error
    assert failure_kinds() == ["eval_config_duplicate"]


def test_duplicate_workstreams_are_reported(eval_account: EvalAccount) -> None:
    break_eval_config(eval_account, '"go-to-market"]', '"research"]')

    _, _, standard_error = run_goodall(["status"])

    assert "research" in standard_error
    assert failure_kinds() == ["eval_config_duplicate"]


def test_several_eval_config_problems_are_all_shown(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    eval_account.write_eval_config(
        VALID_EVAL_CONFIG_TEXT.replace('name = "goodall-pilot"\n', "")
        .replace("workstreams =", "workstream =")
        .replace('id = "grace-hopper"', 'id = "Grace"')
    )

    _, _, standard_error = run_goodall(["status"])

    assert standard_error.count("FAIL  ") == 4
    assert sorted(failure_kinds()) == [
        "eval_config_key_missing",
        "eval_config_key_missing",
        "eval_config_key_unknown",
        "eval_config_value_invalid",
    ]


# Local settings checked against the eval config.


def test_contributor_not_in_roster_names_the_id_and_the_roster(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    eval_account.write_settings(eval_account.valid_settings_text().replace("ada-lovelace", "charles-babbage"))

    exit_code, _, standard_error = run_goodall(["status"])

    assert exit_code == exit_codes.ERROR
    one_line = " ".join(standard_error.split())
    assert one_line.startswith("FAIL Your contributor id, charles-babbage, is not in this eval's roster.")
    assert "ada-lovelace, grace-hopper" in one_line
    # Either file may be the one to change, so both are named.
    assert str(eval_account.settings_file) in standard_error
    assert str(eval_account.eval_config_file) in standard_error
    assert failure_kinds() == ["contributor_not_in_roster"]


def test_main_workstream_not_in_eval_lists_the_valid_ones(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    eval_account.write_settings(eval_account.valid_settings_text().replace('"research"', '"marketing"'))

    _, _, standard_error = run_goodall(["status"])

    one_line = " ".join(standard_error.split())
    assert "marketing" in one_line
    assert "engineering, research, go-to-market" in one_line
    assert str(eval_account.settings_file) in standard_error
    assert failure_kinds() == ["main_workstream_unknown"]


def test_both_cross_checks_are_reported_together(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    eval_account.write_settings(
        eval_account.valid_settings_text().replace("ada-lovelace", "charles-babbage").replace('"research"', '"sales"')
    )
    assert failure_kinds() == ["contributor_not_in_roster", "main_workstream_unknown"]


def test_contributor_setting_that_could_be_a_secret_is_not_echoed(eval_account: EvalAccount) -> None:
    # Only a value shaped like an id is quoted back; anything else in that
    # place may be a key pasted by mistake.
    pasted_secret = "sk-ant-API03_not-a-real-key"
    eval_account.make_valid()
    eval_account.write_settings(eval_account.valid_settings_text().replace("ada-lovelace", pasted_secret))

    assert pasted_secret not in everything_goodall_printed()
    assert failure_kinds() == ["contributor_not_in_roster"]


# The declared scope names repositories by identity, never by a local path.


@pytest.mark.parametrize(
    "repository",
    [
        "/Users/ada/code/goodall-pilot-app",
        "~/code/goodall-pilot-app",
        "./goodall-pilot-app",
        "../goodall-pilot-app",
        "goodall-pilot-app",
        "your-org/goodall-pilot-app",
        "file:///Users/ada/code/goodall-pilot-app",
        "https://github.com/your-org/goodall-pilot-app",
        "C:\\code\\goodall-pilot-app",
        "github.com/your-org/goodall pilot app",
        "github.com/your-org/goodall-pilot-app.git",
        "your-org/goodall-pilot-app/extra",
    ],
)
def test_scope_entry_that_is_not_a_repository_identity_is_rejected(eval_account: EvalAccount, repository: str) -> None:
    break_eval_config(
        eval_account,
        'repository = "github.com/your-org/goodall-pilot-app"',
        f"repository = {json.dumps(repository)}",
    )

    _, _, standard_error = run_goodall(["status"])

    one_line = " ".join(standard_error.split())
    assert "host/owner/name" in one_line
    assert 'repository = "github.com/your-org/goodall-pilot-app"' in one_line
    assert failure_kinds() == ["eval_config_scope_not_a_repository"]


@pytest.mark.parametrize("path_pattern", ["/etc/**", "~/code/**", "../other-repo/**", "src/../../escape"])
def test_scope_path_that_leaves_the_repository_is_rejected(eval_account: EvalAccount, path_pattern: str) -> None:
    break_eval_config(eval_account, 'paths = ["**"]', f'paths = ["src/**", "{path_pattern}"]')

    _, _, standard_error = run_goodall(["status"])

    assert "inside the repository" in " ".join(standard_error.split())
    assert failure_kinds() == ["eval_config_scope_path_invalid"]


def test_several_scope_entries_and_patterns_are_accepted(eval_account: EvalAccount) -> None:
    eval_account.make_valid()
    eval_account.write_eval_config(
        VALID_EVAL_CONFIG_TEXT
        + '\n[[scope]]\nrepository = "gitlab.com/your-org/research-notes"\npaths = ["docs/**", "*.md", ".github/**"]\n'
    )

    exit_code, standard_output, _ = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    assert json.loads(standard_output)["failures"] == []


# The eval config may be published as it stands (ADR 0009), so it holds no
# email address and no secret reference anywhere.


@pytest.mark.parametrize(
    "replace_from, replace_to",
    [
        ('display_name = "Ada Lovelace"', 'display_name = "Ada Lovelace <ada@example.org>"'),
        (
            'description = "Building a SaaS product to first recurring revenue with AI."',
            'description = "Contact ada@example.org about this eval."',
        ),
    ],
)
def test_email_address_in_eval_config_is_rejected_without_being_echoed(
    eval_account: EvalAccount, replace_from: str, replace_to: str
) -> None:
    break_eval_config(eval_account, replace_from, replace_to)

    printed = everything_goodall_printed()

    assert "ada@example.org" not in printed
    assert "email address" in printed
    assert failure_kinds() == ["eval_config_contains_email_address"]


@pytest.mark.parametrize(
    "replace_from, replace_to",
    [
        (
            'description = "Building a SaaS product to first recurring revenue with AI."',
            'description = "op://Eval/anthropic-engineering/credential"',
        ),
        ('paths = ["**"]', 'paths = ["**", "op://Eval/anthropic-engineering/credential"]'),
        # The same rule as the shared env file's: a scheme written in capitals counts.
        (
            'description = "Building a SaaS product to first recurring revenue with AI."',
            'description = "Keys live at OP://Eval/anthropic-engineering/credential"',
        ),
    ],
)
def test_secret_reference_in_eval_config_is_rejected_without_being_echoed(
    eval_account: EvalAccount, replace_from: str, replace_to: str
) -> None:
    break_eval_config(eval_account, replace_from, replace_to)

    printed = everything_goodall_printed()

    assert "anthropic-engineering" not in printed
    assert "secret reference" in printed
    assert failure_kinds() == ["eval_config_contains_secret_reference"]


@pytest.mark.parametrize(
    "replace_from, replace_to",
    [
        ("schema_version = 1", "schema_version = = 1"),
        ('name = "goodall-pilot"\n', ""),
        ("workstreams =", "workstreem ="),
        ("schema_version = 1", "schema_version = 99"),
        ('id = "grace-hopper"', 'id = "ada-lovelace"'),
        ('"github.com/your-org/goodall-pilot-app"', '"~/code/app"'),
        ('paths = ["**"]', 'paths = ["/"]'),
        ('display_name = "Ada Lovelace"', 'display_name = "ada@example.org"'),
        ('display_name = "Ada Lovelace"', 'display_name = "op://Eval/item/field"'),
    ],
)
def test_every_eval_config_failure_says_what_why_and_next_and_exits_1(
    eval_account: EvalAccount, replace_from: str, replace_to: str
) -> None:
    break_eval_config(eval_account, replace_from, replace_to)

    exit_code, standard_output, _ = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    assert document["eval"] is None
    assert document["failures"] != []
    for failure in document["failures"]:
        assert set(failure) == {"kind", "what_happened", "why", "what_to_do_next"}
        assert str(eval_account.eval_config_file) in failure["what_happened"] + failure["what_to_do_next"]


# Every place in local settings and the eval config where a person could
# paste a key by mistake. Each case is: the synthetic secret, which file it
# goes in, the text to replace in that file's valid form ("" puts the new
# text at the start of the file), and what to put there. No case may ever
# print its secret.
PASTED_KEY = "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC"
# A lower-case hex token passes the eval config's name rule, so it tests
# that following the name rule is not enough to be shown.
PASTED_HEX_TOKEN = "0123456789abcdef0123456789abcdef"

SECRET_POSITIONS = {
    "settings unknown key": (PASTED_KEY, "settings", "", f'{PASTED_KEY} = "x"\n'),
    "settings unknown table": (PASTED_KEY, "settings", "", f'[{PASTED_KEY}]\nvalue = "x"\n'),
    "settings table declared twice": (PASTED_KEY, "settings", "", f"[{PASTED_KEY}]\n[{PASTED_KEY}]\n"),
    "settings secrets_manager": (PASTED_KEY, "settings", '"1password"', f'"{PASTED_KEY}"'),
    "settings schema_version": (PASTED_KEY, "settings", "schema_version = 1", f'schema_version = "{PASTED_KEY}"'),
    "settings record_repo": (PASTED_KEY, "settings", '"~/evals/goodall-pilot-records"', f'"{PASTED_KEY}"'),
    "settings main_workstream": (PASTED_KEY, "settings", '"research"', f'"{PASTED_KEY}"'),
    "settings contributor": (PASTED_KEY, "settings", '"ada-lovelace"', f'"{PASTED_KEY}"'),
    "settings contributor shaped like a name": (
        PASTED_HEX_TOKEN,
        "settings",
        '"ada-lovelace"',
        f'"{PASTED_HEX_TOKEN}"',
    ),
    "settings main_workstream shaped like a name": (
        PASTED_HEX_TOKEN,
        "settings",
        '"research"',
        f'"{PASTED_HEX_TOKEN}"',
    ),
    "eval config unknown key": (PASTED_KEY, "eval_config", "", f'{PASTED_KEY} = "x"\n'),
    "eval config unknown key in a block": (
        PASTED_KEY,
        "eval_config",
        'display_name = "Ada Lovelace"\n',
        f'display_name = "Ada Lovelace"\n{PASTED_KEY} = "x"\n',
    ),
    "eval config unknown blocks": (
        PASTED_KEY,
        "eval_config",
        "[[scope]]",
        f'[[{PASTED_KEY}]]\nnote = "x@example.com"\n\n[[scope]]',
    ),
    "eval config key holding a reference": (
        PASTED_KEY,
        "eval_config",
        "[[scope]]",
        f'[extra]\n{PASTED_KEY} = "op://Eval/item/field"\n\n[[scope]]',
    ),
    "eval config table declared twice": (
        PASTED_KEY,
        "eval_config",
        "[[scope]]",
        f"[{PASTED_KEY}]\n[{PASTED_KEY}]\n\n[[scope]]",
    ),
    "eval config workstream": (PASTED_KEY, "eval_config", '"go-to-market"]', f'"{PASTED_KEY}"]'),
    "eval config repeated workstream shaped like a name": (
        PASTED_HEX_TOKEN,
        "eval_config",
        '"go-to-market"]',
        f'"{PASTED_HEX_TOKEN}", "{PASTED_HEX_TOKEN}"]',
    ),
    "eval config repository": (
        PASTED_KEY,
        "eval_config",
        '"github.com/your-org/goodall-pilot-app"',
        f'"{PASTED_KEY}"',
    ),
}


def put_secret_in_place(eval_account: EvalAccount, file_name: str, replace_from: str, replace_to: str) -> None:
    """Write valid files, then put a pasted secret in one place in one of them."""
    eval_account.make_valid()
    if file_name == "settings":
        valid_text = eval_account.valid_settings_text()
    else:
        valid_text = VALID_EVAL_CONFIG_TEXT
    if replace_from == "":
        broken_text = replace_to + valid_text
    else:
        assert replace_from in valid_text, f"{replace_from!r} is not in the {file_name} file"
        broken_text = valid_text.replace(replace_from, replace_to, 1)
    if file_name == "settings":
        eval_account.write_settings(broken_text)
    else:
        eval_account.write_eval_config(broken_text)


@pytest.mark.parametrize("position", list(SECRET_POSITIONS))
def test_a_secret_pasted_anywhere_in_settings_or_eval_config_is_never_shown_by_status(
    eval_account: EvalAccount, position: str
) -> None:
    secret, file_name, replace_from, replace_to = SECRET_POSITIONS[position]
    put_secret_in_place(eval_account, file_name, replace_from, replace_to)

    exit_code, text_output, text_error = run_goodall(["status"])
    json_exit_code, json_output, json_error = run_goodall(["status", "--json"])

    assert exit_code == exit_codes.ERROR
    assert json_exit_code == exit_codes.ERROR
    for printed in [text_output, text_error, json_output, json_error]:
        assert secret not in printed
    assert json.loads(json_output)["failures"] != []
