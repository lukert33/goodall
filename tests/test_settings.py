"""Outside-in tests for `goodall settings`: the setup page, reopened with this account's answers.

`goodall settings` is what the menu bar icon's "Settings…" item runs, with no
terminal in sight. It opens the setup page with the answers already on this
Mac, waits for the answers file the page's one button saves, and applies what
changed (docs/setup-page.md). As in tests/test_onboard_setup_page.py, the fake
runner stands in for `open` and for the person at the browser, the Downloads
folder is inside the temporary home folder, and no real browser is opened.
"""

import io
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, setup_page
from goodall.fake_runner import FakeRunner, ScriptedAnswer
from goodall.runner import Command, CommandOutcome
from goodall.terminal import Terminal

NOW = datetime(2026, 11, 27, 9, 30, 0, tzinfo=UTC)
GOODALL_PROGRAM = "/Users/ada/.local/bin/goodall"


class NoKeyboard(io.StringIO):
    """Standard input that fails the test if goodall ever tries to read an answer from it."""

    def isatty(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("goodall settings tried to read from the terminal; the terminal never asks")


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    pauses: list[float] = []
    monkeypatch.setattr(setup_page, "pause", pauses.append)
    return pauses


def downloads_folder() -> Path:
    return Path(os.environ["HOME"]) / "Downloads"


def one_line(text: str) -> str:
    return " ".join(text.split())


def starting_answers_in_text(page_text: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="goodall-starting-answers">(.*?)</script>', page_text, re.DOTALL
    )
    assert match is not None, "the page has no starting answers"
    starting: dict[str, Any] = json.loads(match.group(1))
    return starting


def git(record_repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(record_repo), *arguments], capture_output=True, text=True, check=True)
    return result.stdout


def new_eval_answers(tmp_path: Path) -> str:
    return f"""schema_version = 1
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
main_workstream = "research"

[folders]
record_repo = "{tmp_path / "evals" / "goodall-pilot-records"}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[claude_code]
goodall_program = "{GOODALL_PROGRAM}"
"""


def settings_answers(tmp_path: Path, one_time_code: str, workstreams: list[str], main_workstream: str) -> str:
    """What the page saves in a settings run: the existing eval, with its full workstream list."""
    listed = ", ".join(f'"{name}"' for name in workstreams)
    return f"""schema_version = 1
one_time_code = "{one_time_code}"
account_mode = "dedicated"
eval = "existing"

[existing_eval]
workstreams = [{listed}]

[contributor]
id = "ada-lovelace"
main_workstream = "{main_workstream}"

[folders]
record_repo = "{tmp_path / "evals" / "goodall-pilot-records"}"
log_directory = "{tmp_path / "evals" / "goodall-pilot-logs.noindex"}"

[claude_code]
goodall_program = "{GOODALL_PROGRAM}"
"""


class PersonAtTheBrowser:
    """Stands in for `open` and for the person: saves an answers file when goodall opens the page."""

    def __init__(self, save: Callable[[Path], tuple[str, str] | None]) -> None:
        self.save = save
        self.page_texts: list[str] = []

    def open(self, command: Command) -> ScriptedAnswer:
        page = Path(command.arguments[1])
        self.page_texts.append(page.read_text())
        saved = self.save(page)
        if saved is not None:
            file_name, text = saved
            downloads_folder().mkdir(parents=True, exist_ok=True)
            (downloads_folder() / file_name).write_text(text)
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0)


def run_goodall(arguments: list[str], person: PersonAtTheBrowser | None = None) -> tuple[int, str, str]:
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    terminal = Terminal(standard_output, standard_error, standard_input=NoKeyboard())
    runner = FakeRunner()
    if person is not None:
        runner.answer_with("open", person.open)
    exit_code = cli.main(arguments, terminal=terminal, runner=runner, clock=lambda: NOW)
    return exit_code, standard_output.getvalue(), standard_error.getvalue()


