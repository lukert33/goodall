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
from goodall.runner import OutputMode
from goodall.terminal import Terminal

# The command the account check runs, by its full path (see goodall.program_checks).
ACCOUNT_GROUPS_COMMAND = ["/usr/bin/id", "-Gn"]

# The groups a standard macOS user account belongs to.
STANDARD_ACCOUNT_GROUPS = "staff everyone localaccounts _appserverusr com.apple.sharepoint.group.1\n"

# The command that asks 1Password's op whether it is signed in. Its exit code
# is the whole answer; what it prints (the account's email address) is
# discarded (see goodall.secrets_manager).
OP_WHOAMI = ["op", "whoami"]

# The commands that list 1Password's vaults, and the items in the one vault
# the env file refers to. Their answers hold names and ids, never a secret
# value (see goodall.secrets_manager).
OP_VAULT_LIST = ["op", "vault", "list", "--format", "json"]
EVAL_VAULT_ID = "q2rv4ffgbye6f3nnzcfwh3ab4m"
OP_ITEM_LIST_EVAL = ["op", "item", "list", "--vault", EVAL_VAULT_ID, "--format", "json"]

# Shaped like op's answers: more keys than goodall uses, including
# additional_information, which for a login item is its username. None of it
# may ever be shown.
VAULT_LIST_ANSWER = json.dumps(
    [
        {"id": EVAL_VAULT_ID, "name": "Eval", "content_version": 12},
        {"id": "x7lmhq5hmp3bt5fgz2vqzd4z3e", "name": "Private", "content_version": 40},
    ]
)
ITEM_LIST_ANSWER = json.dumps(
    [
        {
            "id": "i3cdwwrnsdxk2eqbjvtbdrhs2y",
            "title": "anthropic-engineering",
            "category": "API_CREDENTIAL",
            "vault": {"id": EVAL_VAULT_ID, "name": "Eval"},
            "additional_information": "researcher-only-detail-0001",
        },
        {
            "id": "pd4yoyf7rnx2y4gu7lccc6yvfq",
            "title": "Anthropic Research",
            "category": "API_CREDENTIAL",
            "vault": {"id": EVAL_VAULT_ID, "name": "Eval"},
        },
        {
            "id": "ux7zkl4i5d3wvnjsn5n2l2ebt4",
            "title": "anthropic-admin",
            "category": "API_CREDENTIAL",
            "vault": {"id": EVAL_VAULT_ID, "name": "Eval"},
        },
    ]
)


def healthy_runner() -> FakeRunner:
    """A fake runner for a Mac where every program is installed and the account is a standard one.

    git is real (FakeRunner's default), so the record repo check asks real
    git about a real folder.
    """
    runner = FakeRunner(installed_programs=["git", "gh", "op"])
    runner.answer(ACCOUNT_GROUPS_COMMAND, standard_output=STANDARD_ACCOUNT_GROUPS)
    runner.answer(OP_WHOAMI)
    runner.answer(OP_VAULT_LIST, standard_output=VAULT_LIST_ANSWER)
    runner.answer(OP_ITEM_LIST_EVAL, standard_output=ITEM_LIST_ANSWER)
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


# The agreed shape of an env file (goodall-xes.3): a comment, a blank line,
# a quoted reference with a section, and unquoted references.
VALID_ENV_FILE_TEXT = """\
# Secret references for this eval account. Values are never secrets.

ANTHROPIC_API_KEY_ENGINEERING="op://Eval/anthropic-engineering/credential"
ANTHROPIC_API_KEY_RESEARCH='op://Eval/anthropic research/API keys/credential'
ANTHROPIC_ADMIN_KEY=op://Eval/anthropic-admin/credential
"""

# The agreed starting content of a shared env file (goodall-yjt.9).
VALID_SHARED_ENV_FILE_TEXT = """\
# Non-secret settings for every contributor's launched programs.
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_LOGS_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
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

    @property
    def env_file(self) -> Path:
        return self.config_directory / ".env"

    @property
    def shared_env_file(self) -> Path:
        return self.record_repo / ".env.shared"

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
    account.env_file.write_text(VALID_ENV_FILE_TEXT)
    # Readable by this account alone, as an env file should be.
    account.env_file.chmod(0o600)
    account.shared_env_file.write_text(VALID_SHARED_ENV_FILE_TEXT)
    # The record repo is a real git repository, because real git checks it.
    subprocess.run(["git", "init", "--quiet", str(account.record_repo)], check=True, capture_output=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(account.config_directory))
    # Doctor must not depend on, or write to, the folder it is run from.
    monkeypatch.chdir(tmp_path)
    return account


EXPECTED_HEALTHY_SCREEN = """\
Settings
  pass        Local settings are found
  pass        Local settings are valid
  pass        The record repo folder exists
  pass        The log directory can be written to
  pass        The eval config is found and valid
  pass        This eval account's contributor is in the roster
  pass        The main workstream is one of the eval's workstreams

Tools
  pass        git is installed
  pass        gh is installed
  pass        The secrets manager's command is installed

Eval account
  pass        This macOS user account is a standard account, not an
              administrator

Record repo
  pass        The record repo folder is a git repository

Secrets
  pass        The env file holds only secret references
  pass        Only this macOS user account can read the env file
  pass        The shared env file holds no secrets
  pass        No variable is set in both the env file and the shared env file
  pass        The secrets manager is signed in
  pass        Each secret reference points at an item that exists
  unverified  Each secret reference names a field that exists
              Goodall checked that each item exists, but not the field each
              reference names, because 1Password cannot tell goodall whether a
              field exists without also handing over the field's value, the
              secret itself, so goodall does not ask. Not checked:
              ANTHROPIC_API_KEY_ENGINEERING (line 3), ANTHROPIC_API_KEY_RESEARCH
              (line 4), ANTHROPIC_ADMIN_KEY (line 5).
              Next: Nothing, if you copied each reference with Copy Secret
              Reference in 1Password and have not renamed the field since.
              Otherwise, copy each one that way and paste it over its value in
              the env file.

