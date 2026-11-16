"""Outside-in tests for `goodall doctor`, driven through `cli.main`.

Each test runs goodall the way a contributor or an agent would, hands it a
captured terminal, and checks only what they would observe: the exit code,
the screen, the JSON document and the files on disk.

The whole-screen tests (`EXPECTED_HEALTHY_SCREEN` and
`EXPECTED_SETTINGS_MISSING_SCREEN`) are a contract: a future replacement of
the terminal module would have to produce the same screens at the fixed width
with colour off.
"""

import io
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from goodall import cli, exit_codes
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

# The command the account check runs, by its full path (see goodall.program_checks).
ACCOUNT_GROUPS_COMMAND = ["/usr/bin/id", "-Gn"]

# The groups a standard macOS user account belongs to.
STANDARD_ACCOUNT_GROUPS = "staff everyone localaccounts _appserverusr com.apple.sharepoint.group.1\n"


def healthy_runner() -> FakeRunner:
    """A fake runner for a Mac where every program is installed and the account is a standard one.

    git is real (FakeRunner's default), so the record repo check asks real
    git about a real folder.
    """
    runner = FakeRunner(installed_programs=["git", "gh", "op"])
    runner.answer(ACCOUNT_GROUPS_COMMAND, standard_output=STANDARD_ACCOUNT_GROUPS)
    return runner


def run_goodall(arguments: list[str], runner: FakeRunner | None = None) -> tuple[int, str, str]:
    """Run goodall with a captured terminal; return the exit code, standard output and standard error.

    Without a `runner`, goodall gets `healthy_runner()`.
    """
    if runner is None:
        runner = healthy_runner()
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner)
    return exit_code, standard_output.getvalue(), standard_error.getvalue()


VALID_EVAL_CONFIG_TEXT = """\
schema_version = 1
name = "goodall-pilot"
description = "Building a SaaS product to first recurring revenue with AI."
workstreams = ["engineering", "research", "go-to-market"]
intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]

[[contributors]]
id = "ada-lovelace"
display_name = "Ada Lovelace"

[[scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]
"""


@dataclass
class EvalAccount:
    """A pretend eval account inside tmp_path, with every file goodall doctor reads."""

    home_folder: Path
    config_directory: Path
    record_repo: Path
    log_directory: Path

    @property
    def settings_file(self) -> Path:
        return self.config_directory / "local-settings.toml"

    @property
    def eval_config_file(self) -> Path:
        return self.record_repo / "eval.toml"

    def settings_text(self, main_workstream: str = "research") -> str:
        return (
            "schema_version = 1\n"
            'contributor = "ada-lovelace"\n'
            f'main_workstream = "{main_workstream}"\n'
            'record_repo = "~/evals/goodall-pilot-records"\n'
            f'log_directory = "{self.log_directory}"\n'
            'secrets_manager = "1password"\n'
        )


@pytest.fixture
def healthy_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvalAccount:
    """An eval account where every check passes."""
    home_folder = tmp_path / "home"
    account = EvalAccount(
        home_folder=home_folder,
        config_directory=home_folder / "goodall-config",
        record_repo=home_folder / "evals" / "goodall-pilot-records",
        log_directory=tmp_path / "EvalLogs" / "goodall-pilot",
    )
    for folder in [account.config_directory, account.record_repo, account.log_directory]:
        folder.mkdir(parents=True)
    account.settings_file.write_text(account.settings_text())
    account.eval_config_file.write_text(VALID_EVAL_CONFIG_TEXT)
    # The record repo is a real git repository, because real git checks it.
    subprocess.run(["git", "init", "--quiet", str(account.record_repo)], check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(account.config_directory))
    # Doctor must not depend on, or write to, the folder it is run from.
    monkeypatch.chdir(tmp_path)
    return account


EXPECTED_HEALTHY_SCREEN = """\
Settings
  pass  Local settings are found
  pass  Local settings are valid
  pass  The record repo folder exists
  pass  The log directory can be written to
  pass  The eval config is found and valid
  pass  This account's contributor is in the roster
  pass  The main workstream is one of the eval's workstreams

Tools
  pass  git is installed
  pass  gh is installed
  pass  The secrets manager's command is installed

Eval account
  pass  This macOS user account is a standard account, not an administrator

Record repo
  pass  The record repo folder is a git repository

12 passed, 0 warned, 0 failed, 0 skipped
"""


def test_healthy_setup_passes_every_check_and_exits_0(healthy_account: EvalAccount) -> None:
    exit_code, standard_output, standard_error = run_goodall(["doctor"])

    assert standard_output == EXPECTED_HEALTHY_SCREEN
    assert standard_error == ""
    assert exit_code == exit_codes.SUCCESS


