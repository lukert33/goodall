# Record schemas

This page describes the files goodall and contributors write into an eval's
record repo: **work block records**, **journal entries** and
**interventions**. These are goodall's own records, the middle of the three
layers in ADR 0003. Raw records (transcripts, telemetry) are kept elsewhere,
byte for byte, and are not described here.

The code that builds, writes, reads and checks these files is
`src/goodall/records.py`. The three examples on this page are read by the
tests, so if this page and the code disagree, a test fails.

> **Provisional.** Every shape on this page is a first draft, chosen so that
> work can start, and is expected to change once real evals use it. Each file
> carries `schema_version` so a later goodall can recognise older files and
> migrate them. To change a shape: change the code and this page together,
> and raise `schema_version` if files already written would no longer read.

## Where records live

One file per record, so two contributors never edit the same file and git
never has to merge one.

```
<record repo>/
├── eval.toml
├── CONSTRUCT.md
├── .env.shared
├── work-blocks/
│   └── 20261116T090000Z-ada-lovelace-0a1b2c3d.json
├── journal/
│   └── 20261116T100500Z-ada-lovelace-7f3a9c2e.md
└── interventions/
    └── 20261116T112013Z-ada-lovelace-1c2d3e4f.md
```

### File names

A file's name is its record's `id` plus `.json` or `.md`. An id has three
parts joined by hyphens:

| Part | Example | Why it is there |
| --- | --- | --- |
| UTC time, `YYYYMMDDTHHMMSSZ` | `20261116T090000Z` | First, and fixed-width, so a folder listing sorts records in time order. |
| Contributor id | `ada-lovelace` | Two contributors writing in the same second get different names. |
| Eight random hexadecimal characters | `0a1b2c3d` | The same contributor writing twice in one second (two terminals, two agents) gets different names. |

The time comes from `started_at` for a work block, `written_at` for a journal
entry and `occurred_at` for an intervention, so an intervention written down
later still sorts where it happened.

The name is safe on macOS disks: it has no colon (Finder shows a colon as a
slash), no space, and nothing but lowercase letters, digits and hyphens apart
from the fixed `T` and `Z`, so two names cannot differ only by case on a
case-insensitive disk. The longest possible name is about 100 characters, well
under the 255-byte limit.

## Fields every record has

| Field | Required | Value |
| --- | --- | --- |
| `schema_version` | yes | `1` |
| `record_type` | yes | `work_block`, `journal_entry` or `intervention`. Makes a file self-describing if it is moved out of its folder. |
| `id` | yes | The record id described above; the file name without its extension. |
| `contributor` | yes | The contributor id from the roster in `eval.toml`: lowercase letters, digits and hyphens, at most 64 characters. A contributor is always a person; agents are never contributors. |

Timestamps are always UTC, in whole seconds, written `2026-11-16T09:30:00Z`.
A reference to a work block is that work block's `id`.

## Work block record (JSON)

A logged stretch of one contributor's work on one workstream. It is data that
goodall writes, so it is JSON. Folder: `work-blocks/`.

| Field | Required | Value |
| --- | --- | --- |
| `workstream` | yes | One of the `workstreams` in `eval.toml`. |
| `started_at` | yes | When the work block started. |
| `ended_at` | no | When it ended; `null` while it is still running. |
| `end_state` | no | How it ended: `clean` (closed by `goodall stop`), `abnormal` (ended by something going wrong), or `inferred` (nobody closed it, so its end was taken from its last heartbeat). `null` while running. Set together with `ended_at`: both or neither. |
| `machine.hostname` | yes | The machine's name, as observed. |
| `machine.macos_version` | no | As observed, for example `26.1`; `null` on a system that is not macOS. |
| `machine.goodall_version` | yes | The goodall that wrote the record. |
| `machine.claude_code_version` | no | `null` if Claude Code was not found. |
| `claude_sessions` | yes | The Claude Code session ids of the Claude sessions that fell inside the work block; `[]` if none. |

The machine facts are observed, never configured (ADR 0009). There is no
billing field: goodall does not track billing.

