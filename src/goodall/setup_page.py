"""Plain `goodall onboard`: ask the setup questions on a page in the browser, then set up from the answers.

ADR 0012 is the design: goodall writes a single self-contained setup page
with a one-time code in it, opens it, and waits. The person answers on the
page and presses its one button, Finish setup; the page hands the browser an
answers file to save; this module finds that file, accepts it only if it
carries the same code, and passes it to goodall.onboard, which shows the
plan, makes the changes and reports, exactly as `goodall onboard --answers`
does. The terminal only prints; it never asks.

cli.main calls `run_setup_page_onboarding` when `goodall onboard` has no `--answers`. A later
`goodall settings` (Beads goodall-ayy.13) should call
`ask_on_setup_page(...)` with `starting_answers_from_this_account()`, the
same pair onboarding uses, giving its own `page_title`.

Decisions a reader would otherwise reverse-engineer:

- Where the page is written. In a new folder made by `tempfile.mkdtemp`,
  which on macOS is inside this account's own temporary folder and can be
  opened by this account only; the page itself is readable by this account
  only too, because it carries the one-time code and any secret references
  already in the env file. The folder is removed when the command ends,
  however it ends, Control-C included. A browser that already has the page
  open keeps it.
- Where the answers file arrives, and how it is found. A page opened from a
  file can only hand the browser a download: there is nothing listening to
  send it to, and the File System Access API, which could write to a
  folder goodall chose, is missing from Safari and Firefox. Every browser
  saves downloads to the Downloads folder unless the person changed that or
  it asks each time, so goodall looks in `~/Downloads`, once a second, for
  a file named `goodall-answers` followed by anything a browser adds to
  tell a second download from the first (`goodall-answers (1).toml`,
  `goodall-answers-2.toml`), ending in `.toml`, or in `.toml.txt` for a
  browser that adds its own ending to text. What is not robust: a browser
  set to save somewhere else. The page and the terminal both say to move
  the file into Downloads, or to run `goodall onboard --answers` with its
  path, which needs no code. Goodall cannot read where each browser saves.
- Which file counts. Only one carrying this run's one-time code. A file a
  browser is still writing, and an answers file left from an earlier run,
  carry no code or another one and are passed over; files that were
  already there when the page opened are mentioned once, so a person is
  not puzzled by them. Each file is read again only when its size or time
  changes, so a download that lands in pieces is read once it is whole.
- What happens when the answers are refused. If the file breaks a rule, or
  the machine says no (a record repo folder that is not empty, say), goodall
  shows why and keeps waiting: the page is still open with every answer in
  it, so the person changes the one answer and presses Finish setup again,
  which saves a second file with the same code. Once a run has made changes,
  or found nothing to change, or stopped part way, it ends.
- How long it waits. Up to WAIT_LIMIT_SECONDS, counted in looks at the
  folder rather than by the clock, so time a Mac spends asleep does not
  count against the person. It prints a reminder every REMINDER_SECONDS.
- Control-C stops it cleanly: the page is removed, nothing is left half
  written by this module, and it says whether onboarding had started making
  changes (each change is made in one step and running again is safe).
- What goodall does with the answers file. Nothing: it is the person's,
  saved by their browser. It holds no secret values (goodall refuses a file
  that does), and goodall says where it is, that it can be used again with
  `goodall onboard --answers`, and that it can be deleted. Goodall removes
  only what it wrote itself: the page and its folder.
- Running it again. The page opens with the answers already on this Mac
  (`starting_answers_from_this_account`): local settings, the env file's
  references, the eval config's workstreams and the program the hooks run.
  An account already set up starts on "use the eval already on this Mac",
  so pressing Finish setup without changing anything leaves every file
  unchanged.
- `--json` needs `--answers`. The setup page is for a person at a browser;
  an agent or a script writes the answers file itself. Waiting would
  otherwise print one JSON document per refused attempt.

The page's script has a portable part, setup_page_answers.js, with no page
code in it, which tests/test_setup_page_script.py runs with the JavaScript
macOS comes with. The page decides nothing goodall does not check again:
every answers file goes through goodall.answers_file and goodall.onboard.
"""