# A folder that exists on no machine, so the path in the message, and with it
# the line breaks, are the same everywhere. Goodall only tries to open a file
# inside it, which fails; nothing is created there.
MISSING_CONFIG_DIRECTORY = "/nonexistent-goodall-test/goodall-config"

EXPECTED_SETTINGS_MISSING_SCREEN = """\
Settings
  FAIL  Local settings are found
        No local settings were found. Goodall looked for
        /nonexistent-goodall-test/goodall-config/local-settings.toml, in the
        folder that GOODALL_CONFIG_DIR names.
        Next: Run goodall onboard to set up this eval account, or unset
        GOODALL_CONFIG_DIR if your local settings are in your home folder.

  skip  Local settings are valid
        Waiting for: Local settings are found
  skip  The record repo folder exists
        Waiting for: Local settings are found
  skip  The log directory can be written to
        Waiting for: Local settings are found
  skip  The eval config is found and valid
        Waiting for: Local settings are found
  skip  This account's contributor is in the roster
        Waiting for: Local settings are found
  skip  The main workstream is one of the eval's workstreams
        Waiting for: Local settings are found

Tools
  pass  git is installed
  pass  gh is installed
  skip  The secrets manager's command is installed
        Waiting for: Local settings are found

Eval account
  pass  This macOS user account is a standard account, not an administrator

Record repo
  skip  The record repo folder is a git repository
        Waiting for: Local settings are found

3 passed, 0 warned, 1 failed, 8 skipped
"""


@pytest.fixture
def settings_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOODALL_CONFIG_DIR", MISSING_CONFIG_DIRECTORY)
    monkeypatch.chdir(tmp_path)


def test_missing_local_settings_is_one_failure_and_every_other_check_waits_for_it(settings_missing: None) -> None:
    exit_code, standard_output, standard_error = run_goodall(["doctor"])

    assert standard_output == EXPECTED_SETTINGS_MISSING_SCREEN
    assert standard_error == ""
    assert exit_code == exit_codes.ERROR


def doctor_json() -> tuple[int, dict[str, object]]:
    """Run `goodall doctor --json`; return the exit code and the parsed document."""
    exit_code, standard_output, standard_error = run_goodall(["doctor", "--json"])
    assert standard_error == ""
    # json.loads rejects trailing text, so this also proves standard output
    # holds exactly one document.
    return exit_code, json.loads(standard_output)


def statuses(document: dict[str, object]) -> dict[str, str]:
    """Map each check's id to its status, from a `goodall doctor --json` document."""
    checks = document["checks"]
    assert isinstance(checks, list)
    statuses_by_id = {}
    for check in checks:
        statuses_by_id[check["id"]] = check["status"]
    return statuses_by_id


ALL_CHECK_IDS = [
    "settings.local_settings_found",
    "settings.local_settings_valid",
    "settings.record_repo_found",
    "settings.log_directory_reachable",
    "settings.eval_config_valid",
    "settings.contributor_in_roster",
    "settings.main_workstream_known",
    "tools.git_installed",
    "tools.gh_installed",
    "tools.secrets_manager_installed",
    "eval_account.standard_account",
    "record_repo.is_git_repository",
]


def test_healthy_setup_as_json_gives_the_same_results(healthy_account: EvalAccount) -> None:
    exit_code, document = doctor_json()

    assert exit_code == exit_codes.SUCCESS
    assert document["schema_version"] == 1
    assert document["summary"] == {"passed": 12, "warned": 0, "failed": 0, "skipped": 0}
    checks = document["checks"]
    assert isinstance(checks, list)
    assert checks[0] == {
        "id": "settings.local_settings_found",
        "group": "Settings",
        "status": "pass",
        "summary": "Local settings are found",
        "detail": None,
        "next_step": None,
        "waiting_for": None,
        "failures": [],
    }
    assert list(statuses(document)) == ALL_CHECK_IDS
    # The report shows each check's summary; the JSON carries the same one.
    for check in checks:
        assert check["summary"] in EXPECTED_HEALTHY_SCREEN