18 passed, 0 warned, 0 failed, 0 skipped, 1 unverified
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
  FAIL        Local settings are found
              No local settings were found. Goodall looked for
              /nonexistent-goodall-test/goodall-config/local-settings.toml, in
              the folder that GOODALL_CONFIG_DIR names.
              Next: Run goodall onboard to set up this eval account, or unset
              GOODALL_CONFIG_DIR if your local settings are in your home folder.

  skip        Local settings are valid
              Waiting for: Local settings are found
  skip        The record repo folder exists
              Waiting for: Local settings are found
  skip        The log directory can be written to
              Waiting for: Local settings are found
  skip        The eval config is found and valid
              Waiting for: Local settings are found
  skip        This eval account's contributor is in the roster
              Waiting for: Local settings are found
  skip        The main workstream is one of the eval's workstreams
              Waiting for: Local settings are found

Tools
  pass        git is installed
  pass        gh is installed
  skip        The secrets manager's command is installed
              Waiting for: Local settings are found

Eval account
  pass        This macOS user account is a standard account, not an
              administrator

Record repo
  skip        The record repo folder is a git repository
              Waiting for: Local settings are found

Secrets
  skip        The env file holds only secret references
              Waiting for: Local settings are found
  skip        Only this macOS user account can read the env file
              Waiting for: Local settings are found
  skip        The shared env file holds no secrets
              Waiting for: Local settings are found
  skip        No variable is set in both the env file and the shared env file
              Waiting for: Local settings are found
  skip        The secrets manager is signed in
              Waiting for: Local settings are found
  skip        Each secret reference points at an item that exists
              Waiting for: Local settings are found
  skip        Each secret reference names a field that exists
              Waiting for: Local settings are found

3 passed, 0 warned, 1 failed, 15 skipped, 0 unverified
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


def doctor_json(runner: FakeRunner | None = None) -> tuple[int, dict[str, object]]:
    """Run `goodall doctor --json`; return the exit code and the parsed document.

    Without a `runner`, goodall gets `healthy_runner()`.
    """
    exit_code, standard_output, standard_error = run_goodall(["doctor", "--json"], runner)
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
    "eval_account.standard_account",
    "record_repo.is_git_repository",
    "secrets.env_file_holds_only_references",
    # Shown under "Tools", but run once the env file is read, because the
    # secrets manager is needed only when the env file refers to a secret.
    "tools.secrets_manager_installed",
    "secrets.env_file_private",
    "secrets.shared_env_file_holds_no_secrets",
    "secrets.no_variable_in_both_env_files",
    "secrets.secrets_manager_signed_in",
    "secrets.referenced_items_exist",
    "secrets.referenced_fields_exist",
]


def test_healthy_setup_as_json_gives_the_same_results(healthy_account: EvalAccount) -> None:
    exit_code, document = doctor_json()

    assert exit_code == exit_codes.SUCCESS
    assert document["schema_version"] == 1
    assert document["summary"] == {"passed": 18, "warned": 0, "failed": 0, "skipped": 0, "unverified": 1}
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
        "note": None,
        "failures": [],
    }
    assert list(statuses(document)) == ALL_CHECK_IDS
    # The report shows each check's summary; the JSON carries the same one.
    # Joined into one line, because a long summary wraps in the report.
    healthy_report_text = " ".join(EXPECTED_HEALTHY_SCREEN.split())
    for check in checks:
        assert check["summary"] in healthy_report_text


def test_missing_local_settings_as_json_names_what_each_skip_waited_for(settings_missing: None) -> None:
    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert document["summary"] == {"passed": 3, "warned": 0, "failed": 1, "skipped": 15, "unverified": 0}
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
    assert "  pass        Local settings are found\n" in standard_output
    assert "  FAIL        Local settings are valid\n" in standard_output
    # Both problems are shown, each with its own next step.
    assert "have no contributor" in standard_output
    assert "favourite_colour" in standard_output
    assert standard_output.count("        Next: ") == 2
    assert "Waiting for: Local settings are valid" in standard_output
    assert standard_output.endswith("4 passed, 0 warned, 1 failed, 14 skipped, 0 unverified\n")


def test_relative_settings_folder_override_fails_the_found_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOODALL_CONFIG_DIR", "relative/goodall-config")
    monkeypatch.chdir(tmp_path)

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert statuses(document)["settings.local_settings_found"] == "fail"


@pytest.mark.parametrize("extra_arguments", [[], ["--json"]])
def test_relative_settings_folder_override_is_never_repeated_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_arguments: list[str]
) -> None:
    # The value may be a key pasted into the wrong variable (ADR 0006).
    monkeypatch.setenv("GOODALL_CONFIG_DIR", "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC")
    monkeypatch.chdir(tmp_path)

    exit_code, standard_output, standard_error = run_goodall(["doctor", *extra_arguments])

    assert exit_code == exit_codes.ERROR
    assert "SYNTHETIC" not in standard_output + standard_error
    message = " ".join((standard_output + standard_error).split())
    assert "full path starting with /" in message
    assert "for example /Users/you/goodall-config" in message


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
        "secrets.env_file_holds_only_references": "pass",
        "secrets.env_file_private": "pass",
        "secrets.shared_env_file_holds_no_secrets": "skip",
        "secrets.no_variable_in_both_env_files": "skip",
        "secrets.secrets_manager_signed_in": "pass",
        "secrets.referenced_items_exist": "pass",
        "secrets.referenced_fields_exist": "unverified",
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
    assert "  warn        The log directory can be written to\n              Not recording:" in standard_output
    assert "        Next: If the log directory is on an external drive, connect it." in standard_output
    assert standard_output.endswith("17 passed, 1 warned, 0 failed, 0 skipped, 1 unverified\n")

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
    assert document["summary"] == {"passed": 16, "warned": 0, "failed": 2, "skipped": 0, "unverified": 1}
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
        "paste a key into the env file",
        "let other accounts read the env file",
        "put a secret in the shared env file",
        "remove the shared env file",
        "set a variable in both env files",
        "refer to an item that does not exist",
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
    elif break_setup == "paste a key into the env file":
        healthy_account.env_file.write_text("ANTHROPIC_API_KEY=sk-ant-api03-not-a-reference\nnot a line\n")
    elif break_setup == "let other accounts read the env file":
        healthy_account.env_file.chmod(0o644)
    elif break_setup == "put a secret in the shared env file":
        healthy_account.shared_env_file.write_text(
            "GITHUB_TOKEN=abc\nID=op://Eval/item/field\nX=AKIA0123456789ABCDEFGH\n"
        )
    elif break_setup == "remove the shared env file":
        healthy_account.shared_env_file.unlink()
    elif break_setup == "set a variable in both env files":
        healthy_account.shared_env_file.write_text("DEFAULT_MODEL=opus\n")
        healthy_account.env_file.write_text("DEFAULT_MODEL=op://Eval/item/field\n")
    elif break_setup == "refer to an item that does not exist":
        healthy_account.env_file.write_text("ANTHROPIC_API_KEY=op://Eval/nothing-here/credential\n")
    else:
        healthy_account.eval_config_file.write_text(VALID_EVAL_CONFIG_TEXT.replace("ada-lovelace", "grace-hopper"))

    text_exit_code, standard_output, _ = run_goodall(["doctor"])
    json_exit_code, document = doctor_json()

    assert text_exit_code == json_exit_code
    checks = document["checks"]
    assert isinstance(checks, list)
    for check in checks:
        if check["status"] in ["fail", "warn", "unverified"]:
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
    # And both env files broken, with an env file other accounts can read
    # (goodall must not chmod it).
    healthy_account.env_file.write_text(f"ANTHROPIC_API_KEY={PASTED_KEY}\n")
    healthy_account.env_file.chmod(0o644)
    healthy_account.shared_env_file.write_text("GITHUB_TOKEN=abc\n")
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
    assert document["summary"] == {"passed": 17, "warned": 0, "failed": 1, "skipped": 0, "unverified": 1}
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
    assert document["summary"] == {"passed": 16, "warned": 0, "failed": 1, "skipped": 1, "unverified": 1}
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
    assert document["summary"] == {"passed": 15, "warned": 0, "failed": 1, "skipped": 3, "unverified": 0}
    secrets_manager_check = check_document(document, "tools.secrets_manager_installed")
    assert secrets_manager_check["status"] == "fail"
    assert isinstance(secrets_manager_check["detail"], str)
    assert "op, 1Password's command-line tool," in secrets_manager_check["detail"]
    assert isinstance(secrets_manager_check["next_step"], str)
    assert "https://developer.1password.com/docs/cli/get-started/" in secrets_manager_check["next_step"]


