# The setup page, and plain `goodall onboard`

`goodall onboard` asks its setup questions on a **setup page** in your
browser, never in the terminal (ADR 0012). Nothing is listening behind the
page, and it loads nothing from the internet. The whole exchange is one file
your browser saves.

```
goodall onboard          # opens the page, waits, then sets up
goodall onboard --plan   # the same, but only shows the changes
```

## What happens

1. Goodall writes the page, with a **one-time code** in it, into a new
   folder only your macOS user account can open, and opens it with `open`,
   so it appears in your default browser.
2. You answer on the page and press **Finish setup**. The page checks your
   answers as you type, but it cannot look at your Mac, so it cannot tell
   whether a folder exists.
3. Your browser saves your answers as `goodall-answers.toml` in your
   **Downloads folder**, `~/Downloads`. The file holds no secrets: only
   references into 1Password ([answers-file.md](answers-file.md)).
4. The waiting `goodall onboard` finds the file, checks it carries the
   page's one-time code, shows every change it will make and makes them,
   exactly as `goodall onboard --answers` does. It reports in the terminal;
   the page cannot show the result.

Goodall removes the page and its folder when it ends. It leaves the answers
file where your browser saved it: it is yours. Keep it to set up the same way
again with `goodall onboard --answers`, or delete it.

## While it waits

- It looks in `~/Downloads` once a second, for up to 60 minutes, and says
  every 10 minutes that it is still waiting. Then it stops, changes
  nothing, and says what to do next.
- **Control-C** stops it at any moment. While it is still waiting, nothing
  has changed. If you press it while goodall is making changes, each change
  is made in one step, and running `goodall onboard` again is safe.
- If goodall **refuses the answers** (a record repo folder that already
  holds files, say), it says why and keeps waiting. The page is still open
  with every answer in it: change the one answer and press Finish setup
  again. Your browser saves the second file as `goodall-answers (1).toml` or
  `goodall-answers-2.toml`, and goodall finds that too.
- **An answers file from an earlier run** lying in `~/Downloads` carries
  another code, so goodall leaves it alone, and says so once.

## If your browser saves somewhere else

A page opened from a file can only hand the browser a download; no browser
lets it choose the folder (the File System Access API that could is missing
from Safari and Firefox). Safari, Chrome and Firefox save to `~/Downloads`
unless you changed that, or they ask each time. If yours asks, choose
Downloads. If it saved the file somewhere else, either move it into
`~/Downloads` while goodall waits, or stop goodall with Control-C and run:

```
goodall onboard --answers ~/Desktop/goodall-answers.toml
```

An answers file given with `--answers` needs no one-time code.

## Running it again

Onboarding is safe to run again. The page opens with what is already set up
on this Mac filled in: your account mode, contributor id, main workstream,
folders, the secret references in the env file, and the eval already on this
Mac. Pressing Finish setup without changing anything leaves every file
unchanged, and goodall says "Nothing to change". `goodall settings` (below)
reopens the same page with your current answers.

## Settings: `goodall settings`

```
goodall settings          # opens the page with your current answers, waits, applies what changed
goodall settings --plan   # the same, but only shows the changes
```

This is what the menu bar icon's **Settings…** item runs
([menu-bar-icon.md](menu-bar-icon.md)). It opens the same page with this
account's current answers filled in, waits for the file the page saves,
and applies it exactly as `goodall onboard` does, including the one-time
code and the plan shown first. The page is a little different: it has no
"new eval" choice and no steps to do before the page, its first section is
**Workstreams**, and its button says **Save settings**. Its first use is
registering a new workstream.

**What a settings run may change.** It is `goodall onboard` with the eval
already on this Mac, plus one thing it may do to `eval.toml`, which
onboarding otherwise never writes for an existing eval:

- It may **add workstreams** to the eval config's `workstreams` line, and
  nothing else in `eval.toml`. Every other line, comment and blank line stays
  byte for byte as written, and a comment at the end of the workstreams line
  stays too. New names go at the end, in the order you added them.
- It **never removes or reorders** a workstream: work blocks already
  recorded name theirs, and the eval config is published with the record.
  The page gives no way to remove one, and goodall refuses answers that leave
  one out. To retire a workstream, edit `eval.toml` by hand and commit.
- It **commits** that change in the record repo, as its own commit touching
  only `eval.toml`, with a message naming the workstream. It never adds a
  remote and never pushes (ADR 0023). If `eval.toml` has uncommitted edits
  of yours, goodall refuses, so its commit never carries them; commit or
  undo them and run it again.
- If the workstreams are not one whole line above the first `[table]` (a
  list spread over several lines, say), goodall leaves the file alone and
  says to add the name by hand.
- Everything else (local settings, the env file, the hooks) follows the
  answers as `goodall onboard` does, and an unchanged one is reported
  "unchanged". Saving without a change says "Nothing to change". The new
  workstream can be your main workstream in the same save.

The page carries the full list as an `[existing_eval]` section of the
answers file ([answers-file.md](answers-file.md)), so the same change works
from `goodall onboard --answers`.

**Not set up yet.** `goodall settings` in an account where goodall is not
set up (no local settings, or an eval config it cannot read) opens nothing,
says to run `goodall onboard`, and exits `1`.

**No terminal in sight.** From the menu bar there is no Terminal window:
what goodall prints goes to the icon's log
(`~/Library/Logs/goodall/menu-bar-icon.log`) when the command ends, which
for a command that is still waiting is later. So after you press **Save
settings** the page itself says what happens: goodall applies the file
within a second or two, there is nothing more to do, choosing Settings…
again shows what is saved, and answers goodall refuses keep it waiting for a
corrected file. The page cannot show goodall's result; it has no server
behind it (ADR 0012).

## What the page asks

- **How you will record**: an eval account (dedicated account mode, the
  recommended one) or **Try it out** (mixed account mode) in an account you
  also use for other things (ADR 0021).
- **A new eval, or the one already on this Mac.** There is no "join": the
  record repo is local to this Mac and never pushed (ADR 0023).
- For a new eval: its name, a one-sentence description, its workstreams,
  the repositories its work is in, its construct, and, in an eval account,
  whether it includes ActivityWatch.
- About you: your display name, contributor id and main workstream.
- Where the record repo and the log directory go on this Mac. For a new
  eval it suggests `~/evals/<name>-records` and
  `~/evals/<name>-logs.noindex`.
- Secrets: whether some work blocks are billed to API keys, and if so a
  1Password reference for each, never the key itself.

`goodall settings` shows only the about-you, where, and secrets questions and
the workstreams of the eval already here.

It does not ask, yet: creating the eval account, ActivityWatch's own port
(Part 3 of [setup-by-hand.md](setup-by-hand.md)), or the mixed account mode
timeout.

## Checking the page by hand

The tests run the page's answers-and-checks code with the JavaScript macOS
comes with, and read what it would save back through goodall's own reader
(`tests/test_setup_page_script.py`). They cannot see the page drawn or a
browser saving the file. **Not yet confirmed** in a real browser; to check,
in a scratch macOS user account or with `HOME` pointed at a scratch folder:

- [ ] Run `goodall onboard`. The page opens in Safari and in Chrome, with
      the binoculars and the khaki and forest colors, light and dark.
- [ ] Fill in a new eval and press Finish setup. The file arrives in
      `~/Downloads` named `goodall-answers.toml`, not
      `goodall-answers.toml.txt`, and the terminal picks it up within a
      second or two.
- [ ] Press Finish setup a second time. Note whether the browser asks to
      allow a second download.
- [ ] The browser's developer console shows no errors and no blocked
      requests.
