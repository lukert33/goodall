"""`goodall timeline`: read one work block from the timeline, in time order.

Called by `cli.main`. It shows what happened in one ended work block, the
last one unless `--work-block` names another: what the person did in each
application, what they asked the agent, which tools the agent used and how
they ended, and the journal entries and interventions written, each on one
line at its time of day.

What it shows is only what the timeline holds (goodall.timeline_entries):
times, durations and words from fixed lists. No prompt, reply, window
title, page address or file path is printed, because none is in the
timeline; `--pointers` adds where each entry is in a raw record, for a
person who wants to open it and read the rest.

In order:

1. Find this account's eval, as `goodall status` does.
2. Build the timeline if `--rebuild` was given, if there is none yet, or if
   the one there was built by other versions of goodall's rules
   (goodall.timeline.is_current). Otherwise it is read as it is: `goodall
   stop` builds it when a work block ends.
3. Pick the work block and read every entry that overlaps it.
4. Show them as text, or as one JSON document with `--json`. The document
   always has the keys `work_block`, `entries`, `entries_not_shown`,
   `rule_versions`, `warnings` and `failures`.

Bookkeeping lines of a transcript (kind `entry`) are left out unless
`--all` is given: there are many, and they say little about the work.

Exit codes: 0 when a work block was shown, 1 when there is nothing to show
or the timeline could not be read, 2 when `--work-block` names one that is
not in the timeline.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from goodall import exit_codes, timeline
from goodall.terminal import Failure, Terminal
from goodall.timeline_entries import KIND_ENTRY, KIND_UNFAMILIAR_ENTRY, TimelineEntry, TimelineWorkBlock
from goodall.work_block_commands import (
    Clock,
    build_timeline,
    describe_duration,
    describe_moment,
    find_eval_for_work_blocks,
)

# The kinds left out unless --all is given.
BOOKKEEPING_KINDS = (KIND_ENTRY, KIND_UNFAMILIAR_ENTRY)

# What is shown for an entry no rule could give an actor.
UNKNOWN_ACTOR = "unknown"

# How many work block ids a failure lists when the one asked for is not there.
LISTED_WORK_BLOCKS_LIMIT = 5


@dataclass(frozen=True)
class TimelineRequest:
    """What the person asked `goodall timeline` for."""

    work_block_id: str | None
    rebuild: bool
    show_all: bool
    show_pointers: bool
    as_json: bool


def run_timeline(terminal: Terminal, clock: Clock, request: TimelineRequest) -> int:
    """Show one work block's entries in time order, and return the exit code."""
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        return show_failures(terminal, request, eval_reading, exit_codes.ERROR)
    timeline_file = timeline.timeline_file_in(eval_reading.local_settings.log_directory.path)
    warnings = []
    if request.rebuild or not timeline.is_current(timeline_file):
        build = build_timeline(eval_reading, clock())
        if not build.built:
            return show_failures(terminal, request, [timeline.build_problems_failure(build)], exit_codes.ERROR)
        if build.problems:
            warnings.append(timeline.build_problems_failure(build))
    try:
        work_blocks = timeline.read_work_blocks(timeline_file)
        if not work_blocks:
            return show_failures(terminal, request, [no_work_block_failure()], exit_codes.ERROR)
        work_block = work_blocks[-1]
        if request.work_block_id is not None:
            chosen = find_work_block(work_blocks, request.work_block_id)
            if chosen is None:
                return show_failures(
                    terminal, request, [work_block_unknown_failure(work_blocks)], exit_codes.USAGE_ERROR
                )
            work_block = chosen
        entries = timeline.read_entries_overlapping(timeline_file, work_block.started_at, work_block.ended_at)
        information = timeline.read_information(timeline_file)
    except sqlite3.Error:
        return show_failures(terminal, request, [timeline_unreadable_failure(timeline_file)], exit_codes.ERROR)

    shown = []
    for entry in entries:
        if request.show_all or entry.kind not in BOOKKEEPING_KINDS:
            shown.append(entry)
    not_shown_count = len(entries) - len(shown)
    rule_versions = {}
    for name in timeline.VERSIONS:
        rule_versions[name] = information.get(name, "")
    if request.as_json:
        terminal.write_json(
            {
                "work_block": work_block_as_json(work_block),
                "entries": [entry_as_json(entry) for entry in shown],
                "entries_not_shown": not_shown_count,
                "rule_versions": rule_versions,
                "warnings": [warning.as_json() for warning in warnings],
                "failures": [],
            }
        )
        return exit_codes.SUCCESS
    show_work_block(terminal, request, work_block, shown, not_shown_count, rule_versions)
    terminal.show_warnings(warnings)
    return exit_codes.SUCCESS


