# Claude Code hooks for goodall

Claude Code runs `goodall hook <event>` when each of its hooks fires. This
page shows the settings that make it do so, for a person setting up an eval
account by hand. `goodall onboard` will install them itself once it is
built; until then, nothing installs them.

## What each hook does

| Event | What goodall does | Can it stop Claude Code? |
| --- | --- | --- |
| `UserPromptSubmit` | The prompt gate. With a work block active, it records a heartbeat linking the Claude session and the prompt goes through. Otherwise it refuses the prompt and says what to run, usually `goodall start`. | Yes: it refuses the prompt. |
| `PostToolUse` | Records a heartbeat after each tool call, so a long reply still shows the work block was active. | Never. |
| `Stop` | Records a heartbeat when Claude finishes a reply. | Never. |
| `SessionEnd` | Records a heartbeat when the Claude session ends. | Never. |
| `SessionStart` | Records a heartbeat when a Claude session starts or resumes, linking it to the work block before the first prompt. | Never. |

Heartbeats are recorded only while a work block is active. Transcript copying
will hang off the same `PostToolUse`, `Stop` and `SessionEnd` hooks
(goodall-fs9.6), so these settings will not need to change for it.

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
