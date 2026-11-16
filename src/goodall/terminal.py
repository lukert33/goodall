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
- A command builds its results once, as records such as `Failure` and
  `CheckResult`. The text screen and the JSON document are two renderings of
  the same record. `CheckResult` lives here, beside `Failure`, because ADR
  0012 makes "a check result" one of the things this module offers.
- Colour is used only when the stream is a terminal, is switched off by the
  `NO_COLOR` environment variable (https://no-color.org), and is never the
  only signal: the words FAIL, and in a check report pass, warn and skip,
  are always printed.
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
from dataclasses import asdict, dataclass, field
from enum import StrEnum
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

# ANSI escape sequences, in the basic eight colours, which every terminal
# theme maps to something readable on its own background.
BOLD_RED = "\x1b[1;31m"
BOLD_YELLOW = "\x1b[1;33m"
GREEN = "\x1b[32m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

# In the check report, a check's status word sits in a column after this
# indent, and everything beneath a check (what happened, what to do next) is
# indented to line up with the check's summary.
CHECK_INDENT = "  "
CHECK_DETAIL_INDENT = " " * 8


@dataclass(frozen=True)
class Failure:
    """Something that stopped a command, said so a person can act on it.

    Every failure goodall reports has the same three parts, so a contributor
    learns to read goodall's errors once.

    `kind` is a short fixed name such as "local_settings_missing". It appears
    only in JSON, so an agent can branch on it without matching English
    sentences, which may be reworded.

    `commands_to_try` holds whole commands a person can copy and run, such
    as `pbpaste | goodall journal`. They are shown one per line and never
    wrapped, because a command split across two lines no longer runs when
    pasted. Most failures have none.
    """

    kind: str
    what_happened: str
    why: str
    what_to_do_next: str
    commands_to_try: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        """Return this failure as a dictionary ready for `Terminal.write_json`.

        `commands_to_try` appears only when there are some, so every other
        failure keeps the four keys it has always had.
        """
        document: dict[str, object] = asdict(self)
        if not self.commands_to_try:
            del document["commands_to_try"]
        else:
            document["commands_to_try"] = list(self.commands_to_try)
        return document


class CheckStatus(StrEnum):
    """How one diagnostic check came out.

    The values are what `--json` shows. A StrEnum, so `json.dumps` writes
    the plain word.

    - PASS: what the check looks for is true.
    - FAIL: it is not, and goodall cannot work properly until it is fixed.
    - WARN: it is not, but goodall still works in a reduced way, such as
      "not recording" when the log directory is unplugged.
    - SKIP: the check was not run because a check it depends on failed.
    """

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


# The word printed for each status. FAIL is in capitals so it stands out with
# colour off; the others stay lowercase so the eye lands on failures first.
STATUS_WORDS = {
    CheckStatus.PASS: "pass",
    CheckStatus.FAIL: "FAIL",
    CheckStatus.WARN: "warn",
    CheckStatus.SKIP: "skip",
}

# The colour of each status word, used only when colour is wanted.
STATUS_COLOURS = {
    CheckStatus.PASS: GREEN,
    CheckStatus.FAIL: BOLD_RED,
    CheckStatus.WARN: BOLD_YELLOW,
    CheckStatus.SKIP: "",
}


@dataclass(frozen=True)
class CheckResult:
    """The result of one diagnostic check, as `goodall doctor` reports it.

    The text report and the `--json` document are both built from a list of
    these, so the two can never disagree.

    - `id` is stable, such as "settings.local_settings_valid", so an agent
      can branch on it; `summary` is the one line a person reads, and may be
      reworded.
    - `group` is the heading the check is shown under, such as "Settings".
    - `failures` says what is wrong, in goodall's usual what, why and next
      form. It is empty unless the status is FAIL or WARN, and never empty
      when it is: a failed check always carries a next step.
    - `waiting_for` is set only when the status is SKIP: the id of the
      failed check this one depends on.
    """

    id: str
    group: str
    status: CheckStatus
    summary: str
    failures: list[Failure] = field(default_factory=list)
    waiting_for: str | None = None

    @property
    def detail(self) -> str | None:
        """What happened, as one paragraph, or None when nothing is wrong."""
        if not self.failures:
            return None
        what_happened_parts = []
        for failure in self.failures:
            what_happened_parts.append(failure.what_happened)
        return " ".join(what_happened_parts)

    @property
    def next_step(self) -> str | None:
        """What to do next, as one paragraph, or None when nothing is wrong."""
        if not self.failures:
            return None
        next_step_parts = []
        for failure in self.failures:
            next_step_parts.append(failure.what_to_do_next)
        return " ".join(next_step_parts)

    def as_json(self) -> dict[str, object]:
        """Return this result as a dictionary ready for `Terminal.write_json`.

        `detail` and `next_step` are there for an agent that wants one
        sentence; `failures` keeps each problem's `kind`, for an agent that
        wants to branch on exactly what went wrong.
        """
        failure_documents = []
        for failure in self.failures:
            failure_documents.append(failure.as_json())
        return {
            "id": self.id,
            "group": self.group,
            "status": str(self.status),
            "summary": self.summary,
            "detail": self.detail,
            "next_step": self.next_step,
            "waiting_for": self.waiting_for,
            "failures": failure_documents,
        }


@dataclass(frozen=True)
class CheckCounts:
    """How many checks came out each way: the report's last line and the JSON summary."""

    passed: int
    warned: int
    failed: int
    skipped: int


def count_checks(results: list[CheckResult]) -> CheckCounts:
    """Count the results by status."""
    passed = 0
    warned = 0
    failed = 0
    skipped = 0
    for result in results:
        if result.status == CheckStatus.PASS:
            passed += 1
        elif result.status == CheckStatus.WARN:
            warned += 1
        elif result.status == CheckStatus.FAIL:
            failed += 1
        else:
            skipped += 1
    return CheckCounts(passed=passed, warned=warned, failed=failed, skipped=skipped)


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

    def __init__(
        self,
        standard_output: TextIO | None = None,
        standard_error: TextIO | None = None,
        standard_input: TextIO | None = None,
    ) -> None:
        # Trap: the defaults are looked up now rather than in the signature,
        # so that anything that swaps sys.stdout before goodall runs (pytest's
        # capture, contextlib.redirect_stdout) is respected.
        if standard_output is None:
            standard_output = sys.stdout
        if standard_error is None:
            standard_error = sys.stderr
        if standard_input is None:
            # Trap: sys.stdin is None when goodall is started with standard
            # input closed; read_piped_text treats that as nothing piped.
            standard_input = sys.stdin
        self.standard_output = standard_output
        self.standard_error = standard_error
        self.standard_input: TextIO | None = standard_input

        # Measured once, so one command's output never changes width halfway
        # through if the window is resized.
        self.output_width = measure_width(standard_output)
        self.error_width = measure_width(standard_error)
        self.error_colour = colour_is_wanted(standard_error)
        self.output_colour = colour_is_wanted(standard_output)

        # True once anything has been written to standard output, so the
        # next block knows to start with a blank line.
        self.output_started = False

    def read_piped_text(self) -> str | None:
        """Return everything piped to standard input, or None when nothing can be piped.

        This is not a question: it reads text that was sent with the command
        (`pbpaste | goodall journal`), never what a person types in answer to
        a prompt. So when standard input is a terminal, which means a person
        at the keyboard and nothing piped, it returns None at once instead of
        waiting for them to type.
        """
        if self.standard_input is None or self.standard_input.isatty():
            return None
        return self.standard_input.read()

    def show_facts(self, facts: list[tuple[str, str]]) -> None:
        """Write labelled facts to standard output, one per line, values lined up in one column.

        Each fact is a (label, value) pair, such as ("Eval", "goodall-pilot").
        The value column starts two spaces after the longest label.
        """
        self.start_output_block()
        longest_label_length = 0
        for label, _ in facts:
            longest_label_length = max(longest_label_length, len(label))
        value_column = longest_label_length + 2

        lines = []
        for label, value in facts:
            wrapped = self.wrap(value, self.output_width, " " * value_column)
            lines.append(label.ljust(value_column) + wrapped[value_column:])
        self.standard_output.write("\n".join(lines) + "\n")

    def show_paragraph(self, text: str) -> None:
        """Write a paragraph of text to standard output, wrapped to the output width."""
        self.start_output_block()
        self.standard_output.write(self.wrap(text, self.output_width, "") + "\n")

    def start_output_block(self) -> None:
        """Separate each block on standard output from the one before it with a blank line."""
        if self.output_started:
            self.standard_output.write("\n")
        self.output_started = True

    def wrap(self, text: str, width: int, indent: str) -> str:
        """Wrap `text` to `width`, with every line starting with `indent`."""
        return textwrap.fill(
            text,
            width=width,
            initial_indent=indent,
            subsequent_indent=indent,
            # Trap: by default textwrap splits long words and breaks at
            # hyphens, which would cut a file path in two and stop a
            # contributor copying it. A path longer than a line overflows
            # instead.
            break_long_words=False,
            break_on_hyphens=False,
        )

    def show_failure(self, failure: Failure) -> None:
        """Write a failure to standard error as three labelled paragraphs."""
        paragraphs = [
            self.labelled_paragraph("FAIL", failure.what_happened, BOLD_RED),
            self.labelled_paragraph("Why", failure.why, BOLD),
            self.labelled_paragraph("Next", failure.what_to_do_next, BOLD),
        ]
        if failure.commands_to_try:
            paragraphs.append(self.commands_paragraph(failure.commands_to_try))
        self.standard_error.write("\n\n".join(paragraphs) + "\n")

    def commands_paragraph(self, commands: tuple[str, ...]) -> str:
        """Commands to copy, labelled "Try", one per line and never wrapped, so each pastes as it stands."""
        label = "Try".ljust(LABEL_COLUMN_WIDTH)
        if self.error_colour:
            label = BOLD + "Try" + RESET + label[len("Try") :]
        lines = []
        for position, command in enumerate(commands):
            if position == 0:
                lines.append(label + command)
            else:
                lines.append(" " * LABEL_COLUMN_WIDTH + command)
        return "\n".join(lines)

    def show_failures(self, failures: list[Failure]) -> None:
        """Write several failures to standard error, one after another, with a blank line between them."""
        for position, failure in enumerate(failures):
            if position > 0:
                self.standard_error.write("\n")
            self.show_failure(failure)

    def show_check_report(self, results: list[CheckResult]) -> None:
        """Write a diagnostic report to standard output: checks under group headings, then the counts.

        The whole report goes to standard output, failed checks included,
        because the report is the result the contributor asked for; it can be
        redirected to a file or pasted into an issue in one piece.

        Groups appear in the order of their first check, and checks in the
        order given, which is the order they ran in. Beneath a failed or
        warning check go what happened and the next step; beneath a skipped
        check goes one line naming the check it was waiting for. A blank line
        follows each failed or warning check, so one problem's next step is
        never read as the next check's; skipped checks stay one under
        another, since several usually wait for the same failure.
        """
        self.start_output_block()

        summaries_by_id: dict[str, str] = {}
        group_names: list[str] = []
        for result in results:
            summaries_by_id[result.id] = result.summary
            if result.group not in group_names:
                group_names.append(result.group)

        group_blocks = []
        for group_name in group_names:
            group_results = [result for result in results if result.group == group_name]
            heading = group_name
            if self.output_colour:
                heading = BOLD + group_name + RESET
            group_text = heading + "\n"
            for position, result in enumerate(group_results):
                group_text += self.check_entry(result, summaries_by_id)
                is_last_in_group = position == len(group_results) - 1
                if result.failures and not is_last_in_group:
                    group_text += "\n"
            group_blocks.append(group_text)

        counts = count_checks(results)
        counts_line = (
            f"{counts.passed} passed, {counts.warned} warned, {counts.failed} failed, {counts.skipped} skipped\n"
        )
        self.standard_output.write("\n".join(group_blocks) + "\n" + counts_line)

    def check_entry(self, result: CheckResult, summaries_by_id: dict[str, str]) -> str:
        """The lines for one check: its status word and summary, then anything beneath it.

        Trap: wrapping happens before the status word is coloured, so escape
        codes never count towards the line length.
        """
        word = STATUS_WORDS[result.status]
        status_column = CHECK_INDENT + word.ljust(len(CHECK_DETAIL_INDENT) - len(CHECK_INDENT))
        wrapped_summary = self.wrap(result.summary, self.output_width, CHECK_DETAIL_INDENT)
        colour = STATUS_COLOURS[result.status]
        if self.output_colour and colour != "":
            status_column = status_column.replace(word, colour + word + RESET, 1)
        entry = status_column + wrapped_summary[len(CHECK_DETAIL_INDENT) :] + "\n"

        if result.waiting_for is not None:
            # A skipped check names the summary of the one it waited for,
            # which a person can find higher up in the report; --json gives
            # the id instead.
            waited_for_summary = summaries_by_id.get(result.waiting_for, result.waiting_for)
            entry += self.wrap("Waiting for: " + waited_for_summary, self.output_width, CHECK_DETAIL_INDENT) + "\n"

        failure_texts = []
        for failure in result.failures:
            what_happened = self.wrap(failure.what_happened, self.output_width, CHECK_DETAIL_INDENT)
            next_step = self.wrap("Next: " + failure.what_to_do_next, self.output_width, CHECK_DETAIL_INDENT)
            failure_texts.append(what_happened + "\n" + next_step + "\n")
        return entry + "\n".join(failure_texts)

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
        wrapped = self.wrap(text, self.error_width, " " * LABEL_COLUMN_WIDTH)
        padded_label = label.ljust(LABEL_COLUMN_WIDTH)
        if self.error_colour:
            padded_label = label_colour + label + RESET + padded_label[len(label) :]
        return padded_label + wrapped[LABEL_COLUMN_WIDTH:]
