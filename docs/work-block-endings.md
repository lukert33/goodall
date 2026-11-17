# How a work block ends

A work block is a logged stretch of one contributor's work on one workstream.
It begins when you run `goodall start`. This page says every way it can end,
including the ways goodall ends it for you, so that nothing about your record
is a surprise.

The short version: **goodall never ends a work block while you are working in
it, and it never ends one in the background.** It only ever closes a work
block that has been left idle, and only at the moment you next run
`goodall start`, `goodall stop` or `goodall status`.

## The three ways

Every work block record carries an end state saying which of these happened.

| End state | What happened | When the block is counted as ending |
| --- | --- | --- |
| `clean` | You ran `goodall stop`. | The moment you ran it. |
| `inferred` | Nobody ran `goodall stop`, and the block sat idle for longer than the idle limit. Goodall closed it for you. | Its last heartbeat: the last moment goodall saw work in it. |
| `abnormal` | Goodall could not read the block's marker, so it could not tell whether the block was still running. It recorded what it could recover. | Its last heartbeat, or failing that the last time the marker file changed. |

## When goodall closes a work block for you

Goodall closes a work block on your behalf in one situation only.

1. **You started a work block and did not stop it.** You closed the laptop,
   went home, or simply forgot.
2. **Nothing happened in it for more than three hours.** "Something happening"
   means a heartbeat: goodall records one each time you send a prompt in
   Claude Code, each time Claude uses a tool, and each time Claude finishes a
   reply. Three hours is the idle limit.
3. **You then run `goodall start`, `goodall stop` or `goodall status`.** Before
   doing its own job, that command notices the idle block and closes it.

The block is recorded as ending at its **last heartbeat**, not at the moment
you ran the command. A block you forgot at 5 pm and found the next morning is
recorded as ending at 5 pm, so the night is not counted as work. Its end state
is `inferred`.

Goodall tells you when it does this, in words like these:

> Your work block on research, started Mon 16 Nov 2026, 14:05 GMT, was left
> running: nobody ran goodall stop, and nothing happened in it for more than
> 3 h. Goodall closed it at its last heartbeat, Mon 16 Nov 2026, 17:02 GMT,
> so the time after that is not counted as work.

## What this means for each command

- **`goodall status`** is not a look-only command. If it finds an idle work
  block it closes it, and if a finished work block's record is still waiting
  on this Mac it writes that record into the record repo and pushes it. It
  says so each time. If nothing needs tidying, it only reports.
- **`goodall stop`** run the morning after a forgotten block closes that
  block as `inferred`, at its last heartbeat, and exits successfully. It does
  not record the block as ending when you typed the command.
- **`goodall start`** closes an idle block first, then starts a new one.

## What goodall does not do

- **Nothing runs on a timer.** If you never run a goodall command again, an
  idle block is never closed and its record is never written. Run
  `goodall status` when you come back to a machine.
- **A paused block cannot be picked up again.** Once a block has been idle
  for more than three hours, sending a prompt does not revive it. Claude Code
  refuses the prompt and tells you to run `goodall start`, which closes the
  old block and begins a new one.
- **A pause shorter than three hours changes nothing.** The block stays open
  and the pause is inside it. If a lunch break should not count, run
  `goodall stop` before it and `goodall start` after.

## If the record cannot be written

Ending a work block and writing its record are separate steps. If the record
repo cannot take the record (for example the folder is missing, or git
refuses the commit), the work block is still ended and its record waits in
the log directory on this Mac. The command says so plainly, and the next
`goodall start`, `goodall stop` or `goodall status` writes it. Nothing is
lost.

## The idle limit

The idle limit is three hours for everyone. It is one constant,
`IDLE_LIMIT` in `src/goodall/work_blocks.py`, and is not a setting. If your
eval needs a different value, raise it with the researcher who runs the eval,
because every contributor's records should be made by the same rule.