import json
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

from goodall import exit_codes
from goodall.answers_file import FORMAT_DOCUMENT, NEW_EVAL, read_answers_file
from goodall.claude_code_checks import (
    claude_user_settings_file,
    goodall_hook_commands_by_event,
    is_full_path,
    read_claude_settings,
)
from goodall.env_files import EnvFileReading, read_env_file
from goodall.eval_config import read_eval_config
from goodall.local_settings import (
    DEDICATED_ACCOUNT_MODE,
    LocalSettings,
    RelativeConfigDirectoryError,
    find_local_settings_location,
    read_local_settings_file,
)
from goodall.onboard import OnboardingReport, onboarding_report_from_answers, report_exit_code, show_report
from goodall.runner import Command, Runner
from goodall.secrets_checks import ENV_FILE_NAME
from goodall.secrets_manager import is_well_formed_secret_reference
from goodall.terminal import Failure, Terminal

# The name the page asks the browser to save the answers file under.
ANSWERS_FILE_NAME = "goodall-answers.toml"

# The start of every name a browser may give that file, and the endings
# that count (see the module docstring).
ANSWERS_FILE_NAME_START = "goodall-answers"
ANSWERS_FILE_ENDINGS = [".toml", ".toml.txt"]

# The largest answers file goodall reads. A real one is a few hundred bytes.
LARGEST_ANSWERS_FILE_BYTES = 1_000_000

# The page's own file name, inside the folder made for it.
PAGE_FILE_NAME = "goodall-setup.html"

# Readable and writable by this macOS user account only.
PRIVATE_FILE = 0o600

# How often goodall looks in the Downloads folder, how long it waits in all,
# and how often it reminds the person it is still waiting.
POLL_SECONDS = 1.0
WAIT_LIMIT_SECONDS = 60 * 60.0
REMINDER_SECONDS = 10 * 60.0

# The marked places in setup_page.html that goodall fills in.
STARTING_ANSWERS_PLACE = "GOODALL_STARTING_ANSWERS"
PORTABLE_PART_PLACE = "/*GOODALL_PORTABLE_PART*/"

# The page's heading when goodall onboard opens it.
ONBOARD_PAGE_TITLE = "Setting up your eval"

# What a new eval starts with, as the prototype had it.
DEFAULT_WORKSTREAMS = ["engineering", "research", "go-to-market"]

# The kind of failure for an answers file carrying another page's code.
ONE_TIME_CODE_MISMATCH = "answers_one_time_code_mismatch"


def pause(seconds: float) -> None:
    """Wait between two looks at the Downloads folder. Tests replace it, so they never wait."""
    time.sleep(seconds)


@dataclass(frozen=True)
class StartingAnswers:
    """The answers the setup page shows when it opens, before the person changes anything.

    Folders are as a person would write them (`~/evals/...`), since the page
    shows them. `known_workstreams` are the existing eval's workstreams, for
    the page to offer as the main workstream; empty when there is no
    readable eval config. `goodall_program` is the goodall the hooks already
    run, carried through so a run with unchanged answers leaves the hooks
    unchanged; None means the one found on the PATH.
    """

    account_mode: str = DEDICATED_ACCOUNT_MODE
    eval_choice: str = NEW_EVAL
    eval_name: str = ""
    description: str = ""
    workstreams: list[str] = field(default_factory=lambda: list(DEFAULT_WORKSTREAMS))
    construct: str = ""
    activitywatch: bool = False
    repositories: list[str] = field(default_factory=list)
    contributor_id: str = ""
    display_name: str = ""
    main_workstream: str = ""
    record_repo: str = ""
    log_directory: str = ""
    secret_references: dict[str, str] = field(default_factory=dict)
    goodall_program: str | None = None
    known_workstreams: list[str] = field(default_factory=list)


