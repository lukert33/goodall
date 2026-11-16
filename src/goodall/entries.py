"""`goodall journal` and `goodall intervene`: a contributor adds to the eval's own record.

Called by `cli.main`. A contributor, or an agent working for one, runs
`goodall journal "..."` to add a journal entry, or `goodall intervene
--category ... --decision ... "..."` to add an intervention. Both commands
do the same five things, in this order:

1. Get the text: from the argument, or from standard input when text is
   piped in. Goodall never asks a question in the terminal (ADR 0012), so
   with no text and a person at the keyboard it fails at once and shows how
   to pass the text, instead of sitting waiting for typing.
2. Find this account's eval: local settings, the record repo folder, the
   eval config, and this account's contributor in its roster.
3. Build the record (goodall.records), stamped with the contributor and the
   current UTC time, and check it against the eval config.
4. Write it through goodall.record_repo, which writes the file, commits it
   and pushes it.
5. Say what was recorded and where, as text or as one JSON document.

Exit codes: 0 once the record is committed, because from then on it is
safe, even if the push is still pending (that is shown as a warning); 1 when
it was not committed; 2 for a mistake in how the command was run.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from goodall import exit_codes, records
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.local_settings import read_local_settings
from goodall.record_repo import RecordWriteOutcome, write_record
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal


@dataclass(frozen=True)
class EntryRequest:
    """What the person asked to record, as read from the command line."""

    text: str | None
    as_json: bool


def run_journal(terminal: Terminal, runner: Runner, request: EntryRequest) -> int:
    """Add a journal entry to the record repo and return the exit code."""
    text = get_entry_text(terminal, request, "journal entry", "goodall journal")
    if isinstance(text, Failure):
        terminal.show_failure(text)
        return exit_codes.USAGE_ERROR
    eval_reading = find_eval_for_this_account()
    if isinstance(eval_reading, list):
        terminal.show_failures(eval_reading)
        return exit_codes.ERROR
    entry = records.build_journal_entry(
        contributor=eval_reading.contributor.id,
        written_at=datetime.now(UTC),
        body=text,
    )
    outcome = write_record(runner, eval_reading.local_settings.record_repo.path, entry)
    return report_outcome(terminal, "journal entry", outcome)


def get_entry_text(terminal: Terminal, request: EntryRequest, record_type_name: str, command: str) -> str | Failure:
    """Return the text to record, from the argument or from what was piped in, or a usage failure.

    The argument wins when both are there, and then standard input is not
    read at all: an agent's shell may leave standard input open with
    nothing coming, and reading it would hang.
    """
    text = request.text
    if text is None:
        text = terminal.read_piped_text()
    if text is None:
        return text_missing_failure(record_type_name, command)
    if text.strip() == "":
        return text_empty_failure(record_type_name, command)
    return text


def text_empty_failure(record_type_name: str, command: str) -> Failure:
    """The failure for text that is empty or only spaces and blank lines."""
    return Failure(
        kind="entry_text_empty",
        what_happened=f"The text for the {record_type_name} is empty. Nothing was recorded.",
        why="A record with nothing written in it says nothing about what happened, so goodall does not keep one.",
        what_to_do_next=(
            "Run the command again with the text you meant to record. If you piped it in, check that what you "
            "copied was not empty."
        ),
        commands_to_try=(f'{command} "What happened, and who decided"',),
    )


def text_missing_failure(record_type_name: str, command: str) -> Failure:
    """The failure for a command run with no text, at a keyboard, so nothing was piped in either.

    `command` is the command as a person would type it without the text,
    such as `goodall journal`, so each example can be copied as it stands.
    """
    return Failure(
        kind="entry_text_missing",
        what_happened=f"No text was given for the {record_type_name}. Nothing was recorded.",
        why=(
            "Goodall never asks questions in the terminal, so what you want to record has to come with the "
            "command itself."
        ),
        what_to_do_next=(
            "Put the text after the command, inside double quotes. For several lines, copy them and pipe "
            "them in with pbpaste."
        ),
        commands_to_try=(
            f'{command} "What happened, and who decided"',
            f"pbpaste | {command}",
        ),
    )


def find_eval_for_this_account() -> EvalForThisAccount | list[Failure]:
    """Read local settings and the eval config, and check they agree, as `goodall status` does."""
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        return settings_reading
    return read_eval_for_this_account(settings_reading)


def report_outcome(terminal: Terminal, record_type_name: str, outcome: RecordWriteOutcome) -> int:
    """Say what happened to the record, and return the exit code."""
    if not outcome.committed:
        terminal.show_failures(outcome.failures)
        return exit_codes.ERROR
    terminal.show_paragraph(f"Recorded your {record_type_name}.")
    terminal.show_facts([("File", str(outcome.path))])
    return exit_codes.SUCCESS
