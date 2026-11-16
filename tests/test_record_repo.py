"""Tests for goodall.record_repo: putting a record into the record repo.

These drive `write_record` the way `goodall journal`, `goodall intervene`
and the work-block commands will: hand it a runner, the record repo folder
and a record, and read back the outcome. git runs for real, through the
fake runner, in temporary folders: a record repo with no remote, one whose
remote is a bare repository in another temporary folder, one whose remote
is missing and later appears, and two clones of one remote.

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
from goodall.record_repo import PushState, write_record

ADA = "ada-lovelace"
GRACE = "grace-hopper"
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
    assert outcome.push_state == PushState.NO_REMOTE
    assert outcome.pending_commits == 0
    assert expected_path.read_text() == records.render_record(entry)
    assert commit_subjects(record_repo)[0] == "Add journal entry 20261116T093000Z-ada-lovelace-7f3a9c2e"
    assert git(record_repo, "log", "-1", "--format=%an <%ae>") == "Ada Lovelace <ada.lovelace@example.org>\n"
    assert git(record_repo, "status", "--porcelain") == ""


def make_remote_and_clone(
    tmp_path: Path, clone_name: str = "records", contributor_name: str = "Ada Lovelace"
) -> tuple[Path, Path]:
    """Make a bare repository standing in for the hosted remote, and a record repo cloned from it."""
    seed = make_record_repo(tmp_path / "seed")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(remote))
    record_repo = clone(tmp_path, remote, clone_name, contributor_name)
    return remote, record_repo


def clone(tmp_path: Path, remote: Path, clone_name: str, contributor_name: str) -> Path:
    """Clone `remote` into a new record repo called `clone_name`, with its own identity."""
    record_repo = tmp_path / clone_name
    git(tmp_path, "clone", "--quiet", str(remote), str(record_repo))
    set_identity(record_repo, contributor_name)
    return record_repo


def test_a_record_written_in_a_clone_with_a_reachable_remote_is_pushed(tmp_path: Path) -> None:
    remote, record_repo = make_remote_and_clone(tmp_path)

    outcome = write_record(FakeRunner(), record_repo, journal_entry())

    assert outcome.failures == []
    assert outcome.committed
    assert outcome.push_state == PushState.PUSHED
    assert outcome.pending_commits == 0
    assert commit_subjects(remote, "main")[0] == "Add journal entry 20261116T093000Z-ada-lovelace-7f3a9c2e"


def test_a_push_to_an_unreachable_remote_is_pending_and_goes_out_with_the_next_write(tmp_path: Path) -> None:
    remote, record_repo = make_remote_and_clone(tmp_path)
    # The remote goes away, as when a laptop is offline, and comes back later.
    away = tmp_path / "remote-away.git"
    remote.rename(away)

    first_outcome = write_record(FakeRunner(), record_repo, journal_entry(body="First note.", suffix="00000001"))

    assert first_outcome.committed
    assert first_outcome.push_state == PushState.PENDING
    assert first_outcome.pending_commits == 1
    assert [failure.kind for failure in first_outcome.failures] == ["record_push_failed"]
    assert (record_repo / "journal" / "20261116T093000Z-ada-lovelace-00000001.md").exists()
    assert commit_subjects(record_repo)[0] == "Add journal entry 20261116T093000Z-ada-lovelace-00000001"

    away.rename(remote)
    second_outcome = write_record(FakeRunner(), record_repo, journal_entry(body="Second note.", suffix="00000002"))

    assert second_outcome.failures == []
    assert second_outcome.push_state == PushState.PUSHED
    assert second_outcome.pending_commits == 0
    assert commit_subjects(remote, "main")[:2] == [
        "Add journal entry 20261116T093000Z-ada-lovelace-00000002",
        "Add journal entry 20261116T093000Z-ada-lovelace-00000001",
    ]


def test_a_push_rejected_because_a_collaborator_pushed_first_succeeds_after_merging(tmp_path: Path) -> None:
    remote, adas_record_repo = make_remote_and_clone(tmp_path, "adas-records", "Ada Lovelace")
    graces_record_repo = clone(tmp_path, remote, "graces-records", "Grace Hopper")

    adas_outcome = write_record(FakeRunner(), adas_record_repo, journal_entry(contributor=ADA))
    graces_outcome = write_record(FakeRunner(), graces_record_repo, journal_entry(contributor=GRACE))

    assert adas_outcome.push_state == PushState.PUSHED
    assert graces_outcome.failures == []
    assert graces_outcome.push_state == PushState.PUSHED
    assert graces_outcome.pending_commits == 0
    # Both contributors' commits are on the remote, unrewritten, joined by a merge.
    remote_history = git(remote, "log", "--format=%an|%s", "main").splitlines()
    assert "Ada Lovelace|Add journal entry 20261116T093000Z-ada-lovelace-7f3a9c2e" in remote_history
    assert "Grace Hopper|Add journal entry 20261116T093000Z-grace-hopper-7f3a9c2e" in remote_history
    assert git(remote, "rev-list", "--count", "--merges", "main") == "1\n"
    assert git(adas_record_repo, "rev-parse", "HEAD") in git(remote, "rev-list", "main")
    # Grace's copy now has Ada's record too.
    assert (graces_record_repo / "journal" / "20261116T093000Z-ada-lovelace-7f3a9c2e.md").exists()


def test_a_record_whose_file_already_exists_is_refused_and_the_file_left_alone(tmp_path: Path) -> None:
    record_repo = make_record_repo(tmp_path / "records")
    write_record(FakeRunner(), record_repo, journal_entry(body="The original note."))
    commits_before = git(record_repo, "rev-list", "--count", "HEAD")

    outcome = write_record(FakeRunner(), record_repo, journal_entry(body="A different note with the same id."))

    assert [failure.kind for failure in outcome.failures] == ["record_already_exists"]
    assert not outcome.written
    assert not outcome.committed
    assert outcome.push_state == PushState.NOT_ATTEMPTED
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