def onboard_an_eval(tmp_path: Path) -> Path:
    answers_file = tmp_path / "first-answers.toml"
    answers_file.write_text(new_eval_answers(tmp_path))
    exit_code, _, errors = run_goodall(["onboard", "--answers", str(answers_file)])
    assert exit_code == exit_codes.SUCCESS, errors
    return tmp_path / "evals" / "goodall-pilot-records"


def files_under(folder: Path) -> dict[Path, bytes]:
    """Every file under `folder` except git's own folder, with its bytes."""
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(folder).parts:
            files[path] = path.read_bytes()
    return files


def save_settings_answers(tmp_path: Path, workstreams: list[str], main_workstream: str) -> PersonAtTheBrowser:
    def press_the_one_button(page: Path) -> tuple[str, str]:
        code = starting_answers_in_text(page.read_text())["one_time_code"]
        return "goodall-answers.toml", settings_answers(tmp_path, code, workstreams, main_workstream)

    return PersonAtTheBrowser(press_the_one_button)


def test_adding_a_workstream_changes_only_the_eval_configs_workstream_list(tmp_path: Path) -> None:
    record_repo = onboard_an_eval(tmp_path)
    eval_before = (record_repo / "eval.toml").read_text()
    construct_before = (record_repo / "CONSTRUCT.md").read_bytes()
    config_directory = Path(os.environ["GOODALL_CONFIG_DIR"])
    claude_directory = Path(os.environ["CLAUDE_CONFIG_DIR"])
    elsewhere_before = files_under(config_directory) | files_under(claude_directory)
    person = save_settings_answers(tmp_path, ["engineering", "research", "go-to-market"], "research")

    exit_code, output, errors = run_goodall(["settings"], person)

    assert exit_code == exit_codes.SUCCESS, errors
    assert starting_answers_in_text(person.page_texts[0])["known_workstreams"] == ["engineering", "research"]
    eval_after = (record_repo / "eval.toml").read_text()
    assert eval_after == eval_before.replace(
        'workstreams = ["engineering", "research"]', 'workstreams = ["engineering", "research", "go-to-market"]'
    )
    assert eval_after != eval_before
    assert (record_repo / "CONSTRUCT.md").read_bytes() == construct_before
    assert files_under(config_directory) | files_under(claude_directory) == elsewhere_before
    # Committed in the record repo, only that file, nothing left over, no remote.
    assert git(record_repo, "status", "--porcelain") == ""
    assert git(record_repo, "diff", "--name-only", "HEAD~1", "HEAD").split() == ["eval.toml"]
    assert "go-to-market" in git(record_repo, "log", "-1", "--format=%B")
    assert git(record_repo, "remote") == ""
    # The report calls every other file unchanged.
    rows = {}
    for line in output.splitlines():
        match = re.match(r"^(create|change|unchanged)\s+(\S.*?)\s{2,}", line)
        if match:
            rows[match.group(2)] = match.group(1)
    assert rows["Eval config"] == "change"
    assert rows["Record repo commit"] == "create"
    for what, action in rows.items():
        if what not in ["Eval config", "Record repo commit"]:
            assert action == "unchanged", f"{what} was {action}"


def test_settings_before_goodall_is_set_up_says_to_run_onboard_and_opens_nothing() -> None:
    person = PersonAtTheBrowser(lambda page: None)

    exit_code, output, errors = run_goodall(["settings"], person)

    assert exit_code == exit_codes.ERROR
    assert person.page_texts == []
    assert "goodall onboard" in one_line(output + errors)


def test_the_settings_page_asks_for_workstreams_of_the_eval_already_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboard_an_eval(tmp_path)
    person = PersonAtTheBrowser(lambda page: None)

    def control_c(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_page, "pause", control_c)
    run_goodall(["settings"], person)

    starting = starting_answers_in_text(person.page_texts[0])
    assert starting["settings_run"] is True
    assert starting["page_title"] == "Goodall settings"
    assert starting["answers"]["eval"] == "existing"


