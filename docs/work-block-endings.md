# How a work block ends

A work block is a logged stretch of one contributor's work on one workstream.
It begins when you run `goodall start`. This page says every way it can end,
including the ways goodall ends it for you, so that nothing about your record
is a surprise.

The short version: **goodall never ends a work block without telling you.**
It never ends one while you are working in it. When something other than
your own command starts or stops recording (signing in or out, a script or
schedule, or goodall closing a work block by itself), goodall shows a
notification saying so, with the reason and the workstream. A start or stop
you type, or choose from the menu bar icon, shows none, because you already
know.

Goodall closes a work block by itself only when it has been left idle, or
when it cannot read the block's marker, and only at the moment a goodall
command next runs: `goodall start`, `goodall stop`, `goodall pause` or
`goodall status`.

## The three ways

Every work block record carries an end state saying which of these happened.

| End state | What happened | When the block is counted as ending |
| --- | --- | --- |
| `clean` | You ran `goodall stop` or `goodall pause`. | The moment you ran it. |
| `inferred` | Nobody ran `goodall stop`, and the block sat idle for longer than the idle limit. Goodall closed it for you. | Its last heartbeat: the last moment goodall saw work in it. |
| `abnormal` | Goodall could not read the block's marker, so it could not tell whether the block was still running. It recorded what it could recover, keeps the damaged marker, and, if the marker no longer said which workstream, records the workstream as `unknown` for you to fix (below). | Its last heartbeat, or failing that the last time the marker file changed. |

## When goodall closes a work block for you

Goodall closes a work block on your behalf in one situation only.

1. **You started a work block and did not stop it.** You closed the laptop,
   went home, or simply forgot.
2. **Nothing happened in it for more than three hours.** "Something happening"
   means a heartbeat: goodall records one each time you send a prompt in
   Claude Code, each time Claude uses a tool, and each time Claude finishes a
   reply. Three hours is the idle limit.
3. **A goodall command then runs: `goodall start`, `goodall stop`,
   `goodall pause` or `goodall status`**, typed by you, chosen from the menu
   bar icon, or run by a script. Before doing its own job, that command
   notices the idle block and closes it.

The block is recorded as ending at its **last heartbeat**, not at the moment
you ran the command. A block you forgot at 5 pm and found the next morning is
recorded as ending at 5 pm, so the night is not counted as work. Its end state
is `inferred`.

Goodall tells you when it does this, in two places. The command that
noticed says, in words like these:

> Your work block on research, started Mon 16 Nov 2026, 14:05 GMT, was left
> running: nobody ran goodall stop, and nothing happened in it for more than
> 3 h. Goodall closed it at its last heartbeat, Mon 16 Nov 2026, 17:02 GMT,
> so the time after that is not counted as work.

And a notification appears, because the command may have been run by the
menu bar icon or a script, where nobody reads what it prints:

> **Recording stopped**
> On research. Nothing happened for more than 3 h, so goodall stopped
> recording at the last activity, Mon 16 Nov 2026, 17:02 GMT.

A work block closed because its marker could not be read (the `abnormal`
end state) shows a notification too.

## What this means for each command

