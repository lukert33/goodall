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
import json
import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goodall import cli, exit_codes
from goodall.caller import was_started_by_claude_code
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

    def add_remote(self) -> Path:
        """Give the record repo a shared copy to push to: a bare repository beside the home folder."""
        remote = self.home_folder.parent / "remote.git"
        git(self.home_folder.parent, "clone", "--quiet", "--bare", str(self.record_repo), str(remote))
        git(self.record_repo, "remote", "add", "origin", str(remote))
        git(self.record_repo, "fetch", "--quiet", "origin")
        git(self.record_repo, "branch", "--quiet", "--set-upstream-to=origin/main")
        return remote


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
    assert entry_file.read_text().endswith(
        "+++\n\n# Chose Postgres\n\nThe agent proposed it; I agreed.\nDecided by me.\n"
    )


def test_journal_with_no_text_at_a_keyboard_fails_at_once_and_shows_how_to_pass_text(
    eval_account: EvalAccount,
) -> None:
    # KeyboardInput fails the test if goodall tries to read from it.
    run = run_goodall(["journal"])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_output == ""
    assert run.standard_error.startswith("FAIL  No text was given for the journal entry.")
    assert 'goodall journal "' in run.standard_error
    assert "| goodall journal" in run.standard_error
    assert eval_account.journal_files() == []


@pytest.mark.parametrize(
    ("arguments", "piped_text"),
    [
        (["journal", "   "], None),
        (["journal", ""], None),
        (["journal"], ""),
        (["journal"], "\n \t\n"),
    ],
)
def test_journal_refuses_empty_or_blank_text(
    eval_account: EvalAccount, arguments: list[str], piped_text: str | None
) -> None:
    standard_input = None if piped_text is None else io.StringIO(piped_text)

    run = run_goodall(arguments, standard_input=standard_input)

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_error.startswith("FAIL  The text for the journal entry is empty.")
    assert eval_account.journal_files() == []
    assert eval_account.commit_subjects() == ["Start the record repo"]


@pytest.mark.parametrize(
    ("private_text", "what_is_named"),
    [
        ("Line one is fine.\nAsked bob.smith@example.org about pricing.", "email address"),
        ("Pasted the key sk-ant-abcdefghijklmnopqrstuvwxyz0123 by mistake.", "secret"),
    ],
)
def test_journal_refuses_an_email_address_or_secret_without_repeating_it(
    eval_account: EvalAccount, private_text: str, what_is_named: str
) -> None:
    run = run_goodall(["journal"], standard_input=io.StringIO(private_text))

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_error.startswith("FAIL  Goodall did not record the journal entry")
    assert what_is_named in run.standard_error
    assert "bob.smith" not in run.standard_error + run.standard_output
    assert "abcdefghijklmnop" not in run.standard_error + run.standard_output
    assert eval_account.journal_files() == []


def test_journal_json_prints_one_document_with_the_id_the_path_and_whether_it_was_pushed(
    eval_account: EvalAccount,
) -> None:
    run = run_goodall(["journal", "--json", "Chose Postgres."])

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_error == ""
    [entry_file] = eval_account.journal_files()
    # json.loads refuses trailing text, so this also proves nothing else was printed.
    document = json.loads(run.standard_output)
    assert document == {
        "record_type": "journal_entry",
        "id": entry_file.stem,
        "path": str(entry_file),
        "committed": True,
        "pushed": False,
        "push_state": "no_remote",
        "warnings": [],
        "failures": [],
    }


def test_journal_json_reports_a_usage_failure_in_the_same_shape(eval_account: EvalAccount) -> None:
    run = run_goodall(["journal", "--json"])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_error == ""
    document = json.loads(run.standard_output)
    assert document["id"] is None
    assert document["path"] is None
    assert document["committed"] is False
    assert document["pushed"] is False
    assert [failure["kind"] for failure in document["failures"]] == ["entry_text_missing"]


def test_journal_sent_to_a_reachable_shared_record_repo_says_so(eval_account: EvalAccount) -> None:
    remote = eval_account.add_remote()

    run = run_goodall(["journal", "Chose Postgres."])

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_error == ""
    assert run.standard_output.startswith("Recorded your journal entry and sent it to the shared record repo.\n")
    [entry_file] = eval_account.journal_files()
    assert git(remote, "log", "-1", "--format=%s", "main") == f"Add journal entry {entry_file.stem}\n"