@pytest.mark.parametrize("env_file_text", [None, "# No secrets in this eval.\n"])
def test_an_eval_with_no_secret_references_passes_doctor_without_op_installed(
    healthy_account: EvalAccount, env_file_text: str | None
) -> None:
    # ADR 0006: a secrets manager is required only when a secret exists.
    if env_file_text is None:
        healthy_account.env_file.unlink()
    else:
        healthy_account.env_file.write_text(env_file_text)
    runner = healthy_runner()
    runner.installed_programs.remove("op")

    exit_code, document = doctor_json(runner)

    assert exit_code == exit_codes.SUCCESS
    secrets_manager_check = check_document(document, "tools.secrets_manager_installed")
    assert secrets_manager_check["status"] == "pass"
    assert secrets_manager_check["note"] == ("This eval uses no secret references, so no secrets manager is needed.")


# The groups of an administrator on macOS: the same as a standard account's,
# plus admin and the groups that come with it.
ADMINISTRATOR_ACCOUNT_GROUPS = "staff everyone localaccounts _appserverusr admin _appserveradm _lpadmin\n"


def test_an_administrator_account_fails_with_why_a_standard_account_is_required(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.answer(ACCOUNT_GROUPS_COMMAND, standard_output=ADMINISTRATOR_ACCOUNT_GROUPS)

    exit_code, standard_output, _ = run_goodall(["doctor"], runner)

    assert exit_code == exit_codes.ERROR
    assert "  FAIL        This macOS user account is a standard account, not an\n" in standard_output
    # Joined into one line, so a phrase that wraps still matches.
    report_text = " ".join(standard_output.split())
    assert "is an administrator (it is in the admin group)" in report_text
    # The reason, so a researcher does not wave it away as fussiness.
    assert "cannot reach other accounts' files or install software" in report_text
    assert "safe to hand to an agent" in report_text
    assert standard_output.endswith("17 passed, 0 warned, 1 failed, 0 skipped, 1 unverified\n")


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


def test_a_record_repo_folder_that_is_not_a_git_repository_fails(healthy_account: EvalAccount) -> None:
    # Real git, asked about a real folder that holds the eval config but
    # was never made a repository (copied instead of cloned, say).
    shutil.rmtree(healthy_account.record_repo / ".git")

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    assert document["summary"] == {"passed": 17, "warned": 0, "failed": 1, "skipped": 0, "unverified": 1}
    git_repository_check = check_document(document, "record_repo.is_git_repository")
    assert git_repository_check["status"] == "fail"
    assert isinstance(git_repository_check["detail"], str)
    assert "~/evals/goodall-pilot-records is not a git repository" in git_repository_check["detail"]
    # git's own words, so an unusual cause (such as a folder owned by
    # another account) is not hidden behind goodall's summary.
    assert "not a git repository" in git_repository_check["detail"].split("git said: ")[1]
    assert isinstance(git_repository_check["next_step"], str)
    assert "git clone" in git_repository_check["next_step"]


def test_a_record_repo_folder_inside_another_repository_fails(healthy_account: EvalAccount) -> None:
    # The home folder is a repository (some people keep theirs in git), and
    # the record repo folder is only a folder inside it. Records committed
    # there would go to the wrong repository.
    shutil.rmtree(healthy_account.record_repo / ".git")
    subprocess.run(["git", "init", "--quiet", str(healthy_account.home_folder)], check=True, capture_output=True)

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    git_repository_check = check_document(json.loads(standard_output), "record_repo.is_git_repository")
    assert git_repository_check["status"] == "fail"
    assert isinstance(git_repository_check["detail"], str)
    assert "is inside the git repository" in git_repository_check["detail"]
    assert healthy_account.home_folder.name in git_repository_check["detail"]


def test_a_record_repo_check_whose_git_never_finishes_says_so(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    # git is scripted here rather than real, so it can be made to hang.
    runner.real_programs.remove("git")
    runner.answer_timed_out(["git", "rev-parse", "--show-toplevel"])

    _, standard_output, _ = run_goodall(["doctor", "--json"], runner)

    git_repository_check = check_document(json.loads(standard_output), "record_repo.is_git_repository")
    assert git_repository_check["status"] == "fail"
    assert isinstance(git_repository_check["detail"], str)
    assert "could not ask git" in git_repository_check["detail"]
    assert "`git rev-parse --show-toplevel` timed out after 10 s" in git_repository_check["detail"]


def test_a_command_the_fake_runner_was_not_told_about_fails_the_test_and_names_it(
    healthy_account: EvalAccount,
) -> None:
    # Every program is installed, but the account check's command was
    # never scripted: the test must fail loudly rather than run it.
    runner = FakeRunner(installed_programs=["git", "gh", "op"])

    with pytest.raises(AssertionError, match="`/usr/bin/id -Gn`"):
        run_goodall(["doctor"], runner)


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
    assert document["summary"] == {"passed": 17, "warned": 0, "failed": 1, "skipped": 0, "unverified": 1}


# A recognisable fake key. If any part of it shows up in goodall's output,
# a real pasted key would have too.
PASTED_KEY = "sk-ant-api03-RECOGNISABLEFAKEVALUE-0123456789abcdef"


def test_a_raw_key_pasted_into_the_env_file_fails_names_the_line_and_is_never_shown(
    healthy_account: EvalAccount,
) -> None:
    healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + f'ANTHROPIC_API_KEY_GTM="{PASTED_KEY}"\n')

    exit_code, standard_output, standard_error = run_goodall(["doctor"])
    json_exit_code, json_output, json_error = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    assert json_exit_code == exit_codes.ERROR
    for printed in [standard_output, standard_error, json_output, json_error]:
        assert "RECOGNISABLEFAKEVALUE" not in printed
    assert "  FAIL        The env file holds only secret references\n" in standard_output
    report_text = " ".join(standard_output.split())
    assert "ANTHROPIC_API_KEY_GTM, on line 6 of" in report_text
    assert "paste the secret reference" in report_text
    assert "change the key" in report_text
    assert failure_kinds(json.loads(json_output), "secrets.env_file_holds_only_references") == [
        "env_file_value_not_a_reference"
    ]


# A synthetic base64 token. Base64 is letters and digits, so a pasted token
# reads as a variable name, and its = padding as the = after the name.
PASTED_BASE64_TOKEN = "SYNTHETICsyntheticSYNTHETIC0123456789x9"


@pytest.mark.parametrize("padding", ["=", "=="])
def test_a_pasted_base64_token_is_never_shown_and_neither_env_file_passes_it(
    healthy_account: EvalAccount, padding: str
) -> None:
    pasted_line = f"{PASTED_BASE64_TOKEN}{padding}\n"
    healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + pasted_line)
    healthy_account.shared_env_file.write_text(VALID_SHARED_ENV_FILE_TEXT + pasted_line)

    exit_code, standard_output, standard_error = run_goodall(["doctor"])
    _, json_output, json_error = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    for printed in [standard_output, standard_error, json_output, json_error]:
        assert "SYNTHETICsynthetic" not in printed
    document = json.loads(json_output)
    assert statuses(document)["secrets.env_file_holds_only_references"] == "fail"
    assert statuses(document)["secrets.shared_env_file_holds_no_secrets"] != "pass"
    env_file_check = check_document(document, "secrets.env_file_holds_only_references")
    assert isinstance(env_file_check["detail"], str)
    assert "line 6" in " ".join(env_file_check["detail"].split()).lower()


