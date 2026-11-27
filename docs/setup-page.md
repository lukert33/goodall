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
unchanged, and goodall says "Nothing to change". A later `goodall settings`
will reopen the same page with your current answers.

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
