"""Tests of the setup page's own script: what the page would save, read back by goodall.

The page's script has a portable part, src/goodall/setup_page_answers.js,
with no page code in it (goodall.setup_page explains why). These tests run
those functions with the JavaScript every Mac comes with, `osascript -l
JavaScript` (JavaScriptCore, the engine inside Safari), so they need no
browser and no new dependency: a small driver below starts from the
starting answers goodall writes into the page, makes the changes a person
would make on the page, and returns the answers file text the page would
save, or the problems the page would show. Each test hands that text to
goodall's own reader, so the page and goodall cannot drift apart unnoticed.

What these tests cannot see is the page's drawing and its buttons (the
second script in setup_page.html), nor how each browser saves the file.
docs/setup-page.md says what to check by hand.

They are skipped where there is no osascript, which includes the Linux
runners CI uses; CI's Mac job runs them, as it does the menu bar icon's
model tests (ADR 0025).
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goodall import cli, exit_codes, setup_page
from goodall.answers_file import Answers, read_answers_text
from goodall.fake_runner import FakeRunner, ScriptedAnswer
from goodall.runner import Command, CommandOutcome
from goodall.terminal import Terminal

NOW = datetime(2026, 11, 27, 9, 30, 0, tzinfo=UTC)
CODE = "0123456789abcdef"
REFERENCE = "op://goodall-eval/anthropic-research/credential"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("osascript") is None,
    reason="the setup page's script is run with osascript, which only macOS has",
)

# Runs in osascript after the portable part. `changes` are what a person
# would type: each sets one dotted place in the page's state, as the page's
# own inputs do.
DRIVER = """
function setAt(state, place, value) {
  var parts = place.split(".");
  var holder = state;
  for (var index = 0; index < parts.length - 1; index++) {
    holder = holder[parts[index]];
  }
  holder[parts[parts.length - 1]] = value;
}

