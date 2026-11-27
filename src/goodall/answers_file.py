"""Reading and checking an answers file: one person's answers to the setup questions.

The answers file is what the setup page saves when a person presses its one
button, or what a person writes by hand for unattended setup (ADR 0012,
GLOSSARY.md). `goodall onboard --answers <file>` reads it through
`read_answers_file` and hands the result to goodall.onboard, which works out
the files to write. The format is documented for people in
docs/answers-file.md; that page and this module change together.

What reading gives back: either an `Answers` record, every value in it
checked, or a list of `Failure` records, each saying what is wrong and what
to do next. Like goodall.local_settings and goodall.eval_config, it never
raises for bad input and reports every problem at once, except two cases
where the rest would be noise: a file written for a newer goodall, and a
file whose one-time code is not the one the setup page was given.

What the checks protect:

- Secrets stay in the secrets manager (AGENTS.md invariant 4, ADR 0012).
  Every text value is searched for a key in a format goodall recognizes
  (goodall.quoting.key_format_in), and refused if one is there; the
  `[secrets.references]` table must hold only well-formed secret references
  for the secrets manager named. A secret reference anywhere else is refused
  too, because it would land in a file that is not the env file.
- No value is ever repeated in a message, because any of them could be a
  key pasted into the wrong box. Messages name the place instead: a
  top-level key by its name, others as `[section] key`. A key goodall does
  not know is named only when goodall.quoting says it may be shown.
- What will become the eval config is checked by the eval config's own
  rules (names, the declared scope, no email address), using the patterns
  goodall.eval_config defines, so an answers file cannot make an eval config
  that `goodall doctor` would refuse. goodall.onboard also reads every file
  it would write back through that file's own reader before writing it.

The `[eval_account]` table (ADR 0022) is answered in an administrator
account: `create = true` and a `name` ask the administrator install
(goodall.administrator_install) to create the eval account and stage its
first sign-in setup. It is checked here only for its own form and for
agreeing with the rest of the file: an eval account is for dedicated
account mode, and a new account has no record repo, so it must start a new
eval. Whether an account of that name already exists is a question about
the machine, answered by the administrator install.

What this module cannot check, because it does not look at the machine:
whether folders exist, whether an existing record repo's eval config agrees
with these answers, or whether git can commit. goodall.onboard does that.

The one-time code: the setup page writes one into the answers file, so the
waiting `goodall onboard` accepts only the file from the page it opened
(ADR 0012). `--answers` passes no code to expect, and then a code in the
file is accepted and ignored, so a file the page saved can also be used
unattended.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from goodall import records
from goodall.env_files import VARIABLE_NAME_PATTERN
from goodall.eval_config import (
    EMAIL_ADDRESS_PATTERN,
    NAME_PATTERN,
    NAME_RULE,
    ScopeEntry,
    describe_repository_problem,
    path_pattern_leaves_repository,
)
from goodall.local_settings import (
    ACCOUNT_MODES,
    ACTIVITYWATCH_DEFAULT_PORT,
    ACTIVITYWATCH_INFO_PATH,
    DEDICATED_ACCOUNT_MODE,
    DEVICE_ID_PATTERN,
    HIGHEST_PORT,
    MIXED_ACCOUNT_MODE,
    SUPPORTED_SECRETS_MANAGERS,
    FolderSetting,
    expand_home_folder,
    is_whole_number,
)
from goodall.quoting import KeyFormat, can_show_name, key_format_in, toml_error_as_shown
from goodall.secrets_manager import (
    is_service_account_token_variable,
    is_well_formed_secret_reference,
    looks_like_a_secret_reference,
)
from goodall.terminal import Failure

# The newest layout of the answers file this goodall understands.
SUPPORTED_SCHEMA_VERSION = 1

# The two answers to "is this a new eval?": `new` starts one in a new record
# repo; `existing` uses the record repo, and its eval config, already on
# this Mac.
NEW_EVAL = "new"
EXISTING_EVAL = "existing"
EVAL_CHOICES = [NEW_EVAL, EXISTING_EVAL]

# The key of the top of the file in KNOWN_KEYS.
TOP_OF_FILE = ""

# Every key each part of the answers file may hold. The top of the file is
# TOP_OF_FILE; a section is named as in the file.
KNOWN_KEYS = {
    TOP_OF_FILE: [
        "schema_version",
        "one_time_code",
        "account_mode",
        "eval",
        "new_eval",
        "existing_eval",
        "contributor",
        "folders",
        "secrets",
        "activitywatch",
        "claude_code",
        "eval_account",
    ],
    "new_eval": [
        "name",
        "description",
        "workstreams",
        "intervention_categories",
        "activitywatch",
        "construct",
        "scope",
    ],
    "new_eval.scope": ["repository", "paths"],
    "existing_eval": ["workstreams"],
    "contributor": ["id", "display_name", "main_workstream"],
    "folders": ["record_repo", "log_directory"],
    "secrets": ["manager", "references"],
    "activitywatch": ["port", "device_id"],
    "claude_code": ["goodall_program"],
    "eval_account": ["create", "name"],
}

# A correct line for each key, shown in "for example" advice. Every value is
# made up, branded goodall-.
EXAMPLE_LINES = {
    "schema_version": f"schema_version = {SUPPORTED_SCHEMA_VERSION}",
    "one_time_code": 'one_time_code = "the code the setup page wrote"',
    "account_mode": f'account_mode = "{DEDICATED_ACCOUNT_MODE}"',
    "eval": f'eval = "{NEW_EVAL}"',
    "new_eval": "[new_eval] with name, description, workstreams and at least one [[new_eval.scope]]",
    "contributor": "[contributor] with id and main_workstream (and display_name for a new eval)",
    "folders": "[folders] with record_repo and log_directory",
    "existing_eval": '[existing_eval] with workstreams = ["engineering", "research", "go-to-market"]',
    "existing_eval.workstreams": 'workstreams = ["engineering", "research", "go-to-market"]',
    "new_eval.name": 'name = "goodall-pilot"',
    "new_eval.description": 'description = "Building a product to first recurring revenue with AI agents."',
    "new_eval.workstreams": 'workstreams = ["engineering", "research", "go-to-market"]',
    "new_eval.intervention_categories": (
        'intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]'
    ),
    "new_eval.activitywatch": "activitywatch = false",
    "new_eval.construct": 'construct = "Which parts of the work can be delegated to AI agents?"',
    "new_eval.scope": '[[new_eval.scope]] followed by repository = "github.com/your-org/goodall-pilot-app"',
    "new_eval.scope.repository": 'repository = "github.com/your-org/goodall-pilot-app"',
    "new_eval.scope.paths": 'paths = ["**"]',
    "contributor.id": 'id = "ada-lovelace"',
    "contributor.display_name": 'display_name = "Ada Lovelace"',
    "contributor.main_workstream": 'main_workstream = "research"',
    "folders.record_repo": 'record_repo = "~/evals/goodall-pilot-records"',
    "folders.log_directory": 'log_directory = "~/evals/goodall-pilot-logs.noindex"',
    "secrets.manager": 'manager = "1password"',
    "secrets.references": 'ANTHROPIC_API_KEY_RESEARCH = "op://goodall-eval/anthropic-research/credential"',
    "activitywatch.port": "port = 5610",
    "activitywatch.device_id": 'device_id = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"',
    "claude_code.goodall_program": 'goodall_program = "/Users/ada/.local/bin/goodall"',
    "eval_account.create": "create = true",
    "eval_account.name": 'name = "goodall-eval"',
}

# The account names goodall will ask macOS to create: a lowercase letter
# first, then lowercase letters, digits and hyphens, not ending in a hyphen.
# Narrower than what macOS accepts, on purpose: no capitals, spaces or dots
# to get wrong when typing it at the sign-in window, and no leading
# underscore, which macOS keeps for its own accounts.
ACCOUNT_NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
ACCOUNT_NAME_RULE = "a lowercase letter, then lowercase letters, digits and single hyphens, not ending in a hyphen"


# The one secrets manager used when the answers file does not name one.
DEFAULT_SECRETS_MANAGER = "1password"

# Where a person reads about the format.
FORMAT_DOCUMENT = "docs/answers-file.md in goodall's documentation"


@dataclass(frozen=True)
class NewEval:
    """The answers that become a new eval's eval config, and its construct.

    `construct` is None when the answers give none; goodall then writes no
    construct file.
    """

    name: str
    description: str
    workstreams: list[str]
    intervention_categories: list[str]
    activitywatch: bool
    scope: list[ScopeEntry]
    construct: str | None


@dataclass(frozen=True)
class EvalAccountAnswer:
    """The `[eval_account]` answers: whether to create the eval account, and its name.

    `name` is set exactly when `create` is True.
    """

    create: bool
    name: str | None


@dataclass(frozen=True)
class Answers:
    """An answers file that passed every check in this module.

    - `eval_choice` is NEW_EVAL or EXISTING_EVAL; `new_eval` is set exactly
      when it is NEW_EVAL.
    - `display_name` is None for an existing eval, whose roster already has
      one.
    - `secret_references` maps each env file variable to its reference, in
      the order written.
    - `goodall_program` is the goodall the Claude Code hooks should run, as
      written, or None to use the goodall found on the PATH.
    - `existing_workstreams` is, for an existing eval, the full workstream
      list the person wants in the eval config (`goodall settings` saves
      it); None leaves the eval config as it is. Goodall only ever adds
      names to the eval config's list (docs/setup-page.md).
    - `eval_account` is the `[eval_account]` answer, or None when the file
      has no such table, which creates no account.
    """

    answers_file: Path
    account_mode: str
    eval_choice: str
    new_eval: NewEval | None
    contributor_id: str
    display_name: str | None
    main_workstream: str
    record_repo: FolderSetting
    log_directory: FolderSetting
    secrets_manager: str
    secret_references: dict[str, str] = field(default_factory=dict)
    activitywatch_port: int | None = None
    activitywatch_device_id: str | None = None
    goodall_program: str | None = None
    existing_workstreams: list[str] | None = None
    eval_account: EvalAccountAnswer | None = None


def read_answers_file(answers_file: Path, expected_one_time_code: str | None = None) -> Answers | list[Failure]:
    """Read and check the answers file at `answers_file`; never raises for anything about the file.

    `expected_one_time_code` is the code the setup page was written with, or
    None when no code is expected (`goodall onboard --answers`).
    """
    try:
        file_bytes = answers_file.read_bytes()
    except FileNotFoundError:
        return [answers_file_missing_failure(answers_file)]
    except IsADirectoryError:
        return [answers_file_unreadable_failure(answers_file, "it is a folder, not a file")]
    except OSError as read_error:
        return [answers_file_unreadable_failure(answers_file, str(read_error.strerror))]
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return [answers_file_not_toml_failure(answers_file, "it contains characters that are not UTF-8 text")]
    return read_answers_text(text, answers_file, expected_one_time_code)


def read_answers_text(
    text: str, answers_file: Path, expected_one_time_code: str | None = None
) -> Answers | list[Failure]:
    """Check the text of an answers file; `answers_file` is where it came from, for messages.

    The setup page's waiting command may call this with text it has already
    read, so reading the file and checking its text are two functions.
    """
    try:
        table = tomllib.loads(text)
    except tomllib.TOMLDecodeError as toml_error:
        return [answers_file_not_toml_failure(answers_file, toml_error_as_shown(str(toml_error)))]
    return AnswersReader(answers_file, table).read(expected_one_time_code)


class AnswersReader:
    """Checks one parsed answers file, collecting every problem in `problems`.

    A class only so the file's path and the problems found so far need not
    be passed to every small check; each method reads one part of the file.
    """

    def __init__(self, answers_file: Path, table: dict[str, object]) -> None:
        self.answers_file = answers_file
        self.table = table
        self.problems: list[Failure] = []

    def read(self, expected_one_time_code: str | None) -> Answers | list[Failure]:
        """Check the whole file, in the order its parts are written."""
        schema_version = self.table.get("schema_version")
        if "schema_version" not in self.table:
            self.problems.append(self.key_missing("schema_version", TOP_OF_FILE))
        elif not is_whole_number(schema_version) or schema_version < 1:
            self.invalid("schema_version", TOP_OF_FILE, "must be a whole number without quotes")
        elif schema_version > SUPPORTED_SCHEMA_VERSION:
            return [answers_schema_too_new_failure(self.answers_file, schema_version)]

        if expected_one_time_code is not None and self.table.get("one_time_code") != expected_one_time_code:
            return [one_time_code_mismatch_failure(self.answers_file)]

        # Before anything else, so a key pasted into any box is reported as
        # such, whatever else is wrong with that box.
        self.find_secret_values(self.table, TOP_OF_FILE)
        self.find_unknown_keys(self.table, TOP_OF_FILE)

        one_time_code = self.table.get("one_time_code")
        if one_time_code is not None and not isinstance(one_time_code, str):
            self.invalid("one_time_code", TOP_OF_FILE, "must be text in double quotes")
        account_mode = self.read_choice("account_mode", ACCOUNT_MODES, ACCOUNT_MODE_MEANINGS)
        eval_choice = self.read_choice("eval", EVAL_CHOICES, EVAL_CHOICE_MEANINGS)

        new_eval_table = self.read_section("new_eval", required=eval_choice == NEW_EVAL)
        if eval_choice == EXISTING_EVAL and new_eval_table is not None:
            self.problems.append(new_eval_not_used_failure(self.answers_file))
        new_eval = None
        if eval_choice == NEW_EVAL and new_eval_table is not None:
            new_eval = self.read_new_eval(new_eval_table)

        existing_eval_table = self.read_section("existing_eval", required=False)
        existing_workstreams = None
        if existing_eval_table is not None and eval_choice == NEW_EVAL:
            self.problems.append(existing_eval_not_used_failure(self.answers_file))
        elif existing_eval_table is not None:
            existing_workstreams = self.read_name_list(existing_eval_table, "workstreams", None, "existing_eval")
            if existing_workstreams is not None and records.UNKNOWN_WORKSTREAM in existing_workstreams:
                self.invalid("workstreams", "existing_eval", "may not include unknown, which goodall keeps for itself")
                existing_workstreams = None

        contributor = self.read_section("contributor", required=True)
        contributor_id = None
        display_name = None
        main_workstream = None
        if contributor is not None:
            contributor_id = self.read_name(contributor, "contributor", "id")
            if new_eval_table is not None and eval_choice == NEW_EVAL:
                display_name = self.read_text(contributor, "contributor", "display_name", required=True)
                self.refuse_email_address(display_name, "contributor", "display_name")
            main_workstream = self.read_name(contributor, "contributor", "main_workstream")
            if main_workstream == records.UNKNOWN_WORKSTREAM:
                self.invalid("main_workstream", "contributor", "may not be unknown, which goodall keeps for itself")
                main_workstream = None
            if new_eval is not None and main_workstream is not None and main_workstream not in new_eval.workstreams:
                self.problems.append(
                    main_workstream_not_in_workstreams_failure(self.answers_file, new_eval.workstreams)
                )

        folders = self.read_section("folders", required=True)
        record_repo = None
        log_directory = None
        if folders is not None:
            record_repo = self.read_folder(folders, "record_repo")
            log_directory = self.read_folder(folders, "log_directory")
        if record_repo is not None and log_directory is not None:
            self.check_folders_apart(record_repo, log_directory)

        secrets_manager, secret_references = self.read_secrets()
        activitywatch_port, activitywatch_device_id = self.read_activitywatch()
        goodall_program = self.read_goodall_program()
        eval_account = self.read_eval_account(account_mode, eval_choice)

        if self.problems:
            return self.problems
        # Every check above passed, so every value below is present and usable.
        assert account_mode is not None and eval_choice is not None
        assert contributor_id is not None and main_workstream is not None
        assert record_repo is not None and log_directory is not None
        return Answers(
            answers_file=self.answers_file,
            account_mode=account_mode,
            eval_choice=eval_choice,
            new_eval=new_eval,
            contributor_id=contributor_id,
            display_name=display_name,
            main_workstream=main_workstream,
            record_repo=record_repo,
            log_directory=log_directory,
            secrets_manager=secrets_manager,
            secret_references=secret_references,
            activitywatch_port=activitywatch_port,
            activitywatch_device_id=activitywatch_device_id,
            goodall_program=goodall_program,
            existing_workstreams=existing_workstreams,
            eval_account=eval_account,
        )

    def find_secret_values(self, value: object, place: str) -> None:
        """Refuse every text value, at any depth, holding a key in a recognized format or a secret reference.

        `[secrets.references]` is checked by `read_secrets` instead, which
        says that a reference, not the key, belongs there.
        """
        if place == "secrets.references":
            return
        if isinstance(value, dict):
            for key, inner_value in value.items():
                self.find_secret_values(inner_value, joined(place, key))
            return
        if isinstance(value, list):
            for item in value:
                self.find_secret_values(item, place)
            return
        if not isinstance(value, str):
            return
        key_format = key_format_in(value)
        if key_format is not None:
            self.problems.append(secret_value_failure(self.answers_file, place_as_shown(place), key_format.issuer))
        elif looks_like_a_secret_reference(value):
            self.problems.append(reference_out_of_place_failure(self.answers_file, place_as_shown(place)))

    def find_unknown_keys(self, table: dict[str, object], section: str) -> None:
        """Report every key goodall does not know, in `table` and in the sections under it."""
        for key, value in table.items():
            if key not in KNOWN_KEYS[section]:
                self.problems.append(answers_key_unknown_failure(self.answers_file, key, section))
                continue
            inner_section = joined(section, key)
            if inner_section in KNOWN_KEYS and isinstance(value, dict):
                self.find_unknown_keys(value, inner_section)
            if inner_section in KNOWN_KEYS and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self.find_unknown_keys(item, inner_section)

    def read_choice(self, key: str, choices: list[str], meanings: dict[str, str]) -> str | None:
        """A top-level key whose value must be one of `choices`."""
        if key not in self.table:
            self.problems.append(self.key_missing(key, TOP_OF_FILE))
            return None
        value = self.table[key]
        if not isinstance(value, str) or value not in choices:
            self.problems.append(choice_invalid_failure(self.answers_file, key, meanings))
            return None
        return value

    def read_section(self, name: str, required: bool) -> dict[str, object] | None:
        """The `[name]` table, or None when it is absent (a problem only if `required`) or not a table."""
        if name not in self.table:
            if required:
                self.problems.append(answers_key_missing_failure(self.answers_file, f"[{name}] section"))
            return None
        value = self.table[name]
        if not isinstance(value, dict):
            self.invalid(name, TOP_OF_FILE, f"must be a section of its own, written [{name}] on a line of its own")
            return None
        return value

    def read_new_eval(self, table: dict[str, object]) -> NewEval | None:
        """The `[new_eval]` section, checked by the eval config's own rules."""
        name = self.read_name(table, "new_eval", "name")
        description = self.read_text(table, "new_eval", "description", required=True)
        self.refuse_email_address(description, "new_eval", "description")
        workstreams = self.read_name_list(table, "workstreams", None, "new_eval")
        if workstreams is not None and records.UNKNOWN_WORKSTREAM in workstreams:
            self.invalid("workstreams", "new_eval", "may not include unknown, which goodall keeps for itself")
            workstreams = None
        intervention_categories = self.read_name_list(
            table, "intervention_categories", list(records.DEFAULT_INTERVENTION_CATEGORIES), "new_eval"
        )
        activitywatch = False
        if "activitywatch" in table:
            if isinstance(table["activitywatch"], bool):
                activitywatch = table["activitywatch"]
            else:
                self.invalid("activitywatch", "new_eval", "must be true or false, without quotes")
        construct = self.read_text(table, "new_eval", "construct", required=False)
        scope = self.read_scope(table)
        if name is None or description is None or workstreams is None or intervention_categories is None:
            return None
        if scope is None:
            return None
        return NewEval(
            name=name,
            description=description,
            workstreams=workstreams,
            intervention_categories=intervention_categories,
            activitywatch=activitywatch,
            scope=scope,
            construct=construct,
        )

    def read_scope(self, table: dict[str, object]) -> list[ScopeEntry] | None:
        """The `[[new_eval.scope]]` blocks: at least one, each a repository identity and its path patterns."""
        if "scope" not in table:
            self.problems.append(self.key_missing("scope", "new_eval"))
            return None
        blocks = table["scope"]
        if not isinstance(blocks, list) or len(blocks) == 0:
            self.invalid("scope", "new_eval", "must be one or more [[new_eval.scope]] blocks")
            return None
        scope: list[ScopeEntry] = []
        for position, block in enumerate(blocks):
            where = f"[[new_eval.scope]] number {position + 1}"
            if not isinstance(block, dict):
                self.invalid("scope", "new_eval", "must be one or more [[new_eval.scope]] blocks")
                return None
            repository = self.read_text(block, "new_eval.scope", "repository", required=True, where=where)
            if repository is not None:
                repository_problem = describe_repository_problem(repository)
                if repository_problem is not None:
                    self.problems.append(
                        value_invalid_failure(
                            self.answers_file,
                            f"repository in {where}",
                            f"is not a repository identity: {repository_problem}",
                            EXAMPLE_LINES["new_eval.scope.repository"],
                        )
                    )
                    repository = None
            paths = block.get("paths")
            path_list: list[str] = []
            if not isinstance(paths, list) or len(paths) == 0:
                self.problems.append(
                    value_invalid_failure(
                        self.answers_file,
                        f"paths in {where}",
                        'must be a list of at least one pattern, such as ["**"] for everything',
                        EXAMPLE_LINES["new_eval.scope.paths"],
                    )
                )
                continue
            for path_pattern in paths:
                if not isinstance(path_pattern, str) or path_pattern_leaves_repository(path_pattern):
                    self.problems.append(
                        value_invalid_failure(
                            self.answers_file,
                            f"paths in {where}",
                            "must hold only patterns inside the repository, in double quotes, never a path on "
                            "one machine or one that climbs out with ..",
                            EXAMPLE_LINES["new_eval.scope.paths"],
                        )
                    )
                    path_list = []
                    break
                path_list.append(path_pattern)
            if repository is not None and path_list:
                scope.append(ScopeEntry(repository=repository, paths=path_list))
        if len(scope) != len(blocks):
            return None
        return scope

    def read_text(
        self, table: dict[str, object], section: str, key: str, required: bool, where: str | None = None
    ) -> str | None:
        """Text under `key`, not empty; None when absent (a problem only if `required`) or not text."""
        if key not in table:
            if required:
                if where is None:
                    self.problems.append(self.key_missing(key, section))
                else:
                    self.problems.append(answers_key_missing_failure(self.answers_file, f"{key} in {where}"))
            return None
        value = table[key]
        if not isinstance(value, str) or value.strip() == "":
            self.invalid(key, section, "must be text in double quotes, and not empty")
            return None
        return value

    def read_name(self, table: dict[str, object], section: str, key: str) -> str | None:
        """A required name: lowercase letters, digits and hyphens, as the eval config writes names."""
        value = self.read_text(table, section, key, required=True)
        if value is None:
            return None
        if NAME_PATTERN.fullmatch(value) is None:
            self.invalid(key, section, f"must be a name of {NAME_RULE}")
            return None
        return value

    def read_name_list(
        self, table: dict[str, object], key: str, default: list[str] | None, section: str
    ) -> list[str] | None:
        """A list of names in `[section]`; `default` when absent, which is a problem when there is none."""
        if key not in table:
            if default is None:
                self.problems.append(self.key_missing(key, section))
            return default
        value = table[key]
        if not isinstance(value, list) or len(value) == 0:
            self.invalid(key, section, "must be a list of at least one name in double quotes")
            return None
        names: list[str] = []
        for item in value:
            if not isinstance(item, str) or NAME_PATTERN.fullmatch(item) is None:
                self.invalid(key, section, f"must hold only names of {NAME_RULE}")
                return None
            if item in names:
                self.invalid(key, section, "must not name the same thing twice")
                return None
            names.append(item)
        return names

    def read_folder(self, table: dict[str, object], key: str) -> FolderSetting | None:
        """A folder under `[folders]`: a full path, starting with / or ~/."""
        as_written = self.read_text(table, "folders", key, required=True)
        if as_written is None:
            return None
        if "\x00" in as_written:
            self.invalid(key, "folders", "contains a null character (written \\u0000), which no path can contain")
            return None
        path = expand_home_folder(as_written)
        if not path.is_absolute():
            self.invalid(key, "folders", "is not a full path. A full path starts with / or with ~/, your home folder")
            return None
        return FolderSetting(as_written=as_written, path=path)

    def check_folders_apart(self, record_repo: FolderSetting, log_directory: FolderSetting) -> None:
        """The log directory must be neither the record repo nor inside it, nor the other way round.

        Raw records stay out of the record repo (ADR 0017): the record repo
        is committed, and the log directory holds every transcript.
        """
        if (
            record_repo.path == log_directory.path
            or record_repo.path in log_directory.path.parents
            or log_directory.path in record_repo.path.parents
        ):
            self.problems.append(folders_overlap_failure(self.answers_file))

    def read_secrets(self) -> tuple[str, dict[str, str]]:
        """The secrets manager, and each env file variable with its secret reference."""
        secrets = self.read_section("secrets", required=False)
        if secrets is None:
            return DEFAULT_SECRETS_MANAGER, {}
        manager = DEFAULT_SECRETS_MANAGER
        if "manager" in secrets:
            if secrets["manager"] in SUPPORTED_SECRETS_MANAGERS:
                manager = str(secrets["manager"])
            else:
                self.invalid("manager", "secrets", 'must be "1password", the one secrets manager goodall supports')
        references_table = secrets.get("references", {})
        if not isinstance(references_table, dict):
            self.invalid("references", "secrets", "must be a section of its own, written [secrets.references]")
            return manager, {}
        references: dict[str, str] = {}
        for name, value in references_table.items():
            name_as_shown = variable_as_shown(name)
            if VARIABLE_NAME_PATTERN.fullmatch(name) is None:
                self.problems.append(variable_name_invalid_failure(self.answers_file, name_as_shown))
                continue
            if is_service_account_token_variable(name):
                self.problems.append(service_account_token_failure(self.answers_file, name_as_shown))
                continue
            if not isinstance(value, str) or not is_well_formed_secret_reference(manager, value):
                key_format = None
                if isinstance(value, str):
                    key_format = key_format_in(value)
                self.problems.append(reference_invalid_failure(self.answers_file, name_as_shown, key_format))
                continue
            references[name] = value
        return manager, references

    def read_activitywatch(self) -> tuple[int | None, str | None]:
        """This account's own ActivityWatch port and identifier, when the answers give them (ADR 0016)."""
        activitywatch = self.read_section("activitywatch", required=False)
        if activitywatch is None:
            return None, None
        port = None
        if "port" in activitywatch:
            value = activitywatch["port"]
            if not is_whole_number(value) or value < 1 or value > HIGHEST_PORT:
                self.invalid("port", "activitywatch", f"must be a port number from 1 to {HIGHEST_PORT}, without quotes")
            elif value == ACTIVITYWATCH_DEFAULT_PORT:
                self.problems.append(activitywatch_default_port_failure(self.answers_file))
            else:
                port = value
        device_id = None
        if "device_id" in activitywatch:
            value = activitywatch["device_id"]
            if not isinstance(value, str) or DEVICE_ID_PATTERN.fullmatch(value) is None:
                self.invalid(
                    "device_id",
                    "activitywatch",
                    "is not an identifier in the shape ActivityWatch reports: 36 characters of digits, letters a "
                    f"to f and hyphens. ActivityWatch shows it as device_id at {ACTIVITYWATCH_INFO_PATH} on its own "
                    "port",
                )
            else:
                device_id = value.lower()
        return port, device_id

    def read_goodall_program(self) -> str | None:
        """The goodall the hooks should run, as a full path, or None to find it on the PATH."""
        claude_code = self.read_section("claude_code", required=False)
        if claude_code is None or "goodall_program" not in claude_code:
            return None
        program = self.read_text(claude_code, "claude_code", "goodall_program", required=True)
        if program is None:
            return None
        if not (program.startswith("/") or program.startswith("~/")) or "\n" in program or "\x00" in program:
            self.invalid(
                "goodall_program",
                "claude_code",
                "must be the full path of the goodall command, as command -v goodall prints it",
            )
            return None
        return program

    def read_eval_account(self, account_mode: str | None, eval_choice: str | None) -> EvalAccountAnswer | None:
        """The `[eval_account]` table, or None when the file has none."""
        table = self.read_section("eval_account", required=False)
        if table is None:
            return None
        if "create" not in table:
            self.problems.append(self.key_missing("create", "eval_account"))
            return None
        create = table["create"]
        if not isinstance(create, bool):
            self.invalid("create", "eval_account", "must be true or false, without quotes")
            return None
        if not create:
            if "name" in table:
                self.invalid(
                    "name",
                    "eval_account",
                    "has no effect, because goodall creates an account only with create = true. Remove the name, "
                    "or set create = true",
                )
                return None
            return EvalAccountAnswer(create=False, name=None)

        name = self.read_text(table, "eval_account", "name", required=True)
        if "name" in table and name is None:
            # read_text has already said why: not text, or empty.
            return None
        if name is not None and ACCOUNT_NAME_PATTERN.fullmatch(name) is None:
            self.invalid("name", "eval_account", f"is not an account name goodall will create: use {ACCOUNT_NAME_RULE}")
            return None
        if account_mode is not None and account_mode != DEDICATED_ACCOUNT_MODE:
            self.problems.append(eval_account_needs_dedicated_mode_failure(self.answers_file))
            return None
        if eval_choice is not None and eval_choice != NEW_EVAL:
            self.problems.append(eval_account_needs_new_eval_failure(self.answers_file))
            return None
        if name is None:
            return None
        return EvalAccountAnswer(create=True, name=name)

    def refuse_email_address(self, value: str | None, section: str, key: str) -> None:
        """Refuse an email address in what will be the eval config, which may be published (ADR 0009)."""
        if value is not None and EMAIL_ADDRESS_PATTERN.search(value) is not None:
            self.problems.append(email_address_failure(self.answers_file, place_as_shown(joined(section, key))))

    def key_missing(self, key: str, section: str) -> Failure:
        """The failure for a required key that is not there."""
        return answers_key_missing_failure(self.answers_file, place_as_shown(joined(section, key)))

    def invalid(self, key: str, section: str, problem: str) -> None:
        """Record that `key` in `section` is there but its value cannot be used."""
        self.problems.append(
            value_invalid_failure(
                self.answers_file,
                place_as_shown(joined(section, key)),
                problem,
                EXAMPLE_LINES.get(joined(section, key), FORMAT_DOCUMENT),
            )
        )


