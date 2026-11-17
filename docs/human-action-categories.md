# Human actions from every source, and provisional categories

`goodall.human_activity` puts human actions from every source in one list.
It is the one place to ask what the person did in a time range:

```python
from datetime import datetime, UTC
from goodall.human_activity import human_actions_in_time_range, summarise_human_actions

actions = human_actions_in_time_range(
    datetime(2026, 11, 17, 9, tzinfo=UTC),
    datetime(2026, 11, 17, 17, tzinfo=UTC),
    transcript_paths=[...],  # Claude Code transcripts
    activitywatch_export_paths=[...],  # ActivityWatch exports
)
summary = summarise_human_actions(actions)
```

An action is in the range when it began at or after the start and before the
end. Times with no zone are taken as UTC. Actions come back oldest first.

## Repeated interruptions are merged here, and only here

Pressing Escape while a subagent runs is recorded in the main transcript and
in the subagent's, so parsing both finds the same interruption twice. Raw
transcripts and `parse_human_actions` on each file keep both. The derived list
from `human_actions_in_time_range` (and so the summary) counts the act once,
by the rule `merge_repeated_interruptions`:

Two `interruption` actions are the same act when they have the same
`session_id` (a subagent's entries carry its main session's id) and either

- both name an interrupted tool call, and it is the same call; or
- at least one names none, and they are at most
  `INTERRUPTION_MERGE_WINDOW_SECONDS` (2.0) seconds apart.

So two tool calls stopped close together stay two. The act becomes one
`MergedInterruption` (`kind` `interruption`, `rule`
`merge_repeated_interruptions`) at the earliest original's time, whose
`stands_for` lists every original action. Known gap: when both records name a
call, as when the main transcript's cancelled result names the subagent's
launching call and the subagent's names its own inner call, they stay two.
The structure scan found that about a third of subagent entries carry a
`sessionId` different from their folder's session, for reasons not yet known;
those are never merged with their main session.

## Sources

| source | module | rules |
|---|---|---|
| `claude_code_transcript` | `goodall.human_actions` | [human-action-rules.md](human-action-rules.md) |
| `activitywatch_export` | `goodall.activitywatch` | [activitywatch-rules.md](activitywatch-rules.md) |

## The common shape

Every action, from every source, has:

- `source`: which kind of raw record it came from.
- `kind`: what the person did, finely split.
- `rule`: the name of the rule function that found it.
- `timestamp`: when it began, as the source wrote it.
- `duration_seconds`: how long it lasted, or empty for a moment.

Where in the raw record it came from stays on the source's own record: a
transcript action has `transcript_path`, `line_number`, `entry_id` and
`session_id`; an ActivityWatch action has `bucket_id`, `hostname`,
`event_index` and `event_id`. The rest of each record is the kind's own
fields.

To add a source: write a parser module whose records have the five common
fields, add a parameter for its files to `human_actions_in_time_range`, give
each new kind a row in the table below, and write its rules page.

## Provisional categories

**Provisional.** The kinds are deliberately split finely, to be merged,
renamed or dropped after reviewing real results. This table is a first guess
at a higher-level grouping, in `PROVISIONAL_CATEGORIES`. Nothing uses it but
`category_of(kind)` and `summarise_human_actions`. A kind not in the table is
`uncategorised`.

| kind | provisional category |
|---|---|
| `typed_prompt` | `directed_the_agent` |
| `queued_prompt` | `directed_the_agent` |
| `blocked_prompt` | `directed_the_agent` |
| `question_answer` | `directed_the_agent` |
| `interruption` | `stopped_the_agent` |
| `tool_call_rejection` | `approved_or_refused` |
| `tool_call_approval_once` | `approved_or_refused` |
| `tool_call_approval_always` | `approved_or_refused` |
| `permission_prompt_dismissal` | `approved_or_refused` |
| `plan_approval` | `approved_or_refused` |
| `slash_command` | `steered_the_tool` |
| `permission_mode_change` | `steered_the_tool` |
| `shell_command` | `did_the_work_directly` |
| `sdk_prompt` | `undecided` |
| `application_focus` | `worked_outside_the_agent` |
| `browser_page_focus` | `worked_outside_the_agent` |
| `editor_file_focus` | `worked_outside_the_agent` |
| `active_at_keyboard` | `was_at_the_keyboard` |
| `away_from_keyboard` | `was_elsewhere` |

## The summary

`summarise_human_actions(actions)` returns `total`, `actions_by_kind`,
`actions_by_category` (every kind and category listed, zero when unused) and
`actions_by_source`. A prompt a program sent (`sdk_prompt`) is counted on its
own, never with typed prompts.
