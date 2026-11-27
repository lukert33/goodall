"""Unit tests for goodall.setup_page: the pieces plain `goodall onboard` is built from.

tests/test_onboard_setup_page.py drives the whole command; these tests pin
down one piece each: which files in the Downloads folder count as answers
files, the one-time code, the starting answers the page opens with, and
how they are written into the page.
"""

import dataclasses
import io
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, setup_page
from goodall.answers_file import Answers, read_answers_text
from goodall.fake_runner import FakeRunner
from goodall.terminal import Terminal

NOW = datetime(2026, 11, 27, 9, 30, 0, tzinfo=UTC)
REFERENCE = "op://goodall-eval/anthropic-research/credential"


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")


def answers_text(tmp_path: Path, one_time_code: str = "", account_mode: str = "dedicated") -> str:
    return f"""schema_version = 1
one_time_code = "{one_time_code}"
account_mode = "{account_mode}"
eval = "new"

[new_eval]
name = "goodall-pilot"
description = "Building a product to first recurring revenue with AI agents."
workstreams = ["engineering", "research"]

[[new_eval.scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]

[contributor]
id = "ada-lovelace"
display_name = "Ada Lovelace"
main_workstream = "research"

[folders]
record_repo = "{tmp_path / "evals" / "goodall-pilot-records"}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[secrets.references]
ANTHROPIC_API_KEY_RESEARCH = "{REFERENCE}"

[claude_code]
goodall_program = "/Users/ada/.local/bin/goodall"
"""


def onboard_with_answers(tmp_path: Path, account_mode: str = "dedicated") -> None:
    answers_file = tmp_path / "answers.toml"
    answers_file.write_text(answers_text(tmp_path, account_mode=account_mode))
    standard_error = io.StringIO()
    terminal = Terminal(io.StringIO(), standard_error)
    exit_code = cli.main(
        ["onboard", "--answers", str(answers_file)], terminal=terminal, runner=FakeRunner(), clock=lambda: NOW
    )
    assert exit_code == exit_codes.SUCCESS, standard_error.getvalue()


def page_json(page: Path) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="goodall-starting-answers">(.*?)</script>', page.read_text(), re.DOTALL
    )
    assert match is not None
    document: dict[str, Any] = json.loads(match.group(1))
    return document


def test_answers_files_include_the_names_browsers_give_a_second_download(tmp_path: Path) -> None:
    for name in [
        "goodall-answers.toml",
        "goodall-answers (1).toml",
        "goodall-answers-2.toml",
        "goodall-answers.toml.txt",
        "goodall-answers.toml.crdownload",
        "goodall-answers.toml.part",
        "other-answers.toml",
        "notes.txt",
    ]:
        (tmp_path / name).write_text("")
    (tmp_path / "goodall-answers-3.toml.download").mkdir()

    found = setup_page.answers_file_candidates(tmp_path)

    names = sorted(candidate.name for candidate in found)
    assert names == [
        "goodall-answers (1).toml",
        "goodall-answers-2.toml",
        "goodall-answers.toml",
        "goodall-answers.toml.txt",
    ]


def test_a_missing_downloads_folder_holds_no_answers_files(tmp_path: Path) -> None:
    assert setup_page.answers_file_candidates(tmp_path / "Downloads") == []


def test_a_one_time_code_is_never_taken_for_a_key(tmp_path: Path) -> None:
    for _ in range(50):
        code = setup_page.new_one_time_code()
        reading = read_answers_text(answers_text(tmp_path, code), tmp_path / "answers.toml", code)
        assert isinstance(reading, Answers), reading


def test_each_setup_page_gets_its_own_one_time_code() -> None:
    codes = set()
    for _ in range(20):
        codes.add(setup_page.new_one_time_code())
    assert len(codes) == 20


def test_a_fresh_account_starts_a_new_eval_in_dedicated_account_mode() -> None:
    starting = setup_page.starting_answers_from_this_account()

    assert starting.account_mode == "dedicated"
    assert starting.eval_choice == "new"
    assert starting.contributor_id == ""
    assert starting.workstreams == ["engineering", "research", "go-to-market"]
    assert starting.secret_references == {}


def test_after_onboarding_the_page_starts_from_the_eval_already_on_this_mac(tmp_path: Path) -> None:
    onboard_with_answers(tmp_path, account_mode="mixed")

    starting = setup_page.starting_answers_from_this_account()

    assert starting.eval_choice == "existing"
    assert starting.account_mode == "mixed"
    assert starting.contributor_id == "ada-lovelace"
    assert starting.main_workstream == "research"
    assert starting.record_repo == str(tmp_path / "evals" / "goodall-pilot-records")
    assert starting.log_directory == str(tmp_path / "evals" / "goodall-pilot-logs.noindex")
    assert starting.secret_references == {"ANTHROPIC_API_KEY_RESEARCH": REFERENCE}
    assert starting.known_workstreams == ["engineering", "research"]
    assert starting.goodall_program == "/Users/ada/.local/bin/goodall"
    assert starting.eval_name == "goodall-pilot"


def test_starting_answers_cannot_end_the_script_they_are_written_into(tmp_path: Path) -> None:
    starting = setup_page.starting_answers_from_this_account()
    hostile = "</script><script>alert(1)</script><!--"
    starting = dataclasses.replace(starting, display_name=hostile)

    page = setup_page.write_setup_page(tmp_path, starting, "0123456789abcdef")

    page_text = page.read_text()
    assert "<script>alert(1)" not in page_text
    assert page_json(page)["answers"]["contributor"]["display_name"] == hostile


def test_the_page_carries_the_one_time_code_and_where_the_browser_saves(tmp_path: Path) -> None:
    starting = setup_page.starting_answers_from_this_account()

    page = setup_page.write_setup_page(tmp_path, starting, "0123456789abcdef")

    document = page_json(page)
    assert document["one_time_code"] == "0123456789abcdef"
    assert document["answers_file_name"] == "goodall-answers.toml"
    assert document["downloads_folder"] == "~/Downloads"
    assert oct(page.stat().st_mode & 0o777) == oct(0o600)


def test_the_page_includes_the_portable_script_unchanged(tmp_path: Path) -> None:
    starting = setup_page.starting_answers_from_this_account()

    page = setup_page.write_setup_page(tmp_path, starting, "0123456789abcdef")

    assert setup_page.portable_script_text() in page.read_text()