def test_missing_local_settings_as_json_names_what_each_skip_waited_for(settings_missing: None) -> None:
    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert document["summary"] == {"passed": 3, "warned": 0, "failed": 1, "skipped": 8}
    checks = document["checks"]
    assert isinstance(checks, list)
    failed_check = checks[0]
    assert failed_check["status"] == "fail"
    assert [failure["kind"] for failure in failed_check["failures"]] == ["local_settings_missing"]
    assert "goodall onboard" in failed_check["next_step"]
    assert MISSING_CONFIG_DIRECTORY in failed_check["detail"]
    # Every check that needs to know this account's eval waits for local
    # settings; the checks that do not (git, gh, the account) still run.
    for check in checks[1:]:
        if check["id"] in ["tools.git_installed", "tools.gh_installed", "eval_account.standard_account"]:
            assert check["status"] == "pass"
            continue
        assert check["status"] == "skip"
        assert check["waiting_for"] == "settings.local_settings_found"
        assert check["failures"] == []


def test_invalid_local_settings_fail_once_and_name_every_problem(healthy_account: EvalAccount) -> None:
    healthy_account.settings_file.write_text(
        healthy_account.settings_text().replace('contributor = "ada-lovelace"\n', "") + 'favourite_colour = "green"\n'
    )

    exit_code, standard_output, _ = run_goodall(["doctor"])

    assert exit_code == exit_codes.ERROR
    assert "  pass  Local settings are found\n" in standard_output
    assert "  FAIL  Local settings are valid\n" in standard_output
    # Both problems are shown, each with its own next step.
    assert "have no contributor" in standard_output
    assert "favourite_colour" in standard_output
    assert standard_output.count("        Next: ") == 2
    assert "Waiting for: Local settings are valid" in standard_output
    assert standard_output.endswith("4 passed, 0 warned, 1 failed, 7 skipped\n")


def test_relative_settings_folder_override_fails_the_found_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOODALL_CONFIG_DIR", "relative/goodall-config")
    monkeypatch.chdir(tmp_path)

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert statuses(document)["settings.local_settings_found"] == "fail"


def test_missing_record_repo_skips_the_eval_config_but_still_checks_the_log_directory(
    healthy_account: EvalAccount,
) -> None:
    healthy_account.settings_file.write_text(
        healthy_account.settings_text().replace("~/evals/goodall-pilot-records", "~/evals/not-cloned-yet")
    )

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert statuses(document) == {
        "settings.local_settings_found": "pass",
        "settings.local_settings_valid": "pass",
        "settings.record_repo_found": "fail",
        "settings.log_directory_reachable": "pass",
        "settings.eval_config_valid": "skip",
        "settings.contributor_in_roster": "skip",
        "settings.main_workstream_known": "skip",
        "tools.git_installed": "pass",
        "tools.gh_installed": "pass",
        "tools.secrets_manager_installed": "pass",
        "eval_account.standard_account": "pass",
        "record_repo.is_git_repository": "skip",
    }
    checks = document["checks"]
    assert isinstance(checks, list)
    # The checks two steps away name the record repo, the real cause, not
    # the eval config check they depend on directly.
    for check in checks:
        if check["status"] == "skip":
            assert check["waiting_for"] == "settings.record_repo_found"


def test_unreachable_log_directory_is_a_warning_with_a_next_step_and_exits_0(healthy_account: EvalAccount) -> None:
    # An unplugged drive: goodall is not recording, but nothing is broken.
    healthy_account.settings_file.write_text(
        healthy_account.settings_text().replace(str(healthy_account.log_directory), "/Volumes/NotPluggedIn/goodall")
    )

    exit_code, standard_output, _ = run_goodall(["doctor"])

    assert exit_code == exit_codes.SUCCESS
    assert "  warn  The log directory can be written to\n        Not recording:" in standard_output
    assert "        Next: If the log directory is on an external drive, connect it." in standard_output
    assert standard_output.endswith("11 passed, 1 warned, 0 failed, 0 skipped\n")

    json_exit_code, document = doctor_json()
    assert json_exit_code == exit_code
    assert statuses(document)["settings.log_directory_reachable"] == "warn"


def test_broken_eval_config_skips_the_two_cross_checks(healthy_account: EvalAccount) -> None:
    healthy_account.eval_config_file.write_text(VALID_EVAL_CONFIG_TEXT.replace('name = "goodall-pilot"\n', ""))

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert statuses(document)["settings.eval_config_valid"] == "fail"
    assert statuses(document)["settings.contributor_in_roster"] == "skip"
    assert statuses(document)["settings.main_workstream_known"] == "skip"


def test_contributor_and_main_workstream_are_reported_as_separate_checks(healthy_account: EvalAccount) -> None:
    healthy_account.eval_config_file.write_text(
        VALID_EVAL_CONFIG_TEXT.replace('id = "ada-lovelace"', 'id = "grace-hopper"')
    )
    healthy_account.settings_file.write_text(healthy_account.settings_text(main_workstream="sales"))

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert document["summary"] == {"passed": 10, "warned": 0, "failed": 2, "skipped": 0}
    checks = document["checks"]
    assert isinstance(checks, list)
    assert [failure["kind"] for failure in checks[5]["failures"]] == ["contributor_not_in_roster"]
    assert [failure["kind"] for failure in checks[6]["failures"]] == ["main_workstream_unknown"]


