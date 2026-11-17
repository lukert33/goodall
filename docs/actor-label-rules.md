# Actor labelling rules, version 2

These are the rules `goodall.actor_labels` uses to give every entry of one
Claude Code transcript an actor label: `human` (the person), `ai` (a model) or
`harness` (the software around the model). Each rule is one item in
`ACTOR_LABEL_RULES` in `src/goodall/actor_labels.py`. Change a rule there and
here in the same commit, and raise `ACTOR_LABEL_RULES_VERSION` (ADR 0004).

The person's actions are not detected here: rules 1 to 4a ask
`goodall.human_actions` (see [human-action-rules.md](human-action-rules.md))
which lines hold one, so the two derived records always agree. Raise this
version too when those rules change.

Everything here is provisional. Observed against Claude Code 2.1.x
transcripts in November 2026.

## What comes back

`label_transcript(path)` returns:

- `labels`: one `EntryLabel` per entry, in transcript order, with
  `line_number`, `entry_id` (the entry's `uuid`, or none), `entry_type` (the
  entry's `type`), `actor_label`, `rule_name`, `mixed` and `identity`.
- `unmatched`: one `UnmatchedEntry` per line no rule decided, with
  `line_number`, `entry_id`, `entry_type` and a `reason` that names no
  content: a line that is not JSON or not an object, an entry with no type or
  an unknown type, or a known type no rule matched (with its `origin.kind`, if
  any).
- `rules_version` and `human_action_rules_version`.

Every non-blank line is in exactly one of the two lists. Blank lines hold no
entry and are skipped. Labelling always carries on past a bad line.

`summarise_labels(result)` counts entries per label, per rule (every rule
listed, zero when unused), mixed entries and unmatched entries.

## Three actors, with identity kept apart

There are three actor labels and only three: humans are humans, the harness
is the harness and agents are agents, whichever person, program or agent it
was inside each. Which one, when the raw entry says so, goes in `identity`
beside the label, as a tuple of `what:value` parts (`identity_of`):

- `model:<message.model>` on an `ai` assistant entry;
- `subagent:<agentId>` on an `ai` assistant entry on a sidechain;
- `peer:<name>` or `coordinator:<name>` on another agent's message, from
  `origin.name` or else `origin.from`.

Nothing is inferred: when the entry does not say, `identity` is empty. A
`human` entry never has one, because a transcript does not say which person
typed.

## Mixed entries

One entry gets one label: the actor who produced what the entry is for, its
**primary producer**. When the entry also holds words from another actor, it
is marked `mixed`. Whether a rule's entries are mixed is fixed by the rule, so
the list below says it for each. In version 2 the mixed entries are all
`human`: a typed prompt with harness reminders in front, and the person's
commands, decisions, refusals and interruptions, which the harness writes up
in its own tags and fixed words.

## Rules

Applied in order; the first rule that matches decides. Example shapes are
synthetic and trimmed to the keys the rule looks at.

1. **`person_typed_prompt`** → `human`. The person typed this prompt, and the
   harness added nothing. Decided by `goodall.human_actions` (typed prompt).

   `{"type":"user","message":{"content":"Please write an example function."}}`

2. **`person_typed_prompt_behind_harness_context`** → `human`, mixed. As
   rule 1, but the text begins with `<system-reminder>` blocks the harness put
   in front. The person's words are why the entry exists.

   `{"type":"user","message":{"content":[{"type":"text","text":"<system-reminder>...</system-reminder>"},{"type":"text","text":"Make it return a number."}]}}`

3. **`person_rejected_tool_call`** → `human`, mixed. The person refused a tool
   call. The harness wrote the result, but the decision, and any feedback, is
   the person's.

   `{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"The user doesn't want to proceed with this tool use. ... the user said:\nKeep the file."}]}}`

4. **`person_interrupted`** → `human`, mixed. The person stopped the agent.
   The harness recorded it as a fixed marker or a cancelled tool result.

   `{"type":"user","message":{"content":[{"type":"text","text":"[Request interrupted by user]"}]}}`

4a. **The person's other actions** → `human`. One rule per kind of human
   action, each decided by `goodall.human_actions`, each named for what the
   person did so labels can be regrouped with the actions:

   | rule | human action kind | mixed | why mixed |
   |---|---|---|---|
   | `person_ran_shell_command` | `shell_command` | yes | wrapped in harness tags, often with the output |
   | `person_typed_slash_command` | `slash_command` | yes | name and arguments wrapped in harness tags |
   | `person_approved_tool_call` | `tool_call_approval_once`, `tool_call_approval_always` | yes | the entry also holds the tool's result |
   | `person_dismissed_permission_prompt` | `permission_prompt_dismissal` | yes | the harness wrote the refusal |
   | `person_answered_question` | `question_answer` | yes | also holds the model's questions |
   | `person_approved_plan` | `plan_approval` | yes | the harness wrote the approval up |
   | `person_queued_prompt` | `queued_prompt` (an `attachment` entry) | no | |
   | `person_prompt_blocked_by_hook` | `blocked_prompt` (a `system` entry) | yes | the hook's warning |

   A tool result carrying an approval is labelled `human`, not `harness`,
   because the entry exists only because the person let the call run; most of
   its words are the tool's output, which is why it is mixed.

4b. **`another_agent_sent_message`** → `ai`. A user entry whose `origin.kind`
   is `peer` (an agent in another Claude session) or `coordinator` (an agent
   coordinating this one). Such entries are usually `isMeta`, so this rule
   comes before rule 9. `identity` names the sender when the origin does.

   `{"type":"user","isMeta":true,"origin":{"kind":"peer","name":"example-peer"},"message":{"content":"..."}}`

5. **`harness_wrote_assistant_entry`** → `harness`. An assistant entry Claude
   Code wrote without calling a model: `message.model` is `<synthetic>` or
   `isApiErrorMessage` is true (API errors, for example). Must come before
   rule 6.

   `{"type":"assistant","isApiErrorMessage":true,"message":{"model":"<synthetic>","content":[...]}}`

6. **`model_wrote_assistant_entry`** → `ai`. Any other assistant entry with
   message content: the model's words, its thinking, or the tool calls it
   chose. Subagents' assistant entries (sidechains) count too.

   `{"type":"assistant","message":{"model":"example-model","content":[{"type":"text","text":"..."}]}}`

7. **`harness_returned_tool_results`** → `harness`. A user entry carrying tool
   results that rules 3 and 4 did not claim: the harness ran the tool and
   reported back.

   `{"type":"user","message":{"content":[{"type":"tool_result","is_error":false,"content":"File written."}]}}`

8. **`interruption_marker_closing_a_rejection`** → `harness`. The
   interruption marker Claude Code writes straight after a refusal.
   `goodall.human_actions` counts it as part of the refusal, not a second act
   by the person, so it is the harness closing the refusal.

   `{"type":"user","parentUuid":"<the refusal's uuid>","message":{"content":[{"type":"text","text":"[Request interrupted by user for tool use]"}]}}`

9. **`harness_injected_user_entry`** → `harness`. A user entry the harness
   wrote in the person's place: `isMeta` or `isCompactSummary` true,
   `origin.kind` of `task-notification`, text beginning with a wrapper tag
   (`<command-name>`, `<local-command-stdout>`, `<bash-input>` and the others
   in `claude_code_markers()`), or text that is only reminder blocks.

   `{"type":"user","isMeta":true,"message":{"content":"<local-command-caveat>...</local-command-caveat>"}}`

10. **`agent_instructed_subagent`** → `ai`. A sidechain user entry with text
    and no tool results, from no origin or a `human` origin: the main agent's
    model wrote these instructions to start a subagent.

    `{"type":"user","isSidechain":true,"message":{"content":"Example instructions for a subagent."}}`

11. **`bookkeeping_entry`** → `harness`. Any other known entry type
    (`attachment`, `system`, `file-history-snapshot`, `custom-title`,
    `queue-operation` and the rest of `known_entry_types`): Claude Code keeps
    track of the Claude session, and no one says anything in it.

    `{"type":"file-history-snapshot","messageId":"...","snapshot":{}}`

A `user` or `assistant` entry no rule matches (one with no message content,
for example) is reported as unmatched, not guessed.

A prompt a program sent through the SDK (human action kind `sdk_prompt`) is
reported as unmatched before any rule is tried, with the reason "prompt sent
through the SDK: whether that is the person is undecided". See "Undecided,
waiting for evidence" in [human-action-rules.md](human-action-rules.md).

## Known to be wrong or unknowable in version 2

- **`custom-title`** entries may record a name the person chose, and
  **`ai-title`** a title a model wrote; both are labelled `harness` as
  bookkeeping.
- **Compaction summaries** (`isCompactSummary`) are written by a model but
  injected by the harness; labelled `harness`.
- **Messages from other sessions or a coordinator** (`origin.kind` `peer` or
  `coordinator`) are labelled `ai` (rule 4b). The entry does not prove that
  no person typed the message in the other session; the owner's direction is
  that what arrives from another agent is an agent's.
- **Unfamiliar parts of a labelled entry** (an unknown content block beside a
  tool result, for example) do not change its label; `goodall.human_actions`
  reports them in its own `unfamiliar_entries`.
- **Which person.** A `human` label never names a person: a transcript does
  not say who typed. It is the person at this eval account, with no claim
  that it is the contributor who usually uses it.
