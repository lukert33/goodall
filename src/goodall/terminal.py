"""The terminal module: the only code in goodall that prints.

Every command hands what it wants to say to a `Terminal`, which writes it to
standard output or standard error. Nothing else calls `print`. That gives one
place that decides layout, width and colour, and it gives tests one seam:
`cli.main` accepts a `Terminal`, so a test passes one built on `io.StringIO`
and reads back exactly what a contributor would have seen.

Rules this module keeps (ADR 0012, and "Command-line conventions" in
AGENTS.md):

- It never asks a question. There is deliberately no input method here; the
  setup page is the only place goodall asks a person anything.
- Results go to standard output and failures go to standard error, so a
  contributor can redirect results to a file and still see problems. In
  `--json` mode standard output carries exactly one JSON document and
  nothing else.
- A command builds its results once, as records such as `Failure`. The text
  screen and the JSON document are two renderings of the same record.
- Colour is used only when the stream is a terminal, is switched off by the
  `NO_COLOR` environment variable (https://no-color.org), and is never the
  only signal: the word FAIL is always printed.
- Width is measured once, when the `Terminal` is made, and clamped so lines
  stay readable. Output that is not a terminal (a pipe, a file, a test) is
  always the fixed width, so it is the same on every machine.

It imports only the standard library, because ADR 0012 chose that and the
hook path may later print through it.
"""

import io
import json
import os
import sys
import textwrap
from dataclasses import asdict, dataclass
from typing import TextIO

# Width used whenever output is not a terminal, and whenever a terminal's
# size cannot be measured.
FIXED_WIDTH = 80

# A measured terminal width is kept inside this range: narrower makes every
# paragraph a column of single words, wider makes lines too long to read.
MINIMUM_WIDTH = 40
MAXIMUM_WIDTH = 100

# Each part of a failure starts with a label in a column this wide, and its
# text is indented to match, so the three parts line up.
LABEL_COLUMN_WIDTH = 6

# ANSI escape sequences. Kept to bold and bold red: they read on light and
# dark terminal themes alike.
BOLD_RED = "\x1b[1;31m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


@dataclass(frozen=True)
class Failure:
    """Something that stopped a command, said so a person can act on it.

    Every failure goodall reports has the same three parts, so a contributor
    learns to read goodall's errors once.

    `kind` is a short fixed name such as "local_settings_missing". It appears
    only in JSON, so an agent can branch on it without matching English
    sentences, which may be reworded.
    """

    kind: str
    what_happened: str
    why: str
    what_to_do_next: str

    def as_json(self) -> dict[str, str]:
        """Return this failure as a dictionary ready for `Terminal.write_json`."""
        return asdict(self)


def clamp_width(measured_columns: int) -> int:
    """Keep a measured terminal width between MINIMUM_WIDTH and MAXIMUM_WIDTH."""
    if measured_columns < MINIMUM_WIDTH:
        return MINIMUM_WIDTH
    if measured_columns > MAXIMUM_WIDTH:
        return MAXIMUM_WIDTH
    return measured_columns


def measure_width(stream: TextIO) -> int:
    """Return the width to wrap text at when writing to `stream`.

    Trap: `shutil.get_terminal_size` measures standard output whatever stream
    you pass, and failures go to standard error, so this asks the operating
    system about the stream's own file descriptor instead. A captured stream
    has no file descriptor and raises, which lands on the fixed width.
    """
    if not stream.isatty():
        return FIXED_WIDTH
    try:
        measured_columns = os.get_terminal_size(stream.fileno()).columns
    except (OSError, ValueError, io.UnsupportedOperation):
        return FIXED_WIDTH
    return clamp_width(measured_columns)


def colour_is_wanted(stream: TextIO) -> bool:
    """Decide whether to colour text written to `stream`.

    Colour only on a terminal, because escape codes written to a file or a
    pipe show up as garbage. An empty NO_COLOR counts as unset, as the
    convention says.
    """
    if os.environ.get("NO_COLOR", "") != "":
        return False
    return stream.isatty()


class Terminal:
    """Where every goodall command sends what it has to say.

    Make one per run of goodall. `cli.main` makes the real one, writing to
    the process's standard output and standard error; tests make one on
    `io.StringIO` objects.
    """

    def __init__(self, standard_output: TextIO | None = None, standard_error: TextIO | None = None) -> None:
        # Trap: the defaults are looked up now rather than in the signature,
        # so that anything that swaps sys.stdout before goodall runs (pytest's
        # capture, contextlib.redirect_stdout) is respected.
        if standard_output is None:
            standard_output = sys.stdout
        if standard_error is None:
            standard_error = sys.stderr
        self.standard_output = standard_output
        self.standard_error = standard_error

        # Measured once, so one command's output never changes width halfway
        # through if the window is resized.
        self.error_width = measure_width(standard_error)
        self.error_colour = colour_is_wanted(standard_error)

    def show_failure(self, failure: Failure) -> None:
        """Write a failure to standard error as three labelled paragraphs."""
        paragraphs = [
            self.labelled_paragraph("FAIL", failure.what_happened, BOLD_RED),
            self.labelled_paragraph("Why", failure.why, BOLD),
            self.labelled_paragraph("Next", failure.what_to_do_next, BOLD),
        ]
        self.standard_error.write("\n\n".join(paragraphs) + "\n")

    def write_json(self, document: dict[str, object]) -> None:
        """Write one JSON document to standard output, and nothing else.

        In `--json` mode this is the only thing a command writes to standard
        output, so an agent can parse the whole stream as one document.
        """
        self.standard_output.write(json.dumps(document, indent=2) + "\n")

    def labelled_paragraph(self, label: str, text: str, label_colour: str) -> str:
        """Wrap `text` with `label` in the left column and the rest indented under it.

        Wrapping happens before colour is added, so escape codes never count
        towards the line length.
        """
        indent = " " * LABEL_COLUMN_WIDTH
        wrapped = textwrap.fill(
            text,
            width=self.error_width,
            initial_indent=indent,
            subsequent_indent=indent,
            # Trap: by default textwrap splits long words and breaks at
            # hyphens, which would cut a file path in two and stop a
            # contributor copying it. A path longer than a line overflows
            # instead.
            break_long_words=False,
            break_on_hyphens=False,
        )
        padded_label = label.ljust(LABEL_COLUMN_WIDTH)
        if self.error_colour:
            padded_label = label_colour + label + RESET + padded_label[len(label) :]
        return padded_label + wrapped[LABEL_COLUMN_WIDTH:]
