"""Reading and checking an eval's eval config, and checking local settings against it.

The eval config is the file `eval.toml` at the top of the record repo. It
holds what is true for every contributor to one eval (ADR 0009): the eval's
name and description, its workstreams, its intervention categories, the
roster of contributors, and the declared scope. Local settings say where the
record repo is (goodall.local_settings); this module takes it from there.

Two functions are the way in:

- `read_eval_config(record_repo_folder)` reads and checks the file on its own.
- `read_eval_for_this_account(local_settings)` does that, then checks that
  this macOS user account's contributor is in the roster and that its main
  workstream is one of the eval's workstreams. Those cross-checks live here,
  beside the roster and workstreams they compare against, so every command
  words them the same way.

Both return either records or a list of `Failure` records, and never raise
for bad input. Every problem is reported at once, so a hand edit can fix them
all, with one exception: a file written for a newer schema version gets only
"upgrade goodall". This mirrors goodall.local_settings on purpose: the two
readers are one design, so a contributor learns to read their failures once.

Rules a reader might otherwise reverse-engineer:

- Names (contributor ids, workstreams, intervention categories) use
  lowercase letters, digits and hyphens, such as `go-to-market`. One spelling
  per name means two contributors' records can be compared without guessing
  whether `Research` and `research` are the same workstream.
- The declared scope names a repository by identity, `host/owner/name`,
  never by a local path or a web address: a path names one copy on one
  machine, and the scope must mean the same repository for everyone.
  `paths` are patterns inside that repository and may not climb out of it.
- The eval config may be published as it stands (ADR 0009), so no value in
  it may contain an email address or a secret reference.
- Failures never quote a value from either file back, because a value in
  the wrong place may be a secret pasted by mistake (ADR 0006). The
  exceptions are names that already passed the name rule above, which
  cannot hold an email address or a secret reference, and file paths.

This module imports only the standard library, because the prompt gate will
read the eval config on every prompt (ADR 0005).

The file's shape is provisional by the owner's instruction (goodall-xes.8);
`schema_version` is there so it can change.
"""

import difflib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from goodall.local_settings import LocalSettings, is_whole_number
from goodall.records import UNKNOWN_WORKSTREAM
from goodall.terminal import Failure

# The name of the eval config file at the top of the record repo.
EVAL_CONFIG_FILE_NAME = "eval.toml"

# The newest layout of the eval config this goodall understands. Raise it, and
# teach the reader the new layout, whenever the keys below change meaning.
SUPPORTED_SCHEMA_VERSION = 1

# Every top-level key the eval config may contain, in the order goodall
# reports them. All are required.
KNOWN_KEYS = [
    "schema_version",
    "name",
    "description",
    "workstreams",
    "intervention_categories",
    "contributors",
    "scope",
]

# How messages name the part of the file above the first [[block]]. Messages
# leave it out, because a key with no block named is plainly a top-level one.
TOP_OF_FILE = "the top of the file"

# Top-level keys whose value is a list of names.
NAME_LIST_KEYS = ["workstreams", "intervention_categories"]

# The keys each [[contributors]] block and each [[scope]] block holds. All
# are required.
CONTRIBUTOR_KEYS = ["id", "display_name"]
SCOPE_KEYS = ["repository", "paths"]

# A name: lowercase letters and digits, in words joined by single hyphens,
# such as "ada-lovelace" or "go-to-market".
NAME_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
NAME_RULE = "lowercase letters, digits and hyphens, starting and ending with a letter or digit"

# One part of a repository identity, such as "github.com", "your-org" or
# "goodall-pilot-app".
REPOSITORY_PART_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

# Anything shaped like an email address. Deliberately broad: a false alarm
# costs one edit, a missed address is published.
EMAIL_ADDRESS_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")

# The start of a secret reference. 1Password's op:// is the one kind
# supported today (ADR 0006); add a manager's prefix here when it is.
SECRET_REFERENCE_PREFIXES = ["op://"]

