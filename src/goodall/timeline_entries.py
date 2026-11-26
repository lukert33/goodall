"""What a timeline entry may hold: the column rule, written as lists.

The timeline (GLOSSARY.md, ADR 0019) lists in time order what happened in
an eval, from every kind of raw record. Its one rule is the point of its
design: an entry holds only times, numbers and values from fixed lists,
plus a pointer to its place in a raw record. Anything a person or a website
could have written (a prompt, a reply, a window title, a page address, a
file path, a workstream's name, a note) stays in the raw record, and is
read from there when a person looks.

This module is that rule. It holds the lists, the shape of an entry, and
`entry_problems`, which the database (goodall.timeline) runs on every entry
before writing it. A value that is not in its column's list is never
written, whichever module made the entry, so a mistake in a rule shows up
as a refused entry and not as a leak.

Who calls it: the modules that turn raw records into entries
(goodall.timeline_claude, goodall.timeline_activitywatch,
goodall.timeline_records) build `TimelineEntry` values with the helpers
here, and goodall.timeline checks and stores them. Standard library only.

Decisions a reader would otherwise reverse-engineer:

- A name that is not on a list becomes that column's word for "something
  else" (`other_tool`, `other_application`), never the name itself. A tool
  or an application can be named by anyone who writes one, so its name is
  only as safe as the list it was checked against. The pointer still leads
  to the real name.
- Every tool reached through MCP is `mcp_tool`. Its real name holds the
  name someone gave a server.
- The pointer is the one value that is not from a list: the raw record's
  path. For a Claude session it holds the project folder's name, which is
  made from the folder the agent ran in; for ActivityWatch it holds the
  Mac's hostname. ADR 0019 accepts this for a file that never leaves the
  Mac, and a release drops the pointers.
- Raise TIMELINE_RULES_VERSION whenever a list here, or a rule in one of
  the three modules above, changes what an entry holds. The rules are in
  docs/timeline.md; change both together (ADR 0004).
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from goodall import records
from goodall.actor_labels import ACTOR_LABEL_RULES
from goodall.human_actions import claude_code_markers
from goodall.human_activity import PROVISIONAL_CATEGORIES, UNCATEGORIZED

# The version of the rules that decide what a timeline entry holds.
TIMELINE_RULES_VERSION = "1"

# Where a raw record is kept: the log directory (copies of each source
# application's files, and of ActivityWatch's data) or the record repo
# (what a person wrote on purpose).
IN_LOG_DIRECTORY = "log_directory"
IN_RECORD_REPO = "record_repo"
KEPT_IN = (IN_LOG_DIRECTORY, IN_RECORD_REPO)

# Which kind of raw record an entry came from.
SOURCE_CLAUDE = "claude"
SOURCE_ACTIVITYWATCH = "activitywatch"
SOURCE_RECORD_REPO = "record_repo"
SOURCES = (SOURCE_CLAUDE, SOURCE_ACTIVITYWATCH, SOURCE_RECORD_REPO)

# Which source application wrote a Claude family record, by the value of
# its `entrypoint` field (docs/source-applications.md).
SOURCE_APPLICATIONS_BY_ENTRYPOINT = {
    "claude-desktop": "claude_desktop_code_tab",
    "cli": "claude_code_terminal",
    "sdk-cli": "claude_code_sdk",
}
OTHER_SOURCE_APPLICATION = "other_source_application"
SOURCE_APPLICATIONS = (*SOURCE_APPLICATIONS_BY_ENTRYPOINT.values(), OTHER_SOURCE_APPLICATION)

# Who produced an entry (ADR 0004). None when no rule could say.
ACTORS = ("human", "ai", "harness")

# What a transcript line is, when it is not a human action: a tool the
# model asked for, what came back, the model's words, its reasoning, or any
# other line (bookkeeping, injected context), whose `entry_type` says more.
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_MESSAGE_TEXT = "message_text"
KIND_THINKING = "thinking"
KIND_ENTRY = "entry"
KIND_UNFAMILIAR_ENTRY = "unfamiliar_entry"
TRANSCRIPT_KINDS = (
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_MESSAGE_TEXT,
    KIND_THINKING,
    KIND_ENTRY,
    KIND_UNFAMILIAR_ENTRY,
)

# What a person wrote on purpose, in the record repo.
KIND_JOURNAL_ENTRY = "journal_entry"
KIND_INTERVENTION = "intervention"

# Every kind of entry. The human action kinds, from transcripts and from
# ActivityWatch, are the ones goodall already has (docs/human-action-categories.md).
KINDS = (*PROVISIONAL_CATEGORIES.keys(), *TRANSCRIPT_KINDS, KIND_JOURNAL_ENTRY, KIND_INTERVENTION)

# The provisional category of a human action (goodall.human_activity).
CATEGORIES = (*sorted(set(PROVISIONAL_CATEGORIES.values())), UNCATEGORIZED)

# The types of transcript line Claude Code is known to write.
ENTRY_TYPES = claude_code_markers().known_entry_types

# The names of the rules that give an actor label (docs/actor-label-rules.md).
LABEL_RULES = tuple(rule.name for rule in ACTOR_LABEL_RULES)

# The tools the Claude family provides itself, by the name a transcript
# gives them. Names seen in the development account on 2026-11-26 (names
# only), and the older names Claude Code documents. A tool that is not here
# is `other_tool`; add its name when it is one the application ships.
KNOWN_TOOLS = (
    "Agent",
    "Artifact",
    "ArtifactComments",
    "ArtifactData",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "ListAgents",
    "ListMcpResourcesTool",
    "ListPlugins",
    "ListSkills",
    "LS",
    "Monitor",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "PushNotification",
    "Read",
    "ReadMcpResourceTool",
    "RemoteTrigger",
    "ScheduleWakeup",
    "SearchPlugins",
    "SearchSkills",
    "SendMessage",
    "SendUserFile",
    "Skill",
    "SlashCommand",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "TodoRead",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
)
MCP_TOOL = "mcp_tool"
OTHER_TOOL = "other_tool"
TOOLS = (*KNOWN_TOOLS, MCP_TOOL, OTHER_TOOL)

# How the name of a tool reached through MCP begins.
MCP_TOOL_NAME_PREFIX = "mcp__"

# The applications goodall names when one was in front, by the name
# ActivityWatch's window watcher gives it on macOS. Browsers, terminals,
# editors, the source applications, and what macOS itself shows. An
# application that is not here is `other_application`; add its name when
# knowing it matters to an eval.
KNOWN_APPLICATIONS = (
    "1Password",
    "Activity Monitor",
    "Antigravity",
    "Arc",
    "Brave Browser",
    "Calendar",
    "ChatGPT",
    "Chromium",
    "Claude",
    "Code",
    "Codex",
    "Cursor",
    "Finder",
    "Firefox",
    "Ghostty",
    "Google Chrome",
    "iTerm2",
    "loginwindow",
    "Mail",
    "Messages",
    "Microsoft Edge",
    "Notes",
    "Numbers",
    "Pages",
    "Preview",
    "Safari",
    "Slack",
    "System Settings",
    "Terminal",
    "TextEdit",
    "Warp",
    "Xcode",
    "Zed",
    "zoom.us",
)
OTHER_APPLICATION = "other_application"
APPLICATIONS = (*KNOWN_APPLICATIONS, OTHER_APPLICATION)

# How something ended: whether a tool's result was an error, and what the
# person decided in an intervention (goodall.records.DECISIONS).
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOMES = (OUTCOME_OK, OUTCOME_ERROR, *records.DECISIONS)

# An intervention's category when it is not one the eval config lists.
OTHER_INTERVENTION_CATEGORY = "other_category"


@dataclass(frozen=True)
class RawRecordPointer:
    """Where a raw record is: the log directory or the record repo, and its path inside, with forward slashes.

    The path is the one value in the timeline that is not from a list (see
    the module docstring).
    """

    kept_in: str
    path: PurePosixPath


@dataclass(frozen=True)
class TimelineEntry:
    """One thing that happened, as the timeline holds it.

    - `started_at` is when, with a time zone; `duration_seconds` is how long
      it lasted, or None for something that happened at a moment.
    - `source` says which kind of raw record it came from, and `kind` what
      it was. `actor` is who produced it, or None when no rule could say.
    - `raw_record` and `position` are the pointer: the file, and the place
      in it. For a transcript that is the line's number, counting from 1;
      for a copy of ActivityWatch's events it is the event's place in the
      file's list, counting from 0; for a record in the record repo, which
      is one file, it is None.
    - The rest say more where the kind has more to say, and are None
      otherwise: the source application, the type of transcript line, the
      rule that gave the actor label, the tool, the application in front,
      the human action's category, an intervention's category, and the
      outcome. `in_subagent` is true for a line of a subagent's transcript.
    """

    started_at: datetime
    duration_seconds: float | None
    source: str
    kind: str
    actor: str | None
    raw_record: RawRecordPointer
    position: int | None
    source_application: str | None = None
    entry_type: str | None = None
    label_rule: str | None = None
    tool: str | None = None
    application: str | None = None
    category: str | None = None
    intervention_category: str | None = None
    outcome: str | None = None
    in_subagent: bool = False


def listed_tool(tool_name: str | None) -> str | None:
    """The word the timeline holds for a tool's name: the name if goodall lists it, `mcp_tool`, or `other_tool`."""
    if tool_name is None:
        return None
    if tool_name in KNOWN_TOOLS:
        return tool_name
    if tool_name.startswith(MCP_TOOL_NAME_PREFIX):
        return MCP_TOOL
    return OTHER_TOOL


