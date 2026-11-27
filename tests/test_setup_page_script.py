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

from goodall import cli, exit_codes, setup_page, system_places
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
  if (input.ask === "changes") {
    return JSON.stringify(changesGoodallMakes(state, input.starting.places));
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
        # Reading the page from the eval account, made by hand.
        ["eval_account.choice", "already_made"],
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


# The eval account question: on the dedicated, new-eval path the page asks
# whether goodall creates the eval account (the administrator install,
# docs/administrator-install.md) or the person already made it and is
# reading the page from it.

EVAL_ACCOUNT_NAME = "goodall-eval"
ADMINISTRATOR_ACCOUNT_NAME = "goodall-administrator"
SUDO = "/usr/bin/sudo"
SYSADMINCTL = "/usr/sbin/sysadminctl"


def folders_in_the_new_account() -> list[list[Any]]:
    """Folders written with ~/, which at the eval account's first sign-in means that account's home."""
    return [
        ["folders.record_repo", "~/evals/goodall-pilot-records"],
        ["folders.log_directory", "~/evals/goodall-pilot-logs.noindex"],
    ]


def create_the_eval_account(tmp_path: Path) -> list[list[Any]]:
    """What a person types on a fresh page to have goodall create the eval account."""
    return [
        *new_eval_changes(tmp_path),
        *folders_in_the_new_account(),
        ["eval_account.choice", "create"],
        ["eval_account.name", EVAL_ACCOUNT_NAME],
    ]


def test_choosing_to_create_the_eval_account_saves_its_name(driver_script: Path, tmp_path: Path) -> None:
    text = run_page_script(driver_script, fresh_page_document(), create_the_eval_account(tmp_path), "answers")

    answers = read_back(text, tmp_path)
    assert answers.eval_account is not None
    assert answers.eval_account.create is True
    assert answers.eval_account.name == EVAL_ACCOUNT_NAME
    assert answers.record_repo.as_written == "~/evals/goodall-pilot-records"


def test_an_eval_account_already_made_saves_no_eval_account_section(driver_script: Path, tmp_path: Path) -> None:
    changes = [*new_eval_changes(tmp_path), ["eval_account.choice", "already_made"]]

    text = run_page_script(driver_script, fresh_page_document(), changes, "answers")

    assert "[eval_account]" not in text
    assert read_back(text, tmp_path).eval_account is None


def test_the_page_asks_the_eval_account_question_only_for_a_new_eval_in_an_eval_account(
    driver_script: Path, tmp_path: Path
) -> None:
    unanswered = [change for change in new_eval_changes(tmp_path) if change[0] != "eval_account.choice"]
    mixed = [*unanswered, ["account_mode", "mixed"], ["eval_account.choice", "create"]]

    dedicated_problems = json.loads(run_page_script(driver_script, fresh_page_document(), unanswered, "problems"))
    mixed_problems = json.loads(run_page_script(driver_script, fresh_page_document(), mixed, "problems"))
    mixed_text = run_page_script(driver_script, fresh_page_document(), mixed, "answers")

    assert list(dedicated_problems) == ["eval_account.choice"]
    assert mixed_problems == {}
    assert "[eval_account]" not in mixed_text


def test_the_page_refuses_an_account_name_goodall_would_refuse(driver_script: Path, tmp_path: Path) -> None:
    for bad_name in ["", "Goodall Eval", "1goodall", "goodall-", "goodall--eval", ADMINISTRATOR_ACCOUNT_NAME]:
        changes = [*create_the_eval_account(tmp_path), ["eval_account.name", bad_name]]
        document = fresh_page_document()
        document["this_account_name"] = ADMINISTRATOR_ACCOUNT_NAME

        problems = json.loads(run_page_script(driver_script, document, changes, "problems"))

        assert list(problems) == ["eval_account.name"], bad_name
        if bad_name != ADMINISTRATOR_ACCOUNT_NAME:
            text = run_page_script(driver_script, document, changes, "answers")
            reading = read_answers_text(text, tmp_path / "goodall-answers.toml", CODE)
            assert isinstance(reading, list), bad_name


def test_folders_for_an_account_goodall_creates_must_be_written_from_its_home(
    driver_script: Path, tmp_path: Path
) -> None:
    changes = [*create_the_eval_account(tmp_path), ["folders.record_repo", str(tmp_path / "records")]]

    problems = json.loads(run_page_script(driver_script, fresh_page_document(), changes, "problems"))
    text = run_page_script(driver_script, fresh_page_document(), changes, "answers")

    assert list(problems) == ["folders.record_repo"]
    assert "~/" in problems["folders.record_repo"]
    reading = read_answers_text(text, tmp_path / "goodall-answers.toml", CODE)
    assert isinstance(reading, list)
    assert [failure.kind for failure in reading] == ["answers_eval_account_folder_outside_its_home"]


def test_creating_the_eval_account_leaves_out_this_account_s_goodall_for_the_hooks(
    driver_script: Path, tmp_path: Path
) -> None:
    changes = [
        *create_the_eval_account(tmp_path),
        ["goodall_program", "/Users/goodall-administrator/.local/bin/goodall"],
    ]

    answers = read_back(run_page_script(driver_script, fresh_page_document(), changes, "answers"), tmp_path)

    # The eval account's hooks run the goodall installed for every account.
    assert answers.goodall_program is None


def onboard_through_the_page_as_an_administrator(
    driver_script: Path, changes: list[list[Any]], groups: str = "staff everyone admin"
) -> tuple[int, str, FakeRunner]:
    """Run plain `goodall onboard` in a pretend administrator account; the person presses Finish setup."""

    def press_finish_setup(command: Command) -> ScriptedAnswer:
        page_text = Path(command.arguments[1]).read_text()
        match = re.search(
            r'<script type="application/json" id="goodall-starting-answers">(.*?)</script>', page_text, re.DOTALL
        )
        assert match is not None
        starting = json.loads(match.group(1))
        downloads = Path(os.environ["HOME"]) / "Downloads"
        downloads.mkdir(exist_ok=True)
        (downloads / "goodall-answers.toml").write_text(run_page_script(driver_script, starting, changes, "answers"))
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0)

    def pretend_sudo(command: Command) -> ScriptedAnswer:
        # Only what the real command would do to the isolated folders of tests/conftest.py.
        arguments = command.arguments[1:]
        if arguments[0] == SYSADMINCTL:
            (system_places.USERS_FOLDER / arguments[arguments.index("-addUser") + 1]).mkdir()
        elif "-d" in arguments:
            Path(arguments[-1]).mkdir(parents=True, exist_ok=True)
        else:
            shutil.copyfile(arguments[-2], arguments[-1])
            Path(arguments[-1]).chmod(int(arguments[arguments.index("-m") + 1], 8))
        return ScriptedAnswer(outcome=CommandOutcome.COMPLETED, exit_code=0)

    standard_output = io.StringIO()
    runner = FakeRunner(installed_programs=["goodall"])
    runner.answer_with("open", press_finish_setup)
    runner.answer(["/usr/bin/id", "-Gn"], standard_output=groups + "\n")
    runner.answer(["/usr/bin/dscl", ".", "-list", "/Users"], standard_output="root\nnobody\n")
    runner.answer_with(SUDO, pretend_sudo)
    exit_code = cli.main(
        ["onboard"], terminal=Terminal(standard_output, standard_output), runner=runner, clock=lambda: NOW
    )
    return exit_code, " ".join(standard_output.getvalue().split()), runner


