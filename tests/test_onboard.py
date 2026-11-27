"""Outside-in tests for `goodall onboard --answers <file>`, driven through `cli.main`.

Each test writes an answers file into a temporary folder, runs goodall the
way an unattended setup would, and checks only what a person or an agent
would observe: the exit code, the screen, the JSON document and the files on
disk. The conftest fixtures point HOME, GOODALL_CONFIG_DIR and
CLAUDE_CONFIG_DIR at empty temporary folders, so nothing here can reach the
real home folder, local settings or Claude Code settings.

git runs for real (through the fake runner), because what the record repo
looks like afterwards is what these tests verify. Its identity comes from a
global git config written into the temporary home folder.
"""

import io
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes
from goodall.claude_code_checks import goodall_hook_commands_by_event
from goodall.eval_config import read_eval_for_this_account
from goodall.fake_runner import FakeRunner
from goodall.hook import KNOWN_EVENTS
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.terminal import Terminal

GOODALL_PROGRAM = "/Users/ada/.local/bin/goodall"
REFERENCE = "op://goodall-eval/anthropic-research/credential"
NOW = datetime(2026, 11, 27, 9, 30, 0, tzinfo=UTC)


class NoKeyboard(io.StringIO):
    """Standard input that fails the test if goodall ever tries to read an answer from it."""

    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("goodall onboard tried to read from the terminal; the terminal never asks")


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give git a name and email in the temporary home folder, as Part 2 of docs/setup-by-hand.md asks."""
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")


def claude_settings_file() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"


def config_folder() -> Path:
    return Path(os.environ["GOODALL_CONFIG_DIR"])


def answers_text(tmp_path: Path, main_workstream: str = "research", extra: str = "") -> str:
    return f"""# A goodall answers file for a test.
schema_version = 1
account_mode = "dedicated"
eval = "new"

[new_eval]
name = "goodall-pilot"
description = "Building a product to first recurring revenue with AI agents."
workstreams = ["engineering", "research"]
construct = "Which parts of the work can be delegated to AI agents?"

