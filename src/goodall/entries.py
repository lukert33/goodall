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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from goodall import exit_codes, records
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.local_settings import read_local_settings
from goodall.record_repo import RECORD_TYPE_NAMES, PushState, write_record
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal


@dataclass(frozen=True)
class EntryRequest:
    """What the person asked to record, as read from the command line."""

    text: str | None
    as_json: bool


@dataclass(frozen=True)
class EntryResult:
    """What came of one `goodall journal` or `goodall intervene`, built once and shown as text or JSON.

    - `record_type` is the records module's name for the kind of record,
      such as "journal_entry"; `record_type_name` is the same in words.
    - `record_id` and `path` are None when goodall stopped before it had a
      record to write.
    - `facts` are the labelled lines shown under the summary on success.
    - `warnings` are problems that did not stop the record being kept
      safely, such as a push that is still pending; `failures` are problems
      that did.
    """

    record_type: str
    record_type_name: str
    exit_code: int
    record_id: str | None = None
    path: Path | None = None
    committed: bool = False
    push_state: PushState = PushState.NOT_ATTEMPTED
    facts: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[Failure] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)


def run_journal(terminal: Terminal, runner: Runner, request: EntryRequest) -> int:
    """Add a journal entry to the record repo, say what happened, and return the exit code."""
    result = add_journal_entry(terminal, runner, request)
    show_result(terminal, result, request.as_json)
    return result.exit_code


def add_journal_entry(terminal: Terminal, runner: Runner, request: EntryRequest) -> EntryResult:
    """Build a journal entry from the request, check it, and write it into the record repo."""
    record_type = records.JOURNAL_ENTRY_RECORD_TYPE
    text = get_entry_text(terminal, request, "journal entry", "goodall journal")
    if isinstance(text, Failure):
        return stopped(record_type, exit_codes.USAGE_ERROR, [text])
    eval_reading = find_eval_for_this_account()
    if isinstance(eval_reading, list):
        return stopped(record_type, exit_codes.ERROR, eval_reading)
    entry = records.build_journal_entry(
        contributor=eval_reading.contributor.id,
        written_at=datetime.now(UTC),
        body=text,
        work_block=current_work_block_id(),
    )
    return check_and_write(runner, eval_reading, entry, facts=[])


def current_work_block_id() -> str | None:
    """The id of the work block this account is in now, or None when there is none.

    Work blocks are not built yet, so there is never one. This is the one
    place that will change when they are: both commands stamp their record
    with whatever this returns.
    """
    return None


def stopped(record_type: str, exit_code: int, failures: list[Failure]) -> EntryResult:
    """The result of a command that stopped before it had a record to write."""
    return EntryResult(
        record_type=record_type,
        record_type_name=RECORD_TYPE_NAMES_BY_TYPE[record_type],
        exit_code=exit_code,
        failures=failures,
    )


def check_and_write(
    runner: Runner, eval_for_this_account: EvalForThisAccount, record: records.Record, facts: list[tuple[str, str]]
) -> EntryResult:
    """Check `record` against the eval, write it into the record repo, and say how far that got.

    `facts` are what the command adds to the summary shown on success, after
    the record's id, file and contributor.
    """
    record_type = record_type_of(record)
    problems = check_record(record, eval_for_this_account)
    if problems:
        return stopped(record_type, exit_codes.ERROR, problems)
    outcome = write_record(runner, eval_for_this_account.local_settings.record_repo.path, record)
    if not outcome.committed:
        return stopped(record_type, exit_codes.ERROR, outcome.failures)

    contributor = eval_for_this_account.contributor
    all_facts = [
        ("Id", record.id),
        ("File", str(outcome.path)),
        ("By", f"{contributor.display_name} ({contributor.id})"),
        *facts,
    ]
    # Once committed, the record is safe on this Mac whatever happened to
    # the push, so anything still wrong is a warning and the exit code is 0.
    return EntryResult(
        record_type=record_type,
        record_type_name=RECORD_TYPE_NAMES_BY_TYPE[record_type],
        exit_code=exit_codes.SUCCESS,
        record_id=record.id,
        path=outcome.path,
        committed=True,
        push_state=outcome.push_state,
        facts=all_facts,
        warnings=outcome.failures,
    )


