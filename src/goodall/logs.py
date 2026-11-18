"""`goodall logs`: browse an eval's work blocks and the raw records behind them, in the terminal.

Called by `cli.main` for `goodall logs`, `goodall logs <work block id>` and
`goodall logs --raw <path>`; each form also takes `--json`, except `--raw`.

What each form shows:

- `goodall logs` lists the eval's work blocks, newest first: id, contributor,
  effective workstream (work block fixes applied, ADR 0014), start, end (and
  whether it was inferred or abnormal), and counts of what exists for it:
  Claude sessions copied, telemetry lines and whether the receipts agree
  with them, journal entries and interventions.
- `goodall logs <work block id>` shows one: its record, its fixes, its
  journal entries and interventions, and the path of each raw record, flagged
  `readable` or `unparseable`. Transcript and telemetry content is not
  printed; the paths and counts say where it is.
- `goodall logs --raw <path>` prints one raw file byte for byte, so the
  "raw content verbatim" of the viewer is one command away without goodall
  ever transforming it. The path must be a file inside the log directory.

Read-only, strictly (ADR 0003, AGENTS.md invariants 2 and 9): no file is
created or changed, not even goodall's own bookkeeping, nothing is committed
or pushed, no program is run (this command takes no runner) and no network
call is made. Unlike `goodall status` it never settles a forgotten work
block or writes a waiting record; those are shown as they are, with the
command that would settle them. The log directory does not even need to be
writable, so a drive mounted read-only can be browsed.

Anything goodall cannot read is flagged and the rest is still shown, and the
exit code stays 0, because what was asked (show the records) is done and
nothing is lost. A work block id that is not there exits 1, and one that is
not shaped like an id exits 2, with whole commands to try.

The JSON documents have the same keys whatever happened, as `goodall status
--json` does: `eval`, `log_directory`, `record_repo`, `warnings` and
`failures` always, plus `work_blocks` and `activitywatch` for the list, or
`work_block`, `fixes`, `journal_entries`, `interventions` and `raw_records`
for one work block. Times are UTC, as in records.

PROVISIONAL decisions flagged for the project owner: the shape of both JSON
documents; ActivityWatch copies are counted for the whole eval, not per work
block (ADR 0016 does not tie a copy to a work block); `--raw` refuses paths
outside the log directory so goodall is never a general file printer.
"""

from dataclasses import dataclass
from pathlib import Path

from goodall import exit_codes, log_reading, records, work_blocks
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.folders import describe_folder_problem, record_repo_missing_failure, where_goodall_looked
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import Clock, describe_moment

# How many receipt problems are written out in the text of one work block's
# detail; the JSON document lists them all.
PROBLEMS_SHOWN = 10

# How long the first line of a journal entry or intervention may be before it is cut.
SUMMARY_LENGTH = 90


def run_logs(terminal: Terminal, clock: Clock, work_block_id: str | None, raw_path: str | None, as_json: bool) -> int:
    """Show the list, one work block, or one raw file, and return the exit code."""
    if raw_path is not None and (work_block_id is not None or as_json):
        terminal.show_failure(raw_combined_failure())
        return exit_codes.USAGE_ERROR
    if work_block_id is not None and not records.RECORD_ID_PATTERN.fullmatch(work_block_id):
        # The id is not repeated back: it failed its check, in case something private was pasted.
        terminal.show_failure(work_block_id_invalid_failure())
        return exit_codes.USAGE_ERROR

    eval_reading = find_eval_for_reading()
    if isinstance(eval_reading, list):
        return report_failures(terminal, eval_reading, as_json)

    if raw_path is not None:
        return show_raw_file(terminal, eval_reading, raw_path)
    contents = log_reading.read_record_repo(eval_reading.local_settings.record_repo.path)
    log_directory = eval_reading.local_settings.log_directory.path
    views, bookkeeping_problems = log_reading.find_work_blocks(log_directory, contents, clock())
    if work_block_id is None:
        return show_list(terminal, eval_reading, contents, views, bookkeeping_problems, as_json)
    for view in views:
        if view.record.id == work_block_id:
            return show_one(terminal, eval_reading, contents, view, bookkeeping_problems, as_json)
    return report_failures(terminal, [work_block_not_found_failure(work_block_id, eval_reading)], as_json)


# ---------------------------------------------------------------------------
# Finding the eval without writing anything
# ---------------------------------------------------------------------------