# Shown in "for example" advice, so a contributor sees a correct line. They
# match the agreed example in goodall-xes.8.
EXAMPLE_LINES = {
    "schema_version": f"schema_version = {SUPPORTED_SCHEMA_VERSION}",
    "name": 'name = "goodall-pilot"',
    "description": 'description = "Building a SaaS product to first recurring revenue with AI."',
    "workstreams": 'workstreams = ["engineering", "research", "go-to-market"]',
    "intervention_categories": (
        'intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]'
    ),
    "contributors": '[[contributors]] followed by id = "ada-lovelace" and display_name = "Ada Lovelace"',
    "scope": '[[scope]] followed by repository = "github.com/your-org/goodall-pilot-app" and paths = ["**"]',
    "id": 'id = "ada-lovelace"',
    "display_name": 'display_name = "Ada Lovelace"',
    "repository": 'repository = "github.com/your-org/goodall-pilot-app"',
    "paths": 'paths = ["**"]',
}


@dataclass(frozen=True)
class Contributor:
    """One entry in the eval's roster."""

    id: str
    display_name: str


@dataclass(frozen=True)
class ScopeEntry:
    """One repository in the declared scope, and the path patterns inside it that count."""

    repository: str
    paths: list[str]


@dataclass(frozen=True)
class EvalConfig:
    """An eval config that passed every check in this module."""

    config_file: Path
    schema_version: int
    name: str
    description: str
    workstreams: list[str]
    intervention_categories: list[str]
    contributors: list[Contributor]
    scope: list[ScopeEntry]


@dataclass(frozen=True)
class EvalForThisAccount:
    """Everything goodall knows about this macOS user account's eval, all checked.

    `contributor` is this account's entry in the roster, found through the
    contributor id in local settings.
    """

    local_settings: LocalSettings
    eval_config: EvalConfig
    contributor: Contributor


def read_eval_for_this_account(local_settings: LocalSettings) -> EvalForThisAccount | list[Failure]:
    """Read the eval config from the record repo that local settings name, and check the two agree.

    Assumes the record repo folder exists; the caller checks that first, so
    a missing folder is reported as such rather than as a missing file.
    """
    eval_config_reading = read_eval_config(local_settings.record_repo.path, local_settings.settings_file)
    if isinstance(eval_config_reading, list):
        return eval_config_reading
    eval_config = eval_config_reading

    problems: list[Failure] = []
    this_contributor = find_this_contributor(local_settings, eval_config)
    if this_contributor is None:
        problems.append(contributor_not_in_roster_failure(local_settings, eval_config))
    if not main_workstream_is_known(local_settings, eval_config):
        problems.append(main_workstream_unknown_failure(local_settings, eval_config))

    if problems or this_contributor is None:
        return problems
    return EvalForThisAccount(local_settings=local_settings, eval_config=eval_config, contributor=this_contributor)


def find_this_contributor(local_settings: LocalSettings, eval_config: EvalConfig) -> Contributor | None:
    """Return this macOS user account's entry in the roster, or None when its contributor id is not there.

    One of the two cross-checks between local settings and the eval config;
    `goodall doctor` runs each as its own check, so each is a function.
    """
    for contributor in eval_config.contributors:
        if contributor.id == local_settings.contributor:
            return contributor
    return None


def main_workstream_is_known(local_settings: LocalSettings, eval_config: EvalConfig) -> bool:
    """True when this account's main workstream is one of the eval's workstreams."""
    return local_settings.main_workstream in eval_config.workstreams


