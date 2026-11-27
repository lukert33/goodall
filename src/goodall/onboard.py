"""`goodall onboard`: set up this macOS user account for an eval from an answers file.

`goodall onboard --answers <file>` is the unattended path (ADR 0012): the
answers file is read and checked (goodall.answers_file), every change it
calls for is worked out and shown, and only then are those changes made.
Plain `goodall onboard` opens the setup page instead (goodall.setup_page),
which hands the answers file the page saved to
`onboarding_report_from_answers`, the function `--answers` uses too, so both
paths write the same files the same way. docs/answers-file.md documents the
format and what each run does; docs/setup-by-hand.md is the by-hand
procedure this automates.

What it writes, in this order (each is one `PlannedChange`):

1. The record repo: a new folder made into a git repository with no
   remote, for a new eval. Goodall never adds a remote and never pushes
   (ADR 0023).
2. The eval config, `eval.toml`, for a new eval. For an existing eval the
   eval.toml already in the record repo is left exactly as it is.
3. The construct file, `CONSTRUCT.md`, when the answers give a construct.
4. A commit of those two files, and only those.
5. The log directory, readable by this account only, whose name should end
   in `.noindex` (ADR 0019).
6. The env file, `.env` beside local settings, readable by this account
   only (Q33), holding only secret references (ADR 0010).
7. Local settings, `local-settings.toml`.
8. The Claude Code hooks, in Claude Code's user settings, merged with
   whatever else is there.

Local settings come late and the hooks last on purpose: until local
settings exist, goodall does not treat the account as set up, and once the
hooks are in, an eval account refuses prompts until a work block starts.

How confirmation works without a question: the terminal never asks
(ADR 0012), so supplying the answers file is the confirmation. The whole
plan is printed before anything changes, and `--plan` prints the same plan
and changes nothing, so a person or a script can look first.

Safe to run again, and what that means for each kind of file:

- A file that is not there is written whole, by goodall's own TOML writer
  (goodall.toml_writer).
- A file that already says what the answers say is left untouched, and the
  plan says "unchanged".
- Local settings and the env file are goodall's own files in this account,
  but a person may have edited them. A setting that differs has its one
  line replaced; a setting that is missing is appended in a block marked as
  added by goodall onboard; every other line, comments included, stays as
  it was. The plan names each setting it changes.
- `eval.toml` and `CONSTRUCT.md` are committed records a person may have
  edited on purpose, so goodall never rewrites one that exists. If one
  differs from the answers, the plan says so, as a warning, and leaves it.
- Claude Code's settings file is JSON, which has no comments. Goodall reads
  it, adds or replaces only its own hook commands, and writes it back with
  every other setting kept; the plan says which hooks it adds or replaces.

Every file goodall would write is first read back through that file's own
reader (goodall.local_settings, goodall.eval_config), so onboarding can never
write a file that `goodall status` or `goodall doctor` would refuse.

Two kinds of step are not done here, on purpose: ActivityWatch's own port
settings stay a by-hand step (docs/setup-by-hand.md, Part 3), and the
shared env file is not written, because what it should hold is not decided.
"""

import copy
import json
import os
import shlex
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from goodall import exit_codes
from goodall.answers_file import NEW_EVAL, Answers, NewEval, read_answers_file
from goodall.claude_code_checks import (
    claude_user_settings_file,
    is_full_path,
    read_claude_settings,
    read_goodall_hook_command,
)
from goodall.env_files import EnvEntry, EnvFileNotRead, read_env_file, variable_as_shown
from goodall.eval_config import (
    EVAL_CONFIG_FILE_NAME,
    EvalConfig,
    check_eval_config_table,
    contributor_not_in_roster_failure,
    find_this_contributor,
    main_workstream_is_known,
    main_workstream_unknown_failure,
    missing_activitywatch_settings,
    read_eval_config,
)
from goodall.hook import KNOWN_EVENTS
from goodall.local_settings import (
    LocalSettings,
    LocalSettingsLocation,
    RelativeConfigDirectoryError,
    check_settings_table,
    expand_home_folder,
    find_local_settings_location,
    relative_config_directory_failure,
)
from goodall.record_repo import COMMIT_TIMEOUT_SECONDS, IDENTITY_SETTINGS, run_git
from goodall.runner import Runner
from goodall.secrets_checks import ENV_FILE_NAME, OTHER_ACCOUNTS_PERMISSION_BITS
from goodall.terminal import Failure, Terminal
from goodall.toml_writer import TomlDocument, TomlValue, append_marked_block, render_toml, replace_top_level_line

# The construct file's name at the top of the record repo.
CONSTRUCT_FILE_NAME = "CONSTRUCT.md"

# The end of a log directory's name that keeps Spotlight out (ADR 0019).
SPOTLIGHT_EXCLUDED_ENDING = ".noindex"

# Permissions: folders and files only this account may open, and the usual
# permissions for files anyone on the Mac may read, which the record repo's
# files are, like any repository's.
PRIVATE_FOLDER = 0o700
PRIVATE_FILE = 0o600
ORDINARY_FILE = 0o644

# Claude Code's matcher meaning "every tool", which PostToolUse needs.
EVERY_TOOL_MATCHER = "*"

# The program the hooks run, found on the PATH when the answers do not name one.
GOODALL_PROGRAM_NAME = "goodall"


class ChangeAction(StrEnum):
    """What one planned change does. The values are what `--json` shows."""

    CREATE = "create"
    CHANGE = "change"
    UNCHANGED = "unchanged"


# The steps a planned change is made of. Each is a small record, so the plan
# holds exactly what will be written, and applying writes only that.


@dataclass(frozen=True)
class MakeFolder:
    """Make `path`, and any missing folder above it, unless it already exists; set `mode` on `path` itself."""

    path: Path
    mode: int


@dataclass(frozen=True)
class MakeGitRepository:
    """Run `git init` in `folder`."""

    folder: Path


@dataclass(frozen=True)
class WriteFile:
    """Write `text` to `path` with permissions `mode`, replacing the file in one step."""

    path: Path
    text: str
    mode: int


@dataclass(frozen=True)
class SetPermissions:
    """Set the permissions of `path` to `mode`."""

    path: Path
    mode: int


@dataclass(frozen=True)
class CommitFiles:
    """Stage and commit `relative_paths`, and only them, in the record repo."""

    record_repo: Path
    relative_paths: list[str]
    message: str


Step = MakeFolder | MakeGitRepository | WriteFile | SetPermissions | CommitFiles