def find_eval_for_reading() -> EvalForThisAccount | list[Failure]:
    """Read local settings, check both folders exist, and read the eval config, writing nothing.

    Unlike goodall.work_block_commands.find_eval_for_work_blocks, the log
    directory need not be writable: browsing only reads.
    """
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        return settings_reading
    settings = settings_reading
    failures: list[Failure] = []
    record_repo_problem = describe_folder_problem(settings.record_repo.path, needs_writing=False)
    if record_repo_problem is not None:
        failures.append(record_repo_missing_failure(settings, record_repo_problem))
    log_directory_problem = describe_folder_problem(settings.log_directory.path, needs_writing=False)
    if log_directory_problem is not None:
        failures.append(log_directory_missing_failure(settings, log_directory_problem))
    if failures:
        return failures
    return read_eval_for_this_account(settings)


def log_directory_missing_failure(settings: LocalSettings, problem: str) -> Failure:
    """The failure for a log directory that is not there to be read."""
    return Failure(
        kind="log_directory_unreachable",
        what_happened=(
            f"The log directory {settings.log_directory.as_written} {problem}, so goodall cannot show the raw "
            "records in it." + where_goodall_looked(settings.log_directory)
        ),
        why="The log directory holds the transcripts, telemetry and work block bookkeeping that goodall logs shows.",
        what_to_do_next=(
            "If the log directory is on an external drive, connect it. Otherwise correct log_directory in "
            f"{settings.settings_file}. Then run goodall logs again."
        ),
    )


def raw_combined_failure() -> Failure:
    return Failure(
        kind="logs_raw_combined",
        what_happened="--raw cannot be combined with a work block id or --json. Nothing was shown.",
        why="--raw prints one raw file exactly as it is stored, so there is nothing to select or to wrap in JSON.",
        what_to_do_next="Run goodall logs --raw <path> on its own. goodall logs <work block id> shows the paths.",
        commands_to_try=("goodall logs", f"goodall logs {records.EXAMPLE_RECORD_ID}"),
    )


def work_block_id_invalid_failure() -> Failure:
    return Failure(
        kind="logs_work_block_invalid",
        what_happened="The id given is not a work block id. Nothing was shown.",
        why=(
            f"A work block id looks like {records.EXAMPLE_RECORD_ID}: the UTC time it started, the contributor id "
            "and eight random characters."
        ),
        what_to_do_next="Copy an id from the list that goodall logs shows.",
        commands_to_try=("goodall logs",),
    )


def work_block_not_found_failure(work_block_id: str, eval_for_this_account: EvalForThisAccount) -> Failure:
    """The failure for a well-formed id that matches no work block. The id has passed its check, so it is repeated."""
    return Failure(
        kind="logs_work_block_not_found",
        what_happened=f"No work block with the id {work_block_id} was found. Nothing was changed.",
        why=(
            "Work blocks are read from the record repo at "
            f"{eval_for_this_account.local_settings.record_repo.path} and from the log directory."
        ),
        what_to_do_next=(
            "Run goodall logs to list the work blocks goodall can see, and copy the id from there. If it was "
            "recorded on another Mac, bring this Mac's copy of the record repo up to date first."
        ),
        commands_to_try=("goodall logs",),
    )


def report_failures(terminal: Terminal, failures: list[Failure], as_json: bool) -> int:
    """Show `failures` as text or as JSON with the same keys as any other result, and return the error exit code."""
    if as_json:
        failure_documents = []
        for failure in failures:
            failure_documents.append(failure.as_json())
        terminal.write_json(
            {
                "eval": None,
                "log_directory": None,
                "record_repo": None,
                "work_blocks": [],
                "activitywatch": None,
                "work_block": None,
                "fixes": [],
                "journal_entries": [],
                "interventions": [],
                "raw_records": None,
                "warnings": [],
                "failures": failure_documents,
            }
        )
    else:
        terminal.show_failures(failures)
    return exit_codes.ERROR


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkBlockCounts:
    """What exists for one work block, counted for the list. Nothing here holds any raw content."""

    claude_sessions_listed: int
    claude_sessions_copied: int
    telemetry: log_reading.TelemetrySummary | None
    journal_entries: int
    interventions: int

    def as_json(self) -> dict[str, object]:
        telemetry = None
        if self.telemetry is not None:
            telemetry = self.telemetry.as_json()
        return {
            "claude_sessions": {"listed": self.claude_sessions_listed, "copied": self.claude_sessions_copied},
            "telemetry": telemetry,
            "journal_entries": self.journal_entries,
            "interventions": self.interventions,
        }


