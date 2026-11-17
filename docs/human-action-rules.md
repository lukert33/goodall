# Human-action rules, version 2

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
`unfamiliar_entries`. Every action has these common fields:

- `source`: always `claude_code_transcript`.
- `kind`: one of the kinds below.
- `rule`: the name of the rule function that found it.
- `line_number`, `entry_id` (the entry's `uuid`), `session_id` (the entry's
  `sessionId`) and `transcript_path`: where it was found.
- `timestamp`: the entry's timestamp, as written.
- `duration_seconds`: always empty; a transcript entry is a moment.

`source`, `kind` and `rule` are there so kinds can be merged or renamed after
looking at real results, without re-reading transcripts.

| kind | rule | extra fields |
|---|---|---|
| `typed_prompt` | `is_typed_prompt` | `text`, `image_count` |
| `interruption` | `interruption_from_marker`, `interruption_from_tool_result` | `interrupted` (`"generate"` or `"tool_call"`), `interrupted_tool_call_id` |
| `tool_call_rejection` | `rejection_from_tool_result` | `tool_call_id`, `tool_name`, `decision` (always `"reject"`), `feedback` |
| `shell_command` | `shell_command_from_bash_input` | `text` (the command as typed), `output_line_number`, `output_entry_id` |
| `slash_command` | `slash_command_from_command_tags` | `command_name`, `arguments` (both as typed), `command_type` (`"local"` or `"prompt"`) |
| `tool_call_approval_once` | `approval_from_permission_decision` | `tool_call_id`, `tool_name`, `decision` (always `"approve"`), `permission_source`, `grant_scope`, `rule_source` |
| `tool_call_approval_always` | `approval_from_permission_decision` | as above |
| `permission_prompt_dismissal` | `dismissal_from_permission_decision` | `tool_call_id`, `tool_name`, `decision` (always `"reject"`) |

`tool_name` is read from the assistant entry that made the call, when it is in
the same transcript; only the name is kept, never the tool's input.

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
3a. **Permission decision** (`permission_decision_from_entry`, whose actions
   name the rules `approval_from_permission_decision` and
   `dismissal_from_permission_decision`). Claude Code 2.1.289 and later write
   `permissionDecision` (`decision`, `source`, and sometimes `reasonType`,
   `grantScope`, `ruleSource`) on every tool result entry. Read only when the
   entry answers exactly one call (every entry in the structure scan did):
   - `source: user_temporary`, `decision: accept`: `tool_call_approval_once`.
   - `source: user_permanent`, `decision: accept`: `tool_call_approval_always`,
     the person chose "always allow" and a standing rule was added
     (`ruleSource` says where, such as `localSettings`).
   - `source: user_abort`, `decision: reject`: `permission_prompt_dismissal`,
     the person dismissed the prompt without choosing. Seen once in the scan.
   - `source: config` (a settings rule, the permission mode or the auto-mode
     classifier) and `source: hook` decided without the person: no action.
   - Any other source is reported as unfamiliar.

   A refusal the person chose carries no `permissionDecision`; rule 1 finds
   it. Approvals in transcripts older than 2.1.289 are not recorded and
   cannot be found.
4. **Prepended context** (`strip_prepended_context`). Leading
   `<system-reminder>...</system-reminder>` blocks are removed; what is left is
   what the person typed.
5. **From the person** (`is_from_the_person`). Shared by every rule that reads
   what the person typed: an entry is ruled out when it has `isMeta`,
   `isCompactSummary` or `isSidechain` true, or its `origin.kind` is present
   and not `human`.
6. **Shell command** (`shell_command_from_bash_input`). An entry from the
   person whose text (after rule 4) begins with `<bash-input>`: the person ran
   `!command`. `text` is what is inside the tags. If `<bash-stdout>` or
   `<bash-stderr>` follows in the same entry (newer Claude Code), the output
   link is that entry.
7. **Shell command output** (`link_shell_command_output`). In older Claude
   Code the output is the next entry, beginning with `<bash-stdout>` or
   `<bash-stderr>`, whose `parentUuid` is the command's entry. The command's
   output link then names that entry. Output is never copied.
8. **Slash command** (`slash_command_from_command_tags`). An entry from the
   person whose text (after rule 4) opens with `<command-name>` or
   `<command-message>` and holds a `<command-name>` tag. `command_name` and
   `arguments` are the contents of `<command-name>` and `<command-args>`, as
   typed; `arguments` is empty when there is no `<command-args>` tag.
   `command_type` is `local` when the name tag opens the entry (a command
   Claude Code runs itself; its output follows as `<local-command-stdout>`)
   and `prompt` when the message tag opens it (the command expands into a
   prompt, which follows as a meta entry and is not kept). The tag order
   separated the two in every transcript in the structure scan (Claude Code
   2.1.205 to 2.1.326). Claude Code does not record which commands are built
   in; a plugin's commands have a `plugin:` prefix in their name.
9. **Typed prompt** (`is_typed_prompt`). What is left after rule 4 counts
   when rule 5 allows it, unless the text begins with a harness wrapper tag
   (`<command-name>`, `<command-message>`, `<local-command-...>`,
   `<task-notification>`, `<bash-...>`), or there is neither text nor an image.
   `userType` is not used: it is `external` on every user entry.

## Unfamiliar entries

Reported, with line number and a reason that names no content: a line that is
not JSON or not an object, an entry with no type or an unknown type, a user
entry with no message content, an unknown content block type, an unknown
`origin.kind`, an unknown `permissionDecision.source`. Blank lines are skipped.
Parsing always carries on.

## Not detected in version 2

- **Permission approvals before Claude Code 2.1.289**, which wrote no
  `permissionDecision`.
- **Interruptions between turns**, which leave nothing in a transcript.
- **Prompts refused by the prompt gate**, which leave a `queue-operation`
  entry, not a user entry.
