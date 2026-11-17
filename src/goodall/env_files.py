"""Reading env-style files: the env file and the shared env file.

Both files goodall keeps environment variables in use the same line format
(ADR 0010), so one reader serves both:

- the env file, `.env`, beside local settings, which maps each variable name
  to a secret reference and holds nothing else;
- the shared env file, `.env.shared`, at the top of the record repo, which
  holds the non-secret variables every contributor's launched programs need.

`goodall doctor` reads them to check them (goodall.secrets_checks), and the
code that launches programs will read them to set their environment, which
is why this module imports only the standard library (ADR 0005):
tests/test_hook_path_imports.py proves it.

The line format, deliberately narrow so that every program that reads a
`.env` file agrees on what each line means:

- `NAME=value`, where NAME is letters, digits and underscores and does not
  start with a digit, with no spaces around the `=`.
- The value may be wrapped in double or single quotes, which are removed.
  Nothing inside the quotes is treated specially: there are no escapes and
  no `$VARIABLE` expansion, because secret references never need them.
- An unquoted value may not contain a space. Dotenv readers disagree about
  whether `NAME=value # note` ends at `value` or at `note`, so a value with
  a space must be quoted, which makes its end unambiguous.
- Blank lines, and lines whose first non-space character is `#`, are
  ignored. Spaces at either end of a line are ignored.
- `export NAME=value` is refused, not accepted. These files are read by
  goodall and by secrets managers, not run by a shell, and accepting a shell
  keyword would suggest that shell syntax (expansion, escapes) works here
  when it does not.
- A name given twice is a problem, because which value a program gets
  would depend on which reader read the file.
- An empty value (`NAME=` or `NAME=""`) is a problem. A base64 token ends
  in = padding, so a token pasted on a line of its own reads as `NAME=`;
  refusing empty values keeps such a line from passing as a variable.

Reading never raises for anything in the file: it returns the entries it
could read and a problem for every line it could not. A file that could not
be read at all comes back as `EnvFileNotRead`.

Trap, and the most important rule here: a problem never repeats anything
written on the line, only its line number and, when goodall.quoting says it
may be shown, the variable's name. A malformed line is exactly where a key
pasted by mistake ends up, and goodall's output can reach a terminal
history or a transcript an agent reads (ADR 0006). A well-formed name is
not enough: a base64 token is a well-formed name. Messages that name a
variable go through `variable_as_shown`.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from goodall.quoting import can_show_name

# A variable name: letters, digits and underscores, not starting with a digit.
# This is what a POSIX shell accepts, so every program that reads the
# environment can use the name.
VARIABLE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Each quote character a value may be wrapped in.
QUOTE_CHARACTERS = ['"', "'"]

# How a message refers to a variable whose name it does not show.
UNSHOWN_VARIABLE = "the variable whose name goodall does not show"


@dataclass(frozen=True)
class EnvEntry:
    """One `NAME=value` line, with its quotes removed.

    `line_number` counts from 1, as a text editor does, so a message can
    point the contributor at the line.
    """

    name: str
    value: str
    line_number: int


@dataclass(frozen=True)
class EnvLineProblem:
    """A line the reader could not use.

    `problem` says what is wrong, as the end of a sentence starting "Line N
    ...". It never contains anything written on the line except a variable
    name that `variable_as_shown` lets through.
    """

    line_number: int
    problem: str


@dataclass(frozen=True)
class EnvFileReading:
    """Everything read from an env-style file: the entries, and a problem for each line that could not be read."""

    entries: list[EnvEntry]
    problems: list[EnvLineProblem] = field(default_factory=list)


@dataclass(frozen=True)
class EnvFileNotRead:
    """An env-style file that could not be read at all.

    - `missing` is True when there is no file there, which is not always a
      problem: an eval that uses no secrets needs no env file.
    - `reason` is why it could not be read, as the end of a sentence, such
      as "Permission denied". It never contains the file's contents.
    """

    file: Path
    missing: bool
    reason: str


def read_env_file(env_file: Path) -> EnvFileReading | EnvFileNotRead:
    """Read the env-style file at `env_file`; never raises.

    Trap: asking Path.exists() first would raise PermissionError when a
    folder above cannot be searched, so opening the file is how the reader
    learns whether it is there.
    """
    try:
        file_bytes = env_file.read_bytes()
    except FileNotFoundError:
        return EnvFileNotRead(file=env_file, missing=True, reason="there is no file there")
    except IsADirectoryError:
        return EnvFileNotRead(file=env_file, missing=False, reason="it is a folder, not a file")
    except OSError as read_error:
        return EnvFileNotRead(file=env_file, missing=False, reason=str(read_error.strerror))

    try:
        file_text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return EnvFileNotRead(file=env_file, missing=False, reason="it contains characters that are not UTF-8 text")
    return read_env_text(file_text)


def read_env_text(text: str) -> EnvFileReading:
    """Read the lines of an env-style file; see the module docstring for the format."""
    entries: list[EnvEntry] = []
    problems: list[EnvLineProblem] = []
    first_line_of_each_name: dict[str, int] = {}

    # splitlines() also ends a line at "\r\n", so a file saved on Windows
    # reads the same.
    for line_index, line in enumerate(text.splitlines()):
        line_number = line_index + 1
        stripped_line = line.strip()
        if stripped_line == "" or stripped_line.startswith("#"):
            continue

        entry_or_problem = read_line(stripped_line, line_number)
        if isinstance(entry_or_problem, EnvLineProblem):
            problems.append(entry_or_problem)
            continue

        entry = entry_or_problem
        if entry.name in first_line_of_each_name:
            first_line_number = first_line_of_each_name[entry.name]
            problems.append(
                EnvLineProblem(
                    line_number=line_number,
                    problem=f"sets {variable_as_shown(entry.name)} again, which line {first_line_number} already sets",
                )
            )
            continue
        first_line_of_each_name[entry.name] = line_number
        entries.append(entry)

    return EnvFileReading(entries=entries, problems=problems)


def read_line(stripped_line: str, line_number: int) -> EnvEntry | EnvLineProblem:
    """Read one line that is neither blank nor a comment."""
    if stripped_line.startswith("export "):
        return EnvLineProblem(
            line_number=line_number,
            problem="starts with export, which this file does not use; remove the word export and keep NAME=value",
        )

    name, equals_sign, written_value = stripped_line.partition("=")
    if equals_sign == "":
        return EnvLineProblem(line_number=line_number, problem="is not in the form NAME=value")
    if VARIABLE_NAME_PATTERN.fullmatch(name) is None:
        return EnvLineProblem(
            line_number=line_number,
            problem=(
                "does not start with a variable name: a name is letters, digits and underscores, does not start "
                "with a digit, and is followed straight away by = with no space"
            ),
        )

    value = unquote_value(written_value)
    if value is None:
        return EnvLineProblem(
            line_number=line_number,
            problem=(
                f"sets {variable_as_shown(name)} to a value whose quotes are not right: quote the whole value "
                "with one pair of matching quotes, or none, and put nothing after the closing quote. A value with "
                "a space in it needs quotes"
            ),
        )
    if value == "":
        return EnvLineProblem(
            line_number=line_number,
            problem=(
                "has nothing after the =. A key or token pasted on a line of its own can look like this, so "
                "goodall does not read it as a variable"
            ),
        )
    return EnvEntry(name=name, value=value, line_number=line_number)


def variable_as_shown(name: str) -> str:
    """How a message refers to the variable `name`: by its name, or, when the name could be a secret, without it."""
    if can_show_name(name):
        return name
    return UNSHOWN_VARIABLE


def unquote_value(written_value: str) -> str | None:
    """The value without its surrounding quotes, or None when it cannot be read unambiguously.

    A quoted value must open and close with the same quote character and
    contain no other of that character. An unquoted value may contain no
    quote character and no whitespace.
    """
    for quote_character in QUOTE_CHARACTERS:
        if written_value.startswith(quote_character):
            is_closed = len(written_value) >= 2 and written_value.endswith(quote_character)
            inside = written_value[1:-1]
            if not is_closed or quote_character in inside:
                return None
            return inside

    for character in written_value:
        if character in QUOTE_CHARACTERS or character.isspace():
            return None
    return written_value