def starting_answers_from_this_account() -> StartingAnswers:
    """The answers already on this Mac, for the page to start from; fresh defaults when there are none.

    Reads local settings, the env file, the eval config and Claude Code's
    settings, and changes nothing. Anything that cannot be read is simply
    not filled in: goodall checks every answer again when the page's file
    comes back, and says there what is wrong.
    """
    try:
        location = find_local_settings_location()
    except RelativeConfigDirectoryError:
        return StartingAnswers()
    settings = read_local_settings_file(location)
    if not isinstance(settings, LocalSettings):
        return StartingAnswers()

    known_workstreams: list[str] = []
    eval_name = ""
    eval_config = read_eval_config(settings.record_repo.path, settings.settings_file)
    if not isinstance(eval_config, list):
        known_workstreams = list(eval_config.workstreams)
        eval_name = eval_config.name

    return StartingAnswers(
        account_mode=settings.account_mode,
        eval_choice="existing",
        eval_name=eval_name,
        workstreams=list(known_workstreams),
        contributor_id=settings.contributor,
        main_workstream=settings.main_workstream,
        record_repo=settings.record_repo.as_written,
        log_directory=settings.log_directory.as_written,
        secret_references=references_in_env_file(location.config_directory / ENV_FILE_NAME, settings.secrets_manager),
        goodall_program=program_the_hooks_run(),
        known_workstreams=known_workstreams,
    )


def references_in_env_file(env_file: Path, secrets_manager: str) -> dict[str, str]:
    """Every well-formed secret reference in the env file, by variable; nothing else from it is used."""
    reading = read_env_file(env_file)
    references: dict[str, str] = {}
    if not isinstance(reading, EnvFileReading):
        return references
    for entry in reading.entries:
        if is_well_formed_secret_reference(secrets_manager, entry.value):
            references[entry.name] = entry.value
    return references


def program_the_hooks_run() -> str | None:
    """The one goodall, by full path, that every goodall hook in Claude Code's settings runs; else None."""
    claude_settings = read_claude_settings(claude_user_settings_file())
    if isinstance(claude_settings, Failure):
        return None
    programs = set()
    for hook_commands in goodall_hook_commands_by_event(claude_settings.get("hooks")).values():
        for hook_command in hook_commands:
            programs.add(hook_command.program)
    if len(programs) != 1:
        return None
    program = programs.pop()
    if not is_full_path(program):
        return None
    return program


def new_one_time_code() -> str:
    """A fresh code for one setup page: 16 hexadecimal characters, which no key format goodall knows matches."""
    return secrets.token_hex(8)


def portable_script_text() -> str:
    """The page's portable part, setup_page_answers.js, as shipped inside goodall."""
    return resources.files("goodall").joinpath("setup_page_answers.js").read_text(encoding="utf-8")


def page_template_text() -> str:
    """The setup page with its two places still to fill in, setup_page.html."""
    return resources.files("goodall").joinpath("setup_page.html").read_text(encoding="utf-8")


def as_written_for_people(path: Path) -> str:
    """`path` with the home folder written as ~, as the page shows paths."""
    home = Path.home()
    if path == home or home in path.parents:
        return "~/" + str(path.relative_to(home))
    return str(path)


def downloads_folder() -> Path:
    """Where browsers save downloads unless the person has changed it."""
    return Path.home() / "Downloads"


def page_document(starting: StartingAnswers, one_time_code: str, page_title: str) -> dict[str, object]:
    """Everything the page's script reads when it opens, as one JSON document."""
    try:
        config_directory = find_local_settings_location().config_directory
    except RelativeConfigDirectoryError:
        config_directory = Path.home() / ".config" / "goodall"
    secret_references = []
    for name, reference in starting.secret_references.items():
        secret_references.append({"name": name, "reference": reference})
    return {
        "one_time_code": one_time_code,
        "page_title": page_title,
        "answers_file_name": ANSWERS_FILE_NAME,
        "downloads_folder": as_written_for_people(downloads_folder()),
        "places": {
            "local_settings": as_written_for_people(config_directory / "local-settings.toml"),
            "env_file": as_written_for_people(config_directory / ENV_FILE_NAME),
            "claude_settings": as_written_for_people(claude_user_settings_file()),
        },
        "known_workstreams": list(starting.known_workstreams),
        "answers": {
            "account_mode": starting.account_mode,
            "eval": starting.eval_choice,
            "new_eval": {
                "name": starting.eval_name,
                "description": starting.description,
                "workstreams": list(starting.workstreams),
                "construct": starting.construct,
                "activitywatch": starting.activitywatch,
                "repositories": list(starting.repositories),
            },
            "contributor": {
                "id": starting.contributor_id,
                "display_name": starting.display_name,
                "main_workstream": starting.main_workstream,
            },
            "folders": {"record_repo": starting.record_repo, "log_directory": starting.log_directory},
            "secret_references": secret_references,
            "goodall_program": starting.goodall_program,
        },
    }


