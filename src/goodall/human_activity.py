"""Every human action in a time range, from every source, in one list.

Human actions come from more than one kind of raw record: Claude Code
transcripts (`goodall.human_actions`, what the person did with the agent) and
ActivityWatch exports (`goodall.activitywatch`, what they did outside it).
This module is the one place to ask "what did the person do between these two
times?", and to count the answer by kind and by a provisional category.

Who calls it: analysis code and a researcher looking at real results. Like
the parsers, it reads only the files it is given and imports only the
standard library.

Design decisions a reader would otherwise reverse-engineer:

- Every action, whatever its source, has the fields in `HumanAction`:
  `source`, `kind`, `rule`, `timestamp` and `duration_seconds`. Where in the
  raw record it came from differs by source (a transcript line, an
  ActivityWatch event) and stays on the source's own record.
- To add a source: write a parser module returning records with those five
  fields, add a parameter for its files to `human_actions_in_time_range`, and
  give each new kind a row in `PROVISIONAL_CATEGORIES`.
- The kinds are deliberately finely split; the owner will merge and rename
  them after reviewing real results. `PROVISIONAL_CATEGORIES` is a first
  guess at grouping them, used by nothing except `category_of` and the
  summary. Change it freely.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from goodall.activitywatch import parse_activitywatch_export
from goodall.human_actions import parse_human_actions


class HumanAction(Protocol):
    """What every human action has, whichever raw record it came from.

    `timestamp` is as the source wrote it; `duration_seconds` is None for an
    action that is a moment rather than a stretch of time.
    """

    @property
    def source(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def rule(self) -> str: ...

    @property
    def timestamp(self) -> str | None: ...

    @property
    def duration_seconds(self) -> float | None: ...


# PROVISIONAL. A first grouping of every kind into a higher-level category,
# for looking at real results quickly. Nothing depends on it but
# `category_of` and `summarise_human_actions`; expect it to change once real
# results have been reviewed. Docs: docs/human-action-categories.md.
PROVISIONAL_CATEGORIES: dict[str, str] = {
    # Claude Code transcripts.
    "typed_prompt": "directed_the_agent",
    "queued_prompt": "directed_the_agent",
    "blocked_prompt": "directed_the_agent",
    "question_answer": "directed_the_agent",
    "interruption": "stopped_the_agent",
    "tool_call_rejection": "approved_or_refused",
    "tool_call_approval_once": "approved_or_refused",
    "tool_call_approval_always": "approved_or_refused",
    "permission_prompt_dismissal": "approved_or_refused",
    "plan_approval": "approved_or_refused",
    "slash_command": "steered_the_tool",
    "permission_mode_change": "steered_the_tool",
    "shell_command": "did_the_work_directly",
    "sdk_prompt": "undecided",
    # ActivityWatch exports.
    "application_focus": "worked_outside_the_agent",
    "browser_page_focus": "worked_outside_the_agent",
    "editor_file_focus": "worked_outside_the_agent",
    "active_at_keyboard": "was_at_the_keyboard",
    "away_from_keyboard": "was_elsewhere",
}

# The category of a kind not in the table yet.
UNCATEGORISED = "uncategorised"


def category_of(kind: str) -> str:
    """Return the provisional category of a kind, or "uncategorised" for a kind not in the table."""
    return PROVISIONAL_CATEGORIES.get(kind, UNCATEGORISED)


def human_actions_in_time_range(
    start: datetime,
    end: datetime,
    transcript_paths: Iterable[Path] = (),
    activitywatch_export_paths: Iterable[Path] = (),
) -> list[HumanAction]:
    """Return every human action from the given files that began in [start, end), oldest first.

    `end` itself is outside the range, so consecutive ranges never count an
    action twice. A time with no zone, given or read, is taken as UTC. An
    action is in the range when it began in it, even if its duration runs
    past `end`. An action whose time cannot be read is in no range; the
    per-source parsers report what they could not read.
    """
    actions: list[HumanAction] = []
    for transcript_path in transcript_paths:
        actions.extend(parse_human_actions(transcript_path).actions)
    for export_path in activitywatch_export_paths:
        actions.extend(parse_activitywatch_export(export_path).actions)

    range_start = as_utc(start)
    range_end = as_utc(end)
    timed_actions: list[tuple[datetime, HumanAction]] = []
    for action in actions:
        began = time_of(action)
        if began is not None and range_start <= began < range_end:
            timed_actions.append((began, action))
    # Ties keep the order the files were given in, then each file's order.
    timed_actions.sort(key=lambda timed_action: timed_action[0])
    in_range = []
    for _began, action in timed_actions:
        in_range.append(action)
    return in_range


def as_utc(moment: datetime) -> datetime:
    """Give a time with no zone the UTC zone, so every time can be compared."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def time_of(action: HumanAction) -> datetime | None:
    """Return when an action began, or None when its timestamp is missing or unreadable."""
    if action.timestamp is None:
        return None
    try:
        return as_utc(datetime.fromisoformat(action.timestamp))
    except ValueError:
        return None


@dataclass(frozen=True)
class HumanActionSummary:
    """Counts of a list of human actions, for a researcher to see at a glance.

    Every kind and category in `PROVISIONAL_CATEGORIES` is listed, with zero
    when unused, so a rule that stopped matching shows as a zero rather than
    as a missing line. A kind not in the table is counted under its own name
    and under "uncategorised".
    """

    total: int
    actions_by_kind: dict[str, int]
    actions_by_category: dict[str, int]
    actions_by_source: dict[str, int]


def summarise_human_actions(actions: Sequence[HumanAction]) -> HumanActionSummary:
    """Count actions by kind, by provisional category and by source.

    Pass it the result of `human_actions_in_time_range` to summarise a time
    range.
    """
    actions_by_kind: dict[str, int] = {}
    actions_by_category: dict[str, int] = {}
    for kind, category in PROVISIONAL_CATEGORIES.items():
        actions_by_kind[kind] = 0
        actions_by_category[category] = 0
    actions_by_source: dict[str, int] = {}
    for action in actions:
        actions_by_kind[action.kind] = actions_by_kind.get(action.kind, 0) + 1
        category = category_of(action.kind)
        actions_by_category[category] = actions_by_category.get(category, 0) + 1
        actions_by_source[action.source] = actions_by_source.get(action.source, 0) + 1
    return HumanActionSummary(
        total=len(actions),
        actions_by_kind=actions_by_kind,
        actions_by_category=actions_by_category,
        actions_by_source=actions_by_source,
    )