def test_journal_with_a_pending_push_is_saved_on_this_mac_with_a_warning_and_exits_0(
    eval_account: EvalAccount,
) -> None:
    remote = eval_account.add_remote()
    # The shared copy cannot be reached, as when this Mac is offline.
    remote.rename(remote.with_name("remote-away.git"))

    run = run_goodall(["journal", "Chose Postgres."])

    assert run.exit_code == exit_codes.SUCCESS
    assert "saved on this Mac and will be sent to the shared record repo" in " ".join(run.standard_output.split())
    assert run.standard_error.startswith("WARN  The record is saved and committed")
    assert "\nNext  " in run.standard_error
    assert "FAIL" not in run.standard_error
    [entry_file] = eval_account.journal_files()
    assert eval_account.commit_subjects()[0] == f"Add journal entry {entry_file.stem}"

    json_run = run_goodall(["journal", "--json", "A second note."])

    document = json.loads(json_run.standard_output)
    assert json_run.exit_code == exit_codes.SUCCESS
    assert document["committed"] is True
    assert document["pushed"] is False
    assert document["push_state"] == "pending"
    assert [warning["kind"] for warning in document["warnings"]] == ["record_push_failed"]
    assert document["failures"] == []


def test_journal_that_git_will_not_commit_exits_1_and_says_why(eval_account: EvalAccount) -> None:
    hook = eval_account.record_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'commits are frozen today' >&2\nexit 1\n")
    hook.chmod(0o755)

    run = run_goodall(["journal", "Chose Postgres."])

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_output == ""
    assert run.standard_error.startswith("FAIL  The record was written to")
    assert "commits are frozen today" in run.standard_error


def test_journal_without_local_settings_exits_1_and_says_to_run_onboard(
    eval_account: EvalAccount,
) -> None:
    (eval_account.config_directory / "local-settings.toml").unlink()

    run = run_goodall(["journal", "Chose Postgres."])

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_error.startswith("FAIL  No local settings were found.")
    assert "goodall onboard" in run.standard_error


def test_journal_with_the_record_repo_folder_missing_names_the_folder(eval_account: EvalAccount) -> None:
    eval_account.record_repo.rename(eval_account.record_repo.with_name("moved-away"))

    run = run_goodall(["journal", "Chose Postgres."])

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_error.startswith("FAIL  The record repo folder ~/evals/goodall-pilot-records does not exist.")
    assert "Nothing was recorded." in " ".join(run.standard_error.split())


def test_journal_reads_its_text_from_a_file_written_in_any_editor(eval_account: EvalAccount, tmp_path: Path) -> None:
    note = tmp_path / "Today's note.txt"
    note.write_text("Chose Postgres over SQLite.\n\nThe agent wrote the comparison; I decided.\n")

    run = run_goodall(["journal", "--file", str(note)])

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    [entry_file] = eval_account.journal_files()
    assert entry_file.read_text().endswith(
        "+++\n\nChose Postgres over SQLite.\n\nThe agent wrote the comparison; I decided.\n"
    )
    # The file is the contributor's own draft; goodall leaves it where it was.
    assert note.exists()


def test_journal_given_both_text_and_a_file_is_a_usage_error(eval_account: EvalAccount, tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("From the file.\n")

    run = run_goodall(["journal", "--file", str(note), "From the argument."])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_error.startswith("FAIL  Both text and a file were given for the journal entry.")
    assert eval_account.journal_files() == []


def test_journal_with_a_file_that_is_not_there_names_it(eval_account: EvalAccount, tmp_path: Path) -> None:
    missing_note = tmp_path / "no-such-note.txt"

    run = run_goodall(["journal", "--file", str(missing_note)])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert " ".join(run.standard_error.split()).startswith(f"FAIL Goodall could not read {missing_note}")
    assert eval_account.journal_files() == []


def test_journal_with_a_file_that_is_not_text_says_so_without_showing_it(
    eval_account: EvalAccount, tmp_path: Path
) -> None:
    picture = tmp_path / "screenshot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x00")

    run = run_goodall(["journal", "--file", str(picture)])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert "is not plain text" in " ".join(run.standard_error.split())
    assert eval_account.journal_files() == []