def show_failures(terminal: Terminal, request: TimelineRequest, failures: list[Failure], exit_code: int) -> int:
    """Show why nothing can be shown, as text or as the JSON document, and return `exit_code`."""
    if request.as_json:
        terminal.write_json(
            {
                "work_block": None,
                "entries": [],
                "entries_not_shown": 0,
                "rule_versions": {},
                "warnings": [],
                "failures": [failure.as_json() for failure in failures],
            }
        )
    else:
        terminal.show_failures(failures)
    return exit_code


def work_block_id_of(work_block: TimelineWorkBlock) -> str:
    """A work block's id: the name of its record's file, which is all the timeline keeps of it."""
    return work_block.raw_record.path.stem


def find_work_block(work_blocks: list[TimelineWorkBlock], work_block_id: str) -> TimelineWorkBlock | None:
    """The work block with this id, or None when the timeline has none."""
    for work_block in work_blocks:
        if work_block_id_of(work_block) == work_block_id:
            return work_block
    return None


def show_work_block(
    terminal: Terminal,
    request: TimelineRequest,
    work_block: TimelineWorkBlock,
    shown: list[TimelineEntry],
    not_shown_count: int,
    rule_versions: dict[str, str],
) -> None:
    """Write the work block's facts, then one row for each entry, then what the rows leave out."""
    terminal.show_facts(
        [
            ("Work block", work_block_id_of(work_block)),
            (
                "Ran",
                f"{describe_moment(work_block.started_at)} to {describe_moment(work_block.ended_at)} "
                f"({describe_duration(work_block.ended_at - work_block.started_at)})",
            ),
            ("End state", work_block.end_state or "not recorded"),
            ("Entries", str(len(shown))),
            (
                "Rules",
                f"timeline {rule_versions['timeline_rules_version']}, actor labels "
                f"{rule_versions['actor_label_rules_version']}, human actions "
                f"{rule_versions['human_action_rules_version']}, ActivityWatch "
                f"{rule_versions['activitywatch_rules_version']}",
            ),
        ]
    )
    column_names = ["Time", "Who", "What", "From"]
    if request.show_pointers:
        column_names.append("Raw record")
    rows = []
    for entry in shown:
        row = [
            entry.started_at.astimezone().strftime("%H:%M:%S"),
            entry.actor or UNKNOWN_ACTOR,
            describe_what(entry),
            entry.source_application or entry.source,
        ]
        if request.show_pointers:
            row.append(describe_pointer(entry))
        rows.append(row)
    if rows:
        terminal.show_rows(column_names, rows)
    closing = []
    if not_shown_count == 1:
        closing.append("1 bookkeeping entry is not shown; add --all to see it.")
    elif not_shown_count > 1:
        closing.append(f"{not_shown_count} bookkeeping entries are not shown; add --all to see them.")
    closing.append(
        "Times are this Mac's local time. The timeline holds no prompts, replies, window titles, page addresses or "
        "file paths: those stay in the raw records."
    )
    if not request.show_pointers:
        closing.append("Add --pointers to see where each entry is in a raw record.")
    terminal.show_paragraph(" ".join(closing))


def describe_what(entry: TimelineEntry) -> str:
    """What an entry was, in the timeline's own words: its kind, then whatever the kind adds, then how long."""
    words = [entry.kind]
    if entry.kind in BOOKKEEPING_KINDS and entry.entry_type is not None:
        words.append(entry.entry_type)
    for value in [entry.tool, entry.application, entry.intervention_category, entry.outcome]:
        if value is not None:
            words.append(value)
    if entry.in_subagent:
        words.append("in_subagent")
    if entry.duration_seconds is not None:
        words.append(f"({describe_seconds(entry.duration_seconds)})")
    return " ".join(words)


