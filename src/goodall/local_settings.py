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

Two settings are optional here: `activitywatch_port` and
`activitywatch_device_id`, for the eval account's own copy of ActivityWatch
(ADR 0016). Whether they are needed depends on the eval config, which this
module does not read, so their absence is reported by goodall.eval_config's
cross-check through `goodall doctor`, never here. When present they are
checked here, whatever the eval config says, because a malformed value is
wrong in any eval. The port may not be ActivityWatch's default, 5600: ports
are shared by every macOS user account on a Mac, and ActivityWatch's launcher
silently joins any ActivityWatch already answering on its port, so two
accounts on 5600 mix their data without a warning.

One more setting is optional: `account_mode`, which says whether this macOS
user account is an eval account (`"dedicated"`) or one the person also uses
for other things (`"mixed"`, ADR 0021). It lives here because it is true for
one account on one machine (ADR 0009). Left out, it means dedicated, so every
setup made before mixed account mode existed behaves as it always did. Any
other value is a failure, never a guess: the mode decides whether the prompt
gate refuses prompts, and a misspelled "mixed" silently read as dedicated
would refuse a person's own prompts with no explanation.

One more is optional: `start_at_sign_in`, true or false, which says whether
signing in to this account starts a work block (ADR 0021, `goodall
sign-in`). Left out, it means true: in an eval account signing in means
recording, and the account is the consent boundary. `start_at_sign_in =
false` turns it off. It is read only where `goodall sign-in` runs, which is
the eval account named by the administrator install, so in mixed account
mode it has no effect: nothing starts there unless the person starts it.
It lives here, not in the eval config, because it is about one account on
one Mac (ADR 0009).

