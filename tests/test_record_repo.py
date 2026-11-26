"""Tests for goodall.record_repo: putting a record into the record repo.

These drive `write_record` the way `goodall journal`, `goodall intervene`
and the work-block commands do: hand it a runner, the record repo folder
and a record, and read back the outcome. git runs for real, through the
fake runner, in temporary folders: a record repo with no remote, and one
cloned from a bare repository standing in for a remote someone set up by
hand, which goodall must leave unpushed (ADR 0023).

The tests set up repositories with `subprocess` directly, not through the
runner: setting up is the test's own work, not goodall's. Every git the
tests start runs with the `GIT_*` variables removed and HOME pointed at a
temporary folder, so neither a git hook that happens to be running the
tests nor the developer's own git settings can change what they see.
"""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goodall import records
from goodall.fake_runner import FakeRunner
from goodall.record_repo import write_record

ADA = "ada-lovelace"
MORNING = datetime(2026, 11, 16, 9, 30, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_git_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test an empty home folder, so the developer's git settings and identity are out of reach."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)


def git(working_directory: Path, *arguments: str) -> str:
    """Run git in `working_directory` for the test's own setup or inspection, and return what it printed."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments],
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def make_record_repo(folder: Path, contributor_name: str = "Ada Lovelace") -> Path:
    """Make a fresh record repo at `folder` with a first commit and a configured identity."""
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "--quiet", "--initial-branch=main")
    set_identity(folder, contributor_name)
    (folder / "eval.toml").write_text('name = "test eval"\n')
    git(folder, "add", "eval.toml")
    git(folder, "commit", "--quiet", "-m", "Start the record repo")
    return folder


def set_identity(folder: Path, contributor_name: str) -> None:
    """Configure git's identity for the repository at `folder` only."""
    git(folder, "config", "user.name", contributor_name)
    git(folder, "config", "user.email", contributor_name.lower().replace(" ", ".") + "@example.org")


def journal_entry(
    body: str = "Chose Postgres over SQLite.", contributor: str = ADA, suffix: str = "7f3a9c2e"
) -> records.JournalEntry:
    return records.build_journal_entry(contributor=contributor, written_at=MORNING, body=body, suffix=suffix)


def commit_subjects(folder: Path, revision: str = "HEAD") -> list[str]:
    return git(folder, "log", "--format=%s", revision).splitlines()


def test_a_journal_entry_is_written_and_committed_in_a_record_repo_without_a_remote(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    entry = journal_entry()

    outcome = write_record(FakeRunner(), record_repo, entry)

    expected_path = record_repo / "journal" / "20261116T093000Z-ada-lovelace-7f3a9c2e.md"
    assert outcome.failures == []
    assert outcome.path == expected_path
    assert outcome.committed
    assert expected_path.read_text() == records.render_record(entry)
    assert commit_subjects(record_repo)[0] == "Add journal entry 20261116T093000Z-ada-lovelace-7f3a9c2e"
    assert git(record_repo, "log", "-1", "--format=%an <%ae>") == "Ada Lovelace <ada.lovelace@example.org>\n"
    assert git(record_repo, "status", "--porcelain") == ""


def make_remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Make a bare repository standing in for a remote someone set up by hand, and a record repo cloned from it."""
    seed = make_record_repo(tmp_path / "seed")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(remote))
    record_repo = tmp_path / "records"
    git(tmp_path, "clone", "--quiet", str(remote), str(record_repo))
    set_identity(record_repo, "Ada Lovelace")
    return remote, record_repo


def test_a_record_repo_with_a_remote_set_by_hand_gets_a_local_commit_and_nothing_is_pushed(tmp_path: Path) -> None:
    remote, record_repo = make_remote_and_clone(tmp_path)
    runner = FakeRunner()

    outcome = write_record(runner, record_repo, journal_entry())

    assert outcome.failures == []
    assert outcome.committed
    assert commit_subjects(record_repo)[0] == "Add journal entry 20261116T093000Z-ada-lovelace-7f3a9c2e"
    assert commit_subjects(remote, "main") == ["Start the record repo"]
    for command in runner.calls:
        for word in ["push", "fetch", "remote", "merge"]:
            assert word not in command.arguments


def test_a_record_whose_file_already_exists_is_refused_and_the_file_left_alone(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    write_record(FakeRunner(), record_repo, journal_entry(body="The original note."))
    commits_before = git(record_repo, "rev-list", "--count", "HEAD")

    outcome = write_record(FakeRunner(), record_repo, journal_entry(body="A different note with the same id."))

    assert [failure.kind for failure in outcome.failures] == ["record_already_exists"]
    assert not outcome.written
    assert not outcome.committed
    assert "The original note." in outcome.path.read_text()
    assert git(record_repo, "rev-list", "--count", "HEAD") == commits_before


def test_a_record_whose_folder_leads_outside_the_record_repo_is_refused(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A link where the journal folder should be would carry the record out of the record repo.
    (record_repo / "journal").symlink_to(elsewhere)

    outcome = write_record(FakeRunner(), record_repo, journal_entry())

    assert [failure.kind for failure in outcome.failures] == ["record_path_outside_record_repo"]
    assert not outcome.written
    assert not outcome.committed
    assert list(elsewhere.iterdir()) == []


def test_a_record_repo_without_a_configured_identity_gets_a_failure_saying_how_to_set_one(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    git(record_repo, "config", "--unset", "user.name")
    git(record_repo, "config", "--unset", "user.email")
    commits_before = git(record_repo, "rev-list", "--count", "HEAD")

    outcome = write_record(FakeRunner(), record_repo, journal_entry())

    assert [failure.kind for failure in outcome.failures] == ["record_repo_identity_missing"]
    assert "git config" in outcome.failures[0].what_to_do_next
    assert not outcome.written
    assert not outcome.committed
    assert not outcome.path.exists()
    assert git(record_repo, "rev-list", "--count", "HEAD") == commits_before


def test_a_folder_that_is_not_a_git_repository_gets_a_failure_and_no_file(tmp_path: Path) -> None:
    folder = tmp_path / "not-a-repository"
    folder.mkdir()

    outcome = write_record(FakeRunner(), folder, journal_entry())

    assert [failure.kind for failure in outcome.failures] == ["record_repo_not_git_repository"]
    assert not outcome.written
    assert list(folder.iterdir()) == []


def test_a_folder_inside_another_git_repository_is_not_taken_for_the_record_repo(tmp_path: Path) -> None:
    outer_repository = make_record_repo(tmp_path / "outer")
    folder = outer_repository / "records"
    folder.mkdir()

    outcome = write_record(FakeRunner(), folder, journal_entry())

    assert [failure.kind for failure in outcome.failures] == ["record_repo_not_git_repository"]
    assert list(folder.iterdir()) == []
    assert commit_subjects(outer_repository) == ["Start the record repo"]


def test_only_the_record_is_committed_and_the_contributors_other_changes_are_left_as_they_were(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    (record_repo / "eval.toml").write_text('name = "renamed eval"\n')
    git(record_repo, "add", "eval.toml")
    (record_repo / "draft.md").write_text("Not ready yet.\n")

    outcome = write_record(FakeRunner(), record_repo, journal_entry())

    assert outcome.committed
    committed_files = git(record_repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed_files == ["journal/20261116T093000Z-ada-lovelace-7f3a9c2e.md"]
    assert git(record_repo, "status", "--porcelain").splitlines() == ["M  eval.toml", "?? draft.md"]


def test_a_commit_refused_by_the_contributors_own_hook_leaves_the_record_written_and_says_so(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    hook = record_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'commits are frozen today' >&2\nexit 1\n")
    hook.chmod(0o755)
    commits_before = git(record_repo, "rev-list", "--count", "HEAD")

    outcome = write_record(FakeRunner(), record_repo, journal_entry())

    assert [failure.kind for failure in outcome.failures] == ["record_commit_failed"]
    assert "commits are frozen today" in outcome.failures[0].what_happened
    assert outcome.written
    assert not outcome.committed
    assert outcome.path.exists()
    assert git(record_repo, "rev-list", "--count", "HEAD") == commits_before


def test_stray_git_variables_in_the_environment_cannot_redirect_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    other_repository = make_record_repo(tmp_path / "other", contributor_name="Someone Else")
    # As when goodall runs inside a git hook of another repository.
    monkeypatch.setenv("GIT_DIR", str(other_repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other_repository))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other_repository / ".git" / "index"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Not The Contributor")

    outcome = write_record(FakeRunner(), record_repo, journal_entry())

    assert outcome.failures == []
    assert commit_subjects(record_repo)[0] == "Add journal entry 20261116T093000Z-ada-lovelace-7f3a9c2e"
    assert git(record_repo, "log", "-1", "--format=%an") == "Ada Lovelace\n"
    assert commit_subjects(other_repository) == ["Start the record repo"]


def test_every_git_command_is_bounded_in_time_and_cannot_prompt(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    runner = FakeRunner()

    write_record(runner, record_repo, journal_entry())

    assert runner.calls != []
    for command in runner.calls:
        assert command.timeout_seconds <= 60
        assert command.extra_environment["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_" in command.removed_environment_prefixes


def test_a_record_folder_goodall_may_not_write_in_gets_a_failure_not_an_exception(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    journal_folder = record_repo / "journal"
    journal_folder.mkdir()
    journal_folder.chmod(0o555)
    try:
        outcome = write_record(FakeRunner(), record_repo, journal_entry())
    finally:
        journal_folder.chmod(0o755)

    assert [failure.kind for failure in outcome.failures] == ["record_not_written"]
    assert not outcome.written
    assert not outcome.committed