def failure_kinds(document: dict[str, object], check_id: str) -> list[str]:
    """The kind of each failure of one check in a `goodall doctor --json` document."""
    failures = check_document(document, check_id)["failures"]
    assert isinstance(failures, list)
    kinds = []
    for failure in failures:
        kinds.append(failure["kind"])
    return kinds


def test_a_missing_env_file_passes_with_a_note_because_an_eval_may_use_no_secrets(
    healthy_account: EvalAccount,
) -> None:
    healthy_account.env_file.unlink()

    exit_code, standard_output, _ = run_goodall(["doctor"])
    json_exit_code, json_output, _ = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.SUCCESS
    assert json_exit_code == exit_codes.SUCCESS
    report_text = " ".join(standard_output.split())
    assert "pass The env file holds only secret references There is no env file at " in report_text
    assert "That is fine if this eval uses no secrets." in report_text
    env_file_check = check_document(json.loads(json_output), "secrets.env_file_holds_only_references")
    assert env_file_check["status"] == "pass"
    assert isinstance(env_file_check["note"], str)
    assert str(healthy_account.env_file) in env_file_check["note"]


def test_a_line_in_the_env_file_that_is_not_name_equals_value_is_reported_with_its_line_number(
    healthy_account: EvalAccount,
) -> None:
    # A key pasted on a line of its own, with no name: still never shown.
    healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + f"{PASTED_KEY}\n")

    exit_code, standard_output, standard_error = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    assert "RECOGNISABLEFAKEVALUE" not in standard_output + standard_error
    document = json.loads(standard_output)
    assert failure_kinds(document, "secrets.env_file_holds_only_references") == ["env_file_line_unreadable"]
    detail = check_document(document, "secrets.env_file_holds_only_references")["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("Line 6 of the env file at ")
    assert "is not in the form NAME=value" in detail


@pytest.mark.parametrize("file_name", ["env_file", "shared_env_file"])
def test_an_empty_value_in_either_env_file_is_refused_with_the_placeholder_advice(
    healthy_account: EvalAccount, file_name: str
) -> None:
    env_file = getattr(healthy_account, file_name)
    env_file.write_text(env_file.read_text() + "SYNTHETICsyntheticSYNTHETIC0123456789x9=\n")

    exit_code, standard_output, standard_error = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    assert "SYNTHETICsynthetic" not in standard_output + standard_error
    message = " ".join(standard_output.split())
    assert "give the variable a value" in message
    assert "any short word will do" in message


def test_an_unreadable_env_file_fails_saying_why(healthy_account: EvalAccount) -> None:
    healthy_account.env_file.unlink()
    healthy_account.env_file.mkdir()

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    assert failure_kinds(document, "secrets.env_file_holds_only_references") == ["env_file_unreadable"]


@pytest.mark.parametrize("permissions", [0o644, 0o640, 0o604])
def test_an_env_file_other_accounts_can_read_is_a_warning_with_the_command_to_fix_it(
    healthy_account: EvalAccount, permissions: int
) -> None:
    healthy_account.env_file.chmod(permissions)

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"])

    # References are not secrets, so goodall still works; but they name
    # vaults and items, which other accounts on the Mac have no need to see.
    assert exit_code == exit_codes.SUCCESS
    document = json.loads(standard_output)
    private_check = check_document(document, "secrets.env_file_private")
    assert private_check["status"] == "warn"
    assert failure_kinds(document, "secrets.env_file_private") == ["env_file_readable_by_others"]
    assert private_check["next_step"] == (
        f"Run chmod 600 {healthy_account.env_file} in Terminal, then run goodall doctor again."
    )


def test_the_permissions_check_passes_when_there_is_no_env_file(healthy_account: EvalAccount) -> None:
    healthy_account.env_file.unlink()

    _, standard_output, _ = run_goodall(["doctor", "--json"])

    assert check_document(json.loads(standard_output), "secrets.env_file_private")["status"] == "pass"


def test_a_secret_looking_name_in_the_shared_env_file_fails_saying_to_move_it_to_the_env_file(
    healthy_account: EvalAccount,
) -> None:
    healthy_account.shared_env_file.write_text(
        VALID_SHARED_ENV_FILE_TEXT + f"OPENAI_API_KEY={PASTED_KEY}\ngithub_token=abc\nNOT_A_SECRET_KEYBOARD=uk\n"
    )

    exit_code, standard_output, standard_error = run_goodall(["doctor"])
    _, json_output, _ = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    for printed in [standard_output, standard_error, json_output]:
        assert "RECOGNISABLEFAKEVALUE" not in printed
    assert "  FAIL        The shared env file holds no secrets\n" in standard_output
    document = json.loads(json_output)
    assert failure_kinds(document, "secrets.shared_env_file_holds_no_secrets") == [
        "shared_env_file_secret_name",
        "shared_env_file_secret_name",
    ]
    shared_check = check_document(document, "secrets.shared_env_file_holds_no_secrets")
    assert isinstance(shared_check["detail"], str)
    assert "OPENAI_API_KEY, on line 6" in shared_check["detail"]
    assert "github_token, on line 7" in shared_check["detail"]
    assert "NOT_A_SECRET_KEYBOARD" not in shared_check["detail"]
    assert isinstance(shared_check["next_step"], str)
    assert "move this to the env file as a reference" in shared_check["next_step"]


def test_a_missing_shared_env_file_is_a_warning_with_a_next_step(healthy_account: EvalAccount) -> None:
    healthy_account.shared_env_file.unlink()

    exit_code, standard_output, _ = run_goodall(["doctor"])

    assert exit_code == exit_codes.SUCCESS
    assert "  warn        The shared env file holds no secrets\n" in standard_output
    report_text = " ".join(standard_output.split())
    assert f"There is no shared env file at {healthy_account.shared_env_file}." in report_text
    assert "Next: Run git pull in" in report_text


def test_a_secret_reference_inside_a_shared_env_file_value_fails_like_one_on_its_own(
    healthy_account: EvalAccount,
) -> None:
    # The same rule as the eval config's: a reference anywhere in a value counts.
    healthy_account.shared_env_file.write_text(
        VALID_SHARED_ENV_FILE_TEXT + 'OTEL_EXPORTER_OTLP_HEADERS="Authorization=op://RECOGNISABLEVAULT/otel/x"\n'
    )

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert failure_kinds(document, "secrets.shared_env_file_holds_no_secrets") == ["shared_env_file_secret_reference"]


def test_a_secret_reference_in_the_shared_env_file_fails_because_it_would_not_be_resolved(
    healthy_account: EvalAccount,
) -> None:
    healthy_account.shared_env_file.write_text(
        VALID_SHARED_ENV_FILE_TEXT + 'OTEL_EXPORTER_OTLP_HEADERS="op://RECOGNISABLEVAULT/otel/credential"\n'
    )

    exit_code, standard_output, standard_error = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    assert "RECOGNISABLEVAULT" not in standard_output + standard_error
    document = json.loads(standard_output)
    assert failure_kinds(document, "secrets.shared_env_file_holds_no_secrets") == ["shared_env_file_secret_reference"]
    next_step = check_document(document, "secrets.shared_env_file_holds_no_secrets")["next_step"]
    assert isinstance(next_step, str)
    assert "move this to the env file as a reference" in next_step


@pytest.mark.parametrize(
    "line",
    [
        f"SOME_SETTING={PASTED_KEY}",
        'OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer RECOGNISABLEFAKEVALUE0123456789abcdef"',
        "AWS_ID=AKIARECOGNISABLEFAKEVALUE7",
    ],
)
def test_a_value_in_the_shared_env_file_that_looks_like_a_random_token_is_a_warning(
    healthy_account: EvalAccount, line: str
) -> None:
    healthy_account.shared_env_file.write_text(VALID_SHARED_ENV_FILE_TEXT + line + "\n")

    exit_code, standard_output, standard_error = run_goodall(["doctor", "--json"])

    # Only a guess, so it warns rather than blocking: no list of safe
    # variables exists to tell a token from a long identifier.
    assert exit_code == exit_codes.SUCCESS
    assert "RECOGNISABLEFAKEVALUE" not in standard_output + standard_error
    document = json.loads(standard_output)
    assert check_document(document, "secrets.shared_env_file_holds_no_secrets")["status"] == "warn"
    assert failure_kinds(document, "secrets.shared_env_file_holds_no_secrets") == ["shared_env_file_value_looks_secret"]


@pytest.mark.parametrize(
    "line",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318",
        "OTEL_RESOURCE_ATTRIBUTES=service.name=goodall-pilot,deployment.environment=eval-2026",
        "COLLECTOR_HOST=otel-collector-01.internal.example.com",
        'GREETING="a sentence with ordinary words in it"',
    ],
)
def test_ordinary_values_in_the_shared_env_file_pass(healthy_account: EvalAccount, line: str) -> None:
    healthy_account.shared_env_file.write_text(VALID_SHARED_ENV_FILE_TEXT + line + "\n")

    _, standard_output, _ = run_goodall(["doctor", "--json"])

    document = json.loads(standard_output)
    assert check_document(document, "secrets.shared_env_file_holds_no_secrets")["status"] == "pass"