The file's shape is provisional by the owner's instruction (goodall-xes.8);
`schema_version` is there so it can change.
"""

import difflib
import os
import re
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

# The settings for this account's own ActivityWatch (ADR 0016). Optional
# here; see the module docstring.
ACTIVITYWATCH_SETTING_NAMES = ["activitywatch_port", "activitywatch_device_id"]

# The two account modes (ADR 0021, GLOSSARY.md). Dedicated account mode
# records in an eval account; mixed account mode records in a macOS user
# account the person also uses for other things.
DEDICATED_ACCOUNT_MODE = "dedicated"
MIXED_ACCOUNT_MODE = "mixed"
ACCOUNT_MODES = [DEDICATED_ACCOUNT_MODE, MIXED_ACCOUNT_MODE]

# Every key local settings may contain, in the order goodall reports them.
KNOWN_SETTING_NAMES = [
    "schema_version",
    *TEXT_SETTING_NAMES,
    *ACTIVITYWATCH_SETTING_NAMES,
    "account_mode",
    "start_at_sign_in",
]

# ActivityWatch's own default port, which the eval account's copy must not
# use; see the module docstring.
ACTIVITYWATCH_DEFAULT_PORT = 5600

# The highest port number there is.
HIGHEST_PORT = 65535

# The shape of ActivityWatch's device_id: a UUID, written as 8-4-4-4-12 hex
# digits joined by hyphens. ActivityWatch writes it in lowercase; capitals are
# accepted and recorded in lowercase, so later comparisons need no care.
DEVICE_ID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Where a person finds the device_id: ActivityWatch's server reports it about
# itself at this path, on its own port.
ACTIVITYWATCH_INFO_PATH = "/api/0/info"

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

# Example lines for the ActivityWatch settings, one of which is not text. The
# identifier is made up.
ACTIVITYWATCH_EXAMPLE_LINES = {
    "activitywatch_port": "activitywatch_port = 5610",
    "activitywatch_device_id": 'activitywatch_device_id = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"',
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
    """Local settings that passed every check in this module.

    `activitywatch_port` and `activitywatch_device_id` are None when the file
    does not have them; whether the eval needs them is for goodall.eval_config
    to say. The device_id is always lowercase. `account_mode` is one of
    ACCOUNT_MODES, and is DEDICATED_ACCOUNT_MODE when the file does not say.
    `start_at_sign_in` is True when the file does not say.
    """

    settings_file: Path
    schema_version: int
    contributor: str
    main_workstream: str
    record_repo: FolderSetting
    log_directory: FolderSetting
    secrets_manager: str
    activitywatch_port: int | None = None
    activitywatch_device_id: str | None = None
    account_mode: str = DEDICATED_ACCOUNT_MODE
    start_at_sign_in: bool = True


class RelativeConfigDirectoryError(ValueError):
    """GOODALL_CONFIG_DIR is set to a relative path, which goodall refuses.

    Deliberately carries no copy of the value: it may be a key pasted into
    the wrong variable, and goodall never repeats such text (ADR 0006).
    """

    def __init__(self) -> None:
        super().__init__(f"{CONFIG_DIRECTORY_VARIABLE} must be an absolute path")


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
        raise RelativeConfigDirectoryError()
    return LocalSettingsLocation(config_directory=override_directory, chosen_by_override=True)


def read_local_settings() -> LocalSettings | list[Failure]:
    """Find local settings, read them and check them.

    Returns the settings when the file is present and correct, and otherwise
    a list of at least one failure. Never raises for anything wrong with the
    environment variable, the folder or the file.
    """
    try:
        location = find_local_settings_location()
    except RelativeConfigDirectoryError:
        return [relative_config_directory_failure()]
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

    activitywatch_port = read_activitywatch_port(settings_file, settings_table, problems)
    activitywatch_device_id = read_activitywatch_device_id(settings_file, settings_table, problems)
    account_mode = read_account_mode(settings_file, settings_table, problems)
    start_at_sign_in = read_start_at_sign_in(settings_file, settings_table, problems)

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
        activitywatch_port=activitywatch_port,
        activitywatch_device_id=activitywatch_device_id,
        account_mode=account_mode,
        start_at_sign_in=start_at_sign_in,
    )


def read_activitywatch_port(
    settings_file: Path, settings_table: dict[str, object], problems: list[Failure]
) -> int | None:
    """Return the ActivityWatch port, or None when it is absent or a problem was recorded."""
    if "activitywatch_port" not in settings_table:
        return None
    value = settings_table["activitywatch_port"]
    if not is_whole_number(value) or value < 1 or value > HIGHEST_PORT:
        problems.append(
            value_invalid_failure(
                settings_file,
                "activitywatch_port",
                f"must be a port number from 1 to {HIGHEST_PORT}, written without quotes",
                ACTIVITYWATCH_EXAMPLE_LINES["activitywatch_port"],
            )
        )
        return None
    if value == ACTIVITYWATCH_DEFAULT_PORT:
        problems.append(activitywatch_default_port_failure(settings_file))
        return None
    return value


def read_activitywatch_device_id(
    settings_file: Path, settings_table: dict[str, object], problems: list[Failure]
) -> str | None:
    """Return the ActivityWatch identifier in lowercase, or None when it is absent or a problem was recorded.

    Trap: the value is never shown, because text of the wrong shape here may
    be a key pasted into the wrong setting.
    """
    if "activitywatch_device_id" not in settings_table:
        return None
    value = settings_table["activitywatch_device_id"]
    if not isinstance(value, str) or DEVICE_ID_PATTERN.fullmatch(value) is None:
        problems.append(
            value_invalid_failure(
                settings_file,
                "activitywatch_device_id",
                "is not an identifier in the shape ActivityWatch reports: 36 characters of digits, letters a to f "
                f"and hyphens, in double quotes. ActivityWatch shows it as device_id at {ACTIVITYWATCH_INFO_PATH} "
                "on its own port",
                ACTIVITYWATCH_EXAMPLE_LINES["activitywatch_device_id"],
            )
        )
        return None
    return value.lower()


def read_account_mode(settings_file: Path, settings_table: dict[str, object], problems: list[Failure]) -> str:
    """Return the account mode: dedicated when the file does not say, or when a problem was recorded.

    Trap: the value is never shown, like every other value that is not yet
    known to be safe to repeat (ADR 0006).
    """
    if "account_mode" not in settings_table:
        return DEDICATED_ACCOUNT_MODE
    value = settings_table["account_mode"]
    if not isinstance(value, str) or value not in ACCOUNT_MODES:
        problems.append(
            value_invalid_failure(
                settings_file,
                "account_mode",
                f'must be "{DEDICATED_ACCOUNT_MODE}", for an eval account used for this eval and nothing else, or '
                f'"{MIXED_ACCOUNT_MODE}", for an account you also use for other things, in lowercase and in double '
                "quotes",
                f'account_mode = "{DEDICATED_ACCOUNT_MODE}"',
            )
        )
        return DEDICATED_ACCOUNT_MODE
    return value


def read_start_at_sign_in(settings_file: Path, settings_table: dict[str, object], problems: list[Failure]) -> bool:
    """Whether signing in starts a work block: True when the file does not say, or when a problem was recorded."""
    if "start_at_sign_in" not in settings_table:
        return True
    value = settings_table["start_at_sign_in"]
    if not isinstance(value, bool):
        problems.append(
            value_invalid_failure(
                settings_file,
                "start_at_sign_in",
                "must be true, to start a work block each time you sign in, or false, to start none, without quotes",
                "start_at_sign_in = false",
            )
        )
        return True
    return value


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


def relative_config_directory_failure() -> Failure:
    """The failure for a GOODALL_CONFIG_DIR that is not an absolute path.

    Takes no value on purpose: the message must never repeat what the
    variable holds, which may be a pasted secret (ADR 0006).
    """
    return Failure(
        kind="config_directory_not_absolute",
        what_happened=(
            f"{CONFIG_DIRECTORY_VARIABLE} is not an absolute path. "
            "Goodall does not show its value, in case it is a secret."
        ),
        why=(
            "Goodall would look for local settings relative to whichever folder a command started in, "
            "so two commands could find two different evals."
        ),
        what_to_do_next=(
            f"Set {CONFIG_DIRECTORY_VARIABLE} to an absolute path, which is a full path starting with /, "
            "for example /Users/you/goodall-config, or unset it to use local settings in your home folder."
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


def activitywatch_default_port_failure(settings_file: Path) -> Failure:
    """The failure for an eval account's ActivityWatch set to ActivityWatch's default port (ADR 0016)."""
    return Failure(
        kind="local_settings_activitywatch_default_port",
        what_happened=(
            f"In local settings at {settings_file}, activitywatch_port is {ACTIVITYWATCH_DEFAULT_PORT}, "
            "ActivityWatch's default port, which an eval account's ActivityWatch must not use."
        ),
        why=(
            "Ports are shared by every macOS user account on this Mac, and ActivityWatch silently joins any "
            f"ActivityWatch already answering on its port. On {ACTIVITYWATCH_DEFAULT_PORT}, this eval account's "
            "record could mix with what another account's ActivityWatch records, without any warning."
        ),
        what_to_do_next=(
            "Choose another port that nothing else on this Mac uses, such as 5610. Set it in ActivityWatch's own "
            "settings for its server and for its watchers, restart ActivityWatch, and set the same port in "
            f"{settings_file}: {ACTIVITYWATCH_EXAMPLE_LINES['activitywatch_port']}"
        ),
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
