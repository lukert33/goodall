# The Scout export, version 1

`goodall export scout` builds an Inspect Scout transcript database of one
eval. It is derived data (ADR 0003): every run deletes the last database
goodall built and builds a new one from the raw records and the record repo.
The mapping below is `EXPORT_RULES_VERSION` in
`src/goodall/scout_additions.py`; change both together and raise the version.

It needs the optional `scout` extra (inspect-ai and inspect-scout, see
DEPENDENCIES.md). Without it the command says how to install it, and nothing
else in goodall is affected.

## Where it goes

`<log directory>/exports/scout` unless `--output` names another folder.
goodall replaces only a folder holding its own `goodall-export.json`, and never
writes inside `transcripts/` or `superseded-transcripts/`. Open it with
`scout view -T <folder>`, or from Python with
`inspect_scout.transcripts_from("<folder>")`.

## What is in it

| Transcript | `source_type` | `transcript_id` | Holds |
|---|---|---|---|
| One per Claude session (or per part, when /clear split it) | `claude_code` | Scout's own | What Scout's `claude_code` importer made of `<log directory>/transcripts`, plus goodall's events and metadata below |
| One per work block | `goodall` | `goodall-work-block-<id>` | The work block, its journal entries and its interventions, as info events; no messages |
| Records outside work blocks | `goodall` | `goodall-outside-work-blocks` | Journal entries and interventions with no recorded work block, and prompts the prompt gate refused in Claude sessions Scout made no transcript of; only when there are any |

## Events goodall adds

Every added event has `goodall_actor_label` and other `goodall_` keys in its
metadata, and an id that stays the same from one export to the next.

| What | Inspect event | Notes |
|---|---|---|
| Tool call approved (`tool_call_approval_once`, `tool_call_approval_always`, `plan_approval`) | `ApprovalEvent`, decision `approve`, approver `human` | `call` is copied from Scout's tool event with the same id and placed just before it, in its span. When Scout kept no tool event, `call` has the tool's name and no arguments, and `goodall_tool_call_found` is false. |
| Tool call refused (`tool_call_rejection`, `permission_prompt_dismissal`) | `ApprovalEvent`, decision `reject` | `explanation` is what the person typed with the refusal, if anything. |
| Answer to the agent's question tool (`question_answer`) | `InputEvent`, outcome `accepted` | `input` and `input_ansi` are the answers, one per line. |
| Interruption | `InterruptEvent`, source `user_cancel` | Inspect has no value for a person pressing Escape; `user_cancel` is the nearest. |
| Every other human action, including `blocked_prompt` (refused by the prompt gate) | `InfoEvent`, source `goodall.human_action` | `data` holds the action's fields, as in human-action-rules.md. |
| Work block | `InfoEvent`, source `goodall.work_block` | At its start; `data.effective_workstream` applies any work block fix. Label `harness`. |
| Journal entry | `InfoEvent`, source `goodall.journal_entry` | At the time written. Label `human`. |
| Intervention | `InfoEvent`, source `goodall.intervention` | At the time it happened; also added to the Claude session it names. Not an approval event: it names no tool call. Label `human`. |

Scout's own events carry no actor label: the importer does not keep which
transcript line each came from.

## Metadata for filtering

Each key is a column Scout can filter on, for example
`columns.goodall_work_block == "<id>"`. Lists are stored as JSON text, so
filter them with `like`, for example
`columns.goodall_intervention_categories.like('%"agent-limitation"%')`.

| Key | On | Value |
|---|---|---|
| `goodall_eval` | all | The eval's name |
| `goodall_transcript_kind` | all | `claude_code`, `work_block` or `outside_work_blocks` |
| `goodall_work_block`, `goodall_work_blocks` | all | The one work block, or empty when there are none or several; and the list of all |
| `goodall_workstream`, `goodall_workstreams` | all | The same, for effective workstreams |
| `goodall_contributor`, `goodall_contributors` | all | The same, for contributors |
| `goodall_interventions`, `goodall_intervention_categories`, `goodall_intervention_decisions` | all | How many interventions, and their categories and decisions |
| `goodall_human_entries`, `goodall_ai_entries`, `goodall_harness_entries`, `goodall_unlabelled_entries` | Claude sessions | Entries of the session's own transcript by actor label (actor-label-rules.md) |
| `goodall_human_actions`, `goodall_human_actions_by_kind`, `goodall_refused_prompts`, `goodall_unfamiliar_entries` | Claude sessions | Human actions from the session and its subagents |
| `goodall_started_at`, `goodall_ended_at`, `goodall_end_state`, `goodall_claude_sessions`, `goodall_journal_entries` | work blocks | From the work block record |
| `goodall_actor_label_rules_version`, `goodall_human_action_rules_version`, `goodall_export_rules_version` | all | The rules that made the values |

## Not exported yet

Telemetry, ActivityWatch exports and subagent entries' actor labels.
