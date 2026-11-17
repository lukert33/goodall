"""Finding, reading and checking this macOS user account's local settings.

Local settings are the one file that tells goodall which eval this macOS user
account belongs to (ADR 0008): where its record repo and log directory are,
which contributor the account is and their main workstream, and which secrets
manager it uses. Every command reads them through `read_local_settings`, and
so will the Claude Code hooks, which is why this module imports only the
standard library (ADR 0005) and does nothing slow when imported.

Where goodall looks:

- Normally `~/.config/goodall/local-settings.toml`, a fixed place in the
  account's home folder. A fixed place, rather than searching upward from the
  current folder, because commands and hooks run inside whatever repository
  the work is in, which is rarely the record repo.
- If the environment variable GOODALL_CONFIG_DIR names a folder, goodall
  looks for `local-settings.toml` there instead. This exists so tests and
  goodall's own development never touch a real home folder. It must be an
  absolute path: a relative one would be looked up from whichever folder a
  command or hook happened to start in, so two commands could find two
  different evals.

The env file will sit in the same folder as local settings (ADR 0010), which
is why this module names a folder as well as a file.

What reading gives back: either a `LocalSettings` record, or a list of
`Failure` records, each saying what is wrong, why it matters and what to do
next. Bad input never raises, because a contributor who made a typo needs a
sentence, not a traceback. Every problem in the file is reported at once, so
the contributor can fix them all in one edit, with one exception: settings
written for a newer schema version get only "upgrade goodall", because every
other complaint about a newer layout would be noise.

Failures never quote a setting's value back, except a folder once it is
known to be a full path, which the contributor needs to see as written.
Terminal output in an eval account can end up in a transcript an agent
reads, and a value in the wrong place may be a secret pasted by mistake
(ADR 0006), so goodall names the setting and shows a correct example line
instead. A key goodall does not know is named only when goodall.quoting
says it may be shown, because a pasted key is a valid TOML key too, and the
TOML reader's own messages pass through goodall.quoting for the same reason.

The failures are built here, beside the rule that finds each problem, so
`goodall status`, `goodall doctor` and the prompt gate all word a problem the
same way. This module checks the file only. Whether the folders it names
exist is a question about the machine, answered by goodall.folders for the
commands that need them.

The file's shape is provisional by the owner's instruction (goodall-xes.8);
`schema_version` is there so it can change.
"""

import difflib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from goodall.quoting import can_show_name, toml_error_as_shown
from goodall.terminal import Failure

# The environment variable that overrides where local settings are found.
CONFIG_DIRECTORY_VARIABLE = "GOODALL_CONFIG_DIR"

# The settings folder, relative to the account's home folder, when the
# variable above is not set. ~/.config is where command-line tools on macOS
# and Linux conventionally keep their settings.
DEFAULT_CONFIG_DIRECTORY_IN_HOME = Path(".config") / "goodall"

# The name of the local settings file inside the settings folder.
LOCAL_SETTINGS_FILE_NAME = "local-settings.toml"

# The newest layout of local settings this goodall understands. Raise it, and
# teach the reader the new layout, whenever the keys below change meaning.
SUPPORTED_SCHEMA_VERSION = 1

# Settings whose value is text. Each must be present and not empty.
TEXT_SETTING_NAMES = ["contributor", "main_workstream", "record_repo", "log_directory", "secrets_manager"]

# The text settings above that name a folder on this Mac.
FOLDER_SETTING_NAMES = ["record_repo", "log_directory"]

# Every key local settings may contain, in the order goodall reports them.
KNOWN_SETTING_NAMES = ["schema_version", *TEXT_SETTING_NAMES]

# The secrets managers goodall can work with. ADR 0006: 1Password is the
# only one supported today.
SUPPORTED_SECRETS_MANAGERS = ["1password"]

# Shown in "for example" advice, so a contributor sees a correct line. They
# match the agreed example in goodall-xes.8.
EXAMPLE_VALUES = {
    "contributor": "ada-lovelace",
    "main_workstream": "research",
    "record_repo": "~/evals/goodall-pilot-records",
    "log_directory": "/Volumes/EvalLogs/goodall-pilot",
    "secrets_manager": "1password",
}