def describe_seconds(seconds: float) -> str:
    """A length of time to the second, for a person: `2 min 5 s`, `1 h 0 min 30 s`, `30 s`."""
    whole_seconds = round(seconds)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds_left = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours} h")
    if minutes > 0 or (hours > 0 and seconds_left > 0):
        parts.append(f"{minutes} min")
    if seconds_left > 0 or not parts:
        parts.append(f"{seconds_left} s")
    return " ".join(parts)


def describe_pointer(entry: TimelineEntry) -> str:
    """Where an entry is in a raw record: which folder, the path inside it, and the line or the event's place."""
    pointer = f"{entry.raw_record.kept_in}: {entry.raw_record.path}"
    if entry.position is None:
        return pointer
    if entry.raw_record.path.suffix == ".jsonl":
        return f"{pointer}, line {entry.position}"
    return f"{pointer}, event {entry.position}"


def work_block_as_json(work_block: TimelineWorkBlock) -> dict[str, object]:
    """One work block for the JSON document."""
    return {
        "id": work_block_id_of(work_block),
        "started_at": timeline.format_time(work_block.started_at),
        "ended_at": timeline.format_time(work_block.ended_at),
        "end_state": work_block.end_state,
        "raw_record": {"kept_in": work_block.raw_record.kept_in, "path": str(work_block.raw_record.path)},
    }


def entry_as_json(entry: TimelineEntry) -> dict[str, object]:
    """One entry for the JSON document, with every column the timeline holds for it."""
    return {
        "started_at": timeline.format_time(entry.started_at),
        "duration_seconds": entry.duration_seconds,
        "source": entry.source,
        "source_application": entry.source_application,
        "actor": entry.actor,
        "kind": entry.kind,
        "entry_type": entry.entry_type,
        "label_rule": entry.label_rule,
        "tool": entry.tool,
        "application": entry.application,
        "category": entry.category,
        "intervention_category": entry.intervention_category,
        "outcome": entry.outcome,
        "in_subagent": entry.in_subagent,
        "raw_record": {
            "kept_in": entry.raw_record.kept_in,
            "path": str(entry.raw_record.path),
            "position": entry.position,
        },
    }


def no_work_block_failure() -> Failure:
    """The failure for a timeline with no ended work block in it."""
    return Failure(
        kind="timeline_has_no_work_block",
        what_happened="No work block has ended yet, so there is nothing to show.",
        why="goodall timeline shows one ended work block at a time, and the timeline is built when one ends.",
        what_to_do_next=(
            "If a work block is running, run goodall stop to end it, then goodall timeline. To begin one, run "
            "goodall start."
        ),
    )


def work_block_unknown_failure(work_blocks: list[TimelineWorkBlock]) -> Failure:
    """The failure for a --work-block that is not in the timeline. The id given is not repeated back."""
    latest_ids = []
    for work_block in work_blocks[-LISTED_WORK_BLOCKS_LIMIT:]:
        latest_ids.append(work_block_id_of(work_block))
    return Failure(
        kind="work_block_not_in_timeline",
        what_happened="The --work-block given is not the id of an ended work block in the timeline.",
        why=(
            "A work block's id is shown by goodall stop, and is the name of its file in the record repo's "
            "work-blocks folder."
        ),
        what_to_do_next=(
            f"Use one of the latest: {', '.join(latest_ids)}; or leave --work-block out to see the last one. If the "
            "work block ended a moment ago, run goodall timeline --rebuild first."
        ),
        commands_to_try=(f"goodall timeline --work-block {latest_ids[-1]}",),
    )


def timeline_unreadable_failure(timeline_file: Path) -> Failure:
    """The failure for a timeline file SQLite cannot read."""
    return Failure(
        kind="timeline_unreadable",
        what_happened=f"The timeline, {timeline_file}, could not be read.",
        why="It is worked out from the raw records, which are untouched, so it can be built again at any time.",
        what_to_do_next="Run goodall timeline --rebuild.",
        commands_to_try=("goodall timeline --rebuild",),
    )