def json_for_a_script_element(document: dict[str, object]) -> str:
    """`document` as JSON that cannot end the <script> element it is written into.

    Trap: a value holding "</script>" would end the element early and let the
    rest be read as page code. Writing every <, > and & as a \\u escape keeps
    the JSON the same to JSON.parse and leaves no tag in the page's text.
    """
    text = json.dumps(document, ensure_ascii=False, indent=1)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def write_setup_page(
    folder: Path, starting: StartingAnswers, one_time_code: str, page_title: str = ONBOARD_PAGE_TITLE
) -> Path:
    """Write the setup page into `folder`, readable by this account only, and return its path."""
    template = page_template_text()
    for place in [STARTING_ANSWERS_PLACE, PORTABLE_PART_PLACE]:
        if template.count(place) != 1:
            raise ValueError(f"setup_page.html must hold {place} exactly once")
    page_text = template.replace(
        STARTING_ANSWERS_PLACE, json_for_a_script_element(page_document(starting, one_time_code, page_title))
    )
    page_text = page_text.replace(PORTABLE_PART_PLACE, portable_script_text())
    page = folder / PAGE_FILE_NAME
    # Made private before anything is written into it.
    page.touch(mode=PRIVATE_FILE)
    page.chmod(PRIVATE_FILE)
    page.write_text(page_text, encoding="utf-8")
    return page


def answers_file_candidates(folder: Path) -> list[Path]:
    """The files in `folder` a browser may have saved the page's answers file as; none if there is no folder."""
    candidates: list[Path] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return candidates
    for entry in entries:
        if not entry.name.startswith(ANSWERS_FILE_NAME_START):
            continue
        if not has_answers_file_ending(entry.name):
            continue
        if not entry.is_file():
            continue
        candidates.append(entry)
    return candidates


def has_answers_file_ending(name: str) -> bool:
    """True when `name` ends as an answers file a browser saved does."""
    for ending in ANSWERS_FILE_ENDINGS:
        if name.endswith(ending):
            return True
    return False


def file_signature(path: Path) -> tuple[int, int] | None:
    """The size and modification time of `path`, to tell when it has changed; None when it cannot be read."""
    try:
        status = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return status.st_size, status.st_mtime_ns


@dataclass
class Waiting:
    """Where one run of the setup page has got to. Mutable on purpose: the waiting loop updates it.

    `seen` holds the signature each answers file had when it was last
    read, so an unchanged file is not read twice. `changes_started` is True
    once goodall.onboard has been handed answers, so Control-C can say
    whether anything may have changed.
    """

    one_time_code: str
    downloads: Path
    plan_only: bool
    seen: dict[Path, tuple[int, int]] = field(default_factory=dict)
    changes_started: bool = False


def run_setup_page_onboarding(
    terminal: Terminal, runner: Runner, clock: Callable[[], datetime], plan_only: bool, as_json: bool
) -> int:
    """Plain `goodall onboard`, as goodall.onboard.run_onboard calls it."""
    if as_json:
        terminal.show_failures([json_needs_answers_failure()])
        return exit_codes.USAGE_ERROR
    return ask_on_setup_page(terminal, runner, clock, starting_answers_from_this_account(), plan_only=plan_only)