@dataclass(frozen=True)
class PlannedChange:
    """One thing onboarding would set up, and how.

    - `id` is stable, such as "local_settings", for agents; `what` names it
      for a person, such as "Local settings".
    - `details` are short sentences saying what exactly changes, such as
      "main_workstream changes to engineering". They never hold a secret
      reference: env file lines are named by their variable only.
    - `steps` are what applying it does; an unchanged item has none.
    """

    id: str
    what: str
    path: Path
    action: ChangeAction
    details: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def as_json(self, done: bool) -> dict[str, object]:
        """This change as `--json` shows it; `done` says whether this run made it."""
        return {
            "id": self.id,
            "what": self.what,
            "path": str(self.path),
            "action": str(self.action),
            "details": list(self.details),
            "done": done,
        }


@dataclass(frozen=True)
class OnboardingPlan:
    """Every change the answers call for, in the order they are made, and what to warn about."""

    answers: Answers
    changes: list[PlannedChange]
    warnings: list[Failure]

    @property
    def changes_to_make(self) -> list[PlannedChange]:
        """The changes that are not already true."""
        to_make = []
        for change in self.changes:
            if change.action != ChangeAction.UNCHANGED:
                to_make.append(change)
        return to_make


@dataclass(frozen=True)
class OnboardingReport:
    """What one run of goodall onboard came to, rendered once as text or as JSON.

    `outcome` is one of: "applied" (changes made), "nothing_to_change",
    "planned" (`--plan`: shown, not made), "rejected" (the answers or the
    machine stopped it before anything changed) and "stopped" (a change
    failed part way; what was done is marked).
    """

    answers_file: Path | None
    plan_only: bool
    outcome: str
    plan: OnboardingPlan | None = None
    done_ids: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        """The stable `--json` document."""
        changes = []
        warnings = []
        if self.plan is not None:
            for change in self.plan.changes:
                changes.append(change.as_json(change.id in self.done_ids))
            for warning in self.plan.warnings:
                warnings.append(warning.as_json())
        failures = []
        for failure in self.failures:
            failures.append(failure.as_json())
        answers_file = None
        if self.answers_file is not None:
            answers_file = str(self.answers_file)
        return {
            "answers_file": answers_file,
            "plan_only": self.plan_only,
            "outcome": self.outcome,
            "changes": changes,
            "warnings": warnings,
            "failures": failures,
        }


def run_onboard(
    terminal: Terminal,
    runner: Runner,
    clock: Callable[[], datetime],
    answers_file: Path,
    plan_only: bool,
    as_json: bool,
) -> int:
    """`goodall onboard --answers <file>`, as cli.main calls it. Without --answers, goodall.setup_page runs."""
    return onboard_from_answers_file(terminal, runner, clock, answers_file, plan_only=plan_only, as_json=as_json)


def onboard_from_answers_file(
    terminal: Terminal,
    runner: Runner,
    clock: Callable[[], datetime],
    answers_file: Path,
    *,
    plan_only: bool = False,
    as_json: bool = False,
    expected_one_time_code: str | None = None,
) -> int:
    """Read an answers file, show the plan, and make the changes unless `plan_only`; return the exit code.

    The one way in for both setup paths. `goodall onboard --answers` passes
    no `expected_one_time_code`; the setup page's waiting command passes the
    code it wrote into the page, so only the file that page saved is used.
    """
    reading = read_answers_file(answers_file, expected_one_time_code)
    if isinstance(reading, list):
        report = OnboardingReport(answers_file=answers_file, plan_only=plan_only, outcome="rejected", failures=reading)
        show_report(terminal, report, as_json)
        return exit_codes.ERROR
    return onboard_from_answers(terminal, runner, clock, reading, plan_only=plan_only, as_json=as_json)


def onboard_from_answers(
    terminal: Terminal,
    runner: Runner,
    clock: Callable[[], datetime],
    answers: Answers,
    *,
    plan_only: bool = False,
    as_json: bool = False,
) -> int:
    """Show the plan for answers already read and checked, and make the changes unless `plan_only`."""
    report = onboarding_report_from_answers(terminal, runner, clock, answers, plan_only=plan_only, as_json=as_json)
    return report_exit_code(report)


def onboarding_report_from_answers(
    terminal: Terminal,
    runner: Runner,
    clock: Callable[[], datetime],
    answers: Answers,
    *,
    plan_only: bool = False,
    as_json: bool = False,
) -> OnboardingReport:
    """Do what `onboard_from_answers` does, and return the report instead of the exit code.

    The setup page's waiting command needs the outcome, not only the exit
    code: answers the machine refused ("rejected") can be corrected on the
    page that is still open, so it keeps waiting, while a change that failed
    part way ("stopped") ends the run.
    """
    planning = plan_onboarding(answers, runner, clock())
    if isinstance(planning, list):
        report = OnboardingReport(
            answers_file=answers.answers_file, plan_only=plan_only, outcome="rejected", failures=planning
        )
        show_report(terminal, report, as_json)
        return report
    plan = planning

    if not as_json:
        show_plan(terminal, plan)
    if plan_only:
        report = OnboardingReport(answers_file=answers.answers_file, plan_only=True, outcome="planned", plan=plan)
        show_report(terminal, report, as_json)
        return report
    if not plan.changes_to_make:
        report = OnboardingReport(
            answers_file=answers.answers_file, plan_only=False, outcome="nothing_to_change", plan=plan
        )
        show_report(terminal, report, as_json)
        return report

    done_ids, failure = apply_plan(plan, runner)
    if failure is not None:
        report = OnboardingReport(
            answers_file=answers.answers_file,
            plan_only=False,
            outcome="stopped",
            plan=plan,
            done_ids=done_ids,
            failures=[failure],
        )
        show_report(terminal, report, as_json)
        return report
    report = OnboardingReport(
        answers_file=answers.answers_file, plan_only=False, outcome="applied", plan=plan, done_ids=done_ids
    )
    show_report(terminal, report, as_json)
    return report


def report_exit_code(report: OnboardingReport) -> int:
    """0 when what was asked is done (or was only planned), 1 when it was refused or stopped part way."""
    if report.outcome in ["rejected", "stopped"]:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


# Planning: work out every change, touching nothing. Each `plan_...`
# function adds to `changes`, `warnings` and `failures` and changes no file;
# the only programs run are git's read-only questions.


@dataclass
class Planning:
    """What planning has found so far. Mutable on purpose: each step adds to it."""

    answers: Answers
    runner: Runner
    now: datetime
    changes: list[PlannedChange] = field(default_factory=list)
    warnings: list[Failure] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    # Files in the record repo that already say something other than the
    # answers, which goodall leaves alone and so must not commit either.
    files_left_as_they_are: list[Path] = field(default_factory=list)