<!-- example: work_block -->
```json
{
  "schema_version": 1,
  "record_type": "work_block",
  "id": "20261116T090000Z-ada-lovelace-0a1b2c3d",
  "contributor": "ada-lovelace",
  "workstream": "research",
  "started_at": "2026-11-16T09:00:00Z",
  "ended_at": "2026-11-16T12:15:42Z",
  "end_state": "clean",
  "machine": {
    "hostname": "eval-mac-01",
    "macos_version": "26.1",
    "goodall_version": "0.0.1",
    "claude_code_version": "2.3.4"
  },
  "claude_sessions": [
    "0f6c1d2e-3a4b-4c5d-8e9f-a0b1c2d3e4f5",
    "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"
  ]
}
```

## Journal entry (Markdown)

A contributor's written note about what was decided or done, and by whom.
Folder: `journal/`. The file is a metadata header followed by the entry in
Markdown. It is the person's own testimony, so `goodall journal` refuses to
run from inside Claude Code (an agent's work is in its transcript).

| Field | Required | Value |
| --- | --- | --- |
| `written_at` | yes | When the entry was written. |
| `work_block` | no | The id of the work block it belongs to. Left out when written outside one. |
| body | yes | Everything after the header: the entry itself, in Markdown. Must not be empty. |

<!-- example: journal_entry -->
````markdown
+++
schema_version = 1
record_type = "journal_entry"
id = "20261116T100500Z-ada-lovelace-7f3a9c2e"
contributor = "ada-lovelace"
written_at = 2026-11-16T10:05:00Z
work_block = "20261116T090000Z-ada-lovelace-0a1b2c3d"
+++

# Chose Postgres over SQLite

The agent proposed Postgres for the job queue. I agreed after reading its
comparison, because two workers will write at once.

Decided by me; the comparison was written by the agent.
````

## Intervention (Markdown)

A moment when a person overrode, corrected, redirected or rescued an agent,
or had to act because the agent could not. Folder: `interventions/`.
Like a journal entry it is the person's own testimony, so `goodall intervene`
refuses to run from inside Claude Code.

| Field | Required | Value |
| --- | --- | --- |
| `occurred_at` | yes | When it happened. |
| `written_at` | yes | When it was written down; not before `occurred_at`. |
| `work_block` | no | The id of the work block it happened in. |
| `claude_session` | no | The Claude Code session id of the Claude session it happened in, a UUID. |
| `category` | yes | One of the `intervention_categories` in `eval.toml`. The defaults are `policy-mandated`, `infrastructure`, `agent-limitation` and `freely-delegable`. |
| `decision` | yes | What the person decided: `approve`, `modify`, `reject`, `escalate` or `terminate` (see below). |
| `interrupted` | no | Set only when the person cut the agent short: what the agent was doing, `generate` (writing its reply), `tool_call` (running a tool) or `between_turns`. |
| `question_outcome` | no | Set only when the intervention was the person answering a question the agent asked: `accepted` (answered), `declined` (refused to answer) or `cancelled` (dismissed it). |
| body | yes | What happened and why, in Markdown. Must not be empty. |

Choosing a `decision` (these are Inspect's approval decisions, read for a
person working beside an agent):

| Decision | Use it when the person… |
| --- | --- |
| `approve` | reviewed what the agent was about to do and let it go ahead, where the review itself is worth recording (for example a check that policy requires). |
| `modify` | changed what the agent did or was about to do: corrected or redirected it. |
| `reject` | refused one action, and the agent carried on otherwise. |
| `escalate` | took a step over because the agent could not do it, or handed it to someone else. |
| `terminate` | stopped the agent's work on the task altogether. |

<!-- example: intervention -->
````markdown
+++
schema_version = 1
record_type = "intervention"
id = "20261116T112013Z-ada-lovelace-1c2d3e4f"
contributor = "ada-lovelace"
occurred_at = 2026-11-16T11:20:13Z
written_at = 2026-11-16T11:24:00Z
work_block = "20261116T090000Z-ada-lovelace-0a1b2c3d"
claude_session = "5b8e2a71-9c04-4d3f-b6a2-17e0c9d4f8a3"
category = "agent-limitation"
decision = "modify"
interrupted = "tool_call"
+++

The agent tried to make a failing test pass by deleting its assertion.
I stopped it mid-edit and told it to fix the date parsing instead.
````

## The metadata header: TOML between `+++` lines

Journal entries and interventions start with a header of fields written in
TOML, between two lines holding only `+++`. Everything after the second `+++`
is the body.

Why TOML rather than the more common YAML between `---` lines:

- Python's standard library reads TOML (`tomllib`) but not YAML. Goodall
  adds no dependency for this, and the code that reads records stays safe to
  run from a hook (ADR 0005).
- The eval config and local settings are already TOML, so a contributor
  learns one syntax.
- TOML has a native timestamp, so `written_at = 2026-11-16T10:05:00Z` is read
  as a time, not as text.
- `+++` is the usual marker for a TOML header (Hugo and other static site
  tools use it), so editors and site generators recognise it.

The cost: some Markdown tools show a TOML header as text instead of hiding
it, and the standard library cannot write TOML. Goodall therefore has a
writer of its own, in `records.py`, that covers only what headers hold: whole
numbers, text and UTC timestamps. A field with no value is left out of the
header, because TOML has no `null`.

## What never goes in a record

Records are meant to be publishable after redaction, so:

- **No email addresses.** People are named by contributor id; display names
  live in the roster in `eval.toml`.
- **No secrets and no secret references.** Values that look like an API key,
  token or private key are refused, and the problem message never repeats
  them.
- **No billing.** Goodall does not track billing.

Validation checks every field, and every line of a body, for email addresses
and common secret shapes. This is a safety net, not a guarantee: the hostname
in a work block record, for instance, may still name a person and may need
redacting before publication.

## Mapping to Inspect events

Field values are borrowed from Inspect's event types so that an export can
map goodall's records to Inspect events without translation tables. Goodall
does not import Inspect. Checked against the `inspect_ai` source
(`src/inspect_ai/event/`, main branch, commit `bf036e4`) on 2026-11-16.

| goodall record and field | Inspect event and field | Notes |
| --- | --- | --- |
| **Work block record** | `SpanBeginEvent` + `SpanEndEvent` | A work block is a span: a named stretch that other events fall inside. |
| `id` | `SpanBeginEvent.id`, `SpanEndEvent.id` | |
| `record_type` | `SpanBeginEvent.type` = `"work_block"` | |
| `workstream` | `SpanBeginEvent.name` | |
| `started_at` | `SpanBeginEvent.timestamp` | |
| `ended_at` | `SpanEndEvent.timestamp` | No `SpanEndEvent` while the work block is running. |
| `end_state`, `contributor`, `machine`, `claude_sessions` | `metadata` | Inspect has no field for these. `claude_sessions` says which transcripts (Inspect samples) the span covers. |
| **Journal entry** | `InfoEvent` | |
| `record_type` | `InfoEvent.source` = `"goodall/journal_entry"` | |
| `written_at` | `InfoEvent.timestamp` | |
| body | `InfoEvent.data` | |
| `work_block` | `span_id` | |
| `id`, `contributor` | `metadata` | |
| **Intervention** | `ApprovalEvent`, plus `InterruptEvent` when `interrupted` is set, plus `InputEvent` when `question_outcome` is set | |
| `decision` | `ApprovalEvent.decision` | Same five values. |
| `contributor` | `ApprovalEvent.approver` | |
| body | `ApprovalEvent.explanation` | |
| `occurred_at` | `timestamp` of each event | |
| `interrupted` | `InterruptEvent.interrupted` | Same three values. `InterruptEvent.source` is `"user_cancel"`. |
| `question_outcome` | `InputEvent.outcome` | Same three values. |
| `work_block` | `span_id` | |
| `claude_session` | the sample the events belong to | Picks which transcript the events are added to. |
| `id`, `category`, `written_at` | `metadata` | |

Gaps an export will have to decide: `ApprovalEvent` requires the tool call
being approved (`call`) and the model's `message`, which goodall does not
record (an intervention need not be about a tool call), and `InputEvent`
requires the input text (`input`, `input_ansi`), for which the body is the
nearest thing goodall has.

## Problems and how they are reported

Reading a file never stops with an error. `parse_work_block_record`,
`parse_journal_entry` and `parse_intervention` return the record (when one
could be built) and a list of problems; `validate_record` returns a list of
problems, and can also check the contributor, workstream and category
against the eval config. Each problem names the field and says what to
change, for example:

- `contributor is missing; add it.`
- `decision must be one of approve, modify, reject, escalate, terminate; choose the one that fits.`
- `contributer is not a field of this record; remove it or correct its spelling.`
- `schema_version is 2, so this record was written by a newer goodall (this one understands 1); upgrade goodall.`