@pytest.fixture
def administrator_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pretend administrator account, with goodall and the menu bar icon installed for every account."""
    places = system_places
    monkeypatch.setattr(places, "current_account_name", lambda: ADMINISTRATOR_ACCOUNT_NAME)
    monkeypatch.setattr(places, "ROOT_USER_ID", os.getuid())
    monkeypatch.setattr(setup_page, "pause", lambda seconds: None)
    goodall = places.installed_goodall()
    goodall.parent.mkdir(parents=True)
    goodall.write_text("#!/bin/sh\n")
    goodall.chmod(0o755)
    helper = places.menu_bar_icon_program()
    helper.parent.mkdir(parents=True)
    helper.write_text("")


def test_creating_the_eval_account_on_the_page_runs_the_administrator_install(
    driver_script: Path, tmp_path: Path, administrator_mac: None
) -> None:
    exit_code, output, runner = onboard_through_the_page_as_an_administrator(
        driver_script, create_the_eval_account(tmp_path)
    )

    assert exit_code == exit_codes.SUCCESS, output
    account_creations = []
    for command in runner.calls:
        if command.arguments[:2] == [SUDO, SYSADMINCTL]:
            account_creations.append(command.arguments)
    assert len(account_creations) == 1
    assert account_creations[0][2:4] == ["-addUser", EVAL_ACCOUNT_NAME]
    staged = system_places.staged_answers_file().read_text()
    assert f'name = "{EVAL_ACCOUNT_NAME}"' in staged
    # Nothing is set up in the administrator account itself.
    assert not (Path(os.environ["GOODALL_CONFIG_DIR"]) / "local-settings.toml").exists()
    assert f"sign in as {EVAL_ACCOUNT_NAME}" in output


def test_a_refused_administrator_install_keeps_the_page_waiting(
    driver_script: Path, tmp_path: Path, administrator_mac: None
) -> None:
    exit_code, output, runner = onboard_through_the_page_as_an_administrator(
        driver_script, create_the_eval_account(tmp_path), groups="staff everyone"
    )

    assert "cannot run in this macOS user account" in output
    assert "goodall keeps waiting" in output
    for command in runner.calls:
        assert command.arguments[0] != SUDO, command.as_typed
    # Nobody pressed Finish setup again, so the wait ran out.
    assert exit_code == exit_codes.ERROR


def test_the_finish_step_lists_the_new_account_s_files_not_this_account_s(
    driver_script: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOODALL_CONFIG_DIR", str(tmp_path / "administrator-config"))
    document = fresh_page_document()

    rows = json.loads(run_page_script(driver_script, document, create_the_eval_account(tmp_path), "changes"))

    places = [row[0] for row in rows]
    assert places[:3] == [
        str(system_places.STAGING_FOLDER),
        str(system_places.LAUNCH_AGENTS_FOLDER),
        f"{system_places.USERS_FOLDER}/{EVAL_ACCOUNT_NAME}",
    ]
    assert "~/.config/goodall/local-settings.toml" in places
    assert "~/.claude/settings.json" in places
    # Nothing is set up in the account the page is open in.
    for place in places:
        assert "administrator-config" not in place
    assert "password" in rows[2][1]
