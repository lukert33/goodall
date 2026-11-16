"""Outside-in tests for `goodall journal` and `goodall intervene`, driven through `cli.main`.

Each test runs goodall the way a contributor (or an agent working for one)
would: with arguments, sometimes with text piped to standard input, a
captured terminal and the fake runner. It then checks only what that person
could observe: the exit code, what was printed, and the files and commits in
the record repo.

The eval account is built inside tmp_path: a home folder, local settings, a
record repo that is a real git repository with an eval config committed in
it, and a log directory. git runs for real through the fake runner, with the
developer's own git settings out of reach (HOME points at the temporary
home folder, and every `GIT_*` variable is removed).
"""

import io
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from goodall import cli, exit_codes
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

EVAL_CONFIG_TEXT = """\
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


def git(working_directory: Path, *arguments: str) -> str:
    """Run git for the test's own setup or inspection, and return what it printed."""
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith("GIT_"):
            environment[name] = value
    completed = subprocess.run(
        ["git", *arguments], cwd=working_directory, env=environment, capture_output=True, text=True, check=True
    )
    return completed.stdout


@dataclass
class EvalAccount:
    """A pretend eval account: its home folder, local settings folder and record repo."""

    home_folder: Path
    config_directory: Path
    record_repo: Path

    def journal_files(self) -> list[Path]:
        return sorted((self.record_repo / "journal").glob("*.md"))

    def intervention_files(self) -> list[Path]:
        return sorted((self.record_repo / "interventions").glob("*.md"))

    def commit_subjects(self) -> list[str]:
        return git(self.record_repo, "log", "--format=%s").splitlines()


@pytest.fixture
def eval_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvalAccount:
    """An eval account whose local settings, eval config and record repo are all in order."""
    home_folder = tmp_path / "home"
    config_directory = home_folder / "goodall-config"
    record_repo = home_folder / "evals" / "goodall-pilot-records"
    log_directory = tmp_path / "EvalLogs" / "goodall-pilot"
    for folder in [config_directory, record_repo, log_directory]:
        folder.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home_folder))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_folder / ".config"))
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(config_directory))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)

    (config_directory / "local-settings.toml").write_text(
        "schema_version = 1\n"
        'contributor = "ada-lovelace"\n'
        'main_workstream = "research"\n'
        'record_repo = "~/evals/goodall-pilot-records"\n'
        f'log_directory = "{log_directory}"\n'
        'secrets_manager = "1password"\n'
    )
    git(record_repo, "init", "--quiet", "--initial-branch=main")
    git(record_repo, "config", "user.name", "Ada Lovelace")
    git(record_repo, "config", "user.email", "ada.lovelace@example.org")
    (record_repo / "eval.toml").write_text(EVAL_CONFIG_TEXT)
    git(record_repo, "add", "eval.toml")
    git(record_repo, "commit", "--quiet", "-m", "Start the record repo")
    return EvalAccount(home_folder, config_directory, record_repo)


@dataclass
class GoodallRun:
    """What one run of goodall did, as the person who ran it would see it."""

    exit_code: int
    standard_output: str
    standard_error: str


def run_goodall(arguments: list[str], standard_input: io.StringIO | None = None) -> GoodallRun:
    """Run goodall with a captured terminal and the fake runner.

    `standard_input` is what was piped in; leave it out to run as if
    standard input were a terminal with a person at it.
    """
    if standard_input is None:
        standard_input = KeyboardInput()
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output=standard_output, standard_error=standard_error, standard_input=standard_input)
    exit_code = cli.main(arguments, terminal=terminal, runner=FakeRunner())
    return GoodallRun(exit_code, standard_output.getvalue(), standard_error.getvalue())


class KeyboardInput(io.StringIO):
    """Standard input that is a terminal: a person at a keyboard, not text piped in.

    Reading it fails the test, because goodall must never sit waiting for
    someone to type.
    """

    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("goodall read from the keyboard; it must never wait for typing")


# ---------------------------------------------------------------------------
# goodall journal
# ---------------------------------------------------------------------------


def test_journal_records_the_text_given_as_an_argument_and_commits_it(eval_account: EvalAccount) -> None:
    run = run_goodall(["journal", "Chose Postgres over SQLite, because two workers will write at once."])

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    assert run.standard_error == ""
    [entry_file] = eval_account.journal_files()
    text = entry_file.read_text()
    assert 'record_type = "journal_entry"' in text
    assert 'contributor = "ada-lovelace"' in text
    assert "work_block" not in text
    assert text.endswith("+++\n\nChose Postgres over SQLite, because two workers will write at once.\n")
    assert eval_account.commit_subjects()[0] == f"Add journal entry {entry_file.stem}"
    assert git(eval_account.record_repo, "status", "--porcelain") == ""
    assert "journal entry" in run.standard_output
    assert str(entry_file) in run.standard_output


def test_journal_reads_several_lines_piped_to_standard_input(eval_account: EvalAccount) -> None:
    piped_text = "# Chose Postgres\n\nThe agent proposed it; I agreed.\nDecided by me.\n"

    run = run_goodall(["journal"], standard_input=io.StringIO(piped_text))

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    [entry_file] = eval_account.journal_files()
    assert entry_file.read_text().endswith("+++\n\n# Chose Postgres\n\nThe agent proposed it; I agreed.\nDecided by me.\n")