@pytest.mark.parametrize(
    "break_setup",
    [
        "remove local settings",
        "break local settings",
        "remove record repo",
        "unplug log directory",
        "break eval config",
        "unknown contributor",
    ],
)
def test_every_failure_and_warning_has_a_next_step_and_exit_codes_agree(
    healthy_account: EvalAccount, break_setup: str
) -> None:
    if break_setup == "remove local settings":
        healthy_account.settings_file.unlink()
    elif break_setup == "break local settings":
        healthy_account.settings_file.write_text("schema_version = = 1\n")
    elif break_setup == "remove record repo":
        shutil.rmtree(healthy_account.record_repo)
    elif break_setup == "unplug log directory":
        healthy_account.log_directory.rmdir()
    elif break_setup == "break eval config":
        healthy_account.eval_config_file.write_text("schema_version = 1\n")
    else:
        healthy_account.eval_config_file.write_text(VALID_EVAL_CONFIG_TEXT.replace("ada-lovelace", "grace-hopper"))

    text_exit_code, standard_output, _ = run_goodall(["doctor"])
    json_exit_code, document = doctor_json()

    assert text_exit_code == json_exit_code
    checks = document["checks"]
    assert isinstance(checks, list)
    for check in checks:
        if check["status"] in ["fail", "warn"]:
            assert check["next_step"], f"{check['id']} has no next step"
            assert check["failures"] != []
            # The text report shows the start of the same next step.
            assert "Next: " + check["next_step"].split(" ")[0] in standard_output
        else:
            assert check["next_step"] is None
        if check["status"] == "skip":
            assert check["waiting_for"] in ALL_CHECK_IDS
        else:
            assert check["waiting_for"] is None


def snapshot_of_every_file(folder: Path) -> dict[Path, tuple[int, int, bytes | None]]:
    """Record every file and folder under `folder`: its permissions, modification time and contents.

    Comparing two snapshots shows whether anything was created, deleted,
    changed or even touched in between.
    """
    snapshot: dict[Path, tuple[int, int, bytes | None]] = {}
    for path in sorted(folder.rglob("*")):
        path_status = path.stat()
        contents = path.read_bytes() if path.is_file() else None
        snapshot[path] = (path_status.st_mode, path_status.st_mtime_ns, contents)
    return snapshot


def test_doctor_changes_nothing_on_a_healthy_setup(healthy_account: EvalAccount, tmp_path: Path) -> None:
    before = snapshot_of_every_file(tmp_path)

    run_goodall(["doctor"])
    run_goodall(["doctor", "--json"])

    assert snapshot_of_every_file(tmp_path) == before


def test_doctor_changes_nothing_on_a_broken_setup(healthy_account: EvalAccount, tmp_path: Path) -> None:
    # Every kind of problem at once: a missing log directory (goodall must
    # not create it), a contributor missing from the roster, and a main
    # workstream the eval does not have (goodall must not "fix" either file).
    healthy_account.log_directory.rmdir()
    healthy_account.eval_config_file.write_text(VALID_EVAL_CONFIG_TEXT.replace("ada-lovelace", "grace-hopper"))
    healthy_account.settings_file.write_text(healthy_account.settings_text(main_workstream="sales"))
    before = snapshot_of_every_file(tmp_path)

    exit_code, _, _ = run_goodall(["doctor"])
    run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    assert snapshot_of_every_file(tmp_path) == before
    assert not healthy_account.log_directory.exists()


def test_doctor_changes_nothing_when_local_settings_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_directory = tmp_path / "goodall-config"
    config_directory.mkdir()
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    monkeypatch.chdir(tmp_path)
    before = snapshot_of_every_file(tmp_path)

    run_goodall(["doctor"])
    run_goodall(["doctor", "--json"])

    assert snapshot_of_every_file(tmp_path) == before


def check_document(document: dict[str, object], check_id: str) -> dict[str, object]:
    """The one check with this id from a `goodall doctor --json` document."""
    checks = document["checks"]
    assert isinstance(checks, list)
    for check in checks:
        if check["id"] == check_id:
            assert isinstance(check, dict)
            return check
    raise AssertionError(f"No check {check_id} in the document.")