function run(argv) {
  var input = JSON.parse(argv[0]);
  var state = startingState(input.starting);
  for (var index = 0; index < input.changes.length; index++) {
    setAt(state, input.changes[index][0], input.changes[index][1]);
  }
  if (input.ask === "answers") {
    return answersFileText(state, input.starting.one_time_code);
  }
  return JSON.stringify(problemsIn(state));
}
"""


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    home = Path(os.environ["HOME"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".gitconfig").write_text("[user]\n\tname = Ada Lovelace\n\temail = ada@example.org\n")


@pytest.fixture(scope="module")
def driver_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    script = tmp_path_factory.mktemp("setup-page-script") / "driver.js"
    script.write_text(setup_page.portable_script_text() + "\n" + DRIVER)
    return script


def run_page_script(driver_script: Path, starting: dict[str, Any], changes: list[list[Any]], ask: str) -> str:
    """Ask the page's portable part for the answers text or the problems, after `changes`."""
    driver_input = json.dumps({"starting": starting, "changes": changes, "ask": ask})
    completed = subprocess.run(
        ["osascript", "-l", "JavaScript", str(driver_script), driver_input],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    # osascript ends what `run` returns with one line break of its own.
    return completed.stdout[:-1] if completed.stdout.endswith("\n") else completed.stdout


def fresh_page_document() -> dict[str, Any]:
    document = setup_page.page_document(setup_page.starting_answers_from_this_account(), CODE, "Setting up")
    return json.loads(json.dumps(document))  # type: ignore[no-any-return]


def new_eval_changes(tmp_path: Path) -> list[list[Any]]:
    """What a person types on a fresh page to start a new eval."""
    return [
        ["new_eval.name", "goodall-pilot"],
        ["new_eval.description", "Building a product to first recurring revenue with AI agents."],
        ["new_eval.workstreams", ["engineering", "research"]],
        ["new_eval.construct", "Which parts of the work can be delegated to AI agents?"],
        ["new_eval.repositories", ["github.com/your-org/goodall-pilot-app"]],
        ["contributor.display_name", "Ada Lovelace"],
        ["contributor.id", "ada-lovelace"],
        ["contributor.main_workstream", "research"],
        ["folders.record_repo", str(tmp_path / "evals" / "goodall-pilot-records")],
        ["folders.log_directory", str(tmp_path / "evals" / "goodall-pilot-logs.noindex")],
    ]


def read_back(text: str, tmp_path: Path) -> Answers:
    reading = read_answers_text(text, tmp_path / "goodall-answers.toml", CODE)
    assert isinstance(reading, Answers), [failure.what_happened for failure in reading]
    return reading


def test_a_new_eval_from_the_page_is_an_answers_file_goodall_accepts(driver_script: Path, tmp_path: Path) -> None:
    text = run_page_script(driver_script, fresh_page_document(), new_eval_changes(tmp_path), "answers")

    answers = read_back(text, tmp_path)
    assert answers.account_mode == "dedicated"
    assert answers.eval_choice == "new"
    assert answers.new_eval is not None
    assert answers.new_eval.name == "goodall-pilot"
    assert answers.new_eval.workstreams == ["engineering", "research"]
    assert answers.new_eval.scope[0].repository == "github.com/your-org/goodall-pilot-app"
    assert answers.new_eval.scope[0].paths == ["**"]
    assert answers.new_eval.construct is not None
    assert answers.new_eval.construct.strip() == "Which parts of the work can be delegated to AI agents?"
    assert answers.contributor_id == "ada-lovelace"
    assert answers.display_name == "Ada Lovelace"
    assert answers.record_repo.path == tmp_path / "evals" / "goodall-pilot-records"
    assert answers.secret_references == {}


def test_the_page_finds_nothing_wrong_with_answers_goodall_accepts(driver_script: Path, tmp_path: Path) -> None:
    problems = run_page_script(driver_script, fresh_page_document(), new_eval_changes(tmp_path), "problems")

    assert json.loads(problems) == {}


def test_quotes_backslashes_line_breaks_and_accents_survive_the_answers_file(
    driver_script: Path, tmp_path: Path
) -> None:
    description = 'Ada\'s "pilot": C:\\work, tab\there, café, 日本'
    construct = 'First line with """ three quotes,\nsecond line ending in a backslash \\\n\tthird, indented'
    changes = [
        *new_eval_changes(tmp_path),
        ["new_eval.description", description],
        ["new_eval.construct", construct],
        ["contributor.display_name", 'Zoë O\'Brien "Z"'],
    ]

    answers = read_back(run_page_script(driver_script, fresh_page_document(), changes, "answers"), tmp_path)

    assert answers.new_eval is not None
    assert answers.new_eval.description == description
    assert answers.new_eval.construct == construct + "\n"
    assert answers.display_name == 'Zoë O\'Brien "Z"'


def test_secret_references_are_saved_and_blank_rows_left_out(driver_script: Path, tmp_path: Path) -> None:
    changes = [
        *new_eval_changes(tmp_path),
        ["uses_api_keys", True],
        [
            "secret_references",
            [
                {"name": "ANTHROPIC_API_KEY_RESEARCH", "reference": REFERENCE},
                {"name": "ANTHROPIC_API_KEY_ENGINEERING", "reference": ""},
            ],
        ],
    ]

    answers = read_back(run_page_script(driver_script, fresh_page_document(), changes, "answers"), tmp_path)

    assert answers.secret_references == {"ANTHROPIC_API_KEY_RESEARCH": REFERENCE}


def test_references_are_left_out_when_the_person_says_no_api_keys(driver_script: Path, tmp_path: Path) -> None:
    changes = [
        *new_eval_changes(tmp_path),
        ["secret_references", [{"name": "ANTHROPIC_API_KEY_RESEARCH", "reference": REFERENCE}]],
        ["uses_api_keys", False],
    ]

    answers = read_back(run_page_script(driver_script, fresh_page_document(), changes, "answers"), tmp_path)

    assert answers.secret_references == {}


def test_try_it_out_saves_mixed_account_mode_without_activitywatch(driver_script: Path, tmp_path: Path) -> None:
    changes = [*new_eval_changes(tmp_path), ["account_mode", "mixed"], ["new_eval.activitywatch", True]]

    answers = read_back(run_page_script(driver_script, fresh_page_document(), changes, "answers"), tmp_path)

    assert answers.account_mode == "mixed"
    assert answers.new_eval is not None
    assert answers.new_eval.activitywatch is False


def test_a_key_pasted_as_a_reference_is_caught_on_the_page_and_by_goodall(driver_script: Path, tmp_path: Path) -> None:
    pasted_key = "sk-ant-api03-" + "goodalltestnotarealkey" * 3
    changes = [
        *new_eval_changes(tmp_path),
        ["uses_api_keys", True],
        ["secret_references", [{"name": "ANTHROPIC_API_KEY_RESEARCH", "reference": pasted_key}]],
    ]

    problems = json.loads(run_page_script(driver_script, fresh_page_document(), changes, "problems"))
    text = run_page_script(driver_script, fresh_page_document(), changes, "answers")

    assert "secret_references.0" in problems
    reading = read_answers_text(text, tmp_path / "goodall-answers.toml", CODE)
    assert isinstance(reading, list)
    assert pasted_key not in json.dumps([failure.as_json() for failure in reading])


def test_the_page_shows_a_problem_for_each_answer_goodall_would_refuse(driver_script: Path, tmp_path: Path) -> None:
    changes = [
        *new_eval_changes(tmp_path),
        ["new_eval.name", "Goodall Pilot"],
        ["new_eval.repositories", ["https://github.com/your-org/goodall-pilot-app.git"]],
        ["contributor.display_name", "ada@example.org"],
        ["folders.log_directory", str(tmp_path / "evals" / "goodall-pilot-records" / "logs")],
    ]

    problems = json.loads(run_page_script(driver_script, fresh_page_document(), changes, "problems"))
    text = run_page_script(driver_script, fresh_page_document(), changes, "answers")

    assert sorted(problems) == [
        "contributor.display_name",
        "folders.log_directory",
        "new_eval.name",
        "new_eval.repositories",
    ]
    reading = read_answers_text(text, tmp_path / "goodall-answers.toml", CODE)
    assert isinstance(reading, list)
    assert len(reading) == 4


def onboard_through_the_page(driver_script: Path, changes: list[list[Any]]) -> tuple[int, str]:
    """Run plain `goodall onboard`; the person presses Finish setup after making `changes` on the page."""

    def press_finish_setup(command: Command) -> ScriptedAnswer:
        page_text = Path(command.arguments[1]).read_text()
        match = re.search(
            r'<script type="application/json" id="goodall-starting-answers">(.*?)</script>', page_text, re.DOTALL
        )
        assert match is not None
        starting = json.loads(match.group(1))
        downloads = Path(os.environ["HOME"]) / "Downloads"
        downloads.mkdir(exist_ok=True)
        text = run_page_script(driver_script, starting, changes, "answers")
        (downloads / "goodall-answers.toml").write_text(text)
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0)

    standard_output = io.StringIO()
    standard_error = io.StringIO()
    runner = FakeRunner(installed_programs=["goodall"])
    runner.answer_with("open", press_finish_setup)
    exit_code = cli.main(
        ["onboard"], terminal=Terminal(standard_output, standard_error), runner=runner, clock=lambda: NOW
    )
    return exit_code, standard_output.getvalue() + standard_error.getvalue()


