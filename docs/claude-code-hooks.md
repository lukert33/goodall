# Claude Code hooks for goodall

Claude Code runs `goodall hook <event>` when each of its hooks fires. This
page shows the settings that make it do so, for a person setting up an eval
account by hand. `goodall onboard` will install them itself once it is
built; until then, nothing installs them.

## What each hook does

| Event | What goodall does | Can it stop Claude Code? |
| --- | --- | --- |
| `UserPromptSubmit` | The prompt gate. With a work block active, it records a heartbeat linking the Claude session and the prompt goes through, even when it cannot read what Claude Code sent (see below). Otherwise it refuses the prompt and says what to run, usually `goodall start`. | Yes: it refuses the prompt. |
| `PostToolUse` | Records a heartbeat after each tool call, so a long reply still shows the work block was active. | Never. |
| `Stop` | Records a heartbeat when Claude finishes a reply. | Never. |
| `SessionEnd` | Records a heartbeat when the Claude session ends. | Never. |
| `SessionStart` | Records a heartbeat when a Claude session starts or resumes, linking it to the work block before the first prompt. | Never. |

Heartbeats are recorded only while a work block is active.

The table describes **dedicated account mode**, which is what goodall uses
unless local settings say otherwise. In **mixed account mode**
(`account_mode = "mixed"` in local settings, ADR 0021) the prompt gate is
off; see [When the prompt gate refuses a prompt](#when-the-prompt-gate-refuses-a-prompt).

Hooks copy nothing. Agent sessions are copied when a work block ends, the
same way for every source application (ADR 0019): `goodall stop`, or the
next `goodall start` or `goodall status` after a work block nobody stopped,
copies every Claude session that has a record inside the work block, byte
for byte, into `transcripts/` in the log directory, in the same folder
layout as Claude Code's own `projects` folder (ADR 0015). Goodall copies
everything Claude Code keeps for such a Claude session: the transcript and
the whole of the session's folder beside it, subagent transcripts and tool
results included, and its project's memory (`MEMORY.md` and `memory/`).
From the rest of Claude Code's folder it copies two things and nothing
else: the prompt history, `history.jsonl`, and the Claude session's
file-edit backups, `file-history/<session id>/`. These go into
`claude-config/` in the log directory, in a folder named after Claude
Code's folder (`-Users-ada--claude` for `/Users/ada/.claude`), at the same
paths as in Claude Code's folder. Settings, credentials and everything else
there are never read. A copy that fails is a warning, and goodall tries
again each time you run `goodall start`, `goodall stop` or `goodall status`.
What the copied record is known to lack is in `docs/source-applications.md`.

Goodall finds Claude Code's session files wherever Claude Code keeps them,
with nothing for you to set. The hooks run inside Claude Code, so they know
where it writes, and every hook but the prompt gate notes that folder in
`transcript-sync/claude-config-folders.json` in the log directory.
`goodall start`, `goodall stop` and `goodall status` then look in every
folder noted there, as well as `~/.claude` and the folder your terminal's
own settings point to.

## The settings

Add this to the eval account's Claude Code user settings,
`~/.claude/settings.json`, merging it with any `hooks` already there.
Replace `/Users/ada/.local/bin/goodall` with the full path that
`command -v goodall` prints in that account. A full path is used because
Claude Code may run hooks without the folders your terminal adds to `PATH`;
if it cannot find `goodall`, the prompt gate cannot run.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "/Users/ada/.local/bin/goodall hook UserPromptSubmit" }] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [{ "type": "command", "command": "/Users/ada/.local/bin/goodall hook PostToolUse" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "/Users/ada/.local/bin/goodall hook Stop" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "/Users/ada/.local/bin/goodall hook SessionEnd" }] }
    ],
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "/Users/ada/.local/bin/goodall hook SessionStart" }] }
    ]
  }
}
```

Each command names its event on purpose. Goodall reads the event from the
command, not from what Claude Code sends, so the prompt gate knows it is the
gate even when that input cannot be read, and still refuses the prompt when
no work block is active.

## When the prompt gate refuses a prompt

Which account mode applies is read from local settings on every prompt
(`account_mode`; left out, it means dedicated).

| What goodall finds | Dedicated account mode | Mixed account mode |
| --- | --- | --- |
| Local settings are missing or cannot be read | Refused | Refused: without them goodall cannot know the mode, and dedicated is the safe assumption |
| The log directory cannot be reached | Refused | Goes through, nothing recorded |
| No work block is active | Refused | Goes through, nothing recorded |
| An unexpected error inside goodall | Refused | Goes through, nothing printed |
| A work block is active | Goes through, with a heartbeat | The same |
| A work block is active but what Claude Code sent cannot be read | Goes through, with a notice (next section) | The same |
| A work block is active but its telemetry receiver has stopped | Goes through, with a notice (below) | The same |

In mixed account mode the macOS user account is also used for things that
are not the eval, so goodall never stops a prompt for want of a work block,
and outside a work block the hooks write nothing at all: no heartbeat, no
note of a hook problem, not even where Claude Code keeps its files. While a
work block is active the hooks behave exactly as in dedicated account mode.
Work done outside a work block is simply not recorded.

## When goodall cannot read what Claude Code sends

A newer Claude Code may change the details it sends with each hook. So that
this never blocks every prompt in an eval account until goodall is updated,
the prompt gate lets a prompt through, while a work block is active, even
when it cannot read those details (nothing sent, not JSON, no Claude session
id, or one that is not a session id). In that case goodall:

- records a heartbeat that names no Claude session, so the work block still
  shows activity;
- notes the moment, the event, the work block and which of those four went
  wrong (never anything Claude Code sent, which holds your prompt) in
  `hook-problems/hook-problems.ndjson` in the log directory;
- shows you one sentence inside Claude Code, through the hook's JSON
  `systemMessage`, saying the prompt was sent but not linked and to tell the
  researcher; and
- warns once in the next `goodall status`, `goodall start` or `goodall stop`,
  then files the note in `hook-problems/reported/`.

The Claude session is still copied when the work block ends, because it has
a record inside the work block. Listing it in the work block's record
afterwards is done by time: its prompts fall inside the work block, at the
moments noted. No goodall command does that yet.

With no work block active, unreadable details change nothing: the prompt is
refused as usual. An unexpected error inside the hook still refuses the
prompt. (In mixed account mode neither refuses it; see the table above.)

## When goodall's telemetry receiver has stopped

`goodall start` also starts goodall's telemetry receiver for the work block,
and shows the port it listens on (choose one with `--telemetry-port`).
Claude Code drops its telemetry silently when nothing is listening, so the
prompt gate checks, before each prompt, that the receiver is still running.
It does so without running any program or making any network call: the
receiver holds a lock on its receipt log for as long as it runs, and the
hook only tries that lock. When the receiver has stopped, the prompt still
goes through, and goodall:

- notes the moment in `work-blocks/receiver-down/<work block id>.jsonl` in
  the log directory;
- the first time only, shows you one sentence inside Claude Code, through
  `systemMessage`, saying to run `goodall start`, which starts the receiver
  again on the same port without starting a new work block;
- warns in `goodall status`; and
- records the work block's telemetry as `incomplete` (or `missing`, if none
  arrived at all) when it ends.

Transcripts are copied as usual throughout.

## Exit codes Claude Code sees

- `0`: carry on. Every event except `UserPromptSubmit` always exits 0, even
  when goodall finds something wrong, so a problem in goodall never gets in
  the way of a tool call or keeps Claude from finishing a reply.
  `UserPromptSubmit` exits 0 when it lets a prompt through; if it could not
  read what Claude Code sent, or has just found the telemetry receiver
  stopped, it also prints one JSON object,
  `{"systemMessage": "..."}`, on standard output and nothing else, because
  Claude Code adds any other text there to Claude's context.
- `2`: refuse this prompt, with the reason on standard error. Only
  `UserPromptSubmit` returns it. This is Claude Code's code for refusing a
  prompt; goodall's own commands use 3 for "the prompt gate is closed"
  (AGENTS.md), but Claude Code would let a prompt through on 3.
- `1`: neither the hook command nor what Claude Code sent names an event
  goodall handles. Goodall prints nothing, so nothing reaches Claude, and
  Claude Code carries on. The moment is noted in
  `hook-problems/hook-problems.ndjson` in the log directory (when goodall is
  set up and the log directory can be reached), and the next
  `goodall status`, `start` or `stop` warns once that the hook settings need
  checking against this page.

A command written as a bare `goodall hook`, with no event, still works: goodall
takes the event from `hook_event_name` in what Claude Code sends, and behaves
exactly as if it had been named. When both name an event and they disagree,
the command line wins. A command naming an event goodall does not handle,
such as `goodall hook PreToolUse`, leaves a heartbeat, exits 0 silently, and
is noted the same way.

## Checking it works

`goodall doctor` checks the user settings above: that each of the five
events has a `goodall hook <event>` command, that hooks are not turned off
with `disableAllHooks`, and that the goodall each command names starts
(it runs `goodall --version`). It reads only `hooks` and `disableAllHooks`
from the file and never shows anything written there. Hooks in a project's
own `.claude` folder do not count, because they apply only inside that
project.

In the eval account, with no work block active, send Claude Code any prompt.
It should be refused with a message telling you to run `goodall start`. Run
`goodall start`, send the prompt again, and it should go through.

In mixed account mode the first prompt goes through too. To check the hooks
there, run `goodall start`, send a prompt, and look for the Claude session
under `claude_sessions` in `goodall status --json`.