def ask_on_setup_page(
    terminal: Terminal,
    runner: Runner,
    clock: Callable[[], datetime],
    starting: StartingAnswers,
    *,
    plan_only: bool = False,
    page_title: str = ONBOARD_PAGE_TITLE,
) -> int:
    """Open the setup page with `starting` filled in, wait for its answers file, and set up from it.

    Returns the exit code. The page and its folder are removed whatever
    happens; the answers file is left where the browser saved it.
    """
    waiting = Waiting(one_time_code=new_one_time_code(), downloads=downloads_folder(), plan_only=plan_only)
    page_folder = Path(tempfile.mkdtemp(prefix="goodall-setup-page-"))
    try:
        page = write_setup_page(page_folder, starting, waiting.one_time_code, page_title)
        earlier_files = note_earlier_answers_files(waiting)
        open_result = runner.run(Command(["open", str(page)]))
        show_opening(terminal, page, open_result.problem, earlier_files, waiting)
        return wait_for_answers(terminal, runner, clock, waiting)
    except KeyboardInterrupt:
        terminal.show_paragraph(stopped_words(waiting))
        return exit_codes.ERROR
    finally:
        shutil.rmtree(page_folder, ignore_errors=True)


def note_earlier_answers_files(waiting: Waiting) -> list[Path]:
    """Mark every answers file already in the Downloads folder as seen, and return them.

    They cannot be from this page, which is not open yet, so they are never
    read, unless one changes later.
    """
    earlier_files = []
    for candidate in answers_file_candidates(waiting.downloads):
        signature = file_signature(candidate)
        if signature is not None:
            waiting.seen[candidate] = signature
            earlier_files.append(candidate)
    return earlier_files


def show_opening(
    terminal: Terminal, page: Path, open_problem: str | None, earlier_files: list[Path], waiting: Waiting
) -> None:
    """Say what goodall did, what the person does now, and how to stop."""
    downloads = as_written_for_people(waiting.downloads)
    if open_problem is None:
        terminal.show_paragraph(f"Goodall opened the setup page in your browser: {page}")
    else:
        terminal.show_warnings([page_not_opened_warning(page, open_problem)])
    terminal.show_paragraph(
        "Answer its questions and press Finish setup. Your browser saves your answers as "
        f"{ANSWERS_FILE_NAME} in {downloads}, and goodall picks the file up here, shows every change it will "
        "make, and makes them. If your browser asks where to save, choose that folder."
    )
    if earlier_files:
        names = []
        for earlier_file in earlier_files:
            names.append(earlier_file.name)
        terminal.show_paragraph(
            f"Goodall leaves alone the answers files already in {downloads} ({', '.join(names)}): they came from "
            "an earlier setup page."
        )
    minutes = round(WAIT_LIMIT_SECONDS / 60)
    terminal.show_paragraph(f"Waiting up to {minutes} minutes. Press Control-C to stop without changing anything.")


def wait_for_answers(terminal: Terminal, runner: Runner, clock: Callable[[], datetime], waiting: Waiting) -> int:
    """Look for this page's answers file until one is used, or the wait runs out."""
    waited_seconds = 0.0
    next_reminder = REMINDER_SECONDS
    while waited_seconds < WAIT_LIMIT_SECONDS:
        for candidate in answers_file_candidates(waiting.downloads):
            signature = file_signature(candidate)
            if signature is None or waiting.seen.get(candidate) == signature:
                continue
            waiting.seen[candidate] = signature
            report = try_answers_file(terminal, runner, clock, waiting, candidate)
            if report is None:
                continue
            if report.outcome == "rejected":
                show_keep_waiting(terminal)
                continue
            show_answers_file_left(terminal, candidate)
            return report_exit_code(report)
        pause(POLL_SECONDS)
        waited_seconds += POLL_SECONDS
        if waited_seconds >= next_reminder and waited_seconds < WAIT_LIMIT_SECONDS:
            minutes_left = round((WAIT_LIMIT_SECONDS - waited_seconds) / 60)
            terminal.show_paragraph(
                f"Still waiting for {ANSWERS_FILE_NAME} in {as_written_for_people(waiting.downloads)} "
                f"({minutes_left} minutes left). Press Control-C to stop."
            )
            next_reminder += REMINDER_SECONDS
    terminal.show_failures([wait_ran_out_failure(waiting.downloads)])
    return exit_codes.ERROR


