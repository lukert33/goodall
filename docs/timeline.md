# The timeline: rules version 1, schema version 1

The timeline is a derived record, one for each eval, that lists in time order
what happened: what the person did in each application, what they asked an
agent, which tools the agent used and how they ended, and the journal entries
and interventions written (`GLOSSARY.md`, ADR 0019). This page says what it
holds, the rules that decide that, and how to read it.

It is one SQLite file in the log directory, `timeline/timeline.sqlite3`,
readable only by the macOS user account the eval runs in. Nothing is stored
only there. Goodall builds it whole from the raw records each time, so it can
be deleted at any moment and built again.

## The column rule: read this first

**An entry holds only times, numbers and values from fixed lists, plus a
pointer to its place in a raw record.** Anything a person or a website could
have written stays in the raw record: a prompt, a reply, a tool's input or
output, a window's title, a page's address, a file's path, a workstream's
name, a note.

The rule is code, in `src/goodall/timeline_entries.py`: the lists, and a check
that every entry passes before it is written. An entry with a value off its
list is left out and the build says so. Three things follow from it:

- **A name that is not on a list is not kept.** A tool goodall does not list is
  `other_tool`, and every tool reached through MCP is `mcp_tool`. An
  application goodall does not list is `other_application`. The pointer leads
  to the real name. To have a name kept, add it to `KNOWN_TOOLS` or
  `KNOWN_APPLICATIONS` and raise `TIMELINE_RULES_VERSION`.
- **The pointer is the one exception.** It is the raw record's path, which is
  not from a list. For a Claude session it holds the project folder's name,
  which Claude Code makes from the folder the agent ran in. For ActivityWatch
  it holds the Mac's hostname. For a record in the record repo it holds the
  record's id, which holds the contributor's id.
- **The timeline is never shared as it is.** It is close to shareable, and a
  release (ADR 0017) still drops the pointers, replaces ids and coarsens times.

## Reading it

`goodall timeline` shows the last ended work block: one line for each entry,
at its time of day, with who did it, what it was and where it came from.

| Option | What it does |
|---|---|
| `<work block id>` | Given first, by itself, as for `goodall fixblock`: shows that work block instead of the last. The id is the one `goodall stop` shows. |
| `--pointers` | Adds where each entry is in a raw record: the folder, the path inside it, and the line or the event. |
| `--all` | Shows the bookkeeping lines of a transcript too (kind `entry`). |
| `--rebuild` | Builds the timeline again from the raw records first. |
| `--json` | Prints one JSON document with the keys `work_block`, `entries`, `entries_not_shown`, `rule_versions`, `warnings` and `failures`. |

What it prints is only what the timeline holds, so no private text is printed.
To read the text of an entry, open the file its pointer names: for a
transcript, the line with that number; for a copy of ActivityWatch's events,
the event at that place in the file's list, counting from 0.

Any program that reads SQLite can read the file too. For example, to count
the whole eval's entries by who did them and what they were:

```
sqlite3 timeline/timeline.sqlite3 "SELECT actor, kind, count(*) FROM entries GROUP BY actor, kind"
```

## When it is built

- `goodall stop` builds it, after the agent sessions and ActivityWatch's data
  are copied and the work block's record is written.
- `goodall start` and `goodall status` build it when they have just closed a
  work block nobody stopped.
- `goodall timeline` builds it when there is none, when `--rebuild` is given,
  and when the file was built by another version of the schema or of any set
  of rules below. So a change of shape or of rules needs no migration.

A build writes a new file and then puts it in place of the old one, so a
build that fails or is interrupted leaves the last good timeline. A problem
while building is a warning: the raw records are untouched, and `goodall
timeline --rebuild` builds it again. That holds for an error nobody
expected too, such as a raw record in a shape no rule has met: `goodall
stop` still succeeds, and the warning names the kind of error and never its
message, which could quote the record. `goodall timeline` reports the same
as a failure and exits 1.

Building reads every copied transcript. On a synthetic eval of 300 Claude
sessions and 420 MB of transcripts it took about 10 seconds and gave a 100 MB
file. Not measured on a real eval.

## What it holds

Times are text in UTC, always written `2026-11-16T09:35:00.123000Z`, so
sorting the text sorts the times.

**`information`**: `name` and `value`, one row for each fact about the file.

| name | value |
|---|---|
| `schema_version` | The version of the tables on this page |
| `timeline_rules_version` | The version of the rules on this page |
| `actor_label_rules_version` | `docs/actor-label-rules.md` |
| `human_action_rules_version` | `docs/human-action-rules.md` |
| `activitywatch_rules_version` | `docs/activitywatch-rules.md` |
| `goodall_version` | The goodall that built it |
| `built_at` | When |
| `records_without_a_time` | How many raw records are in no entry (see the rules) |

**`raw_records`**: one row for each raw record an entry points into.

| column | holds |
|---|---|
| `raw_record` | A number |
| `kept_in` | `log_directory` or `record_repo` |
| `path` | The path inside that folder, with forward slashes. The one text not from a list |

**`work_blocks`**: one row for each ended work block.

| column | holds |
|---|---|
| `work_block` | A number |
| `started_at`, `ended_at` | Times |
| `end_state` | `clean`, `abnormal` or `inferred` |
| `raw_record` | The pointer to its record. The record's file name is the work block's id |

