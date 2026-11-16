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
    eval_reading = find_eval_for_this_account()
    if isinstance(eval_reading, list):
        terminal.show_failures(eval_reading)
        return exit_codes.ERROR
    assert request.text is not None
    entry = records.build_journal_entry(
        contributor=eval_reading.contributor.id,
        written_at=datetime.now(UTC),
        body=request.text,
    )
    outcome = write_record(runner, eval_reading.local_settings.record_repo.path, entry)
    return report_outcome(terminal, "journal entry", outcome)


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