def plan_onboarding(answers: Answers, runner: Runner, now: datetime) -> OnboardingPlan | list[Failure]:
    """Every change `answers` call for, or the failures that stop onboarding before it changes anything."""
    try:
        location = find_local_settings_location()
    except RelativeConfigDirectoryError:
        return [relative_config_directory_failure()]

    planning = Planning(answers=answers, runner=runner, now=now)
    record_repo_is_git_repository = plan_record_repo(planning)
    eval_config = plan_eval_config(planning, location)
    if answers.new_eval is not None:
        plan_construct(planning, answers.new_eval)
        plan_record_repo_commit(planning, answers.new_eval, record_repo_is_git_repository)
    plan_log_directory(planning)
    plan_env_file(planning, location)
    local_settings = plan_local_settings(planning, location)
    if local_settings is not None and eval_config is not None:
        check_settings_against_eval(planning, local_settings, eval_config)
    plan_claude_code_hooks(planning)

    if planning.failures:
        return planning.failures
    return OnboardingPlan(answers=answers, changes=planning.changes, warnings=planning.warnings)


def plan_record_repo(planning: Planning) -> bool:
    """Plan the record repo folder; return whether it is already a git repository of its own."""
    answers = planning.answers
    folder = answers.record_repo.path
    is_new_eval = answers.eval_choice == NEW_EVAL
    folder_state = describe_path(folder)
    if folder_state == "missing":
        if not is_new_eval:
            planning.failures.append(existing_record_repo_missing_failure(answers))
            return False
        planning.changes.append(
            PlannedChange(
                id="record_repo",
                what="Record repo",
                path=folder,
                action=ChangeAction.CREATE,
                details=["a new git repository with no remote; goodall commits to it and never pushes"],
                steps=[MakeFolder(folder, 0o755), MakeGitRepository(folder)],
            )
        )
        return False
    if folder_state != "folder":
        planning.failures.append(record_repo_not_a_folder_failure(answers, folder_state))
        return False

    if is_git_repository_of_its_own(planning.runner, folder):
        planning.changes.append(
            PlannedChange(id="record_repo", what="Record repo", path=folder, action=ChangeAction.UNCHANGED)
        )
        return True
    if not is_new_eval:
        planning.failures.append(existing_record_repo_not_git_failure(answers))
        return False
    if folder_is_empty(folder):
        planning.changes.append(
            PlannedChange(
                id="record_repo",
                what="Record repo",
                path=folder,
                action=ChangeAction.CREATE,
                details=["the empty folder becomes a git repository with no remote"],
                steps=[MakeGitRepository(folder)],
            )
        )
        return False
    planning.failures.append(record_repo_folder_not_empty_failure(answers))
    return False


def plan_eval_config(planning: Planning, location: LocalSettingsLocation) -> EvalConfig | None:
    """Plan eval.toml; return the eval config this account will belong to, once onboarding is done."""
    answers = planning.answers
    config_file = answers.record_repo.path / EVAL_CONFIG_FILE_NAME
    if answers.new_eval is None:
        # An existing eval: its eval.toml is read, never written.
        reading = read_eval_config(answers.record_repo.path, location.settings_file)
        if isinstance(reading, list):
            planning.failures.extend(reading)
            return None
        planning.changes.append(
            PlannedChange(id="eval_config", what="Eval config", path=config_file, action=ChangeAction.UNCHANGED)
        )
        return reading

    wanted_text = render_toml(eval_config_document(answers, answers.new_eval))
    wanted_table = tomllib.loads(wanted_text)
    # The safety net the module docstring describes: what goodall writes
    # must pass the eval config's own reader.
    wanted_config = check_eval_config_table(config_file, wanted_table)
    if isinstance(wanted_config, list):
        planning.failures.extend(wanted_config)
        return None

    existing_text = read_text_if_present(config_file)
    if existing_text is None:
        planning.changes.append(
            PlannedChange(
                id="eval_config",
                what="Eval config",
                path=config_file,
                action=ChangeAction.CREATE,
                details=[f"eval {answers.new_eval.name}, with you as its one contributor"],
                steps=[WriteFile(config_file, wanted_text, ORDINARY_FILE)],
            )
        )
        return wanted_config

    planning.changes.append(
        PlannedChange(id="eval_config", what="Eval config", path=config_file, action=ChangeAction.UNCHANGED)
    )
    try:
        existing_table = tomllib.loads(existing_text)
    except tomllib.TOMLDecodeError:
        existing_table = {}
    if existing_table != wanted_table:
        planning.files_left_as_they_are.append(config_file)
        planning.warnings.append(
            record_repo_file_differs_warning(config_file, differing_keys(existing_table, wanted_table))
        )
    # The eval this account belongs to is the one in the file, whatever the
    # answers said, so the cross-checks run against it.
    reading = read_eval_config(answers.record_repo.path, location.settings_file)
    if isinstance(reading, list):
        planning.failures.extend(reading)
        return None
    return reading


def eval_config_document(answers: Answers, new_eval: NewEval) -> TomlDocument:
    """The eval.toml a new eval starts with: these answers, and this contributor as the whole roster."""
    top_level: dict[str, TomlValue] = {
        "schema_version": 1,
        "name": new_eval.name,
        "description": new_eval.description,
        "workstreams": new_eval.workstreams,
        "intervention_categories": new_eval.intervention_categories,
        "activitywatch": new_eval.activitywatch,
    }
    display_name = answers.display_name or answers.contributor_id
    contributors: list[dict[str, TomlValue]] = [{"id": answers.contributor_id, "display_name": display_name}]
    scope: list[dict[str, TomlValue]] = []
    for scope_entry in new_eval.scope:
        scope.append({"repository": scope_entry.repository, "paths": scope_entry.paths})
    return TomlDocument(
        top_level=top_level,
        arrays_of_tables={"contributors": contributors, "scope": scope},
        comment_lines=[
            "The eval config: what is true for every contributor to this eval (ADR 0009).",
            "Written by goodall onboard. It may be published as it stands, so it holds no email address or secret.",
        ],
    )


def plan_construct(planning: Planning, new_eval: NewEval) -> None:
    """Plan CONSTRUCT.md, when the answers give a construct."""
    if new_eval.construct is None:
        return
    construct_file = planning.answers.record_repo.path / CONSTRUCT_FILE_NAME
    wanted_text = construct_text(new_eval)
    existing_text = read_text_if_present(construct_file)
    if existing_text is None:
        planning.changes.append(
            PlannedChange(
                id="construct",
                what="Construct",
                path=construct_file,
                action=ChangeAction.CREATE,
                details=["the question this eval sets out to measure"],
                steps=[WriteFile(construct_file, wanted_text, ORDINARY_FILE)],
            )
        )
        return
    planning.changes.append(
        PlannedChange(id="construct", what="Construct", path=construct_file, action=ChangeAction.UNCHANGED)
    )
    if existing_text != wanted_text:
        planning.files_left_as_they_are.append(construct_file)
        planning.warnings.append(record_repo_file_differs_warning(construct_file, []))