# Each kind of record, in words, by its records module name.
RECORD_TYPE_NAMES_BY_TYPE = {
    records.JOURNAL_ENTRY_RECORD_TYPE: "journal entry",
    records.INTERVENTION_RECORD_TYPE: "intervention",
}


def record_type_of(record: records.Record) -> str:
    """The records module's name for the kind of `record`."""
    if isinstance(record, records.JournalEntry):
        return records.JOURNAL_ENTRY_RECORD_TYPE
    if isinstance(record, records.Intervention):
        return records.INTERVENTION_RECORD_TYPE
    return records.WORK_BLOCK_RECORD_TYPE


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


def check_record(record: records.Record, eval_for_this_account: EvalForThisAccount) -> list[Failure]:
    """Check `record` against the records rules and this eval's config, as one failure per problem.

    The records module refuses email addresses and secrets anywhere in a
    record, and its messages never repeat what they found, so neither do
    these. Nothing a person writes is refused for any other reason.
    """
    eval_config = eval_for_this_account.eval_config
    contributor_ids = []
    for contributor in eval_config.contributors:
        contributor_ids.append(contributor.id)
    problems = records.validate_record(
        record,
        contributor_ids=contributor_ids,
        workstreams=eval_config.workstreams,
        intervention_categories=eval_config.intervention_categories,
    )
    record_type_name = RECORD_TYPE_NAMES[type(record)]
    failures = []
    for problem in problems:
        failures.append(record_problem_failure(record_type_name, problem))
    return failures


def record_problem_failure(record_type_name: str, problem: records.RecordProblem) -> Failure:
    """The failure for one problem the records module found in a record goodall was about to write."""
    message = problem.message
    # The records module calls the text a record's "body", a word a
    # contributor never sees; they wrote "the text", so it is called that.
    if problem.field == "body" and message.startswith("body"):
        message = "The text" + message.removeprefix("body")
    return Failure(
        kind="entry_refused",
        what_happened=f"Goodall did not record the {record_type_name}. {message}",
        why=(
            "Records are meant to be published, so goodall keeps email addresses and secrets out of them, and "
            "keeps every value in the shape the eval config expects."
        ),
        what_to_do_next="Change what the message above names, then run the command again.",
    )


def find_eval_for_this_account() -> EvalForThisAccount | list[Failure]:
    """Read local settings and the eval config, and check they agree, as `goodall status` does."""
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        return settings_reading
    return read_eval_for_this_account(settings_reading)


def show_result(terminal: Terminal, result: EntryResult, as_json: bool) -> None:
    """Show `result` as text, or as one JSON document whose keys are always the same."""
    if as_json:
        path = None
        if result.path is not None:
            path = str(result.path)
        warning_documents = []
        for warning in result.warnings:
            warning_documents.append(warning.as_json())
        failure_documents = []
        for failure in result.failures:
            failure_documents.append(failure.as_json())
        terminal.write_json(
            {
                "record_type": result.record_type,
                "id": result.record_id,
                "path": path,
                "committed": result.committed,
                "pushed": result.push_state == PushState.PUSHED,
                "push_state": str(result.push_state),
                "warnings": warning_documents,
                "failures": failure_documents,
            }
        )
        return
    if not result.committed:
        terminal.show_failures(result.failures)
        return
    terminal.show_paragraph(success_summary(result))
    terminal.show_facts(result.facts)


def success_summary(result: EntryResult) -> str:
    """One sentence saying the record is kept and whether others can see it yet, in a contributor's words.

    "Shared record repo" is the remote; the word "push" is left to the
    warning, which names git's own terms for whoever has to fix it.
    """
    name = result.record_type_name
    if result.push_state == PushState.PUSHED:
        return f"Recorded your {name} and sent it to the shared record repo."
    if result.push_state == PushState.PENDING:
        return (
            f"Recorded your {name}. It is saved on this Mac and will be sent to the shared record repo the next "
            "time goodall records something."
        )
    return f"Recorded your {name}. This record repo has no shared copy to send it to, so it is kept on this Mac."