**`entries`**: one row for each entry, numbered in time order.

| column | holds |
|---|---|
| `entry` | A number |
| `started_at` | A time |
| `ended_at` | `started_at` plus the duration; the same time for a moment |
| `duration_seconds` | A number, or empty for something that happened at a moment |
| `source` | `claude`, `activitywatch` or `record_repo` |
| `source_application` | `claude_desktop_code_tab`, `claude_code_terminal`, `claude_code_sdk` or `other_source_application`; empty when the record does not say |
| `actor` | `human`, `ai` or `harness`; empty when no rule could say |
| `kind` | A human action's kind (`docs/human-action-categories.md`), or `tool_call`, `tool_result`, `message_text`, `thinking`, `entry`, `unfamiliar_entry`, `journal_entry`, `intervention` |
| `entry_type` | For a transcript line, its type as Claude Code writes it (`user`, `assistant`, `system` and so on) |
| `label_rule` | For a transcript line, the name of the rule that gave its actor label (`docs/actor-label-rules.md`) |
| `tool` | A tool goodall lists, `mcp_tool` or `other_tool` |
| `application` | An application goodall lists, or `other_application` |
| `category` | A human action's provisional category (`docs/human-action-categories.md`) |
| `intervention_category` | One of the eval config's intervention categories, or `other_category` |
| `outcome` | `ok` or `error` for a tool result; the person's decision for an intervention (`approve`, `modify`, `reject`, `escalate`, `terminate`) |
| `in_subagent` | 1 for a line of a subagent's transcript, otherwise 0 |
| `raw_record`, `position` | The pointer: the raw record, and the place in it. A transcript line's number, counting from 1; an event's place in its file's list, counting from 0; empty for a record that is one file |

Which work block an entry belongs to is not stored. An entry with a duration
can lie across two work blocks, so belonging is a question of overlap:
`goodall timeline` shows every entry any part of which is inside the work
block, and the whole of the work block's last second counts.

## Rules

Each rule is a function in the module named. Change a rule, its function and
this page in the same commit, and raise `TIMELINE_RULES_VERSION` (ADR 0004).

### Claude family transcripts (`src/goodall/timeline_claude.py`)

Read from `transcripts/` in the log directory: each Claude session's
transcript and its subagents' transcripts. Earlier copies kept in
`superseded-transcripts/` are not read.

1. A line with no time is in no entry. The time is the line's top-level
   `timestamp`. Titles, modes and other bookkeeping lines carry none; they are
   counted in `records_without_a_time`, as is a line that is not JSON.
2. Each human action the human-action rules find on the line is an entry
   whose actor is `human` and whose kind is the action's kind. A prompt sent
   through the SDK (`sdk_prompt`) has no actor: whether that is the person is
   undecided.
3. Unless the line's actor label is `human`, the line also gives entries
   labeled with its actor: one for each tool the model asked for
   (`tool_call`), each tool result (`tool_result`, `ok` or `error`), each piece
   of the model's words (`message_text`) and of its reasoning (`thinking`). A
   line with none of these is one `entry`.
4. A line the person produced gives no entry of its own; its action entries
   stand for it. So a tool result that carries the person's approval or
   refusal appears as that approval or refusal, and not as a tool result too.

### ActivityWatch copies (`src/goodall/timeline_activitywatch.py`)

Read from `activitywatch/` in the log directory (`docs/activitywatch-rules.md`,
"Copies in the log directory").

1. Each event is read by the rule for its bucket's type, the same rules that
   read an export. The entry's kind is the action's kind and its actor is
   `human`.
2. Events with one `id`, in one bucket, from one ActivityWatch (one
   `device_id`) are one entry, from the earliest start seen to the latest end
   seen, because copies overlap. Its pointer leads to the longest of them.
3. An event without an `id` is an entry of its own.
4. An event no rule can read is in no entry and is counted: one in a bucket
   of an unknown type, one with no time, and one whose duration cannot be
   added to its start (not a number, or thousands of years long).

### The record repo (`src/goodall/timeline_records.py`)

1. A journal entry is one `journal_entry`, by `human`, when it was written.
2. An intervention is one `intervention`, by `human`, when it happened, with
   its category and the person's decision.
3. A work block is not an entry. Each ended one is a row of `work_blocks`,
   including one whose record is still waiting in the log directory. A work
   block still running is not in the timeline.

## Not in this version

- Reading an entry's text through `goodall timeline`. The pointer says where
  it is. `goodall logs` (Beads issue `goodall-acp.5`, not built yet) is the
  command for what was recorded and whether it is intact, and for printing
  a raw record; `goodall timeline` is for what happened, in order.
- Telemetry. It is copied, and is in no entry.
- Any source application but the Claude family (`docs/source-applications.md`).
- Searching text, redaction, a release, and `goodall export scout` built from
  the timeline.

## Not confirmed

Built and checked against synthetic records only. Not yet run in a real eval
account: on transcripts a real Claude desktop app or Claude Code wrote, or on
a real copy of ActivityWatch's data. The lists of tools and applications were
written from names seen in the development account on 2026-11-26 and from
memory, so a real work block will show which names are missing: they appear
as `other_tool` and `other_application`.