# What each choice means, for the message that lists them.
ACCOUNT_MODE_MEANINGS = {
    DEDICATED_ACCOUNT_MODE: "for an eval account, used for this eval and nothing else",
    MIXED_ACCOUNT_MODE: "for a macOS user account you also use for other things",
}
EVAL_CHOICE_MEANINGS = {
    NEW_EVAL: "to start a new eval in a new record repo",
    EXISTING_EVAL: "to use the record repo, and its eval.toml, already on this Mac",
}


def joined(section: str, key: str) -> str:
    """The dotted place of `key` inside `section`, such as "contributor.id"."""
    if section == TOP_OF_FILE:
        return key
    return f"{section}.{key}"


def place_as_shown(place: str) -> str:
    """How a message names a dotted place: "account_mode", or "[contributor] id"."""
    section, separator, key = place.rpartition(".")
    if separator == "":
        return key
    return f"[{section}] {key}"


def variable_as_shown(name: str) -> str:
    """An env file variable's name, or words standing for it when the name could itself be a secret."""
    if can_show_name(name):
        return name
    return "a variable whose name goodall does not show"


WHY_NO_SECRETS = (
    "An answers file holds no secret, only references into your secrets manager, so that no key is ever kept in "
    "a file (ADR 0010). Goodall does not show the value, in case it is a key."
)