def read_eval_config(record_repo_folder: Path, local_settings_file: Path) -> EvalConfig | list[Failure]:
    """Read and check the eval config at the top of `record_repo_folder`.

    `local_settings_file` is the file that named the folder. It is only used
    in the message for a missing eval config, where the folder may be wrong.
    """
    config_file = record_repo_folder / EVAL_CONFIG_FILE_NAME

    # Opening the file is how goodall learns whether it exists; see the same
    # trap in goodall.local_settings.read_local_settings_file.
    try:
        file_bytes = config_file.read_bytes()
    except FileNotFoundError:
        return [eval_config_missing_failure(config_file, local_settings_file)]
    except OSError as read_error:
        return [eval_config_unreadable_failure(config_file, read_error)]

    try:
        file_text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return [eval_config_not_toml_failure(config_file, "it contains characters that are not UTF-8 text")]

    try:
        config_table = tomllib.loads(file_text)
    except tomllib.TOMLDecodeError as toml_error:
        # tomllib's message ends with the place, such as "(at line 2, column 8)".
        return [eval_config_not_toml_failure(config_file, str(toml_error))]

    return check_eval_config_table(config_file, config_table)


def check_eval_config_table(config_file: Path, config_table: dict[str, object]) -> EvalConfig | list[Failure]:
    """Check the keys and values parsed from the eval config."""
    problems: list[Failure] = []

    # Stays 0 unless the file holds a usable schema version; 0 is never
    # returned, because a problem is recorded whenever it stays 0.
    schema_version = 0
    schema_version_value = config_table.get("schema_version")
    if "schema_version" not in config_table:
        problems.append(key_missing_failure(config_file, "schema_version", TOP_OF_FILE))
    elif not is_whole_number(schema_version_value) or schema_version_value < 1:
        problems.append(
            value_invalid_failure(config_file, "schema_version", "must be a whole number without quotes", TOP_OF_FILE)
        )
    elif schema_version_value > SUPPORTED_SCHEMA_VERSION:
        return [schema_too_new_failure(config_file, schema_version_value)]
    else:
        schema_version = schema_version_value

    # The privacy checks look at every value in the file, including values
    # under keys goodall does not know, because the whole file is published.
    problems.extend(find_private_values(config_file, config_table, TOP_OF_FILE))

    for key in config_table:
        if key not in KNOWN_KEYS:
            problems.append(key_unknown_failure(config_file, key, KNOWN_KEYS, TOP_OF_FILE))

    name = read_text(config_file, config_table, "name", TOP_OF_FILE, problems)
    description = read_text(config_file, config_table, "description", TOP_OF_FILE, problems)

    name_lists: dict[str, list[str]] = {}
    for key in NAME_LIST_KEYS:
        names = read_name_list(config_file, config_table, key, problems)
        if names is not None:
            name_lists[key] = names

    contributors = read_contributors(config_file, config_table, problems)
    scope = read_scope(config_file, config_table, problems)

    if problems:
        return problems

    # Every check above passed, so every value below is present and usable.
    assert name is not None and description is not None and contributors is not None and scope is not None
    return EvalConfig(
        config_file=config_file,
        schema_version=schema_version,
        name=name,
        description=description,
        workstreams=name_lists["workstreams"],
        intervention_categories=name_lists["intervention_categories"],
        contributors=contributors,
        scope=scope,
    )


def read_text(config_file: Path, table: dict[str, object], key: str, where: str, problems: list[Failure]) -> str | None:
    """Return the text under `key` in `table`, or record a problem and return None.

    `where` says which part of the file `table` is, for the message.
    """
    if key not in table:
        problems.append(key_missing_failure(config_file, key, where))
        return None
    value = table[key]
    if not isinstance(value, str) or value.strip() == "":
        problems.append(value_invalid_failure(config_file, key, "must be text in double quotes, and not empty", where))
        return None
    return value