def count_for(
    log_directory: Path, contents: log_reading.RecordRepoContents, view: log_reading.WorkBlockView
) -> WorkBlockCounts:
    """Count what exists for one work block: copied transcripts, telemetry, journal entries, interventions."""
    copied = 0
    for claude_session in view.claude_sessions:
        if log_reading.find_transcripts(log_directory, claude_session):
            copied += 1
    journal_entries = 0
    for entry in contents.journal_entries:
        if entry.work_block == view.record.id:
            journal_entries += 1
    interventions = 0
    for intervention in contents.interventions:
        if intervention.work_block == view.record.id:
            interventions += 1
    return WorkBlockCounts(
        claude_sessions_listed=len(view.claude_sessions),
        claude_sessions_copied=copied,
        telemetry=log_reading.summarize_telemetry(log_directory, view.record.id),
        journal_entries=journal_entries,
        interventions=interventions,
    )


def show_list(
    terminal: Terminal,
    eval_for_this_account: EvalForThisAccount,
    contents: log_reading.RecordRepoContents,
    views: list[log_reading.WorkBlockView],
    bookkeeping_problems: list[log_reading.FileProblem],
    as_json: bool,
) -> int:
    """Show every work block, newest first, as text or JSON, and return 0."""
    log_directory = eval_for_this_account.local_settings.log_directory.path
    problems = contents.problems + bookkeeping_problems
    activitywatch_copies = log_reading.count_activitywatch_copies(log_directory)
    counts_by_id = {}
    for view in views:
        counts_by_id[view.record.id] = count_for(log_directory, contents, view)

    if as_json:
        work_block_documents = []
        for view in views:
            document = work_block_document(view, contents.fixes)
            document["counts"] = counts_by_id[view.record.id].as_json()
            work_block_documents.append(document)
        terminal.write_json(
            {
                "eval": {"name": eval_for_this_account.eval_config.name},
                "log_directory": str(log_directory),
                "record_repo": str(eval_for_this_account.local_settings.record_repo.path),
                "work_blocks": work_block_documents,
                "activitywatch": {
                    "included": eval_for_this_account.eval_config.activitywatch,
                    "copies": activitywatch_copies,
                },
                "work_block": None,
                "fixes": [],
                "journal_entries": [],
                "interventions": [],
                "raw_records": None,
                "warnings": problem_documents(problems),
                "failures": [],
            }
        )
        return exit_codes.SUCCESS

    eval_name = eval_for_this_account.eval_config.name
    if not views:
        terminal.show_paragraph(
            f"Eval {eval_name}: no work blocks yet. Run goodall start to begin one; goodall logs lists them here."
        )
    else:
        terminal.show_paragraph(
            f"Eval {eval_name}: {plural(len(views), 'work block')}, newest first. "
            "Run goodall logs <work block id> to see one in detail."
        )
    for view in views:
        terminal.show_facts(list_facts(view, counts_by_id[view.record.id], contents.fixes))
    if eval_for_this_account.eval_config.activitywatch or activitywatch_copies > 0:
        terminal.show_paragraph(
            f"ActivityWatch: {plural(activitywatch_copies, 'copy', 'copies')} in the log directory. "
            "Copies belong to the eval, not to one work block."
        )
    terminal.show_warnings(problem_warnings(problems))
    return exit_codes.SUCCESS


def list_facts(
    view: log_reading.WorkBlockView, counts: WorkBlockCounts, fixes: list[records.WorkBlockFix]
) -> list[tuple[str, str]]:
    """The labelled lines that describe one work block in the list."""
    facts = [
        ("Work block", view.record.id),
        ("Contributor", view.record.contributor),
        ("Workstream", describe_workstream(view, fixes)),
        ("Started", describe_moment(view.record.started_at)),
        ("Ended", describe_end(view)),
    ]
    facts.append(("Claude sessions", describe_claude_sessions(counts)))
    facts.append(("Telemetry", describe_telemetry(counts.telemetry, view.record.telemetry)))
    facts.append(
        (
            "Notes",
            f"{plural(counts.journal_entries, 'journal entry', 'journal entries')}, "
            f"{plural(counts.interventions, 'intervention')}",
        )
    )
    return facts


