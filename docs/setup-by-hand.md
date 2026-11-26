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
      quit it there until Part 3's step 3 is done, then start it again on
      its usual port, 5600. Part 3 says why, and checks that the eval
      account's copy keeps clear of it.

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

ActivityWatch silently joins any copy of itself already answering on its
port, including one in another macOS user account, so the eval account's copy
must use a port of its own. These steps were followed on 2026-11-26 with
ActivityWatch 0.14.3 on macOS 26 in a standard account (hand test
goodall-fs9.19), and each held as written.

**Order matters.** Move the eval account's copy off port 5600 (steps 1 to 3)
before ActivityWatch starts in any other account on the Mac. If another
account's copy is already on 5600 when the eval account's first starts, the
eval account's watchers send their data to the other account's server.

- [ ] 1. Start ActivityWatch once (from Applications) and wait for its icon
      in the menu bar. Its settings files are created then, in
      `~/Library/Application Support/activitywatch/`:
      `aw-server/aw-server.toml` and `aw-client/aw-client.toml`. Every line
      in them starts commented out with `#`. Quit ActivityWatch from its
      menu-bar icon.
- [ ] 2. In **both** files, under `[server]`, change `#port = "5600"` to
      `port = "5610"` (or another free port that is not 5600): remove the
      `#` and keep the quotes. Leave `[server-testing]` alone. Start
      ActivityWatch again. Its launcher and both watchers follow the new
      port with no other change.
- [ ] 3. Confirm the server answers on the new port and note its
      `device_id` for Part 4. Use `127.0.0.1`, not `localhost`: the server
      listens only there.

      ```
      curl -s http://127.0.0.1:5610/api/0/info | jq -r .device_id
      ```

- [ ] 4. Confirm the window watcher and the away-from-keyboard watcher are
      sending to the new port. The trailing `/` is needed: without it the
      server answers with a redirect, not the list.

      ```
      curl -s http://127.0.0.1:5610/api/0/buckets/ | jq '[.[] | {type, last_updated}]'
      ```

      It should list a `currentwindow` and an `afkstatus` bucket, each
      updated in the last minute.
- [ ] 5. Allow the permissions the window watcher needs. Every entry is
      named **ActivityWatch**:
      - **Accessibility** (System Settings, Privacy & Security). It applies
        to every account on the Mac, so it may already be on from an
        administrator account; if it is not listed, add ActivityWatch
        there. It gives window titles, and page addresses in Firefox.
      - **Automation**, for Safari and for Chrome. macOS asks the first
        time each browser is in front ("ActivityWatch" wants access to
        control ...). Allow it. This is per account. Without it, Safari's
        windows are recorded with no title and no address.
      - Screen Recording is not needed.
- [ ] 6. Make it start at sign-in: in ActivityWatch's menu-bar menu, turn
      on its start-at-login option. No administrator password is needed.
      macOS then shows a notice about background activity, named after the
      app's developer rather than ActivityWatch. Sign out and back in, then
      repeat step 3: it should answer on 5610 again. ActivityWatch is not
      restarted if it quits or crashes, so check its menu-bar icon is there
      before each work block.
- [ ] 7. If another account on the Mac runs ActivityWatch on 5600, check the
      two stay apart. This should print `different`:

      ```
      [ "$(curl -s http://127.0.0.1:5610/api/0/info | jq -r .device_id)" = "$(curl -s http://127.0.0.1:5600/api/0/info | jq -r .device_id)" ] && echo same || echo different
      ```

      Both accounts' buckets have the same names, because the names come
      from the Mac's name, so the `device_id` is the only thing that tells
      them apart.
- [ ] 8. Do **not** install the ActivityWatch browser extension. It can only
      send to port 5600.

Know what ActivityWatch records before you rely on it:

- **It records everything in the account**, including Safari's private
  windows, with their titles and addresses and nothing marking them
  private.
- **Any account on the Mac can read it.** ActivityWatch's server asks for
  no password, so anyone signed in to another account who knows the port
  can read the eval account's data.
- **Time spent in another account looks like work.** While another account
  is in front, the eval account's copy keeps lengthening its last window
  and does not record the person as away.

When a work block ends, goodall copies ActivityWatch's data into the
`activitywatch` folder of the log directory
(`docs/activitywatch-rules.md`, "Copies in the log directory"). **Not yet
confirmed against a real ActivityWatch:** Part 6 has a check to run after
your first `goodall stop`.

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
- [ ] If the eval includes ActivityWatch, check its copy. `goodall stop`
      should have shown a line `ActivityWatch  copied, 2 buckets` and no
      warning about ActivityWatch. Then, with the log directory from Part 4
      (these commands print counts and file names only, never a title or an
      address; the file names hold the Mac's name):

      ```
      cd ~/evals/domain-test-logs.noindex
      ls -l activitywatch/*/ activitywatch/*/events/
      for f in activitywatch/*/events/*.json; do jq length "$f"; done
      jq '.buckets[] | {start, end}' activitywatch-sync/copies/*.json
      ```

      Expect one folder named after the moment of the stop, holding
      `info.json`, `buckets.json` and one events file for each bucket; every
      file `-rw-------`; a count of events above zero for each; and `start`
      and `end` both `null`, because a bucket's first copy holds everything.
      After a second work block, a second folder appears, and its `start` is
      ten minutes before the first copy's moment.
- [ ] To see the warning, quit ActivityWatch from its menu-bar icon, run
      `goodall start` and `goodall stop`: stop should still succeed, with a
      warning that ActivityWatch's data was not copied. Start ActivityWatch
      again and run `goodall status`: it should say it copied the data.

## What goodall records today, and what it does not

| Source | Recorded today? |
|---|---|
| Claude Code in a terminal | Yes: transcripts, and the prompt gate applies |
| Claude desktop app, Code tab | Expected to be the same; Part 6 confirms |
| Claude desktop app, Cowork and chat | No. Their sessions are kept in your Claude account, not on the Mac (ADR 0019) |
| Antigravity | Not yet (goodall-fs9.24) |
| Codex | Not yet (goodall-fs9.25) |
| ActivityWatch (apps, windows, page addresses) | Yes, copied when a work block ends, if the eval config says `activitywatch = true`. Not yet confirmed on a real eval account, and nothing reads the copies yet |
| Journal entries and interventions | Yes |

There is no timeline to read at the end of a work block yet
(goodall-acp.9), no `goodall dry-run` and no `goodall teardown`.
