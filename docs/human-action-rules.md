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
| `question_answer` | `question_answer_from_tool_result` | `tool_call_id`, `question_count`, `answers` (as given) |
| `plan_approval` | `plan_approval_from_tool_result` | `tool_call_id`, `decision` (always `"approve"`) |
| `queued_prompt` | `queued_prompt_from_attachment` | `text`, `image_count` |
| `sdk_prompt` | `sdk_prompt_from_turn_origin` | `text`, `image_count` |
| `permission_mode_change` | `permission_mode_change_between_prompts` | `from_mode`, `to_mode` |
| `blocked_prompt` | `blocked_prompt_from_hook_warning` | `text` (the queued prompt, if found) |

`tool_name` is read from the assistant entry that made the call, when it is in
the same transcript; only the name is kept, never the tool's input.

`interrupted` and `decision` use the values of Inspect's InterruptEvent and
ApprovalEvent.

## Rules

Human actions are found in entries of type `user`, and in two other places
described at the end of this list (a queued prompt in an `attachment` entry,
a blocked prompt in a `system` entry). Within a `user` entry:

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
   cannot be found. When rule 3b or 3c found an action for the same entry,
   the decision is part of it and is not counted again (answering the
   question tool also writes a `user_temporary` approval).
3b. **Question answer** (`question_answer_from_tool_result`). A tool result
   that is not an error, on an entry whose `toolUseResult` has a `questions`
   list and an `answers` object: the person answered the agent's question
   tool (AskUserQuestion). `answers` are the answer values; the questions are
   the model's words and are not kept. Declining to answer is a rejection
   (rule 1).
3c. **Plan approval** (`plan_approval_from_tool_result`). A tool result that
   is not an error, whose text begins "User has approved your plan", answering
   a call to `ExitPlanMode` (or a call not in this transcript). Rejecting a
   plan is a rejection (rule 1) with `tool_name` `ExitPlanMode`.
4. **Prepended context** (`strip_prepended_context`). Leading
   `<system-reminder>...</system-reminder>` blocks are removed; what is left is
   what the person typed.
5. **From the person** (`is_from_the_person`). Shared by every rule that reads
   what the person typed: an entry is ruled out when it has `isMeta`,
   `isCompactSummary` or `isSidechain` true, or its `origin.kind` is present
   and not `human`, or it has no `origin` and a `turnOrigin` other than
   `human` (seen: `sdk`, `peer`, `task_notification`, `host_synthetic`,
   `scheduled`). `promptSource` is not used: an app that drives Claude Code
   through the SDK writes `promptSource: sdk` on prompts the person typed.
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
9a. **Prompt sent through the SDK** (`is_sdk_prompt`, recorded as rule
   `sdk_prompt_from_turn_origin`). An entry with no `origin`, a `turnOrigin`
   of `sdk`, not meta, not a compaction summary, not on a sidechain, and with
   prompt text as in rule 9: a program sent it. Whether that is the person is
   undecided (see below), so it is never a `typed_prompt`.
9b. **Permission mode change** (`permission_mode_change_between_prompts`). A
   typed prompt whose `permissionMode` differs from the one on the last typed
   prompt that had one. Claude Code records the mode only on prompts, so the
   change is listed just before the prompt it was seen on, with that
   prompt's place and time; it happened some time after the previous prompt.
   Usually the person switched mode; approving a plan also leaves plan mode,
   and an agent can ask to enter it, so this kind is less certain than the
   others.

Outside `user` entries:

10. **Queued prompt** (`queued_prompt_from_attachment`). An `attachment` entry
    whose `attachment` has `type: queued_command`, `commandMode: prompt`,
    `origin.kind: human` and no `isMeta`: the person typed a prompt while the
    agent was working, and Claude Code handed it to the model mid-turn. It is
    not also written as a user entry. Text and images are read as for a typed
    prompt (rule 4 applies). `timestamp` is the attachment's own
    `timestamp` (when it was queued) when present. A queued prompt with no
    origin is not counted; in the scan those were all injected reminders.
11. **Blocked prompt** (`blocked_prompt_from_hook_warning`). A `system` entry
    with `subtype: informational` whose `content` begins "UserPromptSubmit
    operation blocked by hook": a hook, such as goodall's prompt gate,
    refused a prompt the person sent. No user entry is written. `text` is the
    `content` of the last `queue-operation` entry with `operation: enqueue`
    since the last user entry, then used up; it is empty if there was none.
    The hook's message is not kept. Any UserPromptSubmit hook, not only the
    prompt gate, produces this warning.

## Unfamiliar entries

Reported, with line number and a reason that names no content: a line that is
not JSON or not an object, an entry with no type or an unknown type, a user
entry with no message content, an unknown content block type, an unknown
`origin.kind` or `turnOrigin`, an unknown `permissionDecision.source`. Blank
lines are skipped.
Parsing always carries on.

## Not detected in version 2

- **Permission approvals before Claude Code 2.1.289**, which wrote no
  `permissionDecision`.
- **Interruptions between turns**, which leave nothing in a transcript.

## Undecided, waiting for evidence

- **Prompts a program sent** (`sdk_prompt`). A script may send a prompt on
  the person's behalf, or an automation may send one on its own. Whether that
  is the person is undecided until real results show how often it happens; it
  is kept apart, counted separately, and the actor labeller leaves its
  entries unmatched.
