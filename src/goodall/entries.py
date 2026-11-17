"""`goodall journal` and `goodall intervene`: a contributor adds to the eval's own record.

Called by `cli.main`. A contributor, or an agent working for one, runs
`goodall journal "..."` to add a journal entry, or `goodall intervene
--category ... --decision ... "..."` to add an intervention. Both commands
do the same five things, in this order:

1. Get the text: from the argument, from a plain text file (`--file`, for
   someone who would rather write in TextEdit), or from standard input when
   text is piped in (`pbpaste | goodall journal`). Goodall never asks a
   question in the terminal (ADR 0012), so with no text and a person at
   the keyboard it fails at once and shows how to pass the text, instead
   of sitting waiting for typing. Empty text, and text over
   MAXIMUM_TEXT_CHARACTERS, are refused.
2. Find this account's eval: local settings, the record repo folder, the
   eval config, and this account's contributor in its roster.
3. Build the record (goodall.records), stamped with the contributor, the
   current UTC time and the current work block (`current_work_block_id`),
   and check it against the eval config. Nothing a person writes is
   refused except what the records module refuses: email addresses and
   secrets. No failure ever repeats the text.
4. Write it through goodall.record_repo, which writes the file, commits it
   and pushes it.
5. Say what was recorded and where, as text or as one JSON document.

Exit codes: 0 once the record is committed, because from then on it is
safe, even if the push is still pending (that is shown as a warning); 1 when
it was not committed; 2 for a mistake in how the command was run, including
the text and every option, so nothing was attempted.

Open question, deliberately not settled here: an agent can run these
commands too, and the record then names the contributor with nothing saying
an agent wrote the words. Every choice in this module is provisional.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from goodall import exit_codes, records
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.folders import describe_folder_problem, where_goodall_looked
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.record_repo import RECORD_TYPE_NAMES, PushState, write_record
from goodall.runner import Runner
from goodall.terminal import Failure, Terminal


@dataclass(frozen=True)
class EntryRequest:
    """What the person asked to record, as read from the command line.

    `text` is the text given as an argument and `text_file` a file to read
    it from; at most one of them is set, and with neither the text is read
    from standard input.
    """

    text: str | None
    as_json: bool
    text_file: Path | None = None


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


@dataclass(frozen=True)
class InterventionDetails:
    """What `goodall intervene` needs beyond the text, as typed on the command line.

    Each is None when its option was left out. The record schema's other
    optional field, `question_outcome`, is deliberately not offered: it
    describes how a question the agent asked was answered, which the hook
    path can read from the transcript far more reliably than a person can
    recall it afterwards.
    """

    category: str | None
    decision: str | None
    interrupted: str | None = None
    claude_session: str | None = None
    occurred_at: str | None = None


def run_intervene(terminal: Terminal, runner: Runner, request: EntryRequest, details: InterventionDetails) -> int:
    """Add an intervention to the record repo, say what happened, and return the exit code."""
    result = add_intervention(terminal, runner, request, details)
    show_result(terminal, result, request.as_json)
    return result.exit_code


def add_intervention(
    terminal: Terminal, runner: Runner, request: EntryRequest, details: InterventionDetails
) -> EntryResult:
    """Build an intervention from the request, check it, and write it into the record repo."""
    record_type = records.INTERVENTION_RECORD_TYPE
    command = intervene_command(details)
    text = get_entry_text(terminal, request, "intervention", command)
    if isinstance(text, Failure):
        return stopped(record_type, exit_codes.USAGE_ERROR, [text])
    eval_reading = find_eval_for_this_account()
    if isinstance(eval_reading, list):
        return stopped(record_type, exit_codes.ERROR, eval_reading)
    # Both are checked before either is reported, so a person who left out
    # both learns about both at once.
    usage_failures = []
    category_failure = check_category(details.category, eval_reading)
    if category_failure is not None:
        usage_failures.append(category_failure)
    decision_failure = check_decision(details.decision)
    if decision_failure is not None:
        usage_failures.append(decision_failure)
    if details.claude_session is not None and not records.CLAUDE_SESSION_ID_PATTERN.fullmatch(details.claude_session):
        usage_failures.append(claude_session_invalid_failure())
    now = datetime.now(UTC)
    occurred_at = now
    if details.occurred_at is not None:
        occurred_at_or_failure = parse_occurred_at(details.occurred_at, now, command)
        if isinstance(occurred_at_or_failure, Failure):
            usage_failures.append(occurred_at_or_failure)
        else:
            occurred_at = occurred_at_or_failure
    if usage_failures:
        return stopped(record_type, exit_codes.USAGE_ERROR, usage_failures)
    # The checks above guarantee these; the assert tells the type checker.
    assert details.category is not None and details.decision is not None
    intervention = records.build_intervention(
        contributor=eval_reading.contributor.id,
        occurred_at=occurred_at,
        written_at=now,
        category=details.category,
        decision=details.decision,
        body=text,
        work_block=current_work_block_id(),
        claude_session=details.claude_session,
        interrupted=details.interrupted,
    )
    facts = [("Category", details.category), ("Decision", details.decision)]
    return check_and_write(runner, eval_reading, intervention, facts)


def check_category(category: str | None, eval_for_this_account: EvalForThisAccount) -> Failure | None:
    """Say what is wrong with the --category given, or None when it is one of this eval's categories.

    Checked here, after the eval config is read, rather than by argparse,
    because the valid categories are the eval's own, so the failure can list
    them. The value given is not repeated back: it is matched exactly, and
    the list shows the right spelling.
    """
    eval_config = eval_for_this_account.eval_config
    categories = ", ".join(eval_config.intervention_categories)
    first_category = eval_config.intervention_categories[0]
    commands_to_try = (f'goodall intervene --category {first_category} --decision modify "What happened and why"',)
    where_listed = f"the intervention_categories in {eval_config.config_file}"
    if category is None:
        return Failure(
            kind="intervention_category_missing",
            what_happened="No --category was given for the intervention. Nothing was recorded.",
            why=(
                "Every intervention is sorted into one of the eval's categories, so the researcher can compare "
                "like with like."
            ),
            what_to_do_next=f"Add --category with one of: {categories}. They are listed in {where_listed}.",
            commands_to_try=commands_to_try,
        )
    if category not in eval_config.intervention_categories:
        return Failure(
            kind="intervention_category_unknown",
            what_happened="The --category given is not one of this eval's categories. Nothing was recorded.",
            why=(
                "Every intervention is sorted into one of the eval's own categories, spelled exactly as listed, "
                "so the researcher can compare like with like."
            ),
            what_to_do_next=(
                f"Use one of: {categories}, spelled exactly so. They are listed in {where_listed}; to add a new "
                "one, ask whoever runs the eval."
            ),
            commands_to_try=commands_to_try,
        )
    return None


# What each decision means for a person working beside an agent, in the
# words of docs/record-schemas.md. The decisions themselves are Inspect's
# (records.DECISIONS); `goodall intervene --help` shows this list too.
DECISION_MEANINGS = {
    "approve": "you reviewed what the agent was about to do and let it go ahead",
    "modify": "you changed what the agent did or was about to do",
    "reject": "you refused one action, and the agent carried on otherwise",
    "escalate": "you took a step over because the agent could not, or handed it on",
    "terminate": "you stopped the agent's work on the task altogether",
}


def check_decision(decision: str | None) -> Failure | None:
    """Say what is wrong with the --decision given, or None when it is one of the five decisions."""
    if decision in records.DECISIONS:
        return None
    meanings = []
    for known_decision in records.DECISIONS:
        meanings.append(f"{known_decision} ({DECISION_MEANINGS[known_decision]})")
    why = "Each intervention records what the person decided, in one of five words shared with Inspect."
    what_to_do_next = f"Add --decision with one of: {'; '.join(meanings)}."
    if decision is None:
        return Failure(
            kind="intervention_decision_missing",
            what_happened="No --decision was given for the intervention. Nothing was recorded.",
            why=why,
            what_to_do_next=what_to_do_next,
        )
    return Failure(
        kind="intervention_decision_unknown",
        what_happened="The --decision given is not one of the five decisions. Nothing was recorded.",
        why=why + " They are matched exactly, in lowercase.",
        what_to_do_next=what_to_do_next,
    )


def intervene_command(details: InterventionDetails) -> str:
    """The start of a `goodall intervene` command for a failure to offer, without the text.

    It carries the person's own --category and --decision when they are
    well formed, so the example is theirs to copy, and plain capitalised
    words where not. Trap: a placeholder like `<category>` must never be
    offered, because a shell reads `<` as "read from this file", so pasting
    it would fail confusingly or worse.
    """
    category = "CATEGORY"
    if details.category is not None and records.IDENTIFIER_PATTERN.fullmatch(details.category):
        category = details.category
    decision = "DECISION"
    if details.decision in records.DECISIONS:
        decision = details.decision
    return f"goodall intervene --category {category} --decision {decision}"


# A time of day on its own, like 9:30 or 14:05:30.
TIME_OF_DAY_PATTERN = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_occurred_at(typed: str, now: datetime, command: str) -> datetime | Failure:
    """Turn what was typed after --occurred-at into a moment no later than `now`, or a usage failure.

    People think in their own clock, so a time without a zone is this Mac's
    local time, and is converted to UTC for the record. Three forms:

    - a time of day, `14:05`: the most recent 14:05, which is yesterday's
      when it is not 14:05 yet today, so an intervention noted just after
      midnight lands on the right day;
    - a date and time, `2026-11-16 14:05`, in local time;
    - a full timestamp with a zone, `2026-11-16T14:05:00Z` or `+01:00`.

    A date on its own is refused rather than read as midnight, which would
    pretend to know the time. So is a moment after `now`: the schema says an
    intervention is written down no earlier than it happened. `command` is
    the start of the command a failure offers to copy (`intervene_command`).
    """
    local_now = now.astimezone()
    time_of_day = TIME_OF_DAY_PATTERN.fullmatch(typed)
    moment: datetime | None = None
    if time_of_day is not None:
        hour = int(time_of_day.group(1))
        minute = int(time_of_day.group(2))
        second = int(time_of_day.group(3) or "0")
        try:
            moment = local_now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        except ValueError:
            moment = None
        if moment is not None and moment > local_now:
            moment = moment - timedelta(days=1)
    elif ":" in typed:
        try:
            moment = datetime.fromisoformat(typed)
        except ValueError:
            moment = None
        if moment is not None and moment.utcoffset() is None:
            # Trap: astimezone() on a moment with no zone takes it as this
            # Mac's local time, which is what a person typing it meant.
            moment = moment.astimezone()
    if moment is None:
        return occurred_at_unreadable_failure(command)
    if moment > now:
        return occurred_at_in_future_failure(command)
    return moment.astimezone(UTC)


def occurred_at_examples(command: str) -> tuple[str, ...]:
    """Commands showing the forms --occurred-at accepts, for a failure to offer."""
    return (
        f'{command} --occurred-at 14:05 "What happened"',
        f'{command} --occurred-at "2026-11-16 14:05" "What happened"',
    )


def occurred_at_unreadable_failure(command: str) -> Failure:
    """The failure for an --occurred-at goodall cannot read as a moment."""
    return Failure(
        kind="intervention_occurred_at_unreadable",
        what_happened="Goodall could not read the --occurred-at given as a time. Nothing was recorded.",
        why="An intervention records when it happened, so the time has to be exact, to the minute at least.",
        what_to_do_next=(
            "Give a time of day on this Mac's clock, such as 14:05 (the most recent 14:05), or a date and time, "
            "such as 2026-11-16 14:05. Leave --occurred-at out if it happened just now."
        ),
        commands_to_try=occurred_at_examples(command),
    )


def occurred_at_in_future_failure(command: str) -> Failure:
    """The failure for an --occurred-at later than now."""
    return Failure(
        kind="intervention_occurred_at_in_future",
        what_happened="The --occurred-at given is later than now. Nothing was recorded.",
        why="An intervention is recorded once it has happened, so it cannot have happened after now.",
        what_to_do_next=(
            "Check the date and time, and that this Mac's clock is right. A time without a date means the most "
            "recent one, on this Mac's clock."
        ),
        commands_to_try=occurred_at_examples(command),
    )


def claude_session_invalid_failure() -> Failure:
    """The failure for a --claude-session that is not a Claude Code session id.

    The value is not repeated back, in case something private was pasted.
    """
    return Failure(
        kind="intervention_claude_session_invalid",
        what_happened="The --claude-session given is not a Claude Code session id. Nothing was recorded.",
        why=(
            "A session id ties the intervention to the transcript of the Claude session it happened in. It is "
            "a lowercase id like 0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5."
        ),
        what_to_do_next=(
            "In that Claude session, type /status and copy the Session ID it shows. Or leave --claude-session "
            "out: it is optional."
        ),
    )


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
    """Return the text to record, from the argument, a file or what was piped in, or a usage failure.

    With an argument or a file, standard input is not read at all: an
    agent's shell may leave standard input open with nothing coming, and
    reading it would hang. An argument and a file together are refused
    rather than one silently winning.
    """
    if request.text is not None and request.text_file is not None:
        return text_given_twice_failure(record_type_name)
    text = request.text
    if request.text_file is not None:
        text_or_failure = read_text_file(request.text_file)
        if isinstance(text_or_failure, Failure):
            return text_or_failure
        text = text_or_failure
    if text is None:
        try:
            text = terminal.read_piped_text(MAXIMUM_TEXT_CHARACTERS)
        except UnicodeDecodeError:
            return piped_text_not_plain_text_failure(record_type_name)
    if text is None:
        return text_missing_failure(record_type_name, command)
    if text.strip() == "":
        return text_empty_failure(record_type_name, command)
    if len(text) > MAXIMUM_TEXT_CHARACTERS:
        return text_too_long_failure(record_type_name)
    return text


# The longest text one entry may have: about fifteen thousand words, or forty
# printed pages. Far longer than any note, so nothing a person writes is
# refused, but it stops a whole transcript or log pasted by mistake from
# landing in a record that is meant to be read and published. Transcripts
# are kept elsewhere, as raw records.
MAXIMUM_TEXT_CHARACTERS = 100_000


def read_text_file(text_file: Path) -> str | Failure:
    """Read the text of an entry from `text_file`, or say why it could not be.

    The file is the contributor's own draft, written in TextEdit or any
    other editor: goodall reads it and leaves it where it is. It must be
    UTF-8 plain text, which is what TextEdit saves in its "Make Plain Text"
    mode; a rich-text or binary file is refused without showing any of it.
    At most one character more than the limit is read, so a huge file is
    refused without being loaded whole.
    """
    try:
        with text_file.open(encoding="utf-8") as opened_file:
            return opened_file.read(MAXIMUM_TEXT_CHARACTERS + 1)
    except UnicodeDecodeError:
        return text_file_not_plain_text_failure(text_file)
    except OSError as read_error:
        return text_file_unreadable_failure(text_file, read_error)


def text_too_long_failure(record_type_name: str) -> Failure:
    """The failure for text longer than MAXIMUM_TEXT_CHARACTERS."""
    return Failure(
        kind="entry_text_too_long",
        what_happened=(
            f"The text for the {record_type_name} is longer than {MAXIMUM_TEXT_CHARACTERS} characters. "
            "Nothing was recorded."
        ),
        why=(
            "An entry is a note that people will read, so goodall caps its length well beyond any note. Text this "
            "long is usually a transcript or log pasted by mistake, and goodall keeps those separately."
        ),
        what_to_do_next=(
            "Check that you copied what you meant to. To record something this long, write a short entry that "
            "says where the full text is kept."
        ),
    )


def piped_text_not_plain_text_failure(record_type_name: str) -> Failure:
    """The failure for bytes piped to standard input that are not UTF-8 text."""
    return Failure(
        kind="entry_text_not_plain_text",
        what_happened=f"What was piped in for the {record_type_name} is not plain text. Nothing was recorded.",
        why="Records are plain text, so they can be read, compared and published without special software.",
        what_to_do_next="Pipe in plain text, or save the entry as a plain text file and pass it with --file.",
    )


def text_given_twice_failure(record_type_name: str) -> Failure:
    """The failure for text given both after the command and in a file."""
    return Failure(
        kind="entry_text_given_twice",
        what_happened=f"Both text and a file were given for the {record_type_name}. Nothing was recorded.",
        why="Goodall records one text per entry, and will not guess which of the two you meant.",
        what_to_do_next="Run the command again with either the text in quotes or --file, not both.",
    )


def text_file_unreadable_failure(text_file: Path, read_error: OSError) -> Failure:
    """The failure for a --file that is missing, is a folder, or may not be read."""
    return Failure(
        kind="entry_text_file_unreadable",
        what_happened=f"Goodall could not read {text_file}: {read_error.strerror}. Nothing was recorded.",
        why="With --file, the text of the entry is read from that file.",
        what_to_do_next=(
            "Check the file's name and folder. In Finder, holding Option while you right-click a file offers "
            '"Copy as Pathname", which gives its full path to paste after --file.'
        ),
    )


def text_file_not_plain_text_failure(text_file: Path) -> Failure:
    """The failure for a --file that is not UTF-8 plain text, such as a Word document or a picture."""
    return Failure(
        kind="entry_text_file_not_plain_text",
        what_happened=f"The file {text_file} is not plain text, so goodall did not read it. Nothing was recorded.",
        why="Records are plain text, so they can be read, compared and published without special software.",
        what_to_do_next=(
            'Save the entry as plain text: in TextEdit, choose Format, then "Make Plain Text", then save. '
            "Then run the command again with that file."
        ),
    )


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
    # Checked first so a missing folder is reported as that, not as a
    # missing eval config inside it. The log directory is not checked:
    # these records go into the record repo, never the log directory.
    record_repo_problem = describe_folder_problem(settings_reading.record_repo.path, needs_writing=True)
    if record_repo_problem is not None:
        return [record_repo_unusable_failure(settings_reading, record_repo_problem)]
    return read_eval_for_this_account(settings_reading)


def record_repo_unusable_failure(settings: LocalSettings, problem: str) -> Failure:
    """The failure for a record repo folder that is missing or cannot be written in."""
    return Failure(
        kind="record_repo_missing",
        what_happened=(
            f"The record repo folder {settings.record_repo.as_written} {problem}."
            + where_goodall_looked(settings.record_repo)
            + " Nothing was recorded."
        ),
        why="Journal entries and interventions are kept as files in the record repo, so goodall needs it.",
        what_to_do_next=(
            f"If the record repo is somewhere else on this Mac, correct record_repo in {settings.settings_file}. "
            f"If it is not on this Mac yet, clone it to {settings.record_repo.path}. Then run the command again."
        ),
    )


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
    terminal.show_warnings(result.warnings)


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