def listed_application(application_name: str) -> str:
    """The word the timeline holds for an application's name: the name if goodall lists it, or `other_application`."""
    if application_name in KNOWN_APPLICATIONS:
        return application_name
    return OTHER_APPLICATION


def listed_source_application(entrypoint: object) -> str | None:
    """The source application an `entrypoint` value stands for; None when the record carries none."""
    if not isinstance(entrypoint, str) or entrypoint == "":
        return None
    return SOURCE_APPLICATIONS_BY_ENTRYPOINT.get(entrypoint, OTHER_SOURCE_APPLICATION)


def entry_problems(entry: TimelineEntry, intervention_categories: tuple[str, ...]) -> list[str]:
    """Everything about an entry that breaks the column rule, as plain sentences; none when it may be written.

    `intervention_categories` is the eval config's own list, the one list
    that is not this module's. A sentence names the column and never the
    value, because the value is exactly what must not be shown.
    """
    problems = []
    if entry.started_at.tzinfo is None:
        problems.append("started_at has no time zone")
    if entry.duration_seconds is not None and entry.duration_seconds < 0:
        problems.append("duration_seconds is negative")
    if entry.position is not None and entry.position < 0:
        problems.append("position is negative")
    if entry.raw_record.kept_in not in KEPT_IN:
        problems.append("raw_record.kept_in is not on its list")
    if entry.raw_record.path.is_absolute() or ".." in entry.raw_record.path.parts:
        problems.append("raw_record.path does not stay inside its folder")
    listed_values: list[tuple[str, str | None, tuple[str, ...], bool]] = [
        ("source", entry.source, SOURCES, True),
        ("kind", entry.kind, KINDS, True),
        ("actor", entry.actor, ACTORS, False),
        ("source_application", entry.source_application, SOURCE_APPLICATIONS, False),
        ("entry_type", entry.entry_type, ENTRY_TYPES, False),
        ("label_rule", entry.label_rule, LABEL_RULES, False),
        ("tool", entry.tool, TOOLS, False),
        ("application", entry.application, APPLICATIONS, False),
        ("category", entry.category, CATEGORIES, False),
        (
            "intervention_category",
            entry.intervention_category,
            (*intervention_categories, OTHER_INTERVENTION_CATEGORY),
            False,
        ),
        ("outcome", entry.outcome, OUTCOMES, False),
    ]
    for column, value, allowed_values, required in listed_values:
        if value is None:
            if required:
                problems.append(f"{column} is missing")
            continue
        if value not in allowed_values:
            problems.append(f"{column} is not on its list")
    return problems


@dataclass(frozen=True)
class TimelineWorkBlock:
    """One ended work block, as the timeline holds it: when it ran, how it ended, and where its record is.

    Its id and its workstream are not held: the id is the name of the
    record's file, which the pointer gives, and a workstream's name is a
    person's words.
    """

    started_at: datetime
    ended_at: datetime
    end_state: str | None
    raw_record: RawRecordPointer


@dataclass
class SourceEntries:
    """What one kind of raw record gave the timeline.

    `problems` holds a plain sentence for each file that could not be read.
    `without_a_time` counts the records that are in no entry because they
    carry no time goodall can read, or are in a shape no rule knows: they
    are still in the raw record.
    """

    entries: list[TimelineEntry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    without_a_time: int = 0

    def add(self, other: "SourceEntries") -> None:
        """Take in what another reading gave."""
        self.entries.extend(other.entries)
        self.problems.extend(other.problems)
        self.without_a_time += other.without_a_time