def test_running_again_and_accepting_the_defaults_changes_nothing(
    driver_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_page, "pause", lambda seconds: None)
    first_changes = [
        *new_eval_changes(tmp_path),
        ["uses_api_keys", True],
        ["secret_references", [{"name": "ANTHROPIC_API_KEY_RESEARCH", "reference": REFERENCE}]],
        ["goodall_program", "/Users/ada/.local/bin/goodall"],
    ]
    first_exit_code, first_output = onboard_through_the_page(driver_script, first_changes)
    assert first_exit_code == exit_codes.SUCCESS, first_output
    (Path(os.environ["HOME"]) / "Downloads" / "goodall-answers.toml").unlink()

    exit_code, output = onboard_through_the_page(driver_script, [])

    assert exit_code == exit_codes.SUCCESS, output
    assert "Nothing to change" in output
    plan_rows = []
    for line in output.splitlines():
        if line.startswith(("create ", "change ", "unchanged ")):
            plan_rows.append(line)
    # Record repo, eval config, log directory, env file, local settings, hooks.
    assert len(plan_rows) == 6
    for row in plan_rows:
        assert row.startswith("unchanged "), row


def settings_page_document(tmp_path: Path) -> dict[str, Any]:
    """The page `goodall settings` writes for an account whose eval has two workstreams."""
    starting = setup_page.StartingAnswers(
        eval_choice="existing",
        eval_name="goodall-pilot",
        workstreams=["engineering", "research"],
        contributor_id="ada-lovelace",
        main_workstream="research",
        record_repo=str(tmp_path / "evals" / "goodall-pilot-records"),
        log_directory=str(tmp_path / "evals" / "goodall-pilot-logs.noindex"),
        known_workstreams=["engineering", "research"],
        settings_run=True,
    )
    return json.loads(json.dumps(setup_page.page_document(starting, CODE, "Goodall settings")))  # type: ignore[no-any-return]


