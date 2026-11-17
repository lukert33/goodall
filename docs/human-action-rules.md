# Human-action rules, version 1

These are the rules `goodall.human_actions` uses to find what the person did in
one Claude Code transcript. Each rule is a function in
`src/goodall/human_actions.py`, named below; every fixed string a rule matches
is in `claude_code_markers()`. Change a rule, its function and this page in the
same commit, and raise `RULES_VERSION` (ADR 0004).

Observed against Claude Code 2.1.x transcripts in November 2026. Claude Code's
transcript format is internal, so any of these strings may change in a new
version.

## What comes back

`parse_human_actions(path)` returns `actions`, in transcript order, and
`unfamiliar_entries`. Every action has `kind`, `line_number`, `entry_id` (the
entry's `uuid`) and `timestamp`.

| kind | extra fields |
|---|---|
| `typed_prompt` | `text`, `image_count` |
| `interruption` | `interrupted` (`"generate"` or `"tool_call"`), `interrupted_tool_call_id` |
| `tool_call_rejection` | `tool_call_id`, `decision` (always `"reject"`), `feedback` |

`interrupted` and `decision` use the values of Inspect's InterruptEvent and
ApprovalEvent.

## Rules

Only entries of type `user` can hold a human action. Within one:

1. **Rejection** (`rejection_from_tool_result`). A tool result with
   `is_error: true` whose text begins "The user doesn't want to proceed with
   this tool use". Feedback is the text after "the user said:", if present.
   `toolDenialKind` is not used: it reads `permission-rule` both for a
   configured rule and for a refusal with feedback.
2. **Interruption of a running tool** (`interruption_from_tool_result`). A tool
   result with `is_error: true` whose text begins "The user doesn't want to take
   this action right now". The result's `tool_use_id` names the interrupted
   call.
3. **Interruption marker** (`interruption_from_marker`). An entry whose whole
   text is "[Request interrupted by user]" (`generate`) or "[Request
   interrupted by user for tool use]" (`tool_call`, call id unknown). When the
   entry's parent is a rejection, the marker is the end of that rejection and
   is not counted again.
4. **Prepended context** (`strip_prepended_context`). Leading
   `<system-reminder>...</system-reminder>` blocks are removed; what is left is
   what the person typed.
5. **Typed prompt** (`is_typed_prompt`). What is left after rule 4 counts
   unless: the entry has `isMeta`, `isCompactSummary` or `isSidechain` true; its
   `origin.kind` is present and not `human`; the text begins with a harness
   wrapper tag (`<command-name>`, `<command-message>`, `<local-command-...>`,
   `<task-notification>`, `<bash-...>`); or there is neither text nor an image.
   `userType` is not used: it is `external` on every user entry.

## Unfamiliar entries

Reported, with line number and a reason that names no content: a line that is
not JSON or not an object, an entry with no type or an unknown type, a user
entry with no message content, an unknown content block type, an unknown
`origin.kind`. Blank lines are skipped. Parsing always carries on.

## Not detected in version 1

- **Permission approvals.** Newer transcripts carry `permissionDecision` on
  tool results, with `source` values such as `user_temporary` and
  `user_permanent`, which look like the person's approvals. Not yet used.
- **Slash commands and shell commands** the person typed (`/command`, `!command`).
  They are recorded inside wrapper tags and are excluded, even when
  `origin.kind` is `human`.
- **Interruptions between turns**, which leave nothing in a transcript.
- **Prompts refused by the prompt gate**, which leave a `queue-operation`
  entry, not a user entry.
