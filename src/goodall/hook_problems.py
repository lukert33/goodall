"""A note, in the log directory, of each time a Claude Code hook could not do its whole job.

`goodall hook` (goodall.hook) calls `record_hook_problem`; `goodall start`,
`stop` and `status` call `take_hook_problems` while they settle
(goodall.work_block_commands), so the person is told once, then the note is
filed away. This module is on the hook path, so it imports only the standard
library (ADR 0005) and starts no program.

    <log directory>/hook-problems/hook-problems.jsonl   one line per problem, not yet reported
    <log directory>/hook-problems/reported/...          those lines, once a command has said so

One line looks like:

    {"at": "2026-11-16T09:40:00Z", "event": "UserPromptSubmit",
     "work_block": "20261116T093000Z-ada-lovelace-0a1b2c3d", "problem": "not_json"}

`problem` is one of a fixed set of words (PROBLEMS). The hook's input is
never stored, not a byte of it: it holds the person's prompt.

Why it exists (the owner's decision, 2026-11-17): when the prompt gate
cannot read what Claude Code sent with a prompt, perhaps because a newer
Claude Code changed the shape of its input, it lets the prompt through while
a work block is active rather than block every prompt until goodall is
updated, and records the gap so it can be reconciled later. The heartbeat
it leaves names no Claude session, so the link between that Claude session
and the work block is missing. It can be rebuilt afterwards by time: the
sweep still copies every transcript in the eval account, and a transcript
whose prompts fall between the work block's start and end, at the moments
noted here, belongs to it. No tool does that reconciliation yet.

Decisions a reader would otherwise reverse-engineer (PROVISIONAL):

- Reported once, as failed transcript copies are (goodall.transcripts): the
  first command that settles reads the file, says how many times and since
  when, and moves the file into `reported/`, kept, not deleted. If it
  happens again, the next command says so again.
- Only the prompt gate writes here. Other events with unreadable input
  still leave a heartbeat without a Claude session, silently, as before; a
  line per tool call would flood the file while saying nothing new.
"""

import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PROBLEMS_FOLDER_NAME = "hook-problems"
PROBLEMS_FILE_NAME = "hook-problems.jsonl"
REPORTED_FOLDER_NAME = "reported"

# Why the prompt gate could not read what Claude Code sent: nothing was sent;
# it was not one JSON object; it named no Claude session; or what it named is
# not a Claude session id (a lowercase UUID).
INPUT_PROBLEMS = ("empty", "not_json", "no_session_id", "session_id_not_uuid")
PROBLEMS = INPUT_PROBLEMS

# Readable and writable by this macOS user account only, like everything
# else goodall keeps in the log directory.
FILE_PERMISSIONS = 0o600
FOLDER_PERMISSIONS = 0o700


def record_hook_problem(
    log_directory: Path, now: datetime, event_name: str, work_block_id: str | None, problem: str
) -> None:
    """Append one line saying a hook met `problem`, so `goodall status` can say so later.

    One write per line, so hooks firing at once never interleave. Never
    raises: a hook must not fail over its own note, and when even this cannot
    be written (the log directory is unplugged, say), the prompt gate is
    already closed for that reason.
    """
    line = json.dumps(
        {
            "at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": event_name,
            "work_block": work_block_id,
            "problem": problem,
        }
    )
    folder = log_directory / PROBLEMS_FOLDER_NAME
    try:
        folder.mkdir(mode=FOLDER_PERMISSIONS, exist_ok=True)
        descriptor = os.open(folder / PROBLEMS_FILE_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_PERMISSIONS)
        try:
            os.write(descriptor, (line + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError:
        pass


@dataclass(frozen=True)
class HookProblems:
    """What the problems file said when a command took it: how many of each kind, and since when.

    `filed_at` is where the lines were moved and are kept, or the file
    itself when moving it failed.
    """

    unreadable_inputs: int
    first_at: datetime | None
    filed_at: Path


def take_hook_problems(log_directory: Path, now: datetime) -> HookProblems | None:
    """Read the problems hooks noted, file them away under `reported/`, and return them; None when there are none.

    A line that cannot be read still counts (as an unreadable input), so a
    damaged line is never the reason a warning goes unsaid. Never raises for
    anything on the disk.
    """
    folder = log_directory / PROBLEMS_FOLDER_NAME
    problems_file = folder / PROBLEMS_FILE_NAME
    try:
        text = problems_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    unreadable_inputs = 0
    first_at = None
    for line in text.splitlines():
        if line.strip() == "":
            continue
        unreadable_inputs += 1
        at = read_moment(line)
        if at is not None and (first_at is None or at < first_at):
            first_at = at
    if unreadable_inputs == 0:
        return None

    filed_at = problems_file
    reported_folder = folder / REPORTED_FOLDER_NAME
    reported_file = reported_folder / (
        f"hook-problems-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}-{secrets.token_hex(2)}.jsonl"
    )
    try:
        reported_folder.mkdir(mode=FOLDER_PERMISSIONS, exist_ok=True)
        os.rename(problems_file, reported_file)
        filed_at = reported_file
    except OSError:
        pass
    return HookProblems(unreadable_inputs=unreadable_inputs, first_at=first_at, filed_at=filed_at)


def read_moment(line: str) -> datetime | None:
    """The `at` of one line, or None when the line or its time cannot be read."""
    try:
        values = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(values, dict) or not isinstance(values.get("at"), str):
        return None
    try:
        return datetime.fromisoformat(values["at"]).astimezone(UTC)
    except ValueError:
        return None