@dataclass(frozen=True)
class LocalSettingsLocation:
    """Where goodall looks for local settings, and what decided that.

    `chosen_by_override` is True when GOODALL_CONFIG_DIR picked the folder.
    Messages say so, because a stale override left in a shell is an easy
    reason for goodall to "lose" settings that do exist in the home folder.
    """

    config_directory: Path
    chosen_by_override: bool

    @property
    def settings_file(self) -> Path:
        """The local settings file inside the settings folder."""
        return self.config_directory / LOCAL_SETTINGS_FILE_NAME


@dataclass(frozen=True)
class FolderSetting:
    """A folder named in local settings, both as written and as goodall uses it.

    Messages show `as_written` first, because that is what the contributor
    will look for in the file, and `path` too when the two differ, because
    that is where goodall actually looked.
    """

    as_written: str
    path: Path


@dataclass(frozen=True)
class LocalSettings:
    """Local settings that passed every check in this module."""

    settings_file: Path
    schema_version: int
    contributor: str
    main_workstream: str
    record_repo: FolderSetting
    log_directory: FolderSetting
    secrets_manager: str


class RelativeConfigDirectoryError(ValueError):
    """GOODALL_CONFIG_DIR is set to a relative path, which goodall refuses.

    `value` is the variable's value exactly as it was set, so a message can
    quote it back.
    """

    def __init__(self, value: str) -> None:
        super().__init__(f"{CONFIG_DIRECTORY_VARIABLE} must be an absolute path, not {value!r}")
        self.value = value


def find_local_settings_location() -> LocalSettingsLocation:
    """Work out where local settings are, without touching the disk.

    Reads GOODALL_CONFIG_DIR from the environment each time it is called, so
    a test can change it between calls. An empty value counts as unset,
    because `export GOODALL_CONFIG_DIR=` is a common way to clear a variable.

    Raises RelativeConfigDirectoryError when the variable holds a relative
    path. A leading `~` is expanded first, so `~/goodall-test` is accepted.
    """
    override_value = os.environ.get(CONFIG_DIRECTORY_VARIABLE, "")
    if override_value == "":
        return LocalSettingsLocation(
            config_directory=Path.home() / DEFAULT_CONFIG_DIRECTORY_IN_HOME,
            chosen_by_override=False,
        )

    override_directory = Path(override_value).expanduser()
    if not override_directory.is_absolute():
        raise RelativeConfigDirectoryError(override_value)
    return LocalSettingsLocation(config_directory=override_directory, chosen_by_override=True)


def read_local_settings() -> LocalSettings | list[Failure]:
    """Find local settings, read them and check them.

    Returns the settings when the file is present and correct, and otherwise
    a list of at least one failure. Never raises for anything wrong with the
    environment variable, the folder or the file.
    """
    try:
        location = find_local_settings_location()
    except RelativeConfigDirectoryError as relative_directory_error:
        return [relative_config_directory_failure(relative_directory_error.value)]
    return read_local_settings_file(location)


def read_local_settings_file(location: LocalSettingsLocation) -> LocalSettings | list[Failure]:
    """Read and check the local settings file at `location`."""
    settings_file = location.settings_file

    # Opening the file is how goodall learns whether it exists. Trap: asking
    # first with Path.exists() raises PermissionError when the folder cannot
    # be searched, so the question "is it there?" would crash goodall.
    try:
        file_bytes = settings_file.read_bytes()
    except FileNotFoundError:
        return [local_settings_missing_failure(location)]
    except OSError as read_error:
        return [local_settings_unreadable_failure(settings_file, read_error)]

    # Decoding is done here rather than by tomllib so that a file that is not
    # text at all gets its own plain explanation.
    try:
        file_text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return [local_settings_not_toml_failure(settings_file, "it contains characters that are not UTF-8 text")]

    try:
        settings_table = tomllib.loads(file_text)
    except tomllib.TOMLDecodeError as toml_error:
        # tomllib's message ends with the place, for example
        # "Invalid value (at line 2, column 15)", which is what a person
        # editing by hand needs. Some of its messages repeat a key, which
        # toml_error_as_shown takes out.
        return [local_settings_not_toml_failure(settings_file, toml_error_as_shown(str(toml_error)))]

    return check_settings_table(settings_file, settings_table)