def try_answers_file(
    terminal: Terminal, runner: Runner, clock: Callable[[], datetime], waiting: Waiting, candidate: Path
) -> OnboardingReport | None:
    """Use `candidate` if it carries this page's code; return the report, or None when it is not this page's."""
    reading = read_answers_file(candidate, waiting.one_time_code)
    if isinstance(reading, list):
        if len(reading) == 1 and reading[0].kind == ONE_TIME_CODE_MISMATCH:
            return None
        report = OnboardingReport(
            answers_file=candidate, plan_only=waiting.plan_only, outcome="rejected", failures=reading
        )
        show_report(terminal, report, as_json=False)
        return report
    waiting.changes_started = True
    report = onboarding_report_from_answers(terminal, runner, clock, reading, plan_only=waiting.plan_only)
    waiting.changes_started = False
    return report


def show_keep_waiting(terminal: Terminal) -> None:
    """After refused answers: the page is still open, so say how to try again."""
    terminal.show_paragraph(
        "Nothing was changed. Change that answer on the setup page, which is still open, and press Finish setup "
        "again; goodall keeps waiting. Press Control-C to stop."
    )


def show_answers_file_left(terminal: Terminal, answers_file: Path) -> None:
    """Say where the answers file is and that goodall leaves it there."""
    terminal.show_paragraph(
        f"Your answers are in {answers_file}, which goodall leaves where your browser saved it. It holds no "
        "secrets. Keep it to set up the same way again with goodall onboard --answers, or delete it."
    )


def stopped_words(waiting: Waiting) -> str:
    """What Control-C ends with, depending on whether onboarding had begun to make changes."""
    if waiting.changes_started:
        return (
            "Stopped while goodall onboard was making changes. Each change is made in one step, so nothing is "
            "half written; run goodall onboard again, which is safe, to finish."
        )
    return "Stopped. Nothing was changed. Run goodall onboard again when you are ready."


# Failures and warnings. Each says what happened, why and what to do next.


def json_needs_answers_failure() -> Failure:
    """The failure for `goodall onboard --json` without an answers file."""
    return Failure(
        kind="onboard_json_needs_answers",
        what_happened="goodall onboard --json needs --answers. Nothing was changed.",
        why=(
            "Without --answers, goodall onboard opens the setup page for a person to answer in a browser, which an "
            "agent or a script cannot do."
        ),
        what_to_do_next=f"Write an answers file as {FORMAT_DOCUMENT} shows, and pass it with --answers.",
        commands_to_try=("goodall onboard --answers ~/goodall-answers.toml --plan --json",),
    )


def page_not_opened_warning(page: Path, problem: str) -> Failure:
    """The warning when `open` could not show the page."""
    return Failure(
        kind="setup_page_not_opened",
        what_happened=f"Goodall could not open the setup page in your browser: {problem}.",
        why="The page is written, and goodall is waiting for its answers all the same.",
        what_to_do_next=f"Open this file in your browser yourself, by double-clicking it in Finder: {page}",
    )


def wait_ran_out_failure(downloads: Path) -> Failure:
    """The failure when no answers file from this page arrived in time."""
    minutes = round(WAIT_LIMIT_SECONDS / 60)
    return Failure(
        kind="setup_page_wait_ran_out",
        what_happened=(
            f"Goodall waited {minutes} minutes and no answers file from the setup page arrived in "
            f"{as_written_for_people(downloads)}. Nothing was changed."
        ),
        why=(
            "Goodall stops waiting after a while, so a forgotten goodall onboard does not wait forever. The page "
            "has no server behind it, so goodall can learn of your answers only from the file your browser saves."
        ),
        what_to_do_next=(
            "Run goodall onboard again. If your browser saved the answers file somewhere else, run goodall onboard "
            "--answers with that file's path instead: it is set up from that file without the page."
        ),
        commands_to_try=("goodall onboard", "goodall onboard --answers ~/Desktop/goodall-answers.toml"),
    )