def test_a_variable_in_both_env_files_fails_naming_it(healthy_account: EvalAccount) -> None:
    healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + 'OTEL_LOGS_EXPORTER="op://Eval/otel/exporter"\n')

    exit_code, standard_output, _ = run_goodall(["doctor", "--json"])

    assert exit_code == exit_codes.ERROR
    document = json.loads(standard_output)
    both_check = check_document(document, "secrets.no_variable_in_both_env_files")
    assert both_check["status"] == "fail"
    assert failure_kinds(document, "secrets.no_variable_in_both_env_files") == ["variable_in_both_env_files"]
    assert isinstance(both_check["detail"], str)
    assert "OTEL_LOGS_EXPORTER is set on line 6 of the env file" in both_check["detail"]
    assert "and on line 3 of the shared env file" in both_check["detail"]


def test_the_check_for_a_variable_in_both_files_waits_for_a_broken_env_file(healthy_account: EvalAccount) -> None:
    healthy_account.env_file.write_text(f"OTEL_LOGS_EXPORTER={PASTED_KEY}\n")

    _, standard_output, _ = run_goodall(["doctor", "--json"])

    both_check = check_document(json.loads(standard_output), "secrets.no_variable_in_both_env_files")
    assert both_check["status"] == "skip"
    assert both_check["waiting_for"] == "secrets.env_file_holds_only_references"