[[new_eval.scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]

[contributor]
id = "ada-lovelace"
display_name = "Ada Lovelace"
main_workstream = "{main_workstream}"

[folders]
record_repo = "{tmp_path / "evals" / "goodall-pilot-records"}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[secrets]
manager = "1password"

[secrets.references]
ANTHROPIC_API_KEY_RESEARCH = "{REFERENCE}"

[claude_code]
goodall_program = "{GOODALL_PROGRAM}"
{extra}"""


def write_answers(tmp_path: Path, text: str) -> Path:
    answers_file = tmp_path / "answers.toml"
    answers_file.write_text(text)
    return answers_file


def run_goodall(arguments: list[str]) -> tuple[int, str, str]:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output, standard_error, standard_input=NoKeyboard())
    exit_code = cli.main(arguments, terminal=terminal, runner=FakeRunner(), clock=lambda: NOW)
    return exit_code, standard_output.getvalue(), standard_error.getvalue()


def git(folder: Path, *arguments: str) -> str:
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments], cwd=folder, env=environment, capture_output=True, text=True, check=True
    )
    return completed.stdout


def permissions(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def one_line(text: str) -> str:
    return " ".join(text.split())


def test_onboard_with_answers_sets_up_a_new_eval_that_goodall_can_read(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))

    exit_code, output, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.SUCCESS, errors
    settings = read_local_settings()
    assert isinstance(settings, LocalSettings), settings
    assert settings.contributor == "ada-lovelace"
    assert settings.main_workstream == "research"
    assert settings.account_mode == "dedicated"
    eval_for_this_account = read_eval_for_this_account(settings)
    assert not isinstance(eval_for_this_account, list), eval_for_this_account
    assert eval_for_this_account.eval_config.name == "goodall-pilot"

    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    assert git(record_repo, "remote") == ""
    assert git(record_repo, "status", "--porcelain") == ""
    committed_files = git(record_repo, "ls-files").split()
    assert committed_files == ["CONSTRUCT.md", "eval.toml"]
    assert "Which parts of the work" in (record_repo / "CONSTRUCT.md").read_text()

    log_directory = tmp_path / "evals" / "goodall-pilot-logs.noindex"
    assert log_directory.is_dir()
    assert permissions(log_directory) == 0o700

    env_file = config_folder() / ".env"
    assert permissions(env_file) == 0o600
    assert f"ANTHROPIC_API_KEY_RESEARCH={REFERENCE}" in env_file.read_text().splitlines()

    hooks = json.loads(claude_settings_file().read_text())["hooks"]
    commands_by_event = goodall_hook_commands_by_event(hooks)
    for event in KNOWN_EVENTS:
        assert len(commands_by_event[event]) == 1
        assert commands_by_event[event][0].program == GOODALL_PROGRAM
        assert commands_by_event[event][0].event_name == event

    screen = one_line(output)
    assert "create" in screen
    assert str(settings.settings_file) in screen


def test_onboard_shows_the_whole_plan_before_it_changes_anything(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))

    _, output, _ = run_goodall(["onboard", "--answers", str(answers_file)])

    plan_position = output.index("Local settings")
    assert output.index("Claude Code hooks") > plan_position
    assert "Done." in output[plan_position:]


def test_a_second_run_with_the_same_answers_changes_nothing_and_says_so(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))
    run_goodall(["onboard", "--answers", str(answers_file)])
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    commits_before = git(record_repo, "rev-list", "--all")
    settings_before = claude_settings_file().read_text()

    exit_code, output, _ = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.SUCCESS
    assert "create" not in output.split()
    assert "change" not in output.split()
    assert "Nothing to change" in one_line(output)
    assert git(record_repo, "rev-list", "--all") == commits_before
    assert claude_settings_file().read_text() == settings_before


def test_plan_only_shows_the_plan_and_changes_nothing(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))

    exit_code, output, _ = run_goodall(["onboard", "--answers", str(answers_file), "--plan"])

    assert exit_code == exit_codes.SUCCESS
    assert "create" in output.split()
    assert "Nothing was changed" in one_line(output)
    assert not (tmp_path / "evals").exists()
    assert not (config_folder() / "local-settings.toml").exists()
    assert not claude_settings_file().exists()


def test_an_answers_file_holding_a_raw_secret_is_refused_without_repeating_it(tmp_path: Path) -> None:
    raw_key = "sk-ant-api03-" + "Q" * 40
    text = answers_text(tmp_path).replace(REFERENCE, raw_key)
    answers_file = write_answers(tmp_path, text)

    exit_code, output, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.ERROR
    assert raw_key not in output + errors
    assert "ANTHROPIC_API_KEY_RESEARCH" in errors
    assert "op://" in one_line(errors)
    assert not (tmp_path / "evals").exists()
    assert not (config_folder() / "local-settings.toml").exists()
    assert not claude_settings_file().exists()


def test_a_raw_secret_anywhere_in_the_answers_file_is_refused(tmp_path: Path) -> None:
    raw_key = "ghp_" + "a1" * 20
    text = answers_text(tmp_path).replace("Ada Lovelace", raw_key)
    answers_file = write_answers(tmp_path, text)

    exit_code, output, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.ERROR
    assert raw_key not in output + errors
    assert "display_name" in errors
    assert not (tmp_path / "evals").exists()


def test_the_hooks_are_added_without_disturbing_existing_claude_code_settings(tmp_path: Path) -> None:
    existing_settings: dict[str, Any] = {
        "model": "opus",
        "env": {"SOME_SETTING": "kept"},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "afplay /System/Library/Sounds/Glass.aiff"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo checked"}]}],
        },
    }
    claude_settings_file().write_text(json.dumps(existing_settings))
    answers_file = write_answers(tmp_path, answers_text(tmp_path))

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.SUCCESS, errors
    settings = json.loads(claude_settings_file().read_text())
    assert settings["model"] == "opus"
    assert settings["env"] == {"SOME_SETTING": "kept"}
    assert settings["hooks"]["PreToolUse"] == existing_settings["hooks"]["PreToolUse"]
    assert settings["hooks"]["Stop"][0] == existing_settings["hooks"]["Stop"][0]
    commands_by_event = goodall_hook_commands_by_event(settings["hooks"])
    for event in KNOWN_EVENTS:
        assert len(commands_by_event[event]) == 1


def test_an_older_goodall_hook_is_replaced_not_doubled(tmp_path: Path) -> None:
    claude_settings_file().write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/old/bin/goodall hook Stop"}]}]}})
    )
    answers_file = write_answers(tmp_path, answers_text(tmp_path))

    run_goodall(["onboard", "--answers", str(answers_file)])

    settings = json.loads(claude_settings_file().read_text())
    stop_commands = goodall_hook_commands_by_event(settings["hooks"])["Stop"]
    assert [command.program for command in stop_commands] == [GOODALL_PROGRAM]


def test_changed_answers_change_only_what_differs_and_keep_a_person_s_edits(tmp_path: Path) -> None:
    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])
    settings_file = config_folder() / "local-settings.toml"
    edited_text = "# My own note, kept by goodall.\n" + settings_file.read_text()
    settings_file.write_text(edited_text)
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    commits_before = git(record_repo, "rev-list", "--all")

    changed_answers = write_answers(tmp_path, answers_text(tmp_path, main_workstream="engineering"))
    exit_code, output, errors = run_goodall(["onboard", "--answers", str(changed_answers)])

    assert exit_code == exit_codes.SUCCESS, errors
    new_text = settings_file.read_text()
    assert new_text.startswith("# My own note, kept by goodall.\n")
    assert 'main_workstream = "engineering"' in new_text
    assert 'main_workstream = "research"' not in new_text
    assert "main_workstream" in output
    assert git(record_repo, "rev-list", "--all") == commits_before
    settings = read_local_settings()
    assert isinstance(settings, LocalSettings)
    assert settings.main_workstream == "engineering"


def test_a_setting_missing_from_existing_local_settings_is_appended_in_a_marked_block(tmp_path: Path) -> None:
    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])
    settings_file = config_folder() / "local-settings.toml"
    original_text = settings_file.read_text()
    extra = '\n[activitywatch]\nport = 5610\ndevice_id = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"\n'

    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path, extra=extra)))])

    new_text = settings_file.read_text()
    assert new_text.startswith(original_text)
    added = new_text[len(original_text) :]
    assert "goodall onboard" in added
    assert "activitywatch_port = 5610" in added


def test_joining_an_existing_record_repo_leaves_its_eval_config_alone(tmp_path: Path) -> None:
    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    eval_config_before = (record_repo / "eval.toml").read_text()
    commits_before = git(record_repo, "rev-list", "--all")
    (config_folder() / "local-settings.toml").unlink()
    join_text = f"""schema_version = 1
account_mode = "mixed"
eval = "existing"

[contributor]
id = "ada-lovelace"
main_workstream = "engineering"

[folders]
record_repo = "{record_repo}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[claude_code]
goodall_program = "{GOODALL_PROGRAM}"
"""

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(write_answers(tmp_path, join_text))])

    assert exit_code == exit_codes.SUCCESS, errors
    assert (record_repo / "eval.toml").read_text() == eval_config_before
    assert git(record_repo, "rev-list", "--all") == commits_before
    settings = read_local_settings()
    assert isinstance(settings, LocalSettings)
    assert settings.account_mode == "mixed"


def test_json_reports_every_change_in_a_stable_shape(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))

    exit_code, output, _ = run_goodall(["onboard", "--answers", str(answers_file), "--json"])

    assert exit_code == exit_codes.SUCCESS
    document = json.loads(output)
    assert document["outcome"] == "applied"
    assert document["plan_only"] is False
    assert document["answers_file"] == str(answers_file)
    change_ids = [change["id"] for change in document["changes"]]
    assert change_ids == [
        "record_repo",
        "eval_config",
        "construct",
        "record_repo_commit",
        "log_directory",
        "env_file",
        "local_settings",
        "claude_code_hooks",
    ]
    for change in document["changes"]:
        assert set(change) == {"id", "action", "what", "path", "details", "done"}
        assert change["action"] == "create"
        assert change["done"] is True
    assert document["failures"] == []
    assert document["warnings"] == []


def test_json_for_a_refused_answers_file_names_the_failure_and_changes_nothing(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path).replace('account_mode = "dedicated"', ""))

    exit_code, output, _ = run_goodall(["onboard", "--answers", str(answers_file), "--json"])

    assert exit_code == exit_codes.ERROR
    document = json.loads(output)
    assert document["outcome"] == "rejected"
    assert document["changes"] == []
    assert document["failures"][0]["kind"] == "answers_key_missing"
    assert "account_mode" in document["failures"][0]["what_happened"]


def test_a_missing_answers_file_is_an_error_with_a_next_step(tmp_path: Path) -> None:
    exit_code, _, errors = run_goodall(["onboard", "--answers", str(tmp_path / "no-such-answers.toml")])

    assert exit_code == exit_codes.ERROR
    assert "no-such-answers.toml" in errors
    assert "Next" in errors


def test_an_eval_config_a_person_edited_is_left_alone_with_a_warning(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))
    run_goodall(["onboard", "--answers", str(answers_file)])
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    eval_config = record_repo / "eval.toml"
    edited = eval_config.read_text().replace('["engineering", "research"]', '["engineering", "research", "design"]')
    eval_config.write_text(edited)
    git(record_repo, "commit", "--quiet", "-am", "Add the design workstream")
    commits_before = git(record_repo, "rev-list", "--all")

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.SUCCESS
    assert eval_config.read_text() == edited
    assert "left it as it is" in one_line(errors)
    assert "workstreams" in one_line(errors)
    assert git(record_repo, "rev-list", "--all") == commits_before


def test_files_an_interrupted_run_left_uncommitted_are_committed_next_time(tmp_path: Path) -> None:
    answers_file = write_answers(tmp_path, answers_text(tmp_path))
    run_goodall(["onboard", "--answers", str(answers_file)])
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    git(record_repo, "update-ref", "-d", "HEAD")
    git(record_repo, "rm", "--cached", "--quiet", "eval.toml", "CONSTRUCT.md")

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.SUCCESS, errors
    assert git(record_repo, "ls-files").split() == ["CONSTRUCT.md", "eval.toml"]
    assert git(record_repo, "status", "--porcelain") == ""


def test_an_existing_env_file_others_can_read_is_made_private_and_keeps_its_lines(tmp_path: Path) -> None:
    env_file = config_folder() / ".env"
    env_file.write_text("# mine\nOTHER_VARIABLE=op://goodall-eval/other/credential\n")
    env_file.chmod(0o644)

    exit_code, output, errors = run_goodall(
        ["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))]
    )

    assert exit_code == exit_codes.SUCCESS, errors
    assert permissions(env_file) == 0o600
    text = env_file.read_text()
    assert text.startswith("# mine\nOTHER_VARIABLE=op://goodall-eval/other/credential\n")
    assert f"ANTHROPIC_API_KEY_RESEARCH={REFERENCE}" in text
    assert REFERENCE not in output + errors


def test_a_folder_of_someone_s_files_is_not_made_the_record_repo(tmp_path: Path) -> None:
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    record_repo.mkdir(parents=True)
    (record_repo / "notes.txt").write_text("mine")

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])

    assert exit_code == exit_codes.ERROR
    assert "already holds files" in one_line(errors)
    assert not (record_repo / ".git").exists()
    assert not (config_folder() / "local-settings.toml").exists()


# A new eval account has no git identity, so the record repo's first commit
# would fail. Goodall gives the record repo it makes an identity of its own,
# in that repository's own settings only, from the contributor's display name
# and a placeholder address that reaches nobody; the record repo is local
# and never pushed (ADR 0023), so this only labels local history.

PLACEHOLDER_EMAIL = "ada-lovelace@goodall.invalid"


def test_without_a_git_identity_the_new_record_repo_gets_one_of_its_own(tmp_path: Path) -> None:
    global_git_settings = Path(os.environ["HOME"]) / ".gitconfig"
    global_git_settings.unlink()
    record_repo = tmp_path / "evals" / "goodall-pilot-records"

    exit_code, output, errors = run_goodall(
        ["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))]
    )

    assert exit_code == exit_codes.SUCCESS, errors
    assert "Record repo git identity" in output
    assert PLACEHOLDER_EMAIL in one_line(output)
    assert git(record_repo, "config", "--local", "user.name").strip() == "Ada Lovelace"
    assert git(record_repo, "config", "--local", "user.email").strip() == PLACEHOLDER_EMAIL
    assert git(record_repo, "log", "-1", "--format=%an <%ae>").strip() == f"Ada Lovelace <{PLACEHOLDER_EMAIL}>"
    # Global git settings are never touched.
    assert not global_git_settings.exists()


def test_the_plan_shows_the_record_repo_identity_and_makes_nothing(tmp_path: Path) -> None:
    (Path(os.environ["HOME"]) / ".gitconfig").unlink()

    exit_code, output, _ = run_goodall(
        ["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path))), "--plan"]
    )

    assert exit_code == exit_codes.SUCCESS
    assert "Record repo git identity" in output
    assert not (tmp_path / "evals").exists()


def test_an_identity_git_already_has_is_never_overridden(tmp_path: Path) -> None:
    (Path(os.environ["HOME"]) / ".gitconfig").write_text("[user]\n\tname = Grace Hopper\n")
    record_repo = tmp_path / "evals" / "goodall-pilot-records"

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])

    assert exit_code == exit_codes.SUCCESS, errors
    assert git(record_repo, "log", "-1", "--format=%an <%ae>").strip() == f"Grace Hopper <{PLACEHOLDER_EMAIL}>"
    local_settings = git(record_repo, "config", "--local", "--list")
    assert "user.name" not in local_settings


def test_with_a_git_identity_the_record_repo_gets_none_of_its_own(tmp_path: Path) -> None:
    record_repo = tmp_path / "evals" / "goodall-pilot-records"

    exit_code, output, errors = run_goodall(
        ["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))]
    )

    assert exit_code == exit_codes.SUCCESS, errors
    assert "Record repo git identity" not in output
    assert "user." not in git(record_repo, "config", "--local", "--list")
    assert git(record_repo, "log", "-1", "--format=%ae").strip() == "ada@example.org"


def test_a_record_repo_with_history_and_no_identity_is_left_to_its_owner(tmp_path: Path) -> None:
    # Goodall gives an identity only to a record repo it makes, or one with no
    # commits yet (a first setup that stopped part way); a repository with
    # history is someone's, so goodall says how to give git an identity.
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    record_repo.mkdir(parents=True)
    git(record_repo, "init", "--quiet")
    git(record_repo, "commit", "--quiet", "--allow-empty", "-m", "An earlier commit")
    (Path(os.environ["HOME"]) / ".gitconfig").unlink()

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])

    assert exit_code == exit_codes.ERROR
    assert 'git config --global user.name "Your Name"' in errors
    assert "user." not in git(record_repo, "config", "--local", "--list")


def test_an_existing_eval_whose_roster_lacks_this_contributor_is_refused(tmp_path: Path) -> None:
    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])
    (config_folder() / "local-settings.toml").unlink()
    record_repo = tmp_path / "evals" / "goodall-pilot-records"
    join_text = f"""schema_version = 1
account_mode = "dedicated"
eval = "existing"

[contributor]
id = "grace-hopper"
main_workstream = "research"

[folders]
record_repo = "{record_repo}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[claude_code]
goodall_program = "{GOODALL_PROGRAM}"
"""

    exit_code, _, errors = run_goodall(["onboard", "--answers", str(write_answers(tmp_path, join_text))])

    assert exit_code == exit_codes.ERROR
    assert "roster" in one_line(errors)
    assert not (config_folder() / "local-settings.toml").exists()


def test_the_hooks_run_the_goodall_found_on_the_path_when_the_answers_name_none(tmp_path: Path) -> None:
    text = answers_text(tmp_path).replace(f'goodall_program = "{GOODALL_PROGRAM}"', "")
    answers_file = write_answers(tmp_path, text)
    terminal = Terminal(io.StringIO(), io.StringIO(), standard_input=NoKeyboard())
    runner = FakeRunner(installed_programs=["goodall"])

    exit_code = cli.main(
        ["onboard", "--answers", str(answers_file)], terminal=terminal, runner=runner, clock=lambda: NOW
    )

    assert exit_code == exit_codes.SUCCESS
    hooks = json.loads(claude_settings_file().read_text())["hooks"]
    program = goodall_hook_commands_by_event(hooks)["Stop"][0].program
    assert program == "/nonexistent-goodall-test/bin/goodall"


def test_a_changed_reference_replaces_only_its_own_line_in_the_env_file(tmp_path: Path) -> None:
    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, answers_text(tmp_path)))])
    env_file = config_folder() / ".env"
    before = env_file.read_text()
    new_reference = "op://goodall-eval/anthropic-research-2/credential"
    changed_text = answers_text(tmp_path).replace(REFERENCE, new_reference)

    run_goodall(["onboard", "--answers", str(write_answers(tmp_path, changed_text))])

    assert env_file.read_text() == before.replace(REFERENCE, new_reference)
    assert permissions(env_file) == 0o600