def answers_file_missing_failure(answers_file: Path) -> Failure:
    """The failure for an answers file that is not where the command said."""
    return Failure(
        kind="answers_file_missing",
        what_happened=f"There is no answers file at {answers_file}. Nothing was changed.",
        why="goodall onboard --answers reads its answers from that file, so it has nothing to set up from.",
        what_to_do_next=(
            f"Check the path, or write an answers file as {FORMAT_DOCUMENT} shows, then run goodall onboard "
            "--answers with its full path."
        ),
    )


def answers_file_unreadable_failure(answers_file: Path, reason: str) -> Failure:
    """The failure for an answers file that is there but cannot be read."""
    return Failure(
        kind="answers_file_unreadable",
        what_happened=f"The answers file at {answers_file} could not be read: {reason}. Nothing was changed.",
        why="Goodall sets up the eval from that file, so it must be able to read it.",
        what_to_do_next=f"Check that {answers_file} is a file this macOS user account can read, then run it again.",
    )


def answers_file_not_toml_failure(answers_file: Path, reason: str) -> Failure:
    """The failure for an answers file that is not valid TOML."""
    return Failure(
        kind="answers_file_not_toml",
        what_happened=f"The answers file at {answers_file} is not valid TOML: {reason}. Nothing was changed.",
        why="Goodall reads the answers file as TOML, and one mistake stops it reading any of the file.",
        what_to_do_next=(
            f"Open {answers_file} in a text editor and correct the place named above. Text values need double "
            f"quotes around them. {FORMAT_DOCUMENT} shows a whole answers file."
        ),
    )