def read_name_list(config_file: Path, table: dict[str, object], key: str, problems: list[Failure]) -> list[str] | None:
    """Return the list of names under `key`, or record problems and return None.

    The list must hold at least one name, every name must follow the name
    rule, and no name may appear twice.
    """
    where = TOP_OF_FILE
    if key not in table:
        problems.append(key_missing_failure(config_file, key, where))
        return None
    value = table[key]
    if not isinstance(value, list) or len(value) == 0:
        problems.append(
            value_invalid_failure(config_file, key, "must be a list of at least one name in square brackets", where)
        )
        return None

    names: list[str] = []
    found_problem = False
    for item in value:
        if not isinstance(item, str) or NAME_PATTERN.fullmatch(item) is None:
            found_problem = True
            continue
        names.append(item)
    if found_problem:
        problems.append(value_invalid_failure(config_file, key, f"must hold only names made of {NAME_RULE}", where))
        return None

    duplicates = find_duplicates(names)
    for duplicate in duplicates:
        problems.append(duplicate_failure(config_file, f"{key} lists {duplicate} more than once"))
    if duplicates:
        return None
    # Trap: goodall records `unknown` when it cannot tell which workstream a
    # work block was for, so a real workstream by that name would make those
    # work blocks look attributed.
    if key == "workstreams" and UNKNOWN_WORKSTREAM in names:
        problems.append(reserved_workstream_failure(config_file))
        return None
    return names


def read_blocks(
    config_file: Path, table: dict[str, object], key: str, known_keys: list[str], problems: list[Failure]
) -> list[dict[str, object]] | None:
    """Return the [[key]] blocks in `table`, or record a problem and return None.

    Unknown keys inside each block are recorded as problems here, so the
    callers only check the keys they know.
    """
    where = TOP_OF_FILE
    if key not in table:
        problems.append(key_missing_failure(config_file, key, where))
        return None
    value = table[key]
    if not isinstance(value, list) or len(value) == 0:
        problems.append(
            value_invalid_failure(config_file, key, f"must be written as one or more [[{key}]] blocks", where)
        )
        return None

    blocks: list[dict[str, object]] = []
    for position, block in enumerate(value, start=1):
        if not isinstance(block, dict):
            problems.append(
                value_invalid_failure(config_file, key, f"must be written as one or more [[{key}]] blocks", where)
            )
            return None
        for block_key in block:
            if block_key not in known_keys:
                problems.append(key_unknown_failure(config_file, block_key, known_keys, block_name(key, position)))
        blocks.append(block)
    return blocks


def read_contributors(config_file: Path, table: dict[str, object], problems: list[Failure]) -> list[Contributor] | None:
    """Return the roster, or record problems and return None."""
    blocks = read_blocks(config_file, table, "contributors", CONTRIBUTOR_KEYS, problems)
    if blocks is None:
        return None

    problems_before = len(problems)
    contributors: list[Contributor] = []
    for position, block in enumerate(blocks, start=1):
        where = block_name("contributors", position)
        contributor_id = read_text(config_file, block, "id", where, problems)
        display_name = read_text(config_file, block, "display_name", where, problems)
        if contributor_id is not None and NAME_PATTERN.fullmatch(contributor_id) is None:
            problems.append(
                value_invalid_failure(config_file, "id", f"must be made of {NAME_RULE}, such as ada-lovelace", where)
            )
            contributor_id = None
        if contributor_id is not None and display_name is not None:
            contributors.append(Contributor(id=contributor_id, display_name=display_name))

    contributor_ids: list[str] = []
    for contributor in contributors:
        contributor_ids.append(contributor.id)
    for duplicate in find_duplicates(contributor_ids):
        problems.append(duplicate_failure(config_file, f"more than one [[contributors]] block has id {duplicate}"))

    if len(problems) > problems_before:
        return None
    return contributors


def read_scope(config_file: Path, table: dict[str, object], problems: list[Failure]) -> list[ScopeEntry] | None:
    """Return the declared scope, or record problems and return None."""
    blocks = read_blocks(config_file, table, "scope", SCOPE_KEYS, problems)
    if blocks is None:
        return None

    problems_before = len(problems)
    scope: list[ScopeEntry] = []
    for position, block in enumerate(blocks, start=1):
        where = block_name("scope", position)

        repository = read_text(config_file, block, "repository", where, problems)
        if repository is not None:
            repository_problem = describe_repository_problem(repository)
            if repository_problem is not None:
                problems.append(scope_not_a_repository_failure(config_file, where, repository_problem))
                repository = None

        paths = read_path_patterns(config_file, block, where, problems)
        if repository is not None and paths is not None:
            scope.append(ScopeEntry(repository=repository, paths=paths))

    if len(problems) > problems_before:
        return None
    return scope