def construct_text(new_eval: NewEval) -> str:
    """What CONSTRUCT.md holds: a heading naming the eval, then the construct as the answers wrote it."""
    assert new_eval.construct is not None
    return f"# The construct of {new_eval.name}\n\n{new_eval.construct.strip()}\n"


def plan_record_repo_commit(planning: Planning, new_eval: NewEval, record_repo_is_git_repository: bool) -> None:
    """Plan the commit of eval.toml and CONSTRUCT.md: those this run writes, and those left uncommitted before.

    A file left uncommitted by an earlier run that stopped part way is
    committed now, but only when it says exactly what the answers say, so a
    person's own uncommitted edit is never swept into goodall's commit.
    """
    record_repo = planning.answers.record_repo.path
    file_names = [EVAL_CONFIG_FILE_NAME]
    if new_eval.construct is not None:
        file_names.append(CONSTRUCT_FILE_NAME)
    to_commit = []
    for file_name in file_names:
        path = record_repo / file_name
        created_now = False
        for change in planning.changes:
            if change.path == path and change.action == ChangeAction.CREATE:
                created_now = True
        if created_now:
            to_commit.append(file_name)
            continue
        if not record_repo_is_git_repository or path in planning.files_left_as_they_are:
            continue
        status = run_git(planning.runner, record_repo, ["status", "--porcelain", "--", file_name])
        if status.succeeded and status.standard_output.strip() != "":
            to_commit.append(file_name)

    if not to_commit:
        planning.changes.append(
            PlannedChange(
                id="record_repo_commit", what="Record repo commit", path=record_repo, action=ChangeAction.UNCHANGED
            )
        )
        return
    if not git_identity_is_set(planning.runner, record_repo):
        planning.failures.append(git_identity_missing_failure(record_repo))
        return
    message = (
        f"Start the record repo for eval {new_eval.name}\n\n"
        f"Written by goodall onboard for contributor {planning.answers.contributor_id}."
    )
    planning.changes.append(
        PlannedChange(
            id="record_repo_commit",
            what="Record repo commit",
            path=record_repo,
            action=ChangeAction.CREATE,
            details=[f"commits {' and '.join(to_commit)}, and nothing else"],
            steps=[CommitFiles(record_repo, to_commit, message)],
        )
    )


def plan_log_directory(planning: Planning) -> None:
    """Plan the log directory: made readable by this account only, if it is not there yet."""
    log_directory = planning.answers.log_directory.path
    if not log_directory.name.endswith(SPOTLIGHT_EXCLUDED_ENDING):
        planning.warnings.append(log_directory_indexed_warning(planning.answers))
    folder_state = describe_path(log_directory)
    if folder_state == "missing":
        planning.changes.append(
            PlannedChange(
                id="log_directory",
                what="Log directory",
                path=log_directory,
                action=ChangeAction.CREATE,
                details=["a folder only this macOS user account can open"],
                steps=[MakeFolder(log_directory, PRIVATE_FOLDER)],
            )
        )
        return
    if folder_state != "folder":
        planning.failures.append(log_directory_not_a_folder_failure(planning.answers, folder_state))
        return
    planning.changes.append(
        PlannedChange(id="log_directory", what="Log directory", path=log_directory, action=ChangeAction.UNCHANGED)
    )
    if stat.S_IMODE(log_directory.stat().st_mode) & OTHER_ACCOUNTS_PERMISSION_BITS:
        planning.warnings.append(log_directory_open_to_others_warning(log_directory))


def plan_env_file(planning: Planning, location: LocalSettingsLocation) -> None:
    """Plan the env file: secret references only, readable by this account only."""
    env_file = location.config_directory / ENV_FILE_NAME
    references = planning.answers.secret_references
    wanted_lines = []
    for name, reference in references.items():
        wanted_lines.append(env_line(name, reference))

    reading = read_env_file(env_file)
    if isinstance(reading, EnvFileNotRead) and reading.missing:
        text = "\n".join(ENV_FILE_HEADER) + "\n"
        if wanted_lines:
            text += "\n" + "\n".join(wanted_lines) + "\n"
        details = ["readable by this macOS user account only"]
        for name in references:
            details.append(f"sets {variable_as_shown(name)} to its secret reference")
        planning.changes.append(
            PlannedChange(
                id="env_file",
                what="Env file",
                path=env_file,
                action=ChangeAction.CREATE,
                details=details,
                steps=[
                    MakeFolder(location.config_directory, PRIVATE_FOLDER),
                    WriteFile(env_file, text, PRIVATE_FILE),
                ],
            )
        )
        return
    if isinstance(reading, EnvFileNotRead) or reading.problems:
        planning.failures.append(env_file_unusable_failure(env_file))
        return

    lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
    details = []
    lines_to_append: list[str] = []
    entries_by_name: dict[str, EnvEntry] = {}
    for entry in reading.entries:
        entries_by_name[entry.name] = entry
    for name, reference in references.items():
        existing_entry = entries_by_name.get(name)
        if existing_entry is None:
            lines_to_append.append(env_line(name, reference))
            details.append(f"adds {variable_as_shown(name)}, in a block marked as added by goodall onboard")
        elif existing_entry.value != reference:
            line_index = existing_entry.line_number - 1
            line_ending = ""
            if lines[line_index].endswith("\n"):
                line_ending = "\n"
            lines[line_index] = env_line(name, reference) + line_ending
            details.append(f"changes the secret reference of {variable_as_shown(name)}")
    text = "".join(lines)
    if lines_to_append:
        text = append_marked_block(text, added_marker(planning.now), lines_to_append)

    steps: list[Step] = []
    current_mode = stat.S_IMODE(env_file.stat().st_mode)
    if details:
        steps.append(WriteFile(env_file, text, PRIVATE_FILE))
    if current_mode & OTHER_ACCOUNTS_PERMISSION_BITS:
        details.append("made readable by this macOS user account only")
        if not steps:
            steps.append(SetPermissions(env_file, PRIVATE_FILE))
    action = ChangeAction.CHANGE if steps else ChangeAction.UNCHANGED
    planning.changes.append(
        PlannedChange(id="env_file", what="Env file", path=env_file, action=action, details=details, steps=steps)
    )


# The comment at the top of an env file goodall writes.
ENV_FILE_HEADER = [
    "# The env file: secret references only, never a secret itself (ADR 0010).",
    "# Written by goodall onboard. Only this macOS user account may read it.",
]


def env_line(name: str, reference: str) -> str:
    """One NAME=reference line. A reference with a space in it is quoted, as goodall.env_files requires."""
    if " " in reference:
        return f'{name}="{reference}"'
    return f"{name}={reference}"