def answers_schema_too_new_failure(answers_file: Path, schema_version: int) -> Failure:
    """The failure for an answers file written for a newer goodall."""
    return Failure(
        kind="answers_schema_too_new",
        what_happened=(
            f"The answers file at {answers_file} has schema_version {schema_version}, but this goodall understands "
            f"only schema_version {SUPPORTED_SCHEMA_VERSION}. Nothing was changed."
        ),
        why="It was written for a newer goodall, which may ask questions this one does not know.",
        what_to_do_next="Upgrade goodall, or write the answers file for this version, then run it again.",
    )


def one_time_code_mismatch_failure(answers_file: Path) -> Failure:
    """The failure for an answers file that does not carry the code of the setup page goodall opened."""
    return Failure(
        kind="answers_one_time_code_mismatch",
        what_happened=(
            f"The answers file at {answers_file} does not carry the one-time code of the setup page goodall "
            "opened, so goodall did not use it. Nothing was changed."
        ),
        why=(
            "Goodall accepts answers only from the page it opened, so that an older answers file, or one "
            "another program wrote, cannot set up this account."
        ),
        what_to_do_next=(
            "Press Finish setup on the setup page goodall just opened. To set up from this file on purpose, run "
            "goodall onboard --answers with its full path."
        ),
    )