def describe_workstream(view: log_reading.WorkBlockView, fixes: list[records.WorkBlockFix]) -> str:
    """The effective workstream, with what its record said and why it differs, or that it is still unknown."""
    if view.workstream == records.UNKNOWN_WORKSTREAM:
        return "unknown, so far: run goodall status for the goodall fixblock commands that name it"
    if view.workstream != view.record.workstream:
        return f"{view.workstream}, by a fix; its record says {view.record.workstream}"
    return view.workstream


def describe_end(view: log_reading.WorkBlockView) -> str:
    """When a work block ended and how, or that it is still running or waiting."""
    record = view.record
    if view.source == log_reading.RUNNING:
        if view.overdue:
            return "still marked running, but no activity for over three hours; goodall status will close it"
        return "still running"
    assert record.ended_at is not None
    ended = describe_moment(record.ended_at)
    if record.end_state == "inferred":
        ended += " (inferred from its last heartbeat; nobody closed it)"
    elif record.end_state == "abnormal":
        ended += " (abnormal: something went wrong)"
    if view.source == log_reading.WAITING_IN_LOG_DIRECTORY:
        ended += "; waiting in the log directory to be written to the record repo, which goodall status does"
    return ended


def describe_claude_sessions(counts: WorkBlockCounts) -> str:
    if counts.claude_sessions_listed == 0:
        return "none in this work block"
    return (
        f"{counts.claude_sessions_copied} of {plural(counts.claude_sessions_listed, 'Claude session')} copied "
        "into the log directory"
    )


def describe_telemetry(summary: log_reading.TelemetrySummary | None, state_in_record: str | None) -> str:
    """One line on telemetry: lines stored and whether the receipts agree, or that none were stored."""
    if summary is None:
        text = "none stored in the log directory"
    elif summary.unreadable is not None:
        text = f"a folder exists but cannot be read: {summary.unreadable}"
    elif not summary.has_receipt_log:
        text = f"{plural(summary.line_count, 'telemetry line')}, no receipt log to check them against"
    else:
        text = f"{plural(summary.line_count, 'telemetry line')}, "
        if summary.receipt_problems:
            text += f"{plural(len(summary.receipt_problems), 'fault')} against the receipts"
        else:
            text += "receipts agree"
        if summary.repeated_requests:
            text += f", {plural(summary.repeated_requests, 'repeated request')}"
    if state_in_record in ("incomplete", "missing"):
        text += f"; its record says {state_in_record}"
    return text


# ---------------------------------------------------------------------------
# One work block
# ---------------------------------------------------------------------------