def check_settings_table(settings_file: Path, settings_table: dict[str, object]) -> LocalSettings | list[Failure]:
    """Check the keys and values parsed from the local settings file."""
    problems: list[Failure] = []

    # Stays 0 unless the file holds a usable schema version; 0 is never
    # returned, because a problem is recorded whenever it stays 0.
    schema_version = 0
    schema_version_value = settings_table.get("schema_version")
    if "schema_version" not in settings_table:
        problems.append(key_missing_failure(settings_file, "schema_version"))
    elif not is_whole_number(schema_version_value) or schema_version_value < 1:
        problems.append(
            value_invalid_failure(
                settings_file,
                "schema_version",
                "must be a whole number without quotes",
                f"schema_version = {SUPPORTED_SCHEMA_VERSION}",
            )
        )
    elif schema_version_value > SUPPORTED_SCHEMA_VERSION:
        return [schema_too_new_failure(settings_file, schema_version_value)]
    else:
        schema_version = schema_version_value

    # Unknown keys come before missing ones, so a misspelling is read as
    # "contributer is unknown, did you mean contributor?" and then
    # "there is no contributor".
    for setting_name in settings_table:
        if setting_name not in KNOWN_SETTING_NAMES:
            problems.append(key_unknown_failure(settings_file, setting_name))

    text_values: dict[str, str] = {}
    for setting_name in TEXT_SETTING_NAMES:
        if setting_name not in settings_table:
            problems.append(key_missing_failure(settings_file, setting_name))
            continue
        value = settings_table[setting_name]
        if not isinstance(value, str) or value.strip() == "":
            problems.append(
                value_invalid_failure(
                    settings_file,
                    setting_name,
                    "must be text in double quotes, and not empty",
                    f'{setting_name} = "{EXAMPLE_VALUES[setting_name]}"',
                )
            )
            continue
        text_values[setting_name] = value

    folder_settings: dict[str, FolderSetting] = {}
    for setting_name in FOLDER_SETTING_NAMES:
        if setting_name not in text_values:
            continue
        as_written = text_values[setting_name]
        # Trap: the value is not shown here, because until it is known to be
        # a full path it may be a key pasted into the wrong setting.
        path = expand_home_folder(as_written)
        if not path.is_absolute():
            problems.append(
                value_invalid_failure(
                    settings_file,
                    setting_name,
                    "is not a full path. A full path starts with / or with ~/, which means your home folder",
                    f'{setting_name} = "{EXAMPLE_VALUES[setting_name]}"',
                )
            )
            continue
        # A null character (\u0000 in TOML) cannot be in any path, and asking
        # the disk about a path holding one raises ValueError.
        if "\x00" in as_written:
            problems.append(
                value_invalid_failure(
                    settings_file,
                    setting_name,
                    "contains a null character (written \\u0000), which no path can contain",
                    f'{setting_name} = "{EXAMPLE_VALUES[setting_name]}"',
                )
            )
            continue
        folder_settings[setting_name] = FolderSetting(as_written=as_written, path=path)

    secrets_manager = text_values.get("secrets_manager")
    if secrets_manager is not None and secrets_manager not in SUPPORTED_SECRETS_MANAGERS:
        problems.append(secrets_manager_unsupported_failure(settings_file))

    if problems:
        return problems

    # Every check above passed, so every value below is present and usable.
    return LocalSettings(
        settings_file=settings_file,
        schema_version=schema_version,
        contributor=text_values["contributor"],
        main_workstream=text_values["main_workstream"],
        record_repo=folder_settings["record_repo"],
        log_directory=folder_settings["log_directory"],
        secrets_manager=text_values["secrets_manager"],
    )