def answers_key_missing_failure(answers_file: Path, place: str) -> Failure:
    """The failure for a required answer that is not in the file."""
    return Failure(
        kind="answers_key_missing",
        what_happened=f"The answers file at {answers_file} has no {place}. Nothing was changed.",
        why=f"Goodall needs {place} to set up this macOS user account for the eval.",
        what_to_do_next=f"Add it to the answers file. {FORMAT_DOCUMENT} shows each answer with an example.",
    )


def answers_key_unknown_failure(answers_file: Path, key: str, section: str) -> Failure:
    """The failure for a key goodall does not know, named only when it may be shown."""
    known = ", ".join(KNOWN_KEYS[section])
    if section == TOP_OF_FILE:
        where = "at the top of the file"
    else:
        where = f"in [{section}]"
    if can_show_name(key):
        what_happened = f"The answers file at {answers_file} has {key} {where}, which goodall does not know."
        advice = f"Correct or remove {key}."
    else:
        what_happened = (
            f"The answers file at {answers_file} has a key {where} that goodall does not know. Goodall does not "
            "show its name, in case it is a secret pasted by mistake."
        )
        advice = "Remove it. If it was a real key, replace it with whoever issued it, because it may have been exposed."
    return Failure(
        kind="answers_key_unknown",
        what_happened=what_happened + " Nothing was changed.",
        why="An answer goodall does not know has no effect, so a misspelled one would look as if it took effect.",
        what_to_do_next=f"{advice} The keys goodall knows {where} are {known}.",
    )