def plan_local_settings(planning: Planning, location: LocalSettingsLocation) -> LocalSettings | None:
    """Plan local settings; return them as they will be, checked by their own reader."""
    settings_file = location.settings_file
    wanted = wanted_local_settings(planning.answers)
    existing_text = read_text_if_present(settings_file)

    if existing_text is None:
        text = render_toml(
            TomlDocument(
                top_level=wanted,
                comment_lines=[
                    "Local settings: how this macOS user account takes part in its eval (ADR 0009).",
                    "Written by goodall onboard. After a hand edit, goodall status checks them.",
                ],
            )
        )
        local_settings = local_settings_as_written(planning, settings_file, text)
        planning.changes.append(
            PlannedChange(
                id="local_settings",
                what="Local settings",
                path=settings_file,
                action=ChangeAction.CREATE,
                details=[
                    f"contributor {planning.answers.contributor_id}, in {planning.answers.account_mode} account mode"
                ],
                steps=[
                    MakeFolder(location.config_directory, PRIVATE_FOLDER),
                    WriteFile(settings_file, text, PRIVATE_FILE),
                ],
            )
        )
        return local_settings

    try:
        existing_table = tomllib.loads(existing_text)
    except tomllib.TOMLDecodeError:
        planning.failures.append(local_settings_unusable_failure(settings_file))
        return None

    text = existing_text
    details = []
    lines_to_append = []
    for key, value in wanted.items():
        if key not in existing_table:
            lines_to_append.append(toml_setting_line(key, value))
            details.append(f"adds {key} = {shown_value(value)}, in a block marked as added by goodall onboard")
            continue
        if same_setting(key, existing_table[key], value):
            continue
        replaced = replace_top_level_line(text, key, toml_setting_line(key, value))
        if replaced is None:
            planning.failures.append(local_setting_not_replaceable_failure(settings_file, key, value))
            continue
        text = replaced
        details.append(f"{key} changes to {shown_value(value)}")
    if lines_to_append:
        text = append_marked_block(text, added_marker(planning.now), lines_to_append)

    local_settings = local_settings_as_written(planning, settings_file, text)
    if not details:
        planning.changes.append(
            PlannedChange(id="local_settings", what="Local settings", path=settings_file, action=ChangeAction.UNCHANGED)
        )
        return local_settings
    mode = stat.S_IMODE(settings_file.stat().st_mode)
    planning.changes.append(
        PlannedChange(
            id="local_settings",
            what="Local settings",
            path=settings_file,
            action=ChangeAction.CHANGE,
            details=details,
            steps=[WriteFile(settings_file, text, mode)],
        )
    )
    return local_settings


def wanted_local_settings(answers: Answers) -> dict[str, TomlValue]:
    """The local settings the answers call for, in the order goodall writes them.

    Folders are kept as the answers wrote them, so `~/` stays `~/`. A setting
    the answers do not give (the ActivityWatch ones) is left out, so a value
    already in the file stays as it is.
    """
    wanted: dict[str, TomlValue] = {
        "schema_version": 1,
        "contributor": answers.contributor_id,
        "main_workstream": answers.main_workstream,
        "record_repo": answers.record_repo.as_written,
        "log_directory": answers.log_directory.as_written,
        "secrets_manager": answers.secrets_manager,
        "account_mode": answers.account_mode,
    }
    if answers.activitywatch_port is not None:
        wanted["activitywatch_port"] = answers.activitywatch_port
    if answers.activitywatch_device_id is not None:
        wanted["activitywatch_device_id"] = answers.activitywatch_device_id
    return wanted


def same_setting(key: str, existing_value: object, wanted_value: TomlValue) -> bool:
    """True when a setting already says what is wanted; two spellings of one folder count as the same."""
    if key in ["record_repo", "log_directory"] and isinstance(existing_value, str) and isinstance(wanted_value, str):
        return expand_home_folder(existing_value) == expand_home_folder(wanted_value)
    if isinstance(existing_value, str) and key == "activitywatch_device_id":
        return existing_value.lower() == wanted_value
    return existing_value == wanted_value and type(existing_value) is type(wanted_value)


def toml_setting_line(key: str, value: TomlValue) -> str:
    """One `key = value` line of local settings."""
    rendered = render_toml(TomlDocument(top_level={key: value}))
    return rendered.rstrip("\n")


def shown_value(value: TomlValue) -> str:
    """A local setting's value as a plan shows it. Every one comes from checked answers, so none is a secret."""
    return toml_setting_line("x", value)[len("x = ") :]


def local_settings_as_written(planning: Planning, settings_file: Path, text: str) -> LocalSettings | None:
    """Read `text` back as local settings would be read; record its problems as failures."""
    checked = check_settings_table(settings_file, tomllib.loads(text))
    if isinstance(checked, list):
        planning.failures.extend(checked)
        return None
    return checked


def check_settings_against_eval(planning: Planning, local_settings: LocalSettings, eval_config: EvalConfig) -> None:
    """The cross-checks `goodall status` and `goodall doctor` will make, made now, before anything is written."""
    if find_this_contributor(local_settings, eval_config) is None:
        planning.failures.append(contributor_not_in_roster_failure(local_settings, eval_config))
    if not main_workstream_is_known(local_settings, eval_config):
        planning.failures.append(main_workstream_unknown_failure(local_settings, eval_config))
    missing = missing_activitywatch_settings(local_settings, eval_config)
    if missing:
        planning.warnings.append(activitywatch_by_hand_warning(missing))


def plan_claude_code_hooks(planning: Planning) -> None:
    """Plan goodall's hooks in Claude Code's user settings, leaving everything else in the file as it is."""
    settings_file = claude_user_settings_file()
    program = planning.answers.goodall_program
    if program is None:
        found = planning.runner.find_program(GOODALL_PROGRAM_NAME)
        if found is None:
            planning.failures.append(goodall_program_not_found_failure())
            return
        program = str(found)
    if not is_full_path(program):
        planning.failures.append(goodall_program_not_found_failure())
        return

    if describe_path(settings_file) == "missing":
        settings: dict[str, object] = {}
        mode = PRIVATE_FILE
        action = ChangeAction.CREATE
    else:
        reading = read_claude_settings(settings_file)
        if isinstance(reading, Failure):
            planning.failures.append(reading)
            return
        settings = copy.deepcopy(reading)
        mode = stat.S_IMODE(settings_file.stat().st_mode)
        action = ChangeAction.CHANGE
        if settings.get("disableAllHooks") is True:
            planning.warnings.append(hooks_disabled_warning(settings_file))

    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        planning.failures.append(claude_hooks_unusable_failure(settings_file))
        return
    details = []
    for event in KNOWN_EVENTS:
        matcher_groups = hooks.get(event, [])
        if not isinstance(matcher_groups, list):
            planning.failures.append(claude_hooks_unusable_failure(settings_file))
            return
        command = f"{shlex.quote(program)} hook {event}"
        existing_commands = goodall_commands_in(matcher_groups)
        if existing_commands == [command]:
            continue
        if existing_commands:
            details.append(f"replaces goodall's earlier {event} hook")
        else:
            details.append(f"adds the {event} hook")
        hooks[event] = [*without_goodall_hooks(matcher_groups), goodall_matcher_group(event, command)]
    settings["hooks"] = hooks

    if not details:
        planning.changes.append(
            PlannedChange(
                id="claude_code_hooks", what="Claude Code hooks", path=settings_file, action=ChangeAction.UNCHANGED
            )
        )
        return
    details.append(f"each runs {program}")
    if action == ChangeAction.CHANGE:
        details.append("every other setting in the file is kept, and the file is written back as indented JSON")
    planning.changes.append(
        PlannedChange(
            id="claude_code_hooks",
            what="Claude Code hooks",
            path=settings_file,
            action=action,
            details=details,
            steps=[
                MakeFolder(settings_file.parent, PRIVATE_FOLDER),
                WriteFile(settings_file, json.dumps(settings, indent=2) + "\n", mode),
            ],
        )
    )


