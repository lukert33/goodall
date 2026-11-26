# Setting up an eval account by hand

`goodall onboard` is not built yet. This checklist is how to do its job by
hand, for the first eval on a real Mac. It was written on 2026-11-26 from
goodall's code and documents, and **has not yet been followed from start to
finish on a real Mac**. Steps marked **Not yet confirmed** are the ones most
likely to be wrong; please note what really happened beside each.

Words used here are defined in [GLOSSARY.md](../GLOSSARY.md). An **eval
account** is a standard (non-admin) macOS user account used for one eval and
nothing else.

Two rules while you work:

- **Never paste a secret, a window title or a page address** into a note, an
  issue or a chat. Note structure and counts only: "the watcher recorded a
  page address", not the address.
- **Every example value below is made up.** Use your own.

## Part 1: From an administrator macOS user account

An eval account cannot install software for the whole Mac, so these come
first.

- [ ] Create the eval account: System Settings, Users & Groups, Add User,
      with the account type **Standard**.
- [ ] Install, for every account on the Mac: `git`, GitHub's `gh`,
      ActivityWatch, and `uv` (0.13.4 or newer).
- [ ] Install the source applications the eval will use: the Claude desktop
      app, Antigravity, and the Codex desktop app (it installs as
      `ChatGPT.app`).
- [ ] Install 1Password and its `op` command only if the eval will use
      secrets. An eval with no secrets does not need them.
- [ ] If ActivityWatch also runs in another macOS user account on this Mac,
      leave it running there on its usual port, 5600. Part 3 checks that the
      eval account's copy keeps clear of it.

## Part 2: In the eval account, install and sign in

Sign in to the Mac as the eval account for everything from here on.

- [ ] Install goodall:

      ```
      git clone https://github.com/lukert33/goodall.git
      uv tool install ./goodall
      ```

- [ ] Run `command -v goodall` and keep the full path it prints. Part 5
      needs it.
- [ ] Run `goodall status`. It should run, and tell you local settings are
      missing. **Not yet confirmed:** that a tool installed this way inside a
      standard account works without an administrator.
- [ ] Sign in to each source application: Claude, Antigravity, Codex.
- [ ] Run `gh auth login`. `goodall doctor` fails without a GitHub sign-in,
      even for an eval that keeps its record repo on the Mac. Whether that
      should stay so is an open question (goodall-9nf).
- [ ] If the eval uses secrets, sign in to 1Password.

## Part 3: ActivityWatch on its own port

This part is also the hand test in issue goodall-fs9.19. ActivityWatch
silently joins any copy of itself already answering on its port, including
one in another macOS user account, so the eval account's copy must use a port
of its own. Steps 3 to 6 need a terminal.

- [ ] 1. Start ActivityWatch once, then quit it. Find its settings folder.
      **Not yet confirmed:** it is expected under
      `~/Library/Application Support/activitywatch`, holding `aw-server.toml`
      and `aw-client.toml` in folders of their own.
- [ ] 2. In both files, under `[server]`, set `port` to a free port that is
      not 5600, for example `5610`. Start ActivityWatch again.
- [ ] 3. Confirm the server answers on the new port and note its
      `device_id` for Part 4:

      ```
      curl http://127.0.0.1:5610/api/0/info
      ```

- [ ] 4. Confirm the window watcher and the away-from-keyboard watcher are
      sending to the new port: `curl http://127.0.0.1:5610/api/0/buckets`
      should list a window bucket and an AFK bucket, each updated in the last
      minute.
- [ ] 5. With Chrome, Safari and Firefox each in front for a moment, check
      whether the window bucket's newest events have a `url` field. Note
      **which macOS permissions** you were asked for (Accessibility, Screen
      Recording, Automation). Look at field names only.
- [ ] 6. If another account's ActivityWatch is on 5600:
      `curl http://127.0.0.1:5600/api/0/info` should show a **different**
      `device_id`, and the eval account's buckets on 5610 should hold only
      this account's activity.
- [ ] 7. Do **not** install the ActivityWatch browser extension. It can only
      send to port 5600.

What to bring back: for each step, whether it held, the real file paths, and
the permissions asked for. That unblocks goodall-fs9.15, which makes goodall
copy ActivityWatch's data. **Until that is built, goodall reads these
settings but does not record ActivityWatch's data.**

