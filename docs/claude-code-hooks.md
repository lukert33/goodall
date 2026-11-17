# Claude Code hooks for goodall

Claude Code runs `goodall hook <event>` when each of its hooks fires. This
page shows the settings that make it do so, for a person setting up an eval
account by hand. `goodall onboard` will install them itself once it is
built; until then, nothing installs them.

## What each hook does

| Event | What goodall does | Can it stop Claude Code? |
| --- | --- | --- |
| `UserPromptSubmit` | The prompt gate. With a work block active, it records a heartbeat linking the Claude session and the prompt goes through. Otherwise it refuses the prompt and says what to run, usually `goodall start`. | Yes: it refuses the prompt. |
| `PostToolUse` | Records a heartbeat after each tool call, so a long reply still shows the work block was active, and copies the Claude session's transcript into the log directory. | Never. |
| `Stop` | Records a heartbeat when Claude finishes a reply, and copies the transcript. | Never. |
| `SessionEnd` | Records a heartbeat when the Claude session ends, and copies the transcript. | Never. |
| `SessionStart` | Records a heartbeat when a Claude session starts or resumes, linking it to the work block before the first prompt. | Never. |

Heartbeats are recorded only while a work block is active. Transcripts are
copied whether or not one is, byte for byte, into `transcripts/` in the log
directory, in the same folder layout as Claude Code's own `projects` folder
(ADR 0015). Goodall copies everything Claude Code keeps for a Claude
session: the transcript and the whole of the session's folder beside it,
subagent transcripts and tool results included.
A Claude Code that is killed fires no hook, so `goodall start`, `goodall stop`
and `goodall status` also copy any transcript that changed since it was last
copied. A copy that fails inside a hook is silent in Claude Code and noted in
`transcript-sync/failed-copies.jsonl` in the log directory, and
`goodall status` reports it.

Goodall finds Claude Code's transcripts wherever Claude Code keeps them, with
nothing for you to set. The hooks run inside Claude Code, so they look where
it writes, and each one that copies notes that folder in
`transcript-sync/claude-projects-folders.json` in the log directory.
`goodall start`, `goodall stop` and `goodall status` then cover every folder
noted there, as well as `~/.claude/projects` and the folder your terminal's
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
gate even when that input cannot be read, and refuses the prompt rather than
letting it through.

## Exit codes Claude Code sees

- `0`: carry on. Every event except `UserPromptSubmit` always exits 0, even
  when goodall finds something wrong, so a problem in goodall never gets in
  the way of a tool call or keeps Claude from finishing a reply.
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