def test_a_signed_out_secrets_manager_is_one_failure_with_the_sign_in_step(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.answer(OP_WHOAMI, exit_code=1, standard_error="[ERROR] account is not signed in\n")

    exit_code, standard_output, _ = run_goodall(["doctor"], runner)

    assert exit_code == exit_codes.ERROR
    assert "  FAIL        The secrets manager is signed in\n" in standard_output
    report_text = " ".join(standard_output.split())
    assert "1Password is not signed in for this macOS user account: `op whoami` exited with code 1." in report_text
    assert "Next: Open the 1Password app and unlock it." in report_text
    assert "op signin" in report_text
    # What op printed was discarded unread, so it cannot be repeated.
    assert "account is not signed in" not in report_text


def test_a_secrets_manager_that_never_answers_fails_within_its_timeout(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.answer_timed_out(OP_WHOAMI)

    exit_code, document = doctor_json(runner)

    assert exit_code == exit_codes.ERROR
    signed_in_check = check_document(document, "secrets.secrets_manager_signed_in")
    assert signed_in_check["status"] == "fail"
    assert failure_kinds(document, "secrets.secrets_manager_signed_in") == ["secrets_manager_did_not_answer"]
    assert isinstance(signed_in_check["detail"], str)
    assert "`op whoami` timed out after 10 s" in signed_in_check["detail"]


def test_an_env_file_with_no_references_does_not_ask_the_secrets_manager_anything(
    healthy_account: EvalAccount,
) -> None:
    # An eval with no secrets needs no secrets manager (ADR 0006), so a
    # signed-out one must not fail it.
    healthy_account.env_file.unlink()
    runner = healthy_runner()
    runner.answer(OP_WHOAMI, exit_code=1)

    exit_code, document = doctor_json(runner)

    assert exit_code == exit_codes.SUCCESS
    signed_in_check = check_document(document, "secrets.secrets_manager_signed_in")
    assert signed_in_check["status"] == "pass"
    assert signed_in_check["note"] == (
        "The env file refers to no secrets, so goodall did not ask 1Password whether it is signed in."
    )
    for command in runner.calls:
        assert command.arguments[0] != "op"


def test_a_reference_to_an_item_that_does_not_exist_fails_naming_the_variable_and_line_but_not_the_reference(
    healthy_account: EvalAccount,
) -> None:
    missing_reference = "op://Eval/anthropic-gtm-renamed/credential"
    healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + f"ANTHROPIC_API_KEY_GTM={missing_reference}\n")

    exit_code, standard_output, _ = run_goodall(["doctor"])
    json_exit_code, document = doctor_json()

    assert exit_code == json_exit_code == exit_codes.ERROR
    assert "  FAIL        Each secret reference points at an item that exists\n" in standard_output
    report_text = " ".join(standard_output.split())
    assert "ANTHROPIC_API_KEY_GTM, on line 6 of the env file" in report_text
    assert failure_kinds(document, "secrets.referenced_items_exist") == ["secret_reference_item_missing"]
    # A reference names a vault and an item, so neither the reference nor
    # its parts are shown; nor is anything op said about the items it has.
    everything_shown = standard_output + json.dumps(document)
    for not_to_be_shown in [missing_reference, "anthropic-gtm-renamed", "op://", EVAL_VAULT_ID, "researcher-only"]:
        assert not_to_be_shown not in everything_shown


def test_references_whose_items_could_not_be_listed_in_time_are_unverified_not_passed_or_failed(
    healthy_account: EvalAccount,
) -> None:
    runner = healthy_runner()
    runner.answer_timed_out(OP_ITEM_LIST_EVAL)

    exit_code, standard_output, _ = run_goodall(["doctor"], runner)
    json_exit_code, document = doctor_json(runner)

    # Not knowing is not a failure, so doctor still exits 0, but the report
    # says plainly that these were not checked.
    assert exit_code == json_exit_code == exit_codes.SUCCESS
    assert "  unverified  Each secret reference points at an item that exists\n" in standard_output
    items_check = check_document(document, "secrets.referenced_items_exist")
    assert items_check["status"] == "unverified"
    assert failure_kinds(document, "secrets.referenced_items_exist") == ["secret_reference_not_verified"] * 3
    report_text = " ".join(standard_output.split())
    assert "ANTHROPIC_API_KEY_ENGINEERING (line 3)" in report_text
    assert "ANTHROPIC_ADMIN_KEY (line 5)" in report_text
    assert "`op item list` did not answer within 10 s" in report_text
    assert EVAL_VAULT_ID not in standard_output + json.dumps(document)


def test_a_reference_whose_name_two_items_share_is_unverified_and_says_to_use_the_item_id(
    healthy_account: EvalAccount,
) -> None:
    runner = healthy_runner()
    items = json.loads(ITEM_LIST_ANSWER)
    items.append({"id": "zz7zkl4i5d3wvnjsn5n2l2ebt4", "title": "Anthropic-Admin", "vault": {"id": EVAL_VAULT_ID}})
    runner.answer(OP_ITEM_LIST_EVAL, standard_output=json.dumps(items))

    _, document = doctor_json(runner)

    items_check = check_document(document, "secrets.referenced_items_exist")
    assert items_check["status"] == "unverified"
    assert isinstance(items_check["detail"], str)
    assert "ANTHROPIC_ADMIN_KEY (line 5)" in items_check["detail"]
    assert "more than one item in its vault has the name the reference uses" in items_check["detail"]
    assert isinstance(items_check["next_step"], str)
    assert "item's id" in items_check["next_step"]


def test_the_field_a_reference_names_is_reported_unverified_because_checking_it_would_read_the_secret(
    healthy_account: EvalAccount,
) -> None:
    exit_code, document = doctor_json()

    assert exit_code == exit_codes.SUCCESS
    field_check = check_document(document, "secrets.referenced_fields_exist")
    assert field_check["status"] == "unverified"
    assert failure_kinds(document, "secrets.referenced_fields_exist") == ["secret_reference_field_not_verified"]
    assert isinstance(field_check["detail"], str)
    for variable in [
        "ANTHROPIC_API_KEY_ENGINEERING (line 3)",
        "ANTHROPIC_API_KEY_RESEARCH (line 4)",
        "ANTHROPIC_ADMIN_KEY (line 5)",
    ]:
        assert variable in field_check["detail"]
    assert "credential" not in json.dumps(field_check)
    # The items themselves were checked, and do exist.
    assert check_document(document, "secrets.referenced_items_exist")["status"] == "pass"


def test_an_env_file_with_no_references_has_no_field_to_leave_unverified(healthy_account: EvalAccount) -> None:
    healthy_account.env_file.write_text("# No secrets in this eval.\n")

    exit_code, document = doctor_json()

    assert exit_code == exit_codes.SUCCESS
    assert check_document(document, "secrets.referenced_fields_exist")["status"] == "pass"
    summary = document["summary"]
    assert isinstance(summary, dict)
    assert summary["unverified"] == 0


def test_signed_out_skips_the_item_and_field_checks_naming_the_sign_in_check(healthy_account: EvalAccount) -> None:
    runner = healthy_runner()
    runner.answer(OP_WHOAMI, exit_code=1)

    exit_code, document = doctor_json(runner)

    assert exit_code == exit_codes.ERROR
    for check_id in ["secrets.referenced_items_exist", "secrets.referenced_fields_exist"]:
        check = check_document(document, check_id)
        assert check["status"] == "skip"
        assert check["waiting_for"] == "secrets.secrets_manager_signed_in"
    # Only the sign-in question was asked.
    op_commands = []
    for command in runner.calls:
        if command.arguments[0] == "op":
            op_commands.append(command.arguments)
    assert op_commands == [OP_WHOAMI]


def test_a_reference_to_a_vault_this_account_cannot_see_fails_naming_the_variable(
    healthy_account: EvalAccount,
) -> None:
    healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + "ANTHROPIC_API_KEY_GTM=op://Sales/anthropic/credential\n")

    exit_code, standard_output, _ = run_goodall(["doctor"])
    _, document = doctor_json()

    assert exit_code == exit_codes.ERROR
    assert failure_kinds(document, "secrets.referenced_items_exist") == ["secret_reference_vault_missing"]
    report_text = " ".join(standard_output.split())
    assert "ANTHROPIC_API_KEY_GTM, on line 6 of the env file" in report_text
    assert "names a vault this 1Password account cannot see" in report_text
    assert "Sales" not in standard_output + json.dumps(document)


