"""Where this macOS user account's local settings live.

Local settings are the one file that tells goodall which eval this macOS user
account belongs to (ADR 0008): where its record repo and log directory are,
which contributor the account is, and which secrets manager it uses. Every
command finds them here, and so will the Claude Code hooks, which is why this
module imports only the standard library (ADR 0005) and does nothing slow
when imported.

Where goodall looks:

- Normally `~/.config/goodall/settings.toml`, a fixed place in the account's
  home folder. A fixed place, rather than searching upward from the current
  folder, because commands and hooks run inside whatever repository the work
  is in, which is rarely the record repo.
- If the environment variable GOODALL_CONFIG_DIR names a folder, goodall
  looks for `settings.toml` there instead. This exists so tests and
  goodall's own development never touch a real home folder. It must be an
  absolute path: a relative one would be looked up from whichever folder a
  command or hook happened to start in, so two commands could find two
  different evals.

The env file will sit in the same folder as local settings (ADR 0010), which
is why this module names a folder as well as a file.

This module only works out the location. Reading and checking the file
itself comes later.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# The environment variable that overrides where local settings are found.
CONFIG_DIRECTORY_VARIABLE = "GOODALL_CONFIG_DIR"

# The settings folder, relative to the account's home folder, when the
# variable above is not set. ~/.config is where command-line tools on macOS
# and Linux conventionally keep their settings.
DEFAULT_CONFIG_DIRECTORY_IN_HOME = Path(".config") / "goodall"

# The name of the local settings file inside the settings folder.
LOCAL_SETTINGS_FILE_NAME = "settings.toml"


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