def test_journal_refuses_text_longer_than_the_limit_and_keeps_text_at_the_limit(
    eval_account: EvalAccount, tmp_path: Path
) -> None:
    # About fifteen thousand words: far more than a note, as when a whole
    # transcript is pasted by mistake. Transcripts are kept as raw records.
    longest_allowed = "word " * 20_000
    too_long = longest_allowed + "x"

    refused_run = run_goodall(["journal"], standard_input=io.StringIO(too_long))
    long_note = tmp_path / "long.txt"
    long_note.write_text(too_long)
    refused_file_run = run_goodall(["journal", "--file", str(long_note)])
    kept_run = run_goodall(["journal"], standard_input=io.StringIO(longest_allowed))

    assert refused_run.exit_code == exit_codes.USAGE_ERROR
    assert refused_run.standard_error.startswith(
        "FAIL  The text for the journal entry is longer than 100000 characters."
    )
    assert refused_file_run.exit_code == exit_codes.USAGE_ERROR
    assert kept_run.exit_code == exit_codes.SUCCESS
    assert len(eval_account.journal_files()) == 1


# ---------------------------------------------------------------------------
# goodall intervene
# ---------------------------------------------------------------------------


def test_intervene_records_the_category_decision_and_text_and_commits_it(eval_account: EvalAccount) -> None:
    run = run_goodall(
        [
            "intervene",
            "--category",
            "agent-limitation",
            "--decision",
            "modify",
            "The agent tried to delete a failing assertion; I told it to fix the date parsing instead.",
        ]
    )

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    assert run.standard_error == ""
    [intervention_file] = eval_account.intervention_files()
    text = intervention_file.read_text()
    assert 'record_type = "intervention"' in text
    assert 'contributor = "ada-lovelace"' in text
    assert 'category = "agent-limitation"' in text
    assert 'decision = "modify"' in text
    for absent_field in ["work_block", "claude_session", "interrupted", "question_outcome"]:
        assert absent_field not in text
    # Recorded as it happens, so it occurred when it was written.
    occurred_line = next(line for line in text.splitlines() if line.startswith("occurred_at = "))
    written_line = next(line for line in text.splitlines() if line.startswith("written_at = "))
    assert occurred_line.removeprefix("occurred_at = ") == written_line.removeprefix("written_at = ")
    assert text.endswith(
        "+++\n\nThe agent tried to delete a failing assertion; I told it to fix the date parsing instead.\n"
    )
    assert eval_account.commit_subjects()[0] == f"Add intervention {intervention_file.stem}"
    assert run.standard_output.startswith("Recorded your intervention.")
    assert "Category  agent-limitation" in run.standard_output
    assert "Decision  modify" in run.standard_output


@pytest.mark.parametrize(
    "category_arguments",
    [
        [],
        ["--category", "agent_limitation"],
        ["--category", "Agent-Limitation"],
        ["--category", ""],
    ],
)
def test_intervene_without_a_known_category_lists_the_eval_s_categories(
    eval_account: EvalAccount, category_arguments: list[str]
) -> None:
    run = run_goodall(["intervene", *category_arguments, "--decision", "modify", "Stopped it."])

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_output == ""
    flattened_error = " ".join(run.standard_error.split())
    assert "policy-mandated, infrastructure, agent-limitation, freely-delegable" in flattened_error
    assert "intervention_categories" in flattened_error
    assert eval_account.intervention_files() == []


def test_intervene_without_a_category_as_json_names_the_categories_too(eval_account: EvalAccount) -> None:
    run = run_goodall(["intervene", "--json", "--decision", "modify", "Stopped it."])

    assert run.exit_code == exit_codes.USAGE_ERROR
    document = json.loads(run.standard_output)
    [failure] = document["failures"]
    assert failure["kind"] == "intervention_category_missing"
    assert "agent-limitation" in failure["what_to_do_next"]


@pytest.mark.parametrize("decision_arguments", [[], ["--decision", "overrode"], ["--decision", "Modify"]])
def test_intervene_without_a_known_decision_explains_each_decision(
    eval_account: EvalAccount, decision_arguments: list[str]
) -> None:
    run = run_goodall(["intervene", "--category", "agent-limitation", *decision_arguments, "Stopped it."])

    assert run.exit_code == exit_codes.USAGE_ERROR
    flattened_error = " ".join(run.standard_error.split())
    for decision in ["approve", "modify", "reject", "escalate", "terminate"]:
        assert decision in flattened_error
    # Each decision is explained, so a person can choose without the schema document.
    assert "stopped the agent's work on the task altogether" in flattened_error
    assert eval_account.intervention_files() == []


def test_intervene_missing_both_category_and_decision_reports_both_at_once(eval_account: EvalAccount) -> None:
    run = run_goodall(["intervene", "--json", "Stopped it."])

    document = json.loads(run.standard_output)
    assert run.exit_code == exit_codes.USAGE_ERROR
    assert [failure["kind"] for failure in document["failures"]] == [
        "intervention_category_missing",
        "intervention_decision_missing",
    ]