def test_an_answer_from_op_that_goodall_cannot_read_leaves_every_reference_unverified(
    healthy_account: EvalAccount,
) -> None:
    runner = healthy_runner()
    runner.answer(OP_VAULT_LIST, standard_output="Eval  q2rv4ffgbye6f3nnzcfwh3ab4m\n")

    exit_code, document = doctor_json(runner)

    assert exit_code == exit_codes.SUCCESS
    assert check_document(document, "secrets.referenced_items_exist")["status"] == "unverified"
    assert failure_kinds(document, "secrets.referenced_items_exist") == ["secret_reference_not_verified"] * 3
    detail = check_document(document, "secrets.referenced_items_exist")["detail"]
    assert isinstance(detail, str)
    assert "goodall could not read the answer `op vault list` gave" in detail


# The only secrets manager commands whose output goodall may read, by the
# first three words: listings documented to name vaults and items and never
# to give a field's value. Every other secrets manager command must discard
# its output. `op item get`, `op read`, `op inject` and `op run` give values
# and must never be run by goodall doctor at all.
VALUE_FREE_OP_COMMANDS = [["op", "vault", "list"], ["op", "item", "list"]]
OP_COMMANDS_THAT_GIVE_VALUES = [["op", "item", "get"], ["op", "read"], ["op", "inject"], ["op", "run"]]


@pytest.mark.parametrize(
    "scenario",
    ["healthy", "signed out", "no answer", "item listing times out", "missing item", "no references"],
)
def test_every_secrets_manager_command_discards_its_output_or_is_a_value_free_listing(
    healthy_account: EvalAccount, scenario: str
) -> None:
    runner = healthy_runner()
    if scenario == "signed out":
        runner.answer(OP_WHOAMI, exit_code=1)
    elif scenario == "no answer":
        runner.answer_timed_out(OP_WHOAMI)
    elif scenario == "item listing times out":
        runner.answer_timed_out(OP_ITEM_LIST_EVAL)
    elif scenario == "missing item":
        healthy_account.env_file.write_text(VALID_ENV_FILE_TEXT + "X_KEY=op://Eval/nothing-here/credential\n")
    elif scenario == "no references":
        healthy_account.env_file.unlink()

    run_goodall(["doctor"], runner)
    run_goodall(["doctor", "--json"], runner)

    for command in runner.calls:
        if command.arguments[0] != "op":
            continue
        for value_giving_command in OP_COMMANDS_THAT_GIVE_VALUES:
            assert command.arguments[: len(value_giving_command)] != value_giving_command
        if command.output == OutputMode.DISCARD:
            continue
        assert command.arguments[:3] in VALUE_FREE_OP_COMMANDS, f"`{command.as_typed}` kept its output"


# Setups that make doctor report failures, warnings or unverified checks
# whose words come from every module that builds them, including the
# folder, local settings and eval config failures `goodall status` shares.
REPORTED_SETUPS = {
    "healthy": ("settings", "", ""),
    "record repo missing": ("settings", "~/evals/goodall-pilot-records", "~/evals/not-cloned-yet"),
    "log directory missing": ("settings", "LOG_DIRECTORY", "/Volumes/NotPluggedIn/goodall"),
    "settings too new": ("settings", "schema_version = 1", "schema_version = 99"),
    "eval config too new": ("eval_config", "schema_version = 1", "schema_version = 99"),
    "raw value in the env file": ("env", "end", "ANTHROPIC_API_KEY_GTM=not-a-reference\n"),
    "secret name in the shared env file": ("shared_env", "end", "OPENAI_API_KEY=x\n"),
    "token in the shared env file": ("shared_env", "end", f"SOME_SETTING={PASTED_BASE64_TOKEN}\n"),
    "variable in both env files": ("shared_env", "end", "ANTHROPIC_ADMIN_KEY=x\n"),
}