def read_path_patterns(
    config_file: Path, block: dict[str, object], where: str, problems: list[Failure]
) -> list[str] | None:
    """Return a scope entry's path patterns, or record a problem and return None."""
    if "paths" not in block:
        problems.append(key_missing_failure(config_file, "paths", where))
        return None
    value = block["paths"]
    if not isinstance(value, list) or len(value) == 0:
        problems.append(
            value_invalid_failure(
                config_file, "paths", 'must be a list of at least one pattern, such as ["**"] for everything', where
            )
        )
        return None

    paths: list[str] = []
    for item in value:
        if not isinstance(item, str) or item.strip() == "":
            problems.append(
                value_invalid_failure(config_file, "paths", "must hold only patterns in double quotes", where)
            )
            return None
        if path_pattern_leaves_repository(item):
            problems.append(scope_path_invalid_failure(config_file, where))
            return None
        paths.append(item)
    return paths


def path_pattern_leaves_repository(path_pattern: str) -> bool:
    """True when a path pattern points outside its repository.

    A pattern starting with / or ~ is a path on someone's machine, and a ".."
    part climbs out of the repository, so either would put files that are not
    the declared scope into the dataset.
    """
    if path_pattern.startswith("/") or path_pattern.startswith("~"):
        return True
    for part in path_pattern.replace("\\", "/").split("/"):
        if part == "..":
            return True
    return False


def describe_repository_problem(repository: str) -> str | None:
    """Say why `repository` is not a repository identity, or return None if it is one.

    A repository identity is host/owner/name, such as
    github.com/your-org/goodall-pilot-app. Some hosts allow groups inside
    groups, so more than three parts are accepted. The explanation never
    quotes the value.
    """
    if repository.startswith("/") or repository.startswith("~") or repository.startswith("."):
        return "it is a path to a folder on one machine, not the name of a repository"
    if "://" in repository:
        return "it is a web or file address, which says how to reach the repository rather than which one it is"
    if "\\" in repository or ":" in repository:
        return "it looks like a path on one machine or a git address, not the name of a repository"
    if "@" in repository:
        return "it looks like a git address with a user name in it, not the name of a repository"
    if repository.endswith(".git"):
        return "it ends in .git, so it would not match the same repository written without it"

    parts = repository.split("/")
    if len(parts) < 3:
        return "it has fewer than three parts, so the host or the owner is missing"
    for part in parts:
        if REPOSITORY_PART_PATTERN.fullmatch(part) is None or part in [".", ".."]:
            return "it contains spaces, empty parts or characters that a repository name cannot have"
    if "." not in parts[0]:
        return "its first part is not a host name such as github.com"
    return None


def find_private_values(config_file: Path, value: object, where: str) -> list[Failure]:
    """Look through every text value inside `value` for email addresses and secret references.

    Walks tables and lists all the way down, so nothing in the file is
    missed. Returns one failure per value found, naming where it is.
    """
    problems: list[Failure] = []
    if isinstance(value, str):
        if EMAIL_ADDRESS_PATTERN.search(value) is not None:
            problems.append(contains_email_address_failure(config_file, where))
        for prefix in SECRET_REFERENCE_PREFIXES:
            if prefix in value:
                problems.append(contains_secret_reference_failure(config_file, where))
    elif isinstance(value, dict):
        for key, inner_value in value.items():
            if isinstance(inner_value, list) and all_tables(inner_value):
                for position, block in enumerate(inner_value, start=1):
                    problems.extend(find_private_values(config_file, block, block_name(key, position)))
            elif where == TOP_OF_FILE:
                problems.extend(find_private_values(config_file, inner_value, key))
            else:
                problems.extend(find_private_values(config_file, inner_value, f"{key} in {where}"))
    elif isinstance(value, list):
        for item in value:
            problems.extend(find_private_values(config_file, item, where))
    return problems


