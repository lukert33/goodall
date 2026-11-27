"""What goodall needs to know about a source application: the seam between the baseline and each app family.

A source application is an application whose agent sessions goodall
captures (GLOSSARY.md). Applications that write the same files form an app
family, and goodall handles a family once. For every family the baseline is
the same (ADR 0019): when a work block ends, goodall copies, byte for byte,
each agent session that has a record inside the work block.

This module holds the words the two sides share and nothing else:

- the baseline (goodall.agent_session_copies) decides when to copy, applies
  the rule below, reports problems, and tries again later;
- each app family (goodall.claude_family is the first) knows where its
  application keeps agent sessions, how to read a record's time, and where
  its copies go, in the application's own layout.

To add an app family: write a module that builds one AppFamily, as
goodall.claude_family does at its end, and add it to APP_FAMILIES in
goodall.agent_session_copies. For its agent sessions to be in the timeline
too, write a module that turns its copies into entries, as
goodall.timeline_claude does, and call it from goodall.timeline.

Nothing here assumes one file per agent session, because applications
differ: the Claude family keeps a transcript with a folder beside it,
another keeps a database and a transcript for each conversation. An
AgentSession only names the session and the folder it is kept in; the
family's own functions know the rest.

Standard library only: goodall.claude_family imports this module and is on
the hook path (ADR 0005).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from goodall.verbatim_copies import Copying


@dataclass(frozen=True)
class AgentSession:
    """One agent session as its source application keeps it.

    `id` is the application's own id for it. `folder` is the folder the
    application keeps it in; what that folder is, and which files in it
    belong to this session, is for the app family to say.
    """

    id: str
    folder: Path


@dataclass(frozen=True)
class FoundAgentSessions:
    """The agent sessions an app family found, and a plain sentence for each place it could not look."""

    agent_sessions: list[AgentSession]
    problems: list[str]


@dataclass(frozen=True)
class AppFamily:
    """One app family, as the baseline sees it: a name, where its copies go, and three functions.

    - `name` is how goodall names the family to a person: `Claude`.
    - `copies_folder_names` are the folders, directly inside the log
      directory, that hold this family's copies in its own layout. No two
      families share one.
    - `find_agent_sessions(log_directory)` returns every agent session of
      the family in this macOS user account, whatever folder it ran in. The
      log directory is passed because a family may keep notes there about
      where its application's files are.
    - `has_record_inside(agent_session, started_at, ended_at)` says whether
      any of the session's records carries a time inside the work block,
      using is_inside_work_block. It may raise OSError.
    - `copy_agent_session(agent_session, log_directory, now)` copies the
      session's files into the log directory, byte for byte, and returns
      what it did. It may raise OSError.

    For `goodall delete` (goodall.work_block_deletion), which deletes what
    one work block captured:

    - `find_copied_agent_sessions(log_directory)` returns the agent sessions
      whose copies are in the log directory, each with its `folder` in the
      copies, laid out as the application's own, so `has_record_inside`
      reads a copy exactly as it reads the original.
    - `copied_places(log_directory, agent_session_id)` lists every file and
      folder in the log directory that holds a copy of that one agent
      session and of nothing else, earlier copies kept aside included.
      Deleting them deletes the agent session's copies whole.
    - `shared_copies_left` names, for a person and in the middle of a
      sentence, the copies several agent sessions share, which `goodall
      delete` therefore leaves in place; empty when the family has none.
    """

    name: str
    copies_folder_names: tuple[str, ...]
    find_agent_sessions: Callable[[Path], FoundAgentSessions]
    has_record_inside: Callable[[AgentSession, datetime, datetime], bool]
    copy_agent_session: Callable[[AgentSession, Path, datetime], Copying]
    find_copied_agent_sessions: Callable[[Path], FoundAgentSessions]
    copied_places: Callable[[Path, str], list[Path]]
    shared_copies_left: str


def is_inside_work_block(moment: datetime, started_at: datetime, ended_at: datetime) -> bool:
    """True when `moment` is inside the work block that ran from `started_at` to `ended_at`.

    This is the one rule that says which records belong to a work block,
    for every app family. Both ends count. A work block's times are kept in
    whole seconds, with the end rounded down, so the whole of its last
    second counts: a record written half a second before `goodall stop`
    belongs to the work block.
    """
    return started_at <= moment < ended_at + timedelta(seconds=1)