## Part 4: The record repo, the log directory and local settings

- [ ] Create the record repo, a git repository of its own. A remote is
      optional for now; with none, records stay in local commits.

      ```
      mkdir -p ~/evals/domain-test-records
      cd ~/evals/domain-test-records
      git init
      ```

- [ ] In it, write `eval.toml`. Every key is required except
      `activitywatch`. Names are lowercase letters, digits and hyphens. Do
      not put an email address anywhere in it.

      ```toml
      schema_version = 1
      name = "domain-test"
      description = "Choosing and buying a domain name with AI agents."
      workstreams = ["research", "go-to-market"]
      intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]
      activitywatch = true

      [[contributors]]
      id = "ada-lovelace"
      display_name = "Ada Lovelace"

      [[scope]]
      repository = "github.com/your-org/your-repo"
      paths = ["**"]
      ```

- [ ] In it, write `CONSTRUCT.md`: the question this eval sets out to
      measure, in a sentence or two.
- [ ] Commit both files.
- [ ] Create the log directory, outside any repository, with a name ending
      in `.noindex` so Spotlight leaves it alone (ADR 0019). **Not yet
      confirmed:** that the `.noindex` name works on the current macOS. To
      check, put a file with an unusual name inside it, wait a minute, and
      search for that name in Spotlight; it should not be found.

      ```
      mkdir -p ~/evals/domain-test-logs.noindex
      chmod 700 ~/evals/domain-test-logs.noindex
      ```

- [ ] Write local settings at `~/.config/goodall/local-settings.toml`.
      `secrets_manager` must be present even when the eval has no secrets.

      ```toml
      schema_version = 1
      contributor = "ada-lovelace"
      main_workstream = "research"
      record_repo = "~/evals/domain-test-records"
      log_directory = "~/evals/domain-test-logs.noindex"
      secrets_manager = "1password"
      activitywatch_port = 5610
      activitywatch_device_id = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"
      ```

- [ ] Run `goodall status`. It names any setting it cannot read and shows a
      correct example line.

## Part 5: The Claude Code hooks

The prompt gate and transcript copying run from Claude Code's hooks.

- [ ] Add the `hooks` block shown in
      [claude-code-hooks.md](claude-code-hooks.md) to
      `~/.claude/settings.json` in the eval account, merging it with anything
      already there. Replace the example path with the full path from Part 2.
- [ ] **Not yet confirmed:** that the Claude desktop app's Code tab runs
      these hooks the same way Claude Code in a terminal does. Part 6 checks
      it.

Antigravity and Codex have no hooks to add. Goodall does not record their
sessions yet (issues goodall-fs9.24 and goodall-fs9.25).

## Part 6: Check it

- [ ] Run `goodall doctor`. It changes nothing. Fix what it reports and run
      it again. It will warn that the record repo has no remote if you chose
      to have none; that warning is expected.
- [ ] With no work block active, send any prompt in Claude Code in a
      terminal. It should be refused with a message telling you to run
      `goodall start`.
- [ ] Do the same in the Claude desktop app's Code tab. Note whether it is
      refused.
- [ ] Run `goodall start`, send a prompt in each, then run `goodall stop`.
- [ ] Check that the log directory's `transcripts/` folder holds a copy of
      each session, and that the record repo's `work-blocks/` folder holds
      one new file.
- [ ] Try `goodall journal` and `goodall intervene` once each inside a work
      block.

## What goodall records today, and what it does not

| Source | Recorded today? |
|---|---|
| Claude Code in a terminal | Yes: transcripts, and the prompt gate applies |
| Claude desktop app, Code tab | Expected to be the same; Part 6 confirms |
| Claude desktop app, Cowork and chat | No. Their sessions are kept in your Claude account, not on the Mac (ADR 0019) |
| Antigravity | Not yet (goodall-fs9.24) |
| Codex | Not yet (goodall-fs9.25) |
| ActivityWatch (apps, windows, page addresses) | Not yet (goodall-fs9.15) |
| Journal entries and interventions | Yes |

There is no timeline to read at the end of a work block yet
(goodall-acp.9), no `goodall dry-run` and no `goodall teardown`.