def goodall_commands_in(matcher_groups: list[object]) -> list[str]:
    """Every goodall hook command in one event's matcher groups, as written."""
    commands = []
    for matcher_group in matcher_groups:
        for hook in hooks_of(matcher_group):
            if is_goodall_hook(hook):
                assert isinstance(hook, dict)
                commands.append(str(hook["command"]))
    return commands


def without_goodall_hooks(matcher_groups: list[object]) -> list[object]:
    """One event's matcher groups with goodall's own hook commands taken out.

    A group that held only goodall's commands goes with them; any other
    group, and anything goodall does not recognize, is kept as it was.
    """
    kept: list[object] = []
    for matcher_group in matcher_groups:
        hooks = hooks_of(matcher_group)
        other_hooks = []
        for hook in hooks:
            if not is_goodall_hook(hook):
                other_hooks.append(hook)
        if len(other_hooks) == len(hooks):
            kept.append(matcher_group)
            continue
        if other_hooks:
            assert isinstance(matcher_group, dict)
            kept.append({**matcher_group, "hooks": other_hooks})
    return kept


def hooks_of(matcher_group: object) -> list[object]:
    """The hooks in one matcher group, or none when it is not in Claude Code's shape."""
    if not isinstance(matcher_group, dict):
        return []
    hooks = matcher_group.get("hooks")
    if not isinstance(hooks, list):
        return []
    return hooks


def is_goodall_hook(hook: object) -> bool:
    """True for a `goodall hook ...` command hook, whichever goodall and event it names."""
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    command = hook.get("command")
    return isinstance(command, str) and read_goodall_hook_command(command) is not None


def goodall_matcher_group(event: str, command: str) -> dict[str, object]:
    """The matcher group goodall adds for `event`, in the shape docs/claude-code-hooks.md shows."""
    group: dict[str, object] = {}
    if event == "PostToolUse":
        group["matcher"] = EVERY_TOOL_MATCHER
    group["hooks"] = [{"type": "command", "command": command}]
    return group


# Small questions about the machine, asked without changing anything.


def describe_path(path: Path) -> str:
    """ "missing", "folder", "file", or why it cannot be reached; never raises.

    Trap: Path.exists() raises PermissionError when a folder above cannot be
    searched, so this asks with stat() and turns every error into words.
    """
    try:
        path_status = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError as stat_error:
        return f"cannot be reached: {stat_error.strerror}"
    if stat.S_ISDIR(path_status.st_mode):
        return "folder"
    return "file"


def folder_is_empty(folder: Path) -> bool:
    """True when `folder` holds nothing at all, hidden files included."""
    try:
        return next(folder.iterdir(), None) is None
    except OSError:
        return False