def value_invalid_failure(answers_file: Path, place: str, problem: str, example: str) -> Failure:
    """The failure for an answer that is there but cannot be used."""
    return Failure(
        kind="answers_value_invalid",
        what_happened=f"In the answers file at {answers_file}, {place} {problem}. Nothing was changed.",
        why=f"Goodall cannot set up the eval until {place} has the right form.",
        what_to_do_next=f"Change it in the answers file to this form, with your own value: {example}",
    )


def choice_invalid_failure(answers_file: Path, key: str, meanings: dict[str, str]) -> Failure:
    """The failure for a key that must be one of a few words, listing each with what it means."""
    choices = []
    for choice, meaning in meanings.items():
        choices.append(f'{key} = "{choice}", {meaning}')
    return Failure(
        kind="answers_value_invalid",
        what_happened=(
            f"In the answers file at {answers_file}, {key} is not one of the words goodall knows. Nothing was changed."
        ),
        why=f"What goodall sets up depends on {key}, so it does not guess.",
        what_to_do_next="Write one of these, in lowercase and in double quotes: " + "; or ".join(choices) + ".",
    )


def new_eval_not_used_failure(answers_file: Path) -> Failure:
    """The failure for [new_eval] answers given for an existing eval, which would be silently ignored."""
    return Failure(
        kind="answers_section_not_used",
        what_happened=(
            f'The answers file at {answers_file} says eval = "{EXISTING_EVAL}" but also has a [new_eval] section. '
            "Nothing was changed."
        ),
        why=(
            "For an existing eval goodall keeps the eval.toml already in the record repo, so [new_eval] would have "
            "no effect, and goodall does not ignore an answer without saying so."
        ),
        what_to_do_next=(
            f'Remove the [new_eval] section to use the existing eval, or set eval = "{NEW_EVAL}" to start a new one.'
        ),
    )


