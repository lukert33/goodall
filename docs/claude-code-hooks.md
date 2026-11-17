# Claude Code hooks for goodall

Claude Code runs `goodall hook <event>` when each of its hooks fires. This
page shows the settings that make it do so, for a person setting up an eval
account by hand. `goodall onboard` will install them itself once it is
built; until then, nothing installs them.

## What each hook does

| Event | What goodall does | Can it stop Claude Code? |
| --- | --- | --- |
| `UserPromptSubmit` | The prompt gate. With a work block active, it records a heartbeat linking the Claude session and the prompt goes through, even when it cannot read what Claude Code sent (see below). Otherwise it refuses the prompt and says what to run, usually `goodall start`. | Yes: it refuses the prompt. |
| `PostToolUse` | Records a heartbeat after each tool call, so a long reply still shows the work block was active, and copies the Claude session's transcript into the log directory. | Never. |
| `Stop` | Records a heartbeat when Claude finishes a reply, and copies the transcript. | Never. |
| `SessionEnd` | Records a heartbeat when the Claude session ends, and copies the transcript. | Never. |
| `SessionStart` | Records a heartbeat when a Claude session starts or resumes, linking it to the work block before the first prompt. | Never. |

Heartbeats are recorded only while a work block is active. Transcripts are
copied whether or not one is, byte for byte, into `transcripts/` in the log
directory, in the same folder layout as Claude Code's own `projects` folder.
A Claude Code that is killed fires no hook, so `goodall start`, `goodall stop`
and `goodall status` also copy any transcript that changed since it was last
copied. A copy that fails inside a hook is silent in Claude Code and noted in
`transcript-sync/failed-copies.jsonl` in the log directory, and
`goodall status` reports it.

Goodall finds Claude Code's transcripts in `~/.claude/projects`, or in
`projects` inside the folder `CLAUDE_CONFIG_DIR` names when that Claude Code
setting is set. If you set it, set it in your terminal too, so
`goodall status` looks in the same place as the hooks.

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
  `hook-problems/hook-problems.jsonl` in the log directory;
- shows you one sentence inside Claude Code, through the hook's JSON
  `systemMessage`, saying the prompt was sent but not linked and to tell the
  researcher; and
- warns once in the next `goodall status`, `goodall start` or `goodall stop`,
  then files the note in `hook-problems/reported/`.

The Claude session's transcript is still copied by the sweep. Linking it to
the work block afterwards is done by time: its prompts fall inside the work
block, at the moments noted. No goodall command does that yet.

With no work block active, unreadable details change nothing: the prompt is
refused as usual. An unexpected error inside the hook still refuses the
prompt.

## Exit codes Claude Code sees

- `0`: carry on. Every event except `UserPromptSubmit` always exits 0, even
  when goodall finds something wrong, so a problem in goodall never gets in
  the way of a tool call or keeps Claude from finishing a reply.
  `UserPromptSubmit` exits 0 when it lets a prompt through; if it could not
  read what Claude Code sent, it also prints one JSON object,
  `{"systemMessage": "..."}`, on standard output and nothing else, because
  Claude Code adds any other text there to Claude's context.
- `2`: refuse this prompt, with the reason on standard error. Only
  `UserPromptSubmit` returns it. This is Claude Code's code for refusing a
  prompt; goodall's own commands use 3 for "the prompt gate is closed"
  (AGENTS.md), but Claude Code would let a prompt through on 3.
- `1`: the hook command is missing its event name. Claude Code shows the
  message and carries on.

## Checking it works

In the eval account, with no work block active, send Claude Code any prompt.
It should be refused with a message telling you to run `goodall start`. Run
`goodall start`, send the prompt again, and it should go through.