def show_one(
    terminal: Terminal,
    eval_for_this_account: EvalForThisAccount,
    contents: log_reading.RecordRepoContents,
    view: log_reading.WorkBlockView,
    bookkeeping_problems: list[log_reading.FileProblem],
    as_json: bool,
) -> int:
    """Show one work block in detail, as text or JSON, and return 0."""
    log_directory = eval_for_this_account.local_settings.log_directory.path
    problems = contents.problems + bookkeeping_problems
    work_block_id = view.record.id
    fixes = fixes_for(contents, work_block_id)
    journal_entries = []
    for entry in contents.journal_entries:
        if entry.work_block == work_block_id:
            journal_entries.append(entry)
    interventions = []
    for intervention in contents.interventions:
        if intervention.work_block == work_block_id:
            interventions.append(intervention)
    transcripts = []
    for claude_session in view.claude_sessions:
        copies = []
        for transcript_path in log_reading.find_transcripts(log_directory, claude_session):
            copies.append(log_reading.check_jsonl_file(transcript_path))
        transcripts.append((claude_session, copies))
    telemetry = log_reading.summarize_telemetry(log_directory, work_block_id)
    telemetry_files = []
    for telemetry_path in log_reading.telemetry_files(log_directory, work_block_id):
        telemetry_files.append(log_reading.check_jsonl_file(telemetry_path))

    if as_json:
        transcript_documents = []
        for claude_session, copies in transcripts:
            copy_documents = []
            for copy in copies:
                copy_documents.append(copy.as_json())
            transcript_documents.append({"claude_session": claude_session, "copies": copy_documents})
        telemetry_file_documents = []
        for telemetry_file in telemetry_files:
            telemetry_file_documents.append(telemetry_file.as_json())
        telemetry_document = None
        if telemetry is not None:
            telemetry_document = telemetry.as_json()
            telemetry_document["files"] = telemetry_file_documents
        document = work_block_document(view, contents.fixes)
        document["machine"] = {
            "hostname": view.record.machine.hostname,
            "macos_version": view.record.machine.macos_version,
            "goodall_version": view.record.machine.goodall_version,
            "claude_code_version": view.record.machine.claude_code_version,
        }
        document["telemetry_state_in_record"] = view.record.telemetry
        terminal.write_json(
            {
                "eval": {"name": eval_for_this_account.eval_config.name},
                "log_directory": str(log_directory),
                "record_repo": str(eval_for_this_account.local_settings.record_repo.path),
                "work_blocks": [],
                "activitywatch": None,
                "work_block": document,
                "fixes": [fix_document(fix) for fix in fixes],
                "journal_entries": [journal_document(entry) for entry in journal_entries],
                "interventions": [intervention_document(intervention) for intervention in interventions],
                "raw_records": {"transcripts": transcript_documents, "telemetry": telemetry_document},
                "warnings": problem_documents(problems),
                "failures": [],
            }
        )
        return exit_codes.SUCCESS

    record = view.record
    contributor_facts = [
        ("Work block", record.id),
        ("Contributor", record.contributor),
        ("Workstream", describe_workstream(view, contents.fixes)),
        ("Started", describe_moment(record.started_at)),
        ("Ended", describe_end(view)),
        (
            "Machine",
            f"goodall {record.machine.goodall_version} on {record.machine.hostname}"
            + (f", Claude Code {record.machine.claude_code_version}" if record.machine.claude_code_version else ""),
        ),
    ]
    terminal.show_facts(contributor_facts)

    show_fixes(terminal, fixes)
    show_notes(terminal, journal_entries, interventions)
    show_raw_records(terminal, view, transcripts, telemetry, telemetry_files)
    terminal.show_warnings(problem_warnings(problems))
    return exit_codes.SUCCESS


def fixes_for(contents: log_reading.RecordRepoContents, work_block_id: str) -> list[records.WorkBlockFix]:
    """The fixes for one work block, oldest first, so the last one listed is the one that counts."""
    fixes = []
    for fix in contents.fixes:
        if fix.work_block == work_block_id:
            fixes.append(fix)
    fixes.sort(key=lambda fix: (fix.written_at, fix.id))
    return fixes


def show_fixes(terminal: Terminal, fixes: list[records.WorkBlockFix]) -> None:
    if not fixes:
        return
    terminal.show_paragraph("Work block fixes, oldest first. The last one that names a workstream counts.")
    facts = []
    for fix in fixes:
        facts.append(
            (
                "Fix",
                f"{fix.id}: workstream {fix.workstream or 'left alone'}, written {describe_moment(fix.written_at)} "
                f"by {fix.contributor}",
            )
        )
    terminal.show_facts(facts)


def show_notes(
    terminal: Terminal, journal_entries: list[records.JournalEntry], interventions: list[records.Intervention]
) -> None:
    """Journal entries and interventions of one work block: id, when, and the first line of the text."""
    if not journal_entries and not interventions:
        terminal.show_paragraph("No journal entries or interventions name this work block.")
        return
    facts = []
    for entry in journal_entries:
        facts.append(
            (
                "Journal entry",
                f"{entry.id}, written {describe_moment(entry.written_at)}: {first_line(entry.body)}",
            )
        )
    for intervention in interventions:
        facts.append(
            (
                "Intervention",
                f"{intervention.id}, {intervention.category}, {intervention.decision}, "
                f"occurred {describe_moment(intervention.occurred_at)}: {first_line(intervention.body)}",
            )
        )
    terminal.show_facts(facts)


def first_line(body: str) -> str:
    """The first line of a record's text with Markdown heading marks removed, cut to a readable length."""
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            if len(text) > SUMMARY_LENGTH:
                return text[: SUMMARY_LENGTH - 3].rstrip() + "..."
            return text
    return ""