def intervene_arguments(*extra_arguments: str) -> list[str]:
    """A valid `goodall intervene` command line, with `extra_arguments` added before the text."""
    return ["intervene", "--category", "agent-limitation", "--decision", "modify", *extra_arguments, "Stopped it."]


def test_intervene_records_what_was_interrupted_and_which_claude_session(eval_account: EvalAccount) -> None:
    session_id = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"

    run = run_goodall(intervene_arguments("--interrupted", "tool_call", "--claude-session", session_id))

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    [intervention_file] = eval_account.intervention_files()
    text = intervention_file.read_text()
    assert 'interrupted = "tool_call"' in text
    assert f'claude_session = "{session_id}"' in text


def test_intervene_with_an_unknown_interrupted_activity_is_a_usage_error(eval_account: EvalAccount) -> None:
    run = run_goodall(intervene_arguments("--interrupted", "typing"), standard_input=io.StringIO(""))

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert eval_account.intervention_files() == []


def test_intervene_with_a_claude_session_that_is_not_a_session_id_says_where_to_find_one(
    eval_account: EvalAccount,
) -> None:
    run = run_goodall(intervene_arguments("--claude-session", "yesterday afternoon"))

    assert run.exit_code == exit_codes.USAGE_ERROR
    assert run.standard_error.startswith("FAIL  The --claude-session given is not a Claude Code session id.")
    assert "/status" in run.standard_error
    assert eval_account.intervention_files() == []


@pytest.fixture
def new_york_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make this Mac's local time New York's, so a time typed without a zone has a known meaning."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def header_value(record_file: Path, key: str) -> str:
    """The value of `key` in a record file's metadata header, as written."""
    for line in record_file.read_text().splitlines():
        if line.startswith(f"{key} = "):
            return line.removeprefix(f"{key} = ")
    raise AssertionError(f"{key} is not in {record_file}")


@pytest.mark.parametrize(
    ("occurred_at", "expected_utc"),
    [
        ("2026-11-16T09:30:00Z", "2026-11-16T09:30:00Z"),
        ("2026-11-16T10:30:00+01:00", "2026-11-16T09:30:00Z"),
        # No zone given: this Mac's local time, here New York, five hours behind UTC in November.
        ("2026-11-16 09:30", "2026-11-16T14:30:00Z"),
        ("2026-11-16T09:30:15", "2026-11-16T14:30:15Z"),
    ],
)
def test_intervene_records_when_it_occurred_if_that_was_earlier(
    eval_account: EvalAccount, new_york_time: None, occurred_at: str, expected_utc: str
) -> None:
    run = run_goodall(intervene_arguments("--occurred-at", occurred_at))

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    [intervention_file] = eval_account.intervention_files()
    assert header_value(intervention_file, "occurred_at") == expected_utc
    assert header_value(intervention_file, "written_at") != expected_utc
    # The file name sorts by when it happened, not when it was written down.
    assert intervention_file.name.startswith(expected_utc.replace("-", "").replace(":", ""))


def test_intervene_occurred_at_a_time_of_day_means_the_most_recent_one(
    eval_account: EvalAccount, new_york_time: None
) -> None:
    two_hours_ago = datetime.now().astimezone() - timedelta(hours=2)
    expected_utc = two_hours_ago.replace(second=0, microsecond=0).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    run = run_goodall(intervene_arguments("--occurred-at", two_hours_ago.strftime("%H:%M")))

    assert run.exit_code == exit_codes.SUCCESS, run.standard_error
    [intervention_file] = eval_account.intervention_files()
    assert header_value(intervention_file, "occurred_at") == expected_utc