def all_tables(items: list[object]) -> bool:
    """True for a non-empty list whose items are all tables, as [[blocks]] are read."""
    if len(items) == 0:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
    return True


def find_duplicates(names: list[str]) -> list[str]:
    """Return each name that appears more than once, once, in the order first repeated."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    return duplicates


def block_name(key: str, position: int) -> str:
    """Name one [[key]] block for a message, such as "the 2nd [[contributors]] block"."""
    if position % 100 in [11, 12, 13]:
        suffix = "th"
    elif position % 10 == 1:
        suffix = "st"
    elif position % 10 == 2:
        suffix = "nd"
    elif position % 10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    return f"the {position}{suffix} [[{key}]] block"


def in_where(where: str) -> str:
    """Turn a part of the file into words to put after a key, or nothing for the top of the file."""
    if where == TOP_OF_FILE:
        return ""
    return f" in {where}"


def is_name(value: str) -> bool:
    """True when `value` follows the name rule, which means it is safe to quote back."""
    return NAME_PATTERN.fullmatch(value) is not None


def eval_config_missing_failure(config_file: Path, local_settings_file: Path) -> Failure:
    """The failure for a record repo folder with no eval config in it."""
    return Failure(
        kind="eval_config_missing",
        what_happened=f"No eval config was found. Goodall looked for {config_file}, at the top of the record repo.",
        why=(
            "The eval config names the eval, its roster of contributors and its workstreams. Without it goodall "
            "cannot tell which eval this is or who you are in it, so nothing can be recorded."
        ),
        what_to_do_next=(
            f"If record_repo in {local_settings_file} points at the wrong folder, correct it. If the folder is right, "
            "bring the record repo up to date (git pull in that folder), or ask whoever runs the eval whether "
            "eval.toml has been added yet."
        ),
    )


def eval_config_unreadable_failure(config_file: Path, read_error: OSError) -> Failure:
    """The failure for an eval config that exists, or may exist, but cannot be opened."""
    return Failure(
        kind="eval_config_unreadable",
        what_happened=f"The eval config at {config_file} could not be read: {read_error.strerror}.",
        why="Goodall needs to read the eval config to know which eval this is and who you are in it.",
        what_to_do_next=(
            f"Check the permissions of {config_file} and the folders above it: this macOS user account must be "
            "able to read them. Then run goodall status again."
        ),
    )


def eval_config_not_toml_failure(config_file: Path, reason: str) -> Failure:
    """The failure for an eval config that cannot be parsed, usually after a hand edit."""
    return Failure(
        kind="eval_config_not_toml",
        what_happened=f"The eval config at {config_file} is not valid TOML: {reason}.",
        why="Goodall reads the eval config as TOML, and one mistake stops it reading any of the file.",
        what_to_do_next=(
            f"Open {config_file} in a text editor and correct the place named above. "
            'Text values need double quotes around them, for example name = "goodall-pilot".'
        ),
    )


def key_missing_failure(config_file: Path, key: str, where: str) -> Failure:
    """The failure for a required key that is not in the eval config, or not in one of its blocks."""
    return Failure(
        kind="eval_config_key_missing",
        what_happened=f"The eval config at {config_file} has no {key}{in_where(where)}.",
        why=f"Every eval config needs {key}, so that every contributor's records describe the eval the same way.",
        what_to_do_next=(
            f"Add {key}{in_where(where)} in {config_file}, in this form, with the eval's own value: "
            f"{EXAMPLE_LINES[key]}"
        ),
    )


def key_unknown_failure(config_file: Path, key: str, known_keys: list[str], where: str) -> Failure:
    """The failure for a key goodall does not know, which is often a misspelling.

    The key's name is quoted, as local settings do: keys are names a person
    typed, and a secret pasted into a key's place would break the TOML first.
    """
    close_matches = difflib.get_close_matches(key, known_keys, n=1, cutoff=0.75)
    if close_matches:
        suggestion = f" Did you mean {close_matches[0]}?"
    else:
        suggestion = ""
    return Failure(
        kind="eval_config_key_unknown",
        what_happened=(
            f"The eval config at {config_file} contains {key}{in_where(where)}, which goodall does not know."
            f"{suggestion}"
        ),
        why=(
            "A key goodall does not know has no effect, so a misspelled name would look like a setting that took "
            "effect when it did not."
        ),
        what_to_do_next=(
            f"Correct or remove {key}{in_where(where)} in {config_file}. "
            f"The keys goodall knows there are {', '.join(known_keys)}."
        ),
    )


def value_invalid_failure(config_file: Path, key: str, problem: str, where: str) -> Failure:
    """The failure for a key that is present but whose value cannot be used."""
    return Failure(
        kind="eval_config_value_invalid",
        what_happened=f"In the eval config at {config_file}, {key}{in_where(where)} {problem}.",
        why=f"Goodall cannot use {key} until its value has the right form.",
        what_to_do_next=(
            f"Change the line in {config_file} to this form, with the eval's own value: {EXAMPLE_LINES[key]}"
        ),
    )


def duplicate_failure(config_file: Path, what_is_repeated: str) -> Failure:
    """The failure for a contributor id or a name listed twice.

    Only names that passed the name rule reach here, so quoting the repeated
    name back is safe.
    """
    return Failure(
        kind="eval_config_duplicate",
        what_happened=f"In the eval config at {config_file}, {what_is_repeated}.",
        why=(
            "Each contributor id and each name must mean one thing, or records could be attributed to the wrong "
            "person or workstream."
        ),
        what_to_do_next=f"Remove or rename the repeated entry in {config_file} so each appears once.",
    )


def reserved_workstream_failure(config_file: Path) -> Failure:
    """The failure for a workstream named `unknown`, which goodall keeps for work blocks it cannot attribute."""
    return Failure(
        kind="eval_config_reserved_name",
        what_happened=(
            f"In the eval config at {config_file}, workstreams lists {UNKNOWN_WORKSTREAM}, but the name "
            f"{UNKNOWN_WORKSTREAM} is reserved."
        ),
        why=(
            f"Goodall records a work block with the workstream {UNKNOWN_WORKSTREAM} when it cannot tell which "
            "workstream it was for, until someone says which with goodall fixblock. A real workstream by that name "
            "would be mixed up with those."
        ),
        what_to_do_next=(
            f"Rename that workstream in {config_file} to a name that says what the work is, such as other or general."
        ),
    )


def schema_too_new_failure(config_file: Path, schema_version: int) -> Failure:
    """The failure for an eval config written by a newer goodall than this one."""
    return Failure(
        kind="eval_config_schema_too_new",
        what_happened=(
            f"The eval config at {config_file} has schema_version {schema_version}, but this goodall understands "
            f"only schema_version {SUPPORTED_SCHEMA_VERSION}."
        ),
        why=(
            "The eval config was written by a newer goodall, which may use keys this one does not know, "
            "so this goodall could misread it."
        ),
        what_to_do_next="Upgrade goodall to the version the rest of the eval uses, then run goodall status again.",
    )


def scope_not_a_repository_failure(config_file: Path, where: str, problem: str) -> Failure:
    """The failure for a scope entry whose repository is a local path, an address, or malformed."""
    return Failure(
        kind="eval_config_scope_not_a_repository",
        what_happened=(
            f"In the eval config at {config_file}, repository in {where} is not a repository identity: {problem}."
        ),
        why=(
            "The declared scope must mean the same repository on every contributor's machine. A folder path names "
            "one copy on one Mac, and an address names one way of reaching it, so neither says which repository "
            "counts."
        ),
        what_to_do_next=(
            f"Write the repository as host/owner/name in {config_file}, for example "
            'repository = "github.com/your-org/goodall-pilot-app".'
        ),
    )


def scope_path_invalid_failure(config_file: Path, where: str) -> Failure:
    """The failure for a path pattern that points outside its repository."""
    return Failure(
        kind="eval_config_scope_path_invalid",
        what_happened=(
            f"In the eval config at {config_file}, paths in {where} holds a pattern that does not stay inside "
            "the repository."
        ),
        why=(
            "Path patterns choose files inside the repository named beside them. A pattern starting with / or ~, "
            "or containing .., points at files outside it, which are not part of the declared scope."
        ),
        what_to_do_next=(
            f"In {config_file}, write each pattern relative to the top of the repository, for example "
            'paths = ["**"] for everything or paths = ["docs/**"] for one folder.'
        ),
    )


def contains_email_address_failure(config_file: Path, where: str) -> Failure:
    """The failure for an email address anywhere in the eval config (ADR 0009)."""
    return Failure(
        kind="eval_config_contains_email_address",
        what_happened=f"In the eval config at {config_file}, {where} contains an email address.",
        why=(
            "The eval config is meant to be publishable as it stands, so it must identify contributors by id and "
            "display name only, never by an email address."
        ),
        what_to_do_next=f"Remove the email address from {where} in {config_file}.",
    )


def contains_secret_reference_failure(config_file: Path, where: str) -> Failure:
    """The failure for a secret reference anywhere in the eval config (ADR 0009)."""
    return Failure(
        kind="eval_config_contains_secret_reference",
        what_happened=f"In the eval config at {config_file}, {where} contains a secret reference.",
        why=(
            "The eval config is shared and may be published, and a secret reference points into one person's "
            "secrets manager. Secret references belong in the env file in your own macOS user account."
        ),
        what_to_do_next=(
            f"Remove the secret reference from {where} in {config_file}, and put it in the env file beside your "
            "local settings instead."
        ),
    )


def contributor_not_in_roster_failure(local_settings: LocalSettings, eval_config: EvalConfig) -> Failure:
    """The failure for local settings naming a contributor the eval's roster does not have.

    The id from local settings is quoted only when it follows the name rule,
    because anything else in that place may be a secret pasted by mistake.
    """
    if is_name(local_settings.contributor):
        whose_id = f"Your contributor id, {local_settings.contributor},"
    else:
        whose_id = "The contributor id in your local settings"
    roster_ids: list[str] = []
    for contributor in eval_config.contributors:
        roster_ids.append(contributor.id)
    return Failure(
        kind="contributor_not_in_roster",
        what_happened=(f"{whose_id} is not in this eval's roster. The roster's ids are {', '.join(roster_ids)}."),
        why=(
            "Every record names the contributor who made it, so goodall records nothing until it knows who you "
            "are in this eval."
        ),
        what_to_do_next=(
            f"If your id is misspelled, correct contributor in {local_settings.settings_file}. If you are new "
            f"to this eval, ask whoever runs it to add a [[contributors]] block for you to {eval_config.config_file}."
        ),
    )


def main_workstream_unknown_failure(local_settings: LocalSettings, eval_config: EvalConfig) -> Failure:
    """The failure for a main workstream the eval does not have, often after one was renamed."""
    if is_name(local_settings.main_workstream):
        whose_workstream = f"Your main workstream, {local_settings.main_workstream},"
    else:
        whose_workstream = "The main workstream in your local settings"
    return Failure(
        kind="main_workstream_unknown",
        what_happened=(
            f"{whose_workstream} is not one of this eval's workstreams, which are {', '.join(eval_config.workstreams)}."
        ),
        why=(
            "A work block is started on your main workstream unless you choose another, so it must be a workstream "
            "the eval has. A renamed workstream would otherwise break your default without telling you."
        ),
        what_to_do_next=(
            f"Set main_workstream in {local_settings.settings_file} to one of the eval's workstreams. If the "
            f"workstream should exist, ask whoever runs the eval to add it to workstreams in "
            f"{eval_config.config_file}."
        ),
    )