def show_raw_records(
    terminal: Terminal,
    view: log_reading.WorkBlockView,
    transcripts: list[tuple[str, list[log_reading.RawFile]]],
    telemetry: log_reading.TelemetrySummary | None,
    telemetry_files: list[log_reading.RawFile],
) -> None:
    """The paths of this work block's raw records, each flagged readable or unparseable, and how to open them."""
    facts = []
    for claude_session, copies in transcripts:
        if not copies:
            facts.append(("Transcript", f"Claude session {claude_session}: no copy in the log directory"))
        for copy in copies:
            facts.append(("Transcript", describe_raw_file(copy)))
    for telemetry_file in telemetry_files:
        facts.append(("Telemetry file", describe_raw_file(telemetry_file)))
    if facts:
        terminal.show_facts(facts)
    if view.record.claude_sessions == () and not view.claude_sessions:
        terminal.show_paragraph("No Claude session ran in this work block.")
    if telemetry is None:
        terminal.show_paragraph("No telemetry was stored for this work block.")
    else:
        terminal.show_paragraph(describe_telemetry(telemetry, view.record.telemetry).capitalize() + ".")
        shown = telemetry.receipt_problems[:PROBLEMS_SHOWN]
        for problem in shown:
            terminal.show_paragraph(problem)
        if len(telemetry.receipt_problems) > PROBLEMS_SHOWN:
            hidden = len(telemetry.receipt_problems) - PROBLEMS_SHOWN
            terminal.show_paragraph(f"And {hidden} more; goodall logs --json lists every one.")
    terminal.show_paragraph(
        "Raw records are shown as paths and counts only. To read one exactly as stored, run "
        "goodall logs --raw <path>, or open it in any text editor: transcripts and telemetry are JSON Lines files."
    )


def describe_raw_file(raw_file: log_reading.RawFile) -> str:
    """`<path> [readable, 120 lines]`, or `<path> [unparseable: 2 lines are not valid JSON, the first is line 7.]`."""
    if raw_file.status == log_reading.READABLE:
        return f"{raw_file.path} [readable, {plural(raw_file.line_count or 0, 'line')}]"
    return f"{raw_file.path} [unparseable: {raw_file.problem}]"


# ---------------------------------------------------------------------------
# One raw file, byte for byte
# ---------------------------------------------------------------------------


def show_raw_file(terminal: Terminal, eval_for_this_account: EvalForThisAccount, typed_path: str) -> int:
    """Print one file of the log directory exactly as stored, and return the exit code.

    A relative path counts from the log directory, so a path shown by
    goodall logs can be pasted whole. The path is resolved first (every
    symbolic link and `..`), and must land on a regular file inside the log
    directory. The typed path is never repeated in a failure: it has not
    passed its check, in case something private was pasted.
    """
    log_directory = eval_for_this_account.local_settings.log_directory.path
    try:
        resolved_log_directory = log_directory.resolve()
        resolved_path = (log_directory / Path(typed_path).expanduser()).resolve()
    except (OSError, ValueError, RuntimeError):
        terminal.show_failure(raw_path_outside_failure(log_directory))
        return exit_codes.USAGE_ERROR
    if not resolved_path.is_relative_to(resolved_log_directory):
        terminal.show_failure(raw_path_outside_failure(log_directory))
        return exit_codes.USAGE_ERROR
    try:
        with resolved_path.open("rb") as raw_file:
            terminal.copy_bytes_to_output(raw_file)
    except OSError as read_error:
        terminal.show_failure(raw_file_unreadable_failure(read_error))
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def raw_path_outside_failure(log_directory: Path) -> Failure:
    return Failure(
        kind="logs_raw_path_outside_log_directory",
        what_happened="The path given is not a file inside the log directory. Nothing was shown.",
        why="goodall logs --raw prints raw records, which all live in the log directory, and nothing else.",
        what_to_do_next=(
            f"Copy a path that goodall logs <work block id> shows, or give one relative to {log_directory}."
        ),
        commands_to_try=("goodall logs",),
    )


def raw_file_unreadable_failure(read_error: OSError) -> Failure:
    """The failure for a path inside the log directory that is not a file goodall can open."""
    reason = read_error.strerror or "it could not be opened"
    return Failure(
        kind="logs_raw_file_unreadable",
        what_happened=f"The file given cannot be read: {reason}. Nothing was shown.",
        why="A path that is a folder, is missing or cannot be opened by this macOS user account has nothing to print.",
        what_to_do_next=(
            "Check the path against goodall logs <work block id>, which lists the raw records it found, and make "
            "sure the log directory is connected."
        ),
        commands_to_try=("goodall logs",),
    )