@pytest.mark.parametrize("setup", list(REPORTED_SETUPS))
def test_doctor_s_words_cite_no_decision_records_and_name_no_other_command_to_rerun(
    healthy_account: EvalAccount, setup: str
) -> None:
    # A researcher has not read goodall's ADRs, and a next step reused from
    # goodall status must not send someone running doctor to a different command.
    file_name, replace_from, replace_to = REPORTED_SETUPS[setup]
    if replace_from != "":
        put_secret_in_place(healthy_account, file_name, replace_from, replace_to)

    _, standard_output, standard_error = run_goodall(["doctor"])
    _, json_output, _ = run_goodall(["doctor", "--json"])

    for printed in [standard_output, standard_error, json_output]:
        assert "ADR" not in printed
        assert "goodall status" not in printed


# Every place in the files doctor reads where a person could paste a key by
# mistake. Each case is: the synthetic secret, which file it goes in, the
# text to replace in that file's valid form ("" puts the new text at the
# start of the file, and "end" at its end), and what to put there. No case
# may ever print its secret. `goodall status` has the same test for the two
# files it reads (tests/test_status.py).
SYNTHETIC_KEY = "sk-ant-api03-SYNTHETICsyntheticSYNTHETIC"
SYNTHETIC_HEX_TOKEN = "0123456789abcdef0123456789abcdef"
VALID_REFERENCE = "op://Eval/anthropic-admin/credential"

SECRET_POSITIONS = {
    "settings unknown key": (SYNTHETIC_KEY, "settings", "", f'{SYNTHETIC_KEY} = "x"\n'),
    "settings table declared twice": (SYNTHETIC_KEY, "settings", "end", f"[{SYNTHETIC_KEY}]\n[{SYNTHETIC_KEY}]\n"),
    "settings secrets_manager": (SYNTHETIC_KEY, "settings", '"1password"', f'"{SYNTHETIC_KEY}"'),
    "settings record_repo": (SYNTHETIC_KEY, "settings", '"~/evals/goodall-pilot-records"', f'"{SYNTHETIC_KEY}"'),
    "settings log_directory with a null character": (
        SYNTHETIC_KEY,
        "settings",
        "LOG_DIRECTORY",
        f"/{SYNTHETIC_KEY}\\u0000",
    ),
    "settings contributor shaped like a name": (
        SYNTHETIC_HEX_TOKEN,
        "settings",
        '"ada-lovelace"',
        f'"{SYNTHETIC_HEX_TOKEN}"',
    ),
    "eval config unknown key": (SYNTHETIC_KEY, "eval_config", "", f'{SYNTHETIC_KEY} = "x"\n'),
    "eval config key holding a reference": (
        SYNTHETIC_KEY,
        "eval_config",
        "[[scope]]",
        f'[extra]\n{SYNTHETIC_KEY} = "op://Eval/item/field"\n\n[[scope]]',
    ),
    "eval config unknown blocks": (
        SYNTHETIC_KEY,
        "eval_config",
        "[[scope]]",
        f'[[{SYNTHETIC_KEY}]]\nnote = "x@example.com"\n\n[[scope]]',
    ),
    "env file value": (SYNTHETIC_KEY, "env", "end", f'ANTHROPIC_API_KEY_GTM="{SYNTHETIC_KEY}"\n'),
    "env file line on its own": (SYNTHETIC_KEY, "env", "end", f"{SYNTHETIC_KEY}\n"),
    "env file base64 token": (PASTED_BASE64_TOKEN, "env", "end", f"{PASTED_BASE64_TOKEN}==\n"),
    "env file token name with a reference": (
        PASTED_BASE64_TOKEN,
        "env",
        "end",
        f"{PASTED_BASE64_TOKEN}={VALID_REFERENCE}\n",
    ),
    "shared env file base64 token": (PASTED_BASE64_TOKEN, "shared_env", "end", f"{PASTED_BASE64_TOKEN}==\n"),
    "shared env file value": (SYNTHETIC_KEY, "shared_env", "end", f"SOME_SETTING={SYNTHETIC_KEY}\n"),
    "shared env file secret name": (SYNTHETIC_KEY, "shared_env", "end", f"OPENAI_API_KEY={SYNTHETIC_KEY}\n"),
}


def put_secret_in_place(account: EvalAccount, file_name: str, replace_from: str, replace_to: str) -> None:
    """Put a pasted secret in one place in one of the healthy account's files."""
    files = {
        "settings": account.settings_file,
        "eval_config": account.eval_config_file,
        "env": account.env_file,
        "shared_env": account.shared_env_file,
    }
    file = files[file_name]
    valid_text = file.read_text()
    # The log directory's path differs in every test, so a case names it
    # with this placeholder.
    if replace_from == "LOG_DIRECTORY":
        replace_from = str(account.log_directory)
    if replace_from == "":
        broken_text = replace_to + valid_text
    elif replace_from == "end":
        broken_text = valid_text + replace_to
    else:
        assert replace_from in valid_text, f"{replace_from!r} is not in {file}"
        broken_text = valid_text.replace(replace_from, replace_to, 1)
    file.write_text(broken_text)


@pytest.mark.parametrize("position", list(SECRET_POSITIONS))
def test_a_secret_pasted_anywhere_doctor_reads_is_never_shown(healthy_account: EvalAccount, position: str) -> None:
    secret, file_name, replace_from, replace_to = SECRET_POSITIONS[position]
    put_secret_in_place(healthy_account, file_name, replace_from, replace_to)

    _, text_output, text_error = run_goodall(["doctor"])
    _, json_output, json_error = run_goodall(["doctor", "--json"])

    for printed in [text_output, text_error, json_output, json_error]:
        assert secret not in printed
    # Something is always reported about the place: a failure, a warning,
    # or (for a long name holding a well-formed reference) the unverified
    # field check that lists every variable.
    summary = json.loads(json_output)["summary"]
    assert summary["failed"] + summary["warned"] + summary["unverified"] > 0
