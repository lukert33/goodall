"""Goodall's own small TOML writer, and two careful edits to a TOML file a person may have edited.

Python's standard library reads TOML (`tomllib`) but cannot write it, and
adding a package for that is not worth a dependency (AGENTS.md,
"Dependencies"), so goodall writes the little TOML it needs itself.
`goodall onboard` (goodall.onboard) is the first caller: it writes local
settings and the eval config.

What it writes, and nothing more: text, whole numbers, true and false, and
lists of text, as top-level keys and in arrays of tables (`[[contributors]]`).
Those are all the kinds of value local settings and the eval config hold. A
value of any other kind raises TypeError, because that is a mistake in
goodall's own code, not something about the machine.

Two edits for a file that already exists, because a person may have written
comments or settings of their own into it, and those must survive
(owner decision recorded on goodall-ayy.2):

- `append_marked_block` adds lines at the end, under a comment saying goodall
  added them, and leaves every byte before them as it was.
- `replace_top_level_line` replaces the one line that sets a top-level key,
  and leaves every other line as it was; `replace_top_level_value` does the
  same for a new value, and keeps a comment at the end of that line.

Neither edit checks that the result means what was intended; a caller reads
the result back with `tomllib` and compares, because only the caller knows
what the whole file should say.

Imports only the standard library, like everything that reads settings.
"""

import re
import tomllib
from dataclasses import dataclass, field

# A table header, such as [server] or [[contributors]], which ends the part
# of a TOML file where top-level keys may be set.
TABLE_HEADER_PATTERN = re.compile(r"\s*\[")

# The escapes TOML defines for characters that cannot appear bare inside a
# double-quoted string.
NAMED_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}

# The values goodall writes: see the module docstring.
TomlValue = str | int | bool | list[str]


@dataclass(frozen=True)
class TomlDocument:
    """A whole TOML file to write.

    - `top_level` holds the keys above any table, in the order written.
    - `arrays_of_tables` holds each array of tables, such as "contributors",
      with one dictionary per `[[contributors]]` block, written after the
      top-level keys, in order.
    - `comment_lines` are written first, each after a `# `, to say what the
      file is and who wrote it.
    """

    top_level: dict[str, TomlValue]
    arrays_of_tables: dict[str, list[dict[str, TomlValue]]] = field(default_factory=dict)
    comment_lines: list[str] = field(default_factory=list)


def toml_value(value: TomlValue) -> str:
    """`value` as TOML writes it, ready to follow `key = `.

    Trap: `bool` is checked before `int`, because in Python True is an int
    and would otherwise be written as 1.
    """
    if isinstance(value, bool):
        if value:
            return "true"
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return toml_text(value)
    if isinstance(value, list):
        written_items = []
        for item in value:
            if not isinstance(item, str):
                raise TypeError("goodall's TOML writer writes lists of text only")
            written_items.append(toml_text(item))
        return "[" + ", ".join(written_items) + "]"
    raise TypeError(f"goodall's TOML writer does not write a value of type {type(value).__name__}")


def toml_text(text: str) -> str:
    """`text` as a double-quoted TOML string, with every character TOML forbids bare written as an escape."""
    written = '"'
    for character in text:
        if character in NAMED_ESCAPES:
            written += NAMED_ESCAPES[character]
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            written += f"\\u{ord(character):04X}"
        else:
            written += character
    return written + '"'


def toml_line(key: str, value: TomlValue) -> str:
    """One `key = value` line. Keys goodall writes are plain names, so they need no quotes."""
    return f"{key} = {toml_value(value)}"


def render_toml(document: TomlDocument) -> str:
    """The whole text of `document`, ending in a new line."""
    lines = []
    for comment_line in document.comment_lines:
        lines.append(f"# {comment_line}")
    if document.comment_lines:
        lines.append("")
    for key, value in document.top_level.items():
        lines.append(toml_line(key, value))
    for table_name, tables in document.arrays_of_tables.items():
        for table in tables:
            lines.append("")
            lines.append(f"[[{table_name}]]")
            for key, value in table.items():
                lines.append(toml_line(key, value))
    return "\n".join(lines) + "\n"


def append_marked_block(existing_text: str, marker: str, block_lines: list[str]) -> str:
    """`existing_text` with `block_lines` added at the end, under `# marker`, and nothing before them changed.

    A blank line separates the block from what was there, and a missing
    final new line is added first, so the block never joins a person's last
    line. Trap: in TOML a top-level key appended after a [table] would
    belong to that table, so a caller appends top-level keys only to a file
    that has no tables, as local settings never do.
    """
    text = existing_text
    if text != "" and not text.endswith("\n"):
        text += "\n"
    if text != "":
        text += "\n"
    text += f"# {marker}\n"
    for line in block_lines:
        text += line + "\n"
    return text


def replace_top_level_line(existing_text: str, key: str, new_line: str) -> str | None:
    """`existing_text` with the one line setting top-level `key` replaced by `new_line`, or None.

    None when the key is not set on exactly one line above the first table,
    or when that line does not hold a whole `key = value` on its own (a
    value continued over several lines, say). Goodall then leaves the file
    alone and the caller says how to change it by hand.
    """
    lines = existing_text.splitlines(keepends=True)
    position = position_of_top_level_line(lines, key)
    if position is None:
        return None
    lines[position] = new_line + line_ending_of(lines[position])
    return "".join(lines)


def replace_top_level_value(existing_text: str, key: str, value: TomlValue) -> str | None:
    """`existing_text` with the value of top-level `key` replaced by `value`, or None as `replace_top_level_line`.

    Unlike `replace_top_level_line`, a comment at the end of the line stays
    where it was, with the spaces before it. Trap: a `#` inside a quoted
    value is not a comment, so a line with any quote after its first `#` is
    treated as having no comment and is rewritten whole.
    """
    lines = existing_text.splitlines(keepends=True)
    position = position_of_top_level_line(lines, key)
    if position is None:
        return None
    old_line = lines[position].rstrip("\r\n")
    comment_start = old_line.find("#")
    comment = ""
    if comment_start != -1 and '"' not in old_line[comment_start:] and "'" not in old_line[comment_start:]:
        before_comment = old_line[:comment_start]
        spaces_before_comment = before_comment[len(before_comment.rstrip()) :]
        comment = spaces_before_comment + old_line[comment_start:]
    lines[position] = toml_line(key, value) + comment + line_ending_of(lines[position])
    return "".join(lines)


def position_of_top_level_line(lines: list[str], key: str) -> int | None:
    """Where in `lines` the one whole `key = value` line is, above the first table, or None."""
    key_pattern = re.compile(r"\s*" + re.escape(key) + r"\s*=")
    matching_positions = []
    for position, line in enumerate(lines):
        if TABLE_HEADER_PATTERN.match(line):
            break
        if key_pattern.match(line):
            matching_positions.append(position)
    if len(matching_positions) != 1:
        return None
    position = matching_positions[0]
    try:
        tomllib.loads(lines[position])
    except tomllib.TOMLDecodeError:
        return None
    return position


def line_ending_of(line: str) -> str:
    """The new line `line` ends with, or nothing for a last line that has none."""
    if line.endswith("\n"):
        return "\n"
    return ""
