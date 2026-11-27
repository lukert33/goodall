"""Outside-in tests for plain `goodall onboard`: the setup page, driven through `cli.main`.

`goodall onboard` writes the setup page, opens it with `open` and waits for
the answers file the page makes the browser save (ADR 0012). Here the fake
runner stands in for `open` and for the person at the browser: when goodall
"opens" the page, the test reads the one-time code out of the page goodall
wrote and saves an answers file into the Downloads folder, as pressing
Finish setup would. No real browser is ever opened, and the Downloads
folder is inside the temporary home folder the conftest fixtures provide.

The page's own JavaScript is tested in tests/test_setup_page_script.py.
"""

import io
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, setup_page
from goodall.fake_runner import FakeRunner, ScriptedAnswer
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.runner import Command, CommandOutcome
from goodall.terminal import Terminal

GOODALL_PROGRAM = "/Users/ada/.local/bin/goodall"
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


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make every pause between looks at the Downloads folder instant, and keep how long each would have been."""
    pauses: list[float] = []
    monkeypatch.setattr(setup_page, "pause", pauses.append)
    return pauses


def downloads_folder() -> Path:
    return Path(os.environ["HOME"]) / "Downloads"


def starting_answers_in(page: Path) -> dict[str, Any]:
    """The starting answers goodall wrote into the page, as the page's script reads them."""
    return starting_answers_in_text(page.read_text())


def starting_answers_in_text(page_text: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="goodall-starting-answers">(.*?)</script>', page_text, re.DOTALL
    )
    assert match is not None, "the page has no starting answers"
    starting: dict[str, Any] = json.loads(match.group(1))
    return starting


def answers_text(tmp_path: Path, one_time_code: str, account_mode: str = "dedicated") -> str:
    return f"""# goodall answers file, saved by the setup page.
schema_version = 1
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

[claude_code]
goodall_program = "{GOODALL_PROGRAM}"
"""


class PersonAtTheBrowser:
    """Stands in for `open` and for the person: saves an answers file when goodall opens the page.

    `save` is given the page goodall wrote and returns the file name and the
    text to save into the Downloads folder, or None to save nothing.
    `opened_pages` keeps every page goodall asked `open` to show.
    """

    def __init__(self, save: Callable[[Path], tuple[str, str] | None]) -> None:
        self.save = save
        self.opened_pages: list[Path] = []
        self.page_texts: list[str] = []

    def open(self, command: Command) -> ScriptedAnswer:
        page = Path(command.arguments[1])
        self.opened_pages.append(page)
        self.page_texts.append(page.read_text())
        saved = self.save(page)
        if saved is not None:
            file_name, text = saved
            downloads_folder().mkdir(parents=True, exist_ok=True)
            (downloads_folder() / file_name).write_text(text)
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0)


def run_onboard(person: PersonAtTheBrowser, arguments: list[str] | None = None) -> tuple[int, str, str]:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output, standard_error, standard_input=NoKeyboard())
    runner = FakeRunner()
    runner.answer_with("open", person.open)
    exit_code = cli.main(["onboard", *(arguments or [])], terminal=terminal, runner=runner, clock=lambda: NOW)
    return exit_code, standard_output.getvalue(), standard_error.getvalue()


def one_line(text: str) -> str:
    return " ".join(text.split())


def test_onboard_opens_the_setup_page_and_sets_up_from_the_answers_it_saves(tmp_path: Path) -> None:
    def press_finish_setup(page: Path) -> tuple[str, str]:
        code = starting_answers_in(page)["one_time_code"]
        return "goodall-answers.toml", answers_text(tmp_path, code)

    person = PersonAtTheBrowser(press_finish_setup)

    exit_code, output, errors = run_onboard(person)

    assert exit_code == exit_codes.SUCCESS, errors
    assert len(person.opened_pages) == 1
    settings = read_local_settings()
    assert isinstance(settings, LocalSettings), settings
    assert settings.contributor == "ada-lovelace"
    assert (tmp_path / "evals" / "goodall-pilot-records" / "eval.toml").is_file()
    screen = one_line(output)
    assert "Finish setup" in screen
    assert str(downloads_folder()) in screen or "~/Downloads" in screen
    assert "Done." in screen