def read_text_if_present(path: Path) -> str | None:
    """The text of the file at `path`, or None when there is none."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


def is_git_repository_of_its_own(runner: Runner, folder: Path) -> bool:
    """True when `folder` is the top folder of a git repository, not a folder inside another one."""
    result = run_git(runner, folder, ["rev-parse", "--show-toplevel"])
    if not result.succeeded:
        return False
    return Path(result.standard_output.strip()).resolve() == folder.resolve()


def git_identity_is_set(runner: Runner, record_repo: Path) -> bool:
    """True when git has a name and an email to commit with, asked from the nearest folder that exists."""
    folder = record_repo
    while describe_path(folder) != "folder" and folder != folder.parent:
        folder = folder.parent
    for setting in IDENTITY_SETTINGS:
        result = run_git(runner, folder, ["config", "--get", setting])
        if not result.standard_output.strip():
            return False
    return True


def differing_keys(existing_table: dict[str, object], wanted_table: dict[str, object]) -> list[str]:
    """The top-level keys whose values differ between two parsed TOML files, in a stable order."""
    keys = []
    for key in [*wanted_table, *existing_table]:
        if key not in keys and existing_table.get(key) != wanted_table.get(key):
            keys.append(key)
    return keys


def added_marker(now: datetime) -> str:
    """The comment above a block goodall appends to a file a person may have edited."""
    return f"Added by goodall onboard on {now.date().isoformat()}."


# Applying: make exactly the planned changes, in order, stopping at the first
# that fails.


def apply_plan(plan: OnboardingPlan, runner: Runner) -> tuple[list[str], Failure | None]:
    """Make every change in `plan`; return the ids of those made, and the failure that stopped it, if any."""
    done_ids: list[str] = []
    for change in plan.changes_to_make:
        for step in change.steps:
            failure = apply_step(step, runner)
            if failure is not None:
                return done_ids, stopped_failure(change, failure)
        done_ids.append(change.id)
    return done_ids, None


def apply_step(step: Step, runner: Runner) -> str | None:
    """Do one step; return what went wrong, as the end of a sentence, or None."""
    try:
        if isinstance(step, MakeFolder):
            if describe_path(step.path) != "folder":
                step.path.mkdir(parents=True, exist_ok=True)
                # Set after making, because the account's umask may have
                # taken bits away from what mkdir was asked for.
                step.path.chmod(step.mode)
            return None
        if isinstance(step, WriteFile):
            write_file_in_one_step(step.path, step.text, step.mode)
            return None
        if isinstance(step, SetPermissions):
            step.path.chmod(step.mode)
            return None
    except OSError as error:
        return f"{error.strerror} ({error.filename or ''})"
    if isinstance(step, MakeGitRepository):
        result = run_git(runner, step.folder, ["init", "--quiet"])
        if not result.succeeded:
            return f"git init did not work: {result.problem}"
        return None
    add_result = run_git(runner, step.record_repo, ["add", "--", *step.relative_paths])
    if not add_result.succeeded:
        return f"git add did not work: {add_result.problem}"
    commit_result = run_git(
        runner,
        step.record_repo,
        ["commit", "--quiet", "-m", step.message, "--", *step.relative_paths],
        timeout_seconds=COMMIT_TIMEOUT_SECONDS,
    )
    if not commit_result.succeeded:
        return f"git commit did not work: {commit_result.problem}"
    return None


def write_file_in_one_step(path: Path, text: str, mode: int) -> None:
    """Write `text` to `path` with permissions `mode`, so no reader ever sees half a file.

    The text goes to a temporary file beside it, made with the final
    permissions from the start (so a private file is never readable by
    others, even briefly), which is then renamed over `path`.
    """
    temporary_file = path.parent / f".{path.name}.goodall-tmp"
    file_descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(text)
        temporary_file.chmod(mode)
        temporary_file.replace(path)
    except OSError:
        temporary_file.unlink(missing_ok=True)
        raise


# Showing what happened, as text or as one JSON document.


def show_plan(terminal: Terminal, plan: OnboardingPlan) -> None:
    """Write the whole plan: one row per change, then the details of each, then the warnings."""
    terminal.show_paragraph(f"Setting up this macOS user account from the answers file {plan.answers.answers_file}.")
    rows = []
    for change in plan.changes:
        rows.append([str(change.action), change.what, str(change.path)])
    terminal.show_rows(["Change", "What", "Path"], rows)
    for change in plan.changes:
        if change.details:
            terminal.show_paragraph(f"{change.what}: " + "; ".join(change.details) + ".")
    if plan.warnings:
        terminal.show_warnings(plan.warnings)


def show_report(terminal: Terminal, report: OnboardingReport, as_json: bool) -> None:
    """Write how the run ended: the JSON document, or the closing words and any failure."""
    if as_json:
        terminal.write_json(report.as_json())
        return
    if report.failures:
        terminal.show_failures(report.failures)
    if report.outcome == "planned":
        terminal.show_paragraph(
            "Nothing was changed: this was the plan only. Run the same command without --plan to make these changes."
        )
    elif report.outcome == "nothing_to_change":
        terminal.show_paragraph(
            "Nothing to change: everything already matches the answers file. Next: run goodall doctor, which "
            "checks the whole setup."
        )
    elif report.outcome == "applied":
        terminal.show_paragraph(
            f"Done. Goodall made {len(report.done_ids)} of the changes above and nothing else. Next: run goodall "
            "doctor, which checks the whole setup, then goodall start to record your first work block."
        )
    elif report.outcome == "stopped" and report.plan is not None:
        terminal.show_paragraph(
            f"Stopped after {len(report.done_ids)} of {len(report.plan.changes_to_make)} changes. Running the same "
            "command again is safe: it makes only the changes still missing."
        )


# Failures and warnings. Each says what happened, why it matters and what to
# do next; none repeats a value that could be a secret.


def stopped_failure(change: PlannedChange, problem: str) -> Failure:
    """The failure for a change that could not be made part way through onboarding."""
    return Failure(
        kind="onboard_change_failed",
        what_happened=f"Goodall could not finish {change.what.lower()} at {change.path}: {problem}.",
        why="Onboarding stops at the first change that fails, so it never builds on something that is not there.",
        what_to_do_next=(
            "Fix what is named above, then run the same goodall onboard command again: changes already made are "
            "reported unchanged, and only what is still missing is made."
        ),
    )


def existing_record_repo_missing_failure(answers: Answers) -> Failure:
    """The failure for an existing eval whose record repo is not where the answers say."""
    return Failure(
        kind="record_repo_missing",
        what_happened=(
            f"The answers say the eval already exists, but there is no record repo at {answers.record_repo.path}. "
            "Nothing was changed."
        ),
        why='With eval = "existing", goodall uses the record repo already on this Mac and makes no new one.',
        what_to_do_next=(
            'Correct [folders] record_repo in the answers file, or set eval = "new" with a [new_eval] section to '
            "start a new eval there."
        ),
    )


def existing_record_repo_not_git_failure(answers: Answers) -> Failure:
    """The failure for an existing eval whose record repo folder is not a git repository of its own."""
    return Failure(
        kind="record_repo_not_git_repository",
        what_happened=(
            f"The record repo folder {answers.record_repo.path} is not a git repository of its own. Nothing was "
            "changed."
        ),
        why="Goodall keeps the eval's records by committing them in the record repo, so it must be one.",
        what_to_do_next=(
            "Correct [folders] record_repo in the answers file to the record repo's top folder, the one holding "
            "eval.toml and .git."
        ),
    )


def record_repo_not_a_folder_failure(answers: Answers, folder_state: str) -> Failure:
    """The failure for a record repo path that is a file, or that cannot be reached."""
    return Failure(
        kind="record_repo_not_a_folder",
        what_happened=(
            f"The record repo path {answers.record_repo.path} is not a folder goodall can use: {folder_state}. "
            "Nothing was changed."
        ),
        why="The record repo is a folder holding a git repository.",
        what_to_do_next=(
            "Choose another [folders] record_repo in the answers file, or move what is there, then run it again."
        ),
    )


def record_repo_folder_not_empty_failure(answers: Answers) -> Failure:
    """The failure for a new eval whose record repo folder already holds files but no git repository."""
    return Failure(
        kind="record_repo_folder_not_empty",
        what_happened=(
            f"The folder {answers.record_repo.path} already holds files and is not a git repository, so goodall did "
            "not make it the record repo. Nothing was changed."
        ),
        why="Goodall does not turn a folder of someone's files into a repository, where its files would be committed.",
        what_to_do_next=(
            "Choose a folder that does not exist yet or is empty for [folders] record_repo, such as "
            "~/evals/goodall-pilot-records, and run it again."
        ),
    )


def log_directory_not_a_folder_failure(answers: Answers, folder_state: str) -> Failure:
    """The failure for a log directory path that is a file, or that cannot be reached."""
    return Failure(
        kind="log_directory_not_a_folder",
        what_happened=(
            f"The log directory path {answers.log_directory.path} is not a folder goodall can use: {folder_state}. "
            "Nothing was changed."
        ),
        why="Raw records are written into the log directory, so it must be a folder this account can open.",
        what_to_do_next="Choose another [folders] log_directory in the answers file, or move what is there.",
    )


def git_identity_missing_failure(record_repo: Path) -> Failure:
    """The failure for a machine where git has no name or email for the record repo's first commit."""
    return Failure(
        kind="record_repo_identity_missing",
        what_happened=(f"git has no name or email to put on the first commit in {record_repo}. Nothing was changed."),
        why=(
            "Every commit in the record repo names who made it. Goodall uses the name and email you gave git, and "
            "never makes one up. The commits stay on this Mac."
        ),
        what_to_do_next="Tell git who you are, with your own name and email, then run goodall onboard again.",
        commands_to_try=(
            'git config --global user.name "Your Name"',
            'git config --global user.email "you@example.org"',
        ),
    )