def existing_eval_not_used_failure(answers_file: Path) -> Failure:
    """The failure for [existing_eval] answers given for a new eval, which would be silently ignored."""
    return Failure(
        kind="answers_section_not_used",
        what_happened=(
            f'The answers file at {answers_file} says eval = "{NEW_EVAL}" but also has an [existing_eval] section. '
            "Nothing was changed."
        ),
        why="[existing_eval] changes the workstreams of an eval already on this Mac, so it has no effect on a new one.",
        what_to_do_next=(
            f'Remove the [existing_eval] section, or set eval = "{EXISTING_EVAL}" to change the eval already here.'
        ),
    )


def main_workstream_not_in_workstreams_failure(answers_file: Path, workstreams: list[str]) -> Failure:
    """The failure for a main workstream the new eval does not have."""
    return Failure(
        kind="answers_main_workstream_unknown",
        what_happened=(
            f"In the answers file at {answers_file}, [contributor] main_workstream is not one of the workstreams "
            "in [new_eval]. Nothing was changed."
        ),
        why="A work block is recorded on your main workstream unless you name another, so it must be one of them.",
        what_to_do_next=f"Set main_workstream to one of {', '.join(workstreams)}, or add it to workstreams.",
    )


def folders_overlap_failure(answers_file: Path) -> Failure:
    """The failure for a log directory that is, or is inside, the record repo, or the other way round."""
    return Failure(
        kind="answers_folders_overlap",
        what_happened=(
            f"In the answers file at {answers_file}, the record repo and the log directory are the same folder, or "
            "one is inside the other. Nothing was changed."
        ),
        why=(
            "The record repo is a git repository goodall commits to, and the log directory holds every raw record, "
            "transcripts included, which must never be committed."
        ),
        what_to_do_next=(
            "Choose two separate folders, for example "
            f"{EXAMPLE_LINES['folders.record_repo']} and {EXAMPLE_LINES['folders.log_directory']}."
        ),
    )


def secret_value_failure(answers_file: Path, place: str, issuer: str) -> Failure:
    """The failure for a key in a recognized format, found where an answer should be."""
    return Failure(
        kind="answers_secret_value",
        what_happened=(
            f"In the answers file at {answers_file}, {place} holds what looks like a key issued by {issuer}. "
            "Nothing was changed."
        ),
        why=WHY_NO_SECRETS,
        what_to_do_next=(
            f"Remove the key from the answers file, and replace the key with {issuer}, because it may have been "
            "exposed. A key goodall should use belongs in your secrets manager, with its reference "
            f"(op://vault/item/field) under [secrets.references]; {FORMAT_DOCUMENT} shows how."
        ),
    )