def test_plan_shows_the_workstream_change_and_makes_none(tmp_path: Path) -> None:
    record_repo = onboard_an_eval(tmp_path)
    eval_before = (record_repo / "eval.toml").read_text()
    head_before = git(record_repo, "rev-parse", "HEAD")
    person = save_settings_answers(tmp_path, ["engineering", "research", "go-to-market"], "research")

    exit_code, output, errors = run_goodall(["settings", "--plan"], person)

    assert exit_code == exit_codes.SUCCESS, errors
    assert "go-to-market" in output
    assert (record_repo / "eval.toml").read_text() == eval_before
    assert git(record_repo, "rev-parse", "HEAD") == head_before


def test_the_new_workstream_can_be_the_main_workstream(tmp_path: Path) -> None:
    onboard_an_eval(tmp_path)
    person = save_settings_answers(tmp_path, ["engineering", "research", "go-to-market"], "go-to-market")

    exit_code, _, errors = run_goodall(["settings"], person)

    assert exit_code == exit_codes.SUCCESS, errors
    local_settings = (Path(os.environ["GOODALL_CONFIG_DIR"]) / "local-settings.toml").read_text()
    assert 'main_workstream = "go-to-market"' in local_settings


def test_a_hand_edited_eval_config_keeps_its_other_lines_and_comments(tmp_path: Path) -> None:
    record_repo = onboard_an_eval(tmp_path)
    config_file = record_repo / "eval.toml"
    edited = config_file.read_text().replace(
        'workstreams = ["engineering", "research"]',
        '# who we are\nworkstreams = ["engineering", "research"]   # add yours here',
    )
    config_file.write_text(edited)
    git(record_repo, "commit", "-am", "Add notes to the eval config")
    person = save_settings_answers(tmp_path, ["engineering", "research", "ops"], "research")

    exit_code, _, errors = run_goodall(["settings"], person)

    assert exit_code == exit_codes.SUCCESS, errors
    assert config_file.read_text() == edited.replace(
        '["engineering", "research"]   # add yours here', '["engineering", "research", "ops"]   # add yours here'
    )


def test_uncommitted_edits_to_the_eval_config_are_never_swept_into_goodalls_commit(tmp_path: Path) -> None:
    record_repo = onboard_an_eval(tmp_path)
    config_file = record_repo / "eval.toml"
    config_file.write_text("# my unfinished edit\n" + config_file.read_text())
    edited = config_file.read_text()
    answers_file = tmp_path / "answers.toml"
    answers_file.write_text(settings_answers(tmp_path, "unused", ["engineering", "research", "ops"], "research"))

    exit_code, output, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.ERROR
    assert "not committed" in one_line(output + errors)
    assert config_file.read_text() == edited


def test_workstreams_are_never_removed(tmp_path: Path) -> None:
    record_repo = onboard_an_eval(tmp_path)
    eval_before = (record_repo / "eval.toml").read_text()
    answers_file = tmp_path / "answers.toml"
    answers_file.write_text(settings_answers(tmp_path, "unused", ["research", "ops"], "research"))

    exit_code, output, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.ERROR
    assert "engineering" in one_line(output + errors)
    assert (record_repo / "eval.toml").read_text() == eval_before


def test_a_workstream_line_goodall_cannot_edit_is_left_alone_with_a_by_hand_step(tmp_path: Path) -> None:
    record_repo = onboard_an_eval(tmp_path)
    config_file = record_repo / "eval.toml"
    config_file.write_text(
        config_file.read_text().replace(
            'workstreams = ["engineering", "research"]', 'workstreams = [\n  "engineering",\n  "research",\n]'
        )
    )
    git(record_repo, "commit", "-am", "Spread the workstreams over lines")
    edited = config_file.read_text()
    answers_file = tmp_path / "answers.toml"
    answers_file.write_text(settings_answers(tmp_path, "unused", ["engineering", "research", "ops"], "research"))

    exit_code, output, errors = run_goodall(["onboard", "--answers", str(answers_file)])

    assert exit_code == exit_codes.ERROR
    assert "by hand" in one_line(output + errors)
    assert config_file.read_text() == edited
