# Source applications: what goodall copies, and what each record is known to lack

A source application is an application whose agent sessions goodall
captures. Applications that write the same files form an app family
(`GLOSSARY.md`). For every app family the baseline is the same (ADR 0019):
when a work block ends, goodall copies, byte for byte, each agent session
that has a record inside the work block, in the application's own layout.
Goodall reconstructs nothing, so what a record lacks stays lacking. This
page says what that is, one family at a time.

| App family | Source applications | Copied to, in the log directory | Code |
| --- | --- | --- | --- |
| Claude | The Claude desktop app's Code tab; Claude Code in a terminal | `transcripts/`, `claude-config/` | `src/goodall/claude_family.py` |

The Antigravity family (`goodall-fs9.24`) and the Codex family
(`goodall-fs9.25`) are not built yet. Each adds a row here and a section
below, and a module that turns its copies into timeline entries
(`docs/timeline.md`).

## The Claude family

Both source applications write the same files in Claude Code's config
folder, `~/.claude` unless `CLAUDE_CONFIG_DIR` names another:
`projects/<project folder>/<session id>.jsonl`, one JSON record on each
line, with a folder `<session id>/` beside it for subagent transcripts
(`subagents/`), tool results (`tool-results/`) and a few small files. What
goodall copies, and where, is listed at the top of
`src/goodall/claude_family.py`.

The structure below was observed in the development account on 2026-11-26:
key names, record types and counts only, never content. 333 session files
held 485,685 records, dated from July to November 2026.

- **Which application wrote a record** is in its `entrypoint` field. The
  values seen were `claude-desktop`, `cli` and `sdk-cli`. Goodall copies
  all alike and reads the field nowhere.
- **When a record was written** is in its top-level `timestamp`, in UTC to
  the millisecond. This is the only field goodall reads when it decides
  whether an agent session belongs to a work block.

### What the Claude record is known to lack

- **A time on every record.** Most record types carry no `timestamp`:
  titles, modes, file-history snapshots and other bookkeeping lines. The
  `user`, `assistant`, `attachment` and `system` records all carry one.
  A record without one cannot be placed in time except by its position in
  the file, and does not count when goodall decides whether an agent
  session belongs to a work block.
- **The application on every record.** `entrypoint` is present on the
  `user`, `assistant`, `attachment` and `system` records and on no other
  type, about half of all records.
- **The model's reasoning in full, possibly.** `thinking` blocks are in the
  record with a `signature`. Whether their text is the model's full
  reasoning or a summary of it was not checked, and may differ by model
  and version.
- **The system prompt and tool definitions, possibly.** Record types named
  `api-request`, `api-request-shape` and `api-request-blob` were seen;
  their content was not read. Until someone checks with a synthetic
  session, treat the system prompt and the tool definitions as not known
  to be in the record.
- **Anything written after the copy.** Goodall copies when the work block
  ends. An agent still working after `goodall stop` writes records the
  copy does not hold. They are copied only if the agent session also has a
  record inside a later work block, because the whole file is copied again
  then.
- **Files Claude Code removed before the work block ended.** Claude Code
  deletes old session files by its own retention setting. A work block
  ends within hours, so this matters only for a session file from long
  before that is still being added to.
- **Sessions that are not on this Mac.** Cowork and plain chat in the
  Claude desktop app, and Claude Code sessions run in the cloud, are kept
  in the person's Claude account. Goodall makes no network calls and holds
  no credentials, so it does not capture them (ADR 0019).
- **What the person saw and did outside the agent session.** Which human
  actions a transcript can and cannot show is in
  `docs/human-action-rules.md`.

What only Claude Code adds on top of the baseline, through its hooks: the
prompt gate and heartbeats (`docs/claude-code-hooks.md`), and telemetry
(ADR 0002). None of them is needed for an agent session to be copied.