def test_a_settings_page_saves_the_full_workstream_list_for_goodall_to_add_from(
    driver_script: Path, tmp_path: Path
) -> None:
    changes = [["known_workstreams", ["engineering", "research", "go-to-market"]]]

    text = run_page_script(driver_script, settings_page_document(tmp_path), changes, "answers")

    answers = read_back(text, tmp_path)
    assert answers.eval_choice == "existing"
    assert answers.existing_workstreams == ["engineering", "research", "go-to-market"]


def test_a_settings_page_with_no_change_still_saves_the_unchanged_list(driver_script: Path, tmp_path: Path) -> None:
    text = run_page_script(driver_script, settings_page_document(tmp_path), [], "answers")

    assert read_back(text, tmp_path).existing_workstreams == ["engineering", "research"]


def test_a_page_that_is_not_a_settings_page_leaves_workstreams_out_of_its_answers(
    driver_script: Path, tmp_path: Path
) -> None:
    document = settings_page_document(tmp_path)
    document["settings_run"] = False

    text = run_page_script(driver_script, document, [], "answers")

    assert read_back(text, tmp_path).existing_workstreams is None


def test_the_settings_page_shows_a_problem_for_a_workstream_goodall_would_refuse(
    driver_script: Path, tmp_path: Path
) -> None:
    for bad_name in ["Go To Market", "unknown"]:
        changes = [["known_workstreams", ["engineering", "research", bad_name]]]

        problems = json.loads(run_page_script(driver_script, settings_page_document(tmp_path), changes, "problems"))

        assert "known_workstreams" in problems, bad_name


def test_the_whole_page_script_parses(tmp_path: Path) -> None:
    # The page's drawing code cannot run without a browser, but it can at
    # least be read by the same engine Safari uses: a syntax error anywhere
    # in it would leave the person a blank page.
    page = setup_page.write_setup_page(tmp_path, setup_page.starting_answers_from_this_account(), CODE)
    scripts = re.findall(r"<script>(.*?)</script>", page.read_text(), re.DOTALL)
    assert len(scripts) == 2
    check = tmp_path / "parse-check.js"
    check.write_text(
        "function pageScripts() {\n" + "\n".join(scripts) + '\n}\nfunction run(argv) { return "parsed"; }\n'
    )

    completed = subprocess.run(
        ["osascript", "-l", "JavaScript", str(check)], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "parsed"