@pytest.mark.parametrize("occurred_at", ["2099-01-01T00:00:00Z", "half past nine", "2026-13-40 09:30", ""])
def test_intervene_occurred_at_in_the_future_or_unreadable_is_a_usage_error_with_examples(
    eval_account: EvalAccount, occurred_at: str
) -> None:
    run = run_goodall(intervene_arguments("--occurred-at", occurred_at))

    assert run.exit_code == exit_codes.USAGE_ERROR
    flattened_error = " ".join(run.standard_error.split())
    assert "--occurred-at 14:05" in flattened_error
    assert eval_account.intervention_files() == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["intervene", "--category", "agent-limitation", "--decision", "modify"],
        ["intervene", "--category", "agent-limitation", "--decision", "modify", "--occurred-at", "soon", "Stopped."],
        ["intervene", "--decision", "modify", "Stopped it."],
    ],
)
def test_every_command_offered_to_try_is_safe_to_paste_into_a_shell(
    eval_account: EvalAccount, arguments: list[str]
) -> None:
    run = run_goodall(arguments)

    try_lines = []
    in_try_block = False
    for line in run.standard_error.splitlines():
        if line.startswith("Try   "):
            in_try_block = True
        elif not line.startswith("      "):
            in_try_block = False
        if in_try_block:
            try_lines.append(line)
    assert try_lines != []
    for line in try_lines:
        # < and > are redirections in a shell: a placeholder like <category>
        # pasted as it stands would read or overwrite a file.
        assert "<" not in line and ">" not in line, line
        # An intervention needs both, so every example shows both.
        assert "goodall intervene --category " in line
        assert " --decision " in line


def test_intervene_json_prints_one_document_with_the_id_the_path_and_whether_it_was_pushed(
    eval_account: EvalAccount,
) -> None:
    eval_account.add_remote()

    run = run_goodall(intervene_arguments("--json"))

    assert run.exit_code == exit_codes.SUCCESS
    assert run.standard_error == ""
    [intervention_file] = eval_account.intervention_files()
    document = json.loads(run.standard_output)
    assert document == {
        "record_type": "intervention",
        "id": intervention_file.stem,
        "path": str(intervention_file),
        "committed": True,
        "pushed": True,
        "push_state": "pushed",
        "warnings": [],
        "failures": [],
    }


# ---------------------------------------------------------------------------
# Both commands decline when Claude Code started them
# ---------------------------------------------------------------------------

INTERVENE_ARGUMENTS = ["intervene", "--category", "infrastructure", "--decision", "escalate"]


class PipedInputThatMustNotBeRead(io.StringIO):
    """Piped-in text that fails the test if read: a refused command reads nothing."""

    def isatty(self) -> bool:
        return False

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("goodall read standard input before refusing")


@pytest.mark.parametrize(
    "arguments",
    [["journal", "SECRET-WORDS"], [*INTERVENE_ARGUMENTS, "SECRET-WORDS"]],
    ids=["journal", "intervene"],
)
def test_inside_claude_code_the_command_refuses_and_records_nothing(
    eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    commits_before = eval_account.commit_subjects()

    run = run_goodall(arguments)

    assert run.exit_code == exit_codes.ERROR
    assert run.standard_output == ""
    assert "was not recorded" in run.standard_error
    assert "Terminal app" in run.standard_error
    assert "do not try again" in run.standard_error
    assert "SECRET-WORDS" not in run.standard_error
    assert eval_account.journal_files() == []
    assert eval_account.intervention_files() == []
    assert eval_account.commit_subjects() == commits_before
    assert git(eval_account.record_repo, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "arguments",
    [["journal", "SECRET-WORDS", "--json"], [*INTERVENE_ARGUMENTS, "SECRET-WORDS", "--json"]],
    ids=["journal", "intervene"],
)
def test_inside_claude_code_json_failure_has_a_kind_an_agent_can_stop_on(
    eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")

    run = run_goodall(arguments)

    assert run.exit_code == exit_codes.ERROR
    document = json.loads(run.standard_output)
    [failure] = document["failures"]
    assert failure["kind"] == "must_be_run_by_a_person"
    assert document["committed"] is False
    assert document["id"] is None
    assert "SECRET-WORDS" not in run.standard_output


def test_inside_claude_code_the_refusal_comes_before_reading_text_or_settings(
    eval_account: EvalAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    (eval_account.config_directory / "local-settings.toml").unlink()

    run = run_goodall(["journal"], standard_input=PipedInputThatMustNotBeRead())

    assert run.exit_code == exit_codes.ERROR
    assert "was not recorded" in run.standard_error
    assert "local-settings" not in run.standard_error


def test_outside_claude_code_the_commands_still_record(eval_account: EvalAccount) -> None:
    assert run_goodall(["journal", "A note."]).exit_code == exit_codes.SUCCESS
    assert run_goodall([*INTERVENE_ARGUMENTS, "I stepped in."]).exit_code == exit_codes.SUCCESS


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("", False), ("true", False), (None, False)],
)
def test_was_started_by_claude_code_only_for_the_value_claude_code_sets(value: str | None, expected: bool) -> None:
    environment = {}
    if value is not None:
        environment["CLAUDECODE"] = value

    assert was_started_by_claude_code(environment) is expected