def is_whole_number(value: object) -> TypeGuard[int]:
    """True for a TOML integer such as 1.

    Trap: in Python `True` is an int, so `schema_version = true` would pass a
    plain isinstance check.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def expand_home_folder(as_written: str) -> Path:
    """Turn a folder setting into a path, reading a leading ~ as this account's home.

    Only `~` on its own and `~/...` are expanded. `~someone/...` is left as
    it is, so it fails the full-path check: it would point into another
    macOS user account's home, which an eval account must not depend on.
    Path.expanduser would expand it, which is why it is not used here.
    """
    if as_written == "~":
        return Path.home()
    if as_written.startswith("~/"):
        return Path.home() / as_written[2:]
    return Path(as_written)


def local_settings_missing_failure(location: LocalSettingsLocation) -> Failure:
    """The failure for a macOS user account where goodall has never been set up.

    When GOODALL_CONFIG_DIR chose the folder, the message says so and offers
    unsetting it, because a variable left over from testing is the likeliest
    reason settings that exist in the home folder were not found. Otherwise it
    does not mention the variable: it is for tests and goodall's own
    development, and ADR 0008 rejected it as the way contributors find their
    eval.
    """
    if location.chosen_by_override:
        where_goodall_looked = f"in the folder that {CONFIG_DIRECTORY_VARIABLE} names"
        what_to_do_next = (
            "Run goodall onboard to set up this eval account, "
            f"or unset {CONFIG_DIRECTORY_VARIABLE} if your local settings are in your home folder."
        )
    else:
        where_goodall_looked = "the usual place in this macOS user account's home folder"
        what_to_do_next = "Run goodall onboard to set up this eval account."

    return Failure(
        kind="local_settings_missing",
        what_happened=(
            f"No local settings were found. Goodall looked for {location.settings_file}, {where_goodall_looked}."
        ),
        why=(
            "Local settings tell goodall which eval this macOS user account belongs to: where its record repo "
            "and log directory are and which contributor you are. Without them goodall cannot find the eval, "
            "so nothing can be recorded."
        ),
        what_to_do_next=what_to_do_next,
    )


def local_settings_unreadable_failure(settings_file: Path, read_error: OSError) -> Failure:
    """The failure for local settings that exist, or may exist, but cannot be opened.

    The operating system's own reason (for example "Permission denied") is
    quoted, because it is the most precise clue there is.
    """
    return Failure(
        kind="local_settings_unreadable",
        what_happened=f"Local settings at {settings_file} could not be read: {read_error.strerror}.",
        why=(
            "Goodall needs to read local settings to find this macOS user account's eval, "
            "so it cannot tell whether a work block is active."
        ),
        what_to_do_next=(
            f"Check the permissions of {settings_file} and the folders above it: "
            "this macOS user account must be able to read them. Then run the same goodall command again."
        ),
    )


def relative_config_directory_failure(value: str) -> Failure:
    """The failure for a GOODALL_CONFIG_DIR that is not an absolute path."""
    return Failure(
        kind="config_directory_not_absolute",
        what_happened=f"{CONFIG_DIRECTORY_VARIABLE} is set to {value}, which is not an absolute path.",
        why=(
            "Goodall would look for local settings relative to whichever folder a command started in, "
            "so two commands could find two different evals."
        ),
        what_to_do_next=(
            f"Set {CONFIG_DIRECTORY_VARIABLE} to a path starting with / or ~, "
            "or unset it to use local settings in your home folder."
        ),
    )


def local_settings_not_toml_failure(settings_file: Path, reason: str) -> Failure:
    """The failure for a settings file that cannot be parsed, usually after a hand edit."""
    return Failure(
        kind="local_settings_not_toml",
        what_happened=f"Local settings at {settings_file} are not valid TOML: {reason}.",
        why="Goodall reads local settings as TOML, and one mistake stops it reading any of the file.",
        what_to_do_next=(
            f"Open {settings_file} in a text editor and correct the place named above. "
            'Text values need double quotes around them, for example contributor = "ada-lovelace".'
        ),
    )


def key_missing_failure(settings_file: Path, setting_name: str) -> Failure:
    """The failure for a required setting that is not in the file."""
    if setting_name == "schema_version":
        example_line = f"schema_version = {SUPPORTED_SCHEMA_VERSION}"
    else:
        example_line = f'{setting_name} = "{EXAMPLE_VALUES[setting_name]}"'
    return Failure(
        kind="local_settings_key_missing",
        what_happened=f"Local settings at {settings_file} have no {setting_name}.",
        why=f"Goodall needs {setting_name} to know how this macOS user account takes part in the eval.",
        what_to_do_next=f"Add a line to {settings_file} in this form, with your own value: {example_line}",
    )


def key_unknown_failure(settings_file: Path, setting_name: str) -> Failure:
    """The failure for a key goodall does not know, which is often a misspelling.

    It is reported rather than ignored, because an ignored misspelling looks
    like a setting that took effect.

    Trap: the key is named only when goodall.quoting says it may be shown.
    A key pasted by mistake (`sk-ant-api03-... = "x"`) is a valid bare TOML
    key, so reaching this point says nothing about whether it is a secret.
    """
    if not can_show_name(setting_name):
        return Failure(
            kind="local_settings_key_unknown",
            what_happened=(
                f"Local settings at {settings_file} contain a key goodall does not know. Goodall does not show "
                "its name, in case it is a secret pasted by mistake."
            ),
            why=(
                "A setting goodall does not know has no effect, so a misspelled name would look like a setting "
                "that took effect when it did not."
            ),
            what_to_do_next=(
                f"Open {settings_file} and remove or correct every key that is not one of these: "
                f"{', '.join(KNOWN_SETTING_NAMES)}. If it was a real key, change it with whoever issued it, because "
                "it may have been exposed."
            ),
        )

    close_matches = difflib.get_close_matches(setting_name, KNOWN_SETTING_NAMES, n=1, cutoff=0.75)
    if close_matches:
        suggestion = f" Did you mean {close_matches[0]}?"
    else:
        suggestion = ""
    return Failure(
        kind="local_settings_key_unknown",
        what_happened=(
            f"Local settings at {settings_file} contain {setting_name}, which goodall does not know.{suggestion}"
        ),
        why=(
            "A setting goodall does not know has no effect, so a misspelled name would look like a setting "
            "that took effect when it did not."
        ),
        what_to_do_next=(
            f"Correct or remove {setting_name} in {settings_file}. "
            f"The settings goodall knows are {', '.join(KNOWN_SETTING_NAMES)}."
        ),
    )


def value_invalid_failure(settings_file: Path, setting_name: str, problem: str, example_line: str) -> Failure:
    """The failure for a setting that is present but whose value cannot be used."""
    return Failure(
        kind="local_settings_value_invalid",
        what_happened=f"In local settings at {settings_file}, {setting_name} {problem}.",
        why=f"Goodall cannot use {setting_name} until its value has the right form.",
        what_to_do_next=f"Change the line in {settings_file} to this form, with your own value: {example_line}",
    )


def schema_too_new_failure(settings_file: Path, schema_version: int) -> Failure:
    """The failure for settings written by a newer goodall than this one."""
    return Failure(
        kind="local_settings_schema_too_new",
        what_happened=(
            f"Local settings at {settings_file} have schema_version {schema_version}, but this goodall "
            f"understands only schema_version {SUPPORTED_SCHEMA_VERSION}."
        ),
        why=(
            "The settings were written by a newer goodall, which may use keys this one does not know, "
            "so this goodall could misread them."
        ),
        what_to_do_next=(
            "Upgrade goodall to the version used to set up this eval account, then run the same goodall command again."
        ),
    )


def secrets_manager_unsupported_failure(settings_file: Path) -> Failure:
    """The failure for a secrets manager goodall cannot work with (ADR 0006)."""
    supported = ", ".join(f'"{name}"' for name in SUPPORTED_SECRETS_MANAGERS)
    return Failure(
        kind="local_settings_secrets_manager_unsupported",
        what_happened=(
            f"In local settings at {settings_file}, secrets_manager names a secrets manager goodall cannot work with."
        ),
        why=(
            "Goodall keeps secrets out of files by having a secrets manager hand them to the programs it "
            f"launches, and the only secrets manager it can do that with today is {supported}."
        ),
        what_to_do_next=(
            f'Set secrets_manager = "1password" in {settings_file}, and keep this eval\'s secrets in 1Password.'
        ),
    )