def env_file_unusable_failure(env_file: Path) -> Failure:
    """The failure for an existing env file goodall cannot read or that has lines it cannot read."""
    return Failure(
        kind="env_file_unusable",
        what_happened=(
            f"The env file at {env_file} cannot be read, or has a line goodall cannot read, so goodall did not change "
            "it. Nothing was changed."
        ),
        why="Goodall changes only lines it understands, so it never loses one you wrote.",
        what_to_do_next=(
            "Run goodall doctor, which names each line to correct, fix them, then run goodall onboard again."
        ),
    )


def local_settings_unusable_failure(settings_file: Path) -> Failure:
    """The failure for existing local settings that are not valid TOML."""
    return Failure(
        kind="local_settings_not_toml",
        what_happened=(
            f"The local settings at {settings_file} are not valid TOML, so goodall did not change them. Nothing was "
            "changed."
        ),
        why="Goodall changes only settings it can read, so it never loses one you wrote.",
        what_to_do_next="Run goodall status, which says where the mistake is, fix it, then run goodall onboard again.",
    )


def local_setting_not_replaceable_failure(settings_file: Path, key: str, value: TomlValue) -> Failure:
    """The failure for a setting goodall cannot change on its one line."""
    return Failure(
        kind="local_setting_not_replaceable",
        what_happened=(
            f"Local settings at {settings_file} set {key} in a way goodall cannot change on one line, so goodall did "
            "not change the file. Nothing was changed."
        ),
        why="Goodall changes a setting only by replacing its one line, so the rest of the file stays as you wrote it.",
        what_to_do_next=(
            f"Change that setting by hand to {toml_setting_line(key, value)}, then run goodall onboard again."
        ),
    )


def goodall_program_not_found_failure() -> Failure:
    """The failure for a machine where goodall cannot tell which goodall the Claude Code hooks should run."""
    return Failure(
        kind="goodall_program_not_found",
        what_happened=(
            "Goodall could not find the full path of the goodall command for the Claude Code hooks to run. Nothing "
            "was changed."
        ),
        why=(
            "Claude Code may run hooks without the folders your terminal adds to PATH, so each hook names goodall by "
            "its full path; without one, the prompt gate could not run."
        ),
        what_to_do_next=(
            "Run command -v goodall in Terminal and write the full path it prints in the answers file as "
            '[claude_code] goodall_program = "/Users/you/.local/bin/goodall", then run goodall onboard again.'
        ),
    )


def claude_hooks_unusable_failure(settings_file: Path) -> Failure:
    """The failure for Claude Code settings whose hooks are not in the shape Claude Code uses."""
    return Failure(
        kind="claude_code_hooks_unusable",
        what_happened=(
            f"The hooks in Claude Code's settings at {settings_file} are not in the shape Claude Code uses, so "
            "goodall did not change the file. Nothing was changed."
        ),
        why="Goodall adds its hooks beside yours, and does not guess how to merge with a shape it does not know.",
        what_to_do_next=(
            "Compare the hooks in that file with docs/claude-code-hooks.md in goodall's documentation, correct them, "
            "and run goodall onboard again."
        ),
    )


def record_repo_file_differs_warning(path: Path, keys: list[str]) -> Failure:
    """The warning for eval.toml or CONSTRUCT.md that already says something other than the answers."""
    which = ""
    if keys:
        which = f" (it differs in {', '.join(keys)})"
    return Failure(
        kind="record_repo_file_differs",
        what_happened=(
            f"{path} already exists and says something other than the answers{which}. Goodall left it as it is."
        ),
        why=(
            "It is a committed record a person may have edited on purpose, so goodall never rewrites it. The eval "
            "goes on using what the file says."
        ),
        what_to_do_next=(
            "If the answers are right, edit the file by hand to match and commit the change with git in the record "
            "repo. Otherwise nothing needs doing."
        ),
    )


def log_directory_indexed_warning(answers: Answers) -> Failure:
    """The warning for a log directory whose name does not keep Spotlight out (ADR 0019)."""
    return Failure(
        kind="log_directory_not_noindex",
        what_happened=f"The log directory {answers.log_directory.path} does not end in {SPOTLIGHT_EXCLUDED_ENDING}.",
        why=(
            "Spotlight leaves a folder alone when its name ends in .noindex, so goodall's copies of transcripts do "
            "not appear among your own files when you search the Mac."
        ),
        what_to_do_next=(
            f"Consider a log directory whose name ends in {SPOTLIGHT_EXCLUDED_ENDING}, such as "
            "~/evals/goodall-pilot-logs.noindex, in [folders] log_directory."
        ),
    )


def log_directory_open_to_others_warning(log_directory: Path) -> Failure:
    """The warning for an existing log directory other accounts on the Mac can open."""
    return Failure(
        kind="log_directory_open_to_others",
        what_happened=f"Other macOS user accounts on this Mac can open the log directory {log_directory}.",
        why="It holds every raw record, transcripts included, which are among the most private files goodall keeps.",
        what_to_do_next="Goodall left it as it is. To keep others out, run the command below.",
        commands_to_try=(f"chmod 700 {shlex.quote(str(log_directory))}",),
    )


def hooks_disabled_warning(settings_file: Path) -> Failure:
    """The warning for Claude Code settings that turn every hook off."""
    return Failure(
        kind="claude_code_hooks_disabled",
        what_happened=f"Claude Code's settings at {settings_file} turn every hook off with disableAllHooks.",
        why="With hooks off, the prompt gate never runs, so nothing is recorded and no prompt is refused.",
        what_to_do_next="Goodall left that setting alone. Remove the disableAllHooks line from that file by hand.",
    )


def activitywatch_by_hand_warning(missing: list[str]) -> Failure:
    """The warning for an eval that includes ActivityWatch when its settings are not in the answers yet."""
    return Failure(
        kind="activitywatch_by_hand",
        what_happened=(
            f"The eval includes ActivityWatch, and the answers do not give {' or '.join(missing)} yet, so goodall "
            "doctor will report them missing."
        ),
        why=(
            "This account's ActivityWatch needs a port of its own (ADR 0016). Goodall onboard does not set that up "
            "yet; it stays a by-hand step."
        ),
        what_to_do_next=(
            "Follow Part 3 of docs/setup-by-hand.md, then add [activitywatch] port and device_id to the answers file "
            "and run goodall onboard again."
        ),
    )