def test_missing_gh_is_one_failure_naming_it_and_saying_the_owner_of_the_mac_installs_it(
    healthy_account: EvalAccount,
) -> None:
    runner = healthy_runner()
    runner.installed_programs.remove("gh")

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"], runner)

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    assert document["summary"] == {"passed": 11, "warned": 0, "failed": 1, "skipped": 0}
    gh_check = check_document(document, "tools.gh_installed")
    assert gh_check["status"] == "fail"
    assert isinstance(gh_check["detail"], str)
    assert "gh" in gh_check["detail"]
    assert isinstance(gh_check["next_step"], str)
    # On an eval account the contributor cannot install software, so the
    # next step goes to whoever administers the Mac.
    assert "owner of this Mac" in gh_check["next_step"]
    assert "administrator account" in gh_check["next_step"]
    assert "https://cli.github.com" in gh_check["next_step"]


def test_missing_git_is_one_failure_and_the_record_repo_check_waits_for_it(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.installed_programs.remove("git")

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"], runner)

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    assert document["summary"] == {"passed": 10, "warned": 0, "failed": 1, "skipped": 1}
    git_check = check_document(document, "tools.git_installed")
    assert git_check["status"] == "fail"
    assert isinstance(git_check["next_step"], str)
    assert "xcode-select --install" in git_check["next_step"]
    assert check_document(document, "record_repo.is_git_repository")["waiting_for"] == "tools.git_installed"


def test_missing_op_fails_the_secrets_manager_check_naming_1password(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.installed_programs.remove("op")

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"], runner)

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    assert document["summary"] == {"passed": 11, "warned": 0, "failed": 1, "skipped": 0}
    secrets_manager_check = check_document(document, "tools.secrets_manager_installed")
    assert secrets_manager_check["status"] == "fail"
    assert isinstance(secrets_manager_check["detail"], str)
    assert "op, 1Password's command-line tool," in secrets_manager_check["detail"]
    assert isinstance(secrets_manager_check["next_step"], str)
    assert "https://developer.1password.com/docs/cli/get-started/" in secrets_manager_check["next_step"]


# The groups of an administrator on macOS: the same as a standard account's,
# plus admin and the groups that come with it.
ADMINISTRATOR_ACCOUNT_GROUPS = "staff everyone localaccounts _appserverusr admin _appserveradm _lpadmin\n"


def test_an_administrator_account_fails_with_why_a_standard_account_is_required(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.answer(ACCOUNT_GROUPS_COMMAND, standard_output=ADMINISTRATOR_ACCOUNT_GROUPS)

    exit_code, standard_output, _ = run_goodall(["doctor"], runner)

    assert exit_code == exit_codes.ERROR
    assert "  FAIL  This macOS user account is a standard account, not an administrator\n" in standard_output
    # Joined into one line, so a phrase that wraps still matches.
    report_text = " ".join(standard_output.split())
    assert "is an administrator (it is in the admin group)" in report_text
    # The reason, so a researcher does not wave it away as fussiness.
    assert "cannot reach other accounts' files or install software" in report_text
    assert "safe to hand to an agent" in report_text
    assert standard_output.endswith("11 passed, 0 warned, 1 failed, 0 skipped\n")


@pytest.mark.parametrize(
    ("groups", "expected_status"),
    [
        # Linux has no admin group; an account that can become root is in
        # sudo (Debian, Ubuntu) or wheel (Fedora, and root on macOS).
        ("ada adm cdrom sudo dip plugdev\n", "fail"),
        ("ada wheel\n", "fail"),
        ("ada docker users\n", "pass"),
    ],
)
def test_the_account_check_also_recognises_administrators_on_linux(
    healthy_account: EvalAccount, groups: str, expected_status: str
) -> None:
    runner = healthy_runner()
    runner.answer(ACCOUNT_GROUPS_COMMAND, standard_output=groups)

    _, standard_output, _ = run_goodall(["doctor", "--json"], runner)

    document = json.loads(standard_output)
    assert check_document(document, "eval_account.standard_account")["status"] == expected_status


def test_an_account_check_that_never_finishes_is_reported_as_timed_out_and_doctor_still_completes(
    healthy_account: EvalAccount,
) -> None:
    runner = healthy_runner()
    runner.answer_timed_out(ACCOUNT_GROUPS_COMMAND)

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"], runner)

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    # Not being able to ask is a failure, never a pass: an unanswered
    # question must not read as "standard account".
    account_check = check_document(document, "eval_account.standard_account")
    assert account_check["status"] == "fail"
    assert isinstance(account_check["detail"], str)
    assert "`/usr/bin/id -Gn` timed out after 10 s" in account_check["detail"]
    # Every other check still ran.
    assert document["summary"] == {"passed": 11, "warned": 0, "failed": 1, "skipped": 0}