def test_the_setup_page_is_removed_when_onboard_ends(tmp_path: Path) -> None:
    def press_finish_setup(page: Path) -> tuple[str, str]:
        return "goodall-answers.toml", answers_text(tmp_path, starting_answers_in(page)["one_time_code"])

    person = PersonAtTheBrowser(press_finish_setup)

    run_onboard(person)

    page = person.opened_pages[0]
    assert not page.exists()
    assert not page.parent.exists()


def test_the_page_is_self_contained_and_makes_no_network_requests(tmp_path: Path) -> None:
    person = PersonAtTheBrowser(
        lambda page: ("goodall-answers.toml", answers_text(tmp_path, starting_answers_in(page)["one_time_code"]))
    )

    run_onboard(person)

    page_text = person.page_texts[0]
    assert "http://" not in page_text
    assert "https://" not in page_text
    assert "Content-Security-Policy" in page_text
    assert "default-src 'none'" in page_text
    assert "Finish setup" in page_text
    assert "uploaded" not in page_text.replace("Nothing is uploaded", "")


def test_an_answers_file_from_an_earlier_page_is_left_alone(tmp_path: Path) -> None:
    downloads_folder().mkdir()
    earlier_file = downloads_folder() / "goodall-answers.toml"
    earlier_file.write_text(answers_text(tmp_path, "an-earlier-code"))

    def press_finish_setup(page: Path) -> tuple[str, str]:
        # A browser gives a second download with the same name another name.
        return "goodall-answers (1).toml", answers_text(tmp_path, starting_answers_in(page)["one_time_code"])

    exit_code, output, errors = run_onboard(PersonAtTheBrowser(press_finish_setup))

    assert exit_code == exit_codes.SUCCESS, errors
    screen = one_line(output)
    assert "goodall-answers.toml" in screen
    assert "earlier setup page" in screen
    assert "goodall-answers (1).toml" in screen
    assert earlier_file.read_text() == answers_text(tmp_path, "an-earlier-code")


def test_a_file_with_another_code_is_never_used(tmp_path: Path, no_real_waiting: list[float]) -> None:
    person = PersonAtTheBrowser(lambda page: ("goodall-answers.toml", answers_text(tmp_path, "not-this-page")))

    exit_code, _, errors = run_onboard(person)

    assert exit_code == exit_codes.ERROR
    assert "no answers file from the setup page arrived" in one_line(errors)
    assert isinstance(read_local_settings(), list)
    assert sum(no_real_waiting) == setup_page.WAIT_LIMIT_SECONDS


def test_refused_answers_keep_goodall_waiting_for_corrected_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The person first saves answers whose record repo folder holds files,
    # then, told so in the terminal, picks another folder and presses
    # Finish setup again on the page that is still open.
    occupied = tmp_path / "evals" / "goodall-pilot-records"
    occupied.mkdir(parents=True)
    (occupied / "notes.txt").write_text("someone's own file")
    codes: list[str] = []

    def press_finish_setup(page: Path) -> tuple[str, str]:
        codes.append(starting_answers_in(page)["one_time_code"])
        return "goodall-answers.toml", answers_text(tmp_path, codes[0])

    def press_again_after_a_moment(seconds: float) -> None:
        corrected = answers_text(tmp_path, codes[0]).replace("goodall-pilot-records", "goodall-pilot-records-2")
        (downloads_folder() / "goodall-answers-2.toml").write_text(corrected)

    monkeypatch.setattr(setup_page, "pause", press_again_after_a_moment)

    exit_code, output, errors = run_onboard(PersonAtTheBrowser(press_finish_setup))

    assert exit_code == exit_codes.SUCCESS, errors
    assert str(occupied) in one_line(errors)
    assert "press Finish setup again" in one_line(output)
    assert (tmp_path / "evals" / "goodall-pilot-records-2" / "eval.toml").is_file()
    assert sorted(path.name for path in occupied.iterdir()) == ["notes.txt"]