- **`goodall status`** is not a look-only command. If it finds an idle work
  block it closes it, and if a finished work block's record is still waiting
  on this Mac it writes that record into the record repo and commits it. It
  says so each time. If nothing needs tidying, it only reports. It also
  lists any of your work blocks still waiting for a workstream (see "Fixing
  an unknown workstream" below).
- **`goodall stop`** run the morning after a forgotten block closes that
  block as `inferred`, at its last heartbeat, and exits successfully. It does
  not record the block as ending when you typed the command.
- **`goodall start`** closes an idle block first, then starts a new one.

## Notifications

Goodall shows a notification whenever recording starts or stops without
your own command. Each says "Recording started" or "Recording stopped",
the workstream, and why:

| What happened | The notification says |
| --- | --- |
| You signed in, and recording started | Recording started. On research, because you signed in. |
| A script or schedule ran `goodall start --background script` | Recording started. On research, started by a script or schedule. |
| You signed out, and recording stopped | Recording stopped. On research, because you signed out. |
| A script or schedule ran `goodall stop --background script` (or `goodall pause --background script`) | Recording stopped. On research, stopped (or paused) by a script or schedule. |
| Goodall closed a work block left idle | Recording stopped. On research. Nothing happened for more than 3 h, so goodall stopped recording at the last activity, … |
| Goodall closed a work block whose marker it could not read | Recording stopped. On research. goodall could not read what it was recording, so it stopped. … |

Nothing starts a work block at sign-in or stops one at sign-out yet; the
words are ready for when something does.

A notification never asks a question and has no buttons. It comes from the
menu bar icon's helper, under goodall's own name (ADR 0024,
[menu-bar-icon.md](menu-bar-icon.md)). macOS asks you once whether goodall
may show notifications, the first time the icon starts after goodall is set
up; that question and its words are macOS's own. If you dismiss it or do
not answer, macOS treats that as no, and only System Settings >
Notifications > goodall changes it.

A notification that could not be shown (notifications turned off, or the
menu bar icon not installed) is a warning printed by the command. It never
stops a work block from starting or ending: by the time goodall asks for
it, the work block has already started or ended.

### For scripts and schedules: `--background`

`goodall start`, `goodall stop` and `goodall pause` take
`--background <reason>`, which says that something other than you is
running the command:

| Command | Reasons it takes |
| --- | --- |
| `goodall start` | `sign-in`, `script` |
| `goodall stop` | `sign-out`, `script` |
| `goodall pause` | `script` |

A script or schedule that starts or stops recording must pass
`--background script`; otherwise you would not be told. The LaunchAgent
that will start a work block when you sign in will pass `sign-in`. A
`--background` start that finds a work block already running, or a
`--background` stop with nothing to stop, changes nothing and shows
nothing.

## What goodall does not do

- **Nothing runs on a timer.** If you never run a goodall command again, an
  idle block is never closed and its record is never written. Run
  `goodall status` when you come back to a machine.
- **An idle block cannot be picked up again.** Once a block has been idle
  for more than three hours, sending a prompt does not revive it. Claude Code
  refuses the prompt and tells you to run `goodall start`, which closes the
  old block and begins a new one.
- **A break shorter than three hours changes nothing by itself.** The block
  stays open and the break is inside it. If a lunch break should not count,
  run `goodall pause` before it and `goodall start` after (see "Pausing"
  below).

## Pausing

`goodall pause` is for a break you mean to come back from. It ends the work
block exactly as `goodall stop` does: the end state is `clean`, the agent
sessions are copied, and the record is written. There is no paused state
inside a work block. While paused, goodall is simply not recording, so
Claude Code refuses prompts, and the refusal says that recording is paused.
That is dedicated account mode. In mixed account mode the prompt gate is
off, so a prompt sent while paused goes through and is not recorded, as at
any time outside a work block.

What `goodall pause` adds is a small note in the log directory
(`work-blocks/pause.json`) saying which work block was paused. Then:

- **`goodall start`** begins a **new** work block. Its record names the
  paused one in the field `continues_work_block`
  (`docs/record-schemas.md`), so analysis can read the two as one stretch
  of work with a gap. Without `--workstream` it is on the paused block's
  workstream; with `--workstream` it can be on another, and still names the
  paused block.
- **`goodall stop`** clears the pause. There is no work block to end, and
  it says so and exits successfully. The next work block continues nothing.
- **`goodall status`** says that goodall is not recording and that
  recording is paused, with the paused work block and its workstream. With
  `--json`, `paused` is `true` and `paused_work_block` holds its `id`,
  `workstream` and `paused_at`. The exit code is 1, as whenever no work
  block is active.

A pause does not expire: after a weekend, `goodall start` still continues
the block you paused on Friday. Run `goodall stop` if that is not what you
want.

`goodall pause` when recording is already paused changes nothing and exits
successfully. With no work block active and nothing paused it exits 1,
because there is nothing to continue. If the work block had been left idle
past the idle limit, goodall closes it at its last heartbeat (`inferred`,
as above) and still remembers the pause, so the next work block names it.

One thing a pause does not do: an agent session that runs across a pause is
still copied whole, because copies are byte for byte (ADR 0019 and
ADR 0021).

## If the record cannot be written

Ending a work block and writing its record are separate steps. If the record
repo cannot take the record (for example the folder is missing, or git
refuses the commit), the work block is still ended and its record waits in
the log directory on this Mac. The command says so plainly, and the next
`goodall start`, `goodall stop` or `goodall status` writes it. Nothing is
lost.

`goodall stop` exits `0` in this case and shows a warning rather than a
failure, because what you asked for (ending the work block) is done and the
record is safe. You do not need to run it again; running it again would only
say that no work block is active. Goodall exits `1` only when the thing you
asked for did not happen.

## Fixing an unknown workstream

Goodall would rather keep a work block with a gap in it than lose it. When
it cannot read a block's marker, it still records the block, with the end
state `abnormal`, and fills each gap with what it can honestly say: this
eval account's contributor, and the time the marker file last changed when
the start cannot be read. If the marker no longer says which workstream the
block was for, the record says `unknown`. `unknown` is reserved: no eval
may name a real workstream that. If not even a start time can be read,
nothing is recorded, because any time goodall wrote would be invented; the
damaged marker is kept and goodall asks you to tell the researcher.

`goodall status` then lists each of your work blocks waiting for its
workstream, with one command per workstream, ready to copy:

```
goodall fixblock 20261116T093000Z-ada-lovelace-7f3a9c2e --workstream research
```

Run the one that names the workstream the block was for. The record is
never changed (records are never rewritten); goodall writes a small
**work block fix** beside it in the record repo, naming the block and the
workstream, and analysis reads the two together. You can also use
`goodall fixblock` on a block recorded on the wrong workstream, and you can
fix a block again: every fix is kept, and the latest one counts. The
fix's file format is in `docs/record-schemas.md`, and the reasons are in
ADR 0014.

## The idle limit

The idle limit is three hours for everyone. It is one constant,
`IDLE_LIMIT` in `src/goodall/work_blocks.py`, and is not a setting. If your
eval needs a different value, raise it with the researcher who runs the eval,
because every contributor's records should be made by the same rule.