# ---------------------------------------------------------------------------
# JSON documents and warnings
# ---------------------------------------------------------------------------


def work_block_document(view: log_reading.WorkBlockView, fixes: list[records.WorkBlockFix]) -> dict[str, object]:
    """One work block in a JSON document. Times are UTC, as in records."""
    record = view.record
    ended_at = None
    if record.ended_at is not None:
        ended_at = work_blocks.format_timestamp(record.ended_at)
    fix_ids = []
    for fix in fixes_for_ids(fixes, record.id):
        fix_ids.append(fix.id)
    return {
        "id": record.id,
        "contributor": record.contributor,
        "workstream": view.workstream,
        "recorded_workstream": record.workstream,
        "fixes": fix_ids,
        "started_at": work_blocks.format_timestamp(record.started_at),
        "ended_at": ended_at,
        "end_state": record.end_state,
        "state": state_word(view),
        "claude_sessions": list(view.claude_sessions),
    }


def fixes_for_ids(fixes: list[records.WorkBlockFix], work_block_id: str) -> list[records.WorkBlockFix]:
    selected = []
    for fix in fixes:
        if fix.work_block == work_block_id:
            selected.append(fix)
    selected.sort(key=lambda fix: (fix.written_at, fix.id))
    return selected


def state_word(view: log_reading.WorkBlockView) -> str:
    """One word for an agent to branch on: `recorded`, `waiting`, `running` or `overdue`."""
    if view.source == log_reading.RUNNING:
        if view.overdue:
            return "overdue"
        return "running"
    if view.source == log_reading.WAITING_IN_LOG_DIRECTORY:
        return "waiting"
    return "recorded"


def fix_document(fix: records.WorkBlockFix) -> dict[str, object]:
    return {
        "id": fix.id,
        "contributor": fix.contributor,
        "written_at": work_blocks.format_timestamp(fix.written_at),
        "workstream": fix.workstream,
    }


def journal_document(entry: records.JournalEntry) -> dict[str, object]:
    return {
        "id": entry.id,
        "contributor": entry.contributor,
        "written_at": work_blocks.format_timestamp(entry.written_at),
        "body": entry.body,
    }


def intervention_document(intervention: records.Intervention) -> dict[str, object]:
    return {
        "id": intervention.id,
        "contributor": intervention.contributor,
        "occurred_at": work_blocks.format_timestamp(intervention.occurred_at),
        "written_at": work_blocks.format_timestamp(intervention.written_at),
        "category": intervention.category,
        "decision": intervention.decision,
        "interrupted": intervention.interrupted,
        "question_outcome": intervention.question_outcome,
        "claude_session": intervention.claude_session,
        "body": intervention.body,
    }


def problem_documents(problems: list[log_reading.FileProblem]) -> list[dict[str, object]]:
    """The files goodall could not read cleanly, as JSON: the list's `warnings`."""
    documents: list[dict[str, object]] = []
    for problem in problems:
        documents.append(problem.as_json())
    return documents


def problem_warnings(problems: list[log_reading.FileProblem]) -> list[Failure]:
    """One warning per file goodall could not read cleanly, each saying what to do next."""
    warnings = []
    for problem in problems:
        if problem.usable:
            what_happened = f"The {problem.record_type} file {problem.path} has faults. " + " ".join(problem.messages)
            why = "Goodall shows what it could read, and leaves the file exactly as it is."
        else:
            what_happened = (
                f"The {problem.record_type} file {problem.path} cannot be read, so it is not shown. "
                + " ".join(problem.messages)
            )
            why = "Goodall shows only records it can read, and leaves the file exactly as it is."
        warnings.append(
            Failure(
                kind="record_file_unreadable",
                what_happened=what_happened,
                why=why,
                what_to_do_next=(
                    "Open the file and correct what is named above, or ask whoever wrote it. Raw records are never "
                    "edited; a record repo file can be corrected and committed like any file."
                ),
            )
        )
    return warnings


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """`1 telemetry line`, `3 telemetry lines`; `plural_form` for words that do not just add an s."""
    if count == 1:
        return f"{count} {singular}"
    if plural_form is None:
        plural_form = singular + "s"
    return f"{count} {plural_form}"