def reference_out_of_place_failure(answers_file: Path, place: str) -> Failure:
    """The failure for a secret reference outside [secrets.references]."""
    return Failure(
        kind="answers_value_invalid",
        what_happened=(
            f"In the answers file at {answers_file}, {place} holds a secret reference. Nothing was changed."
        ),
        why=(
            "A secret reference names the vault and item a secret is kept in. Goodall keeps references only in "
            "the env file, readable by this account alone, never in local settings or the eval config."
        ),
        what_to_do_next=(
            "Move the reference under [secrets.references], with the name of the environment variable it is for, "
            f"for example {EXAMPLE_LINES['secrets.references']}."
        ),
    )


def reference_invalid_failure(answers_file: Path, name_as_shown: str, key_format: KeyFormat | None) -> Failure:
    """The failure for a value under [secrets.references] that is not a well-formed secret reference.

    `key_format` is the format the value matched, if any, so the message can
    say who replaces a key that was pasted here.
    """
    advice = ""
    if key_format is not None:
        advice = (
            f" It looks like a key issued by {key_format.issuer}: replace that key with {key_format.issuer}, "
            "because it may have been exposed."
        )
    return Failure(
        kind="answers_reference_invalid",
        what_happened=(
            f"In the answers file at {answers_file}, [secrets.references] {name_as_shown} is not a 1Password "
            "secret reference. Nothing was changed."
        ),
        why=WHY_NO_SECRETS,
        what_to_do_next=(
            "Put the key in 1Password and write its reference instead, in the form op://vault/item/field, never "
            f"the key itself: for example {EXAMPLE_LINES['secrets.references']}.{advice}"
        ),
    )


def variable_name_invalid_failure(answers_file: Path, name_as_shown: str) -> Failure:
    """The failure for a name under [secrets.references] that cannot be an environment variable."""
    return Failure(
        kind="answers_value_invalid",
        what_happened=(
            f"In the answers file at {answers_file}, [secrets.references] has {name_as_shown}, which is not an "
            "environment variable name. Nothing was changed."
        ),
        why="Each reference is handed to programs under its name, so the name must be one a program can read.",
        what_to_do_next=(
            "Use letters, digits and underscores, not starting with a digit, such as ANTHROPIC_API_KEY_RESEARCH."
        ),
    )


def service_account_token_failure(answers_file: Path, name_as_shown: str) -> Failure:
    """The failure for 1Password's service account token among the references (goodall.secrets_manager)."""
    return Failure(
        kind="answers_value_invalid",
        what_happened=(
            f"In the answers file at {answers_file}, [secrets.references] sets {name_as_shown}. Nothing was changed."
        ),
        why=(
            "1Password needs that token before it can resolve any reference, so it can never be a reference "
            "itself, and in a file it would be a secret anyone reading the file could take."
        ),
        what_to_do_next=(
            f"Remove {name_as_shown} from the answers file. Sign in to 1Password in this account instead, as "
            "goodall doctor describes."
        ),
    )


def email_address_failure(answers_file: Path, place: str) -> Failure:
    """The failure for an email address in what will become the eval config."""
    return Failure(
        kind="answers_value_invalid",
        what_happened=(
            f"In the answers file at {answers_file}, {place} holds an email address. Goodall does not show it. "
            "Nothing was changed."
        ),
        why="It would be written into the eval config, which is made to be published as it stands (ADR 0009).",
        what_to_do_next='Write a name instead, such as display_name = "Ada Lovelace".',
    )


def eval_account_needs_dedicated_mode_failure(answers_file: Path) -> Failure:
    """The failure for an eval account to create in mixed account mode, which records in an account of one's own."""
    return Failure(
        kind="answers_eval_account_needs_dedicated_mode",
        what_happened=(
            f"The answers file at {answers_file} asks goodall to create an eval account, but its account_mode is "
            "not dedicated. Nothing was changed."
        ),
        why=(
            "An eval account is used for one eval and nothing else, which is dedicated account mode. Mixed account "
            "mode records in an account you already use, so there is no account to create (ADR 0021)."
        ),
        what_to_do_next=(
            f'Set account_mode = "{DEDICATED_ACCOUNT_MODE}" to create the eval account, or remove the '
            "[eval_account] section and run goodall onboard in the account you mean to record in."
        ),
    )


def eval_account_needs_new_eval_failure(answers_file: Path) -> Failure:
    """The failure for an eval account to create for an existing eval, whose record repo a new account cannot have."""
    return Failure(
        kind="answers_eval_account_needs_new_eval",
        what_happened=(
            f"The answers file at {answers_file} asks goodall to create an eval account, but says eval = "
            f'"{EXISTING_EVAL}". Nothing was changed.'
        ),
        why=(
            "An existing eval's record repo is in the macOS user account that already records it. A new account "
            "starts empty, so it has no record repo to use."
        ),
        what_to_do_next=(
            f'Set eval = "{NEW_EVAL}" and describe the eval under [new_eval] to start it in the new account. To '
            "carry on an existing eval, run goodall onboard in the account that has its record repo instead."
        ),
    )


def activitywatch_default_port_failure(answers_file: Path) -> Failure:
    """The failure for ActivityWatch's own default port, which an eval account must not use (ADR 0016)."""
    return Failure(
        kind="answers_activitywatch_default_port",
        what_happened=(
            f"In the answers file at {answers_file}, [activitywatch] port is {ACTIVITYWATCH_DEFAULT_PORT}, "
            "ActivityWatch's default port. Nothing was changed."
        ),
        why=(
            "Ports are shared by every macOS user account on this Mac, and ActivityWatch silently joins any "
            "ActivityWatch already answering on its port, so this account's record could mix with another's."
        ),
        what_to_do_next=(
            "Choose another port nothing else on this Mac uses, such as port = 5610, set it in ActivityWatch's "
            "own settings as Part 3 of docs/setup-by-hand.md shows, and write the same port here."
        ),
    )