def test_control_c_while_waiting_stops_cleanly_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def control_c(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_page, "pause", control_c)
    person = PersonAtTheBrowser(lambda page: None)

    exit_code, output, _ = run_onboard(person)

    assert exit_code == exit_codes.ERROR
    assert "Stopped. Nothing was changed." in one_line(output)
    assert not person.opened_pages[0].exists()
    assert isinstance(read_local_settings(), list)


def test_waiting_ends_with_a_next_step_when_no_answers_arrive(no_real_waiting: list[float]) -> None:
    exit_code, output, errors = run_onboard(PersonAtTheBrowser(lambda page: None))

    assert exit_code == exit_codes.ERROR
    screen = one_line(errors)
    assert "Nothing was changed" in screen
    assert "goodall onboard --answers" in screen
    assert "Still waiting" in one_line(output)
    assert sum(no_real_waiting) == setup_page.WAIT_LIMIT_SECONDS


def test_a_page_the_browser_could_not_open_is_named_so_the_person_can_open_it(tmp_path: Path) -> None:
    pages: list[Path] = []

    def open_fails(command: Command) -> ScriptedAnswer:
        page = Path(command.arguments[1])
        pages.append(page)
        downloads_folder().mkdir(exist_ok=True)
        code = starting_answers_in(page)["one_time_code"]
        (downloads_folder() / "goodall-answers.toml").write_text(answers_text(tmp_path, code))
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=1, standard_error="no application")

    standard_output = io.StringIO()
    standard_error = io.StringIO()
    runner = FakeRunner()
    runner.answer_with("open", open_fails)
    exit_code = cli.main(
        ["onboard"], terminal=Terminal(standard_output, standard_error), runner=runner, clock=lambda: NOW
    )

    assert exit_code == exit_codes.SUCCESS
    assert str(pages[0]) in one_line(standard_error.getvalue())
    assert "double-clicking" in one_line(standard_error.getvalue())


def test_plan_on_the_setup_page_shows_the_plan_and_changes_nothing(tmp_path: Path) -> None:
    person = PersonAtTheBrowser(
        lambda page: ("goodall-answers.toml", answers_text(tmp_path, starting_answers_in(page)["one_time_code"]))
    )

    exit_code, output, _ = run_onboard(person, ["--plan"])

    assert exit_code == exit_codes.SUCCESS
    assert "Nothing was changed" in one_line(output)
    assert not (tmp_path / "evals").exists()


def test_json_without_answers_is_a_usage_error_that_opens_nothing() -> None:
    person = PersonAtTheBrowser(lambda page: None)

    exit_code, _, errors = run_onboard(person, ["--json"])

    assert exit_code == exit_codes.USAGE_ERROR
    assert person.opened_pages == []
    assert "--answers" in errors


def test_the_page_opens_with_the_answers_already_on_this_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_visit = PersonAtTheBrowser(
        lambda page: ("goodall-answers.toml", answers_text(tmp_path, starting_answers_in(page)["one_time_code"]))
    )
    run_onboard(first_visit)

    def control_c(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_page, "pause", control_c)
    second_visit = PersonAtTheBrowser(lambda page: None)
    run_onboard(second_visit)

    starting = starting_answers_in_text(second_visit.page_texts[0])
    assert starting["answers"]["eval"] == "existing"
    assert starting["answers"]["contributor"]["id"] == "ada-lovelace"
    assert starting["answers"]["goodall_program"] == GOODALL_PROGRAM
    assert starting["known_workstreams"] == ["engineering", "research"]
    assert starting["one_time_code"] != starting_answers_in_text(first_visit.page_texts[0])["one_time_code"]
