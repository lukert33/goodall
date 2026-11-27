# Setting up an eval account by hand

`goodall onboard` is not built yet. This checklist is how to do its job by
hand, for the first eval on a real Mac. It was written on 2026-11-26 from
goodall's code and documents, and **has not yet been followed from start to
finish on a real Mac**. On 2026-11-27 Parts 4 and 6 were rehearsed in a
scratch home folder, which added the git identity, PATH and Claude Code
steps to Part 2. Steps marked **Not yet confirmed** are the ones most
likely to be wrong; please note what really happened beside each.

Words used here are defined in [GLOSSARY.md](../GLOSSARY.md). An **eval
account** is a standard (non-admin) macOS user account used for one eval and
nothing else.

Two rules while you work:

- **Never paste a secret, a window title or a page address** into a note, an
  issue or a chat. Note structure and counts only: "the watcher recorded a
  page address", not the address.
- **Every example value below is made up.** Use your own.

## Before you start: removing an earlier install

Skip this on a Mac that has never had goodall or ActivityWatch. To start
again from nothing, do these in this order. Nothing here touches your
Claude, Antigravity or Codex sign-ins.

- [ ] **Keep or delete the old records first.** An old eval's record repo
      and log directory are its records; goodall never deletes them. Move
      them somewhere safe, or delete them, before anything else.
- [ ] **In each macOS user account that ran goodall:**
      - Quit goodall from the menu bar icon, if it is there.
      - Remove goodall's hooks from `~/.claude/settings.json`: delete each
        entry whose command runs `goodall hook`. Until they are gone, Claude
        Code refuses every prompt in that account.
      - Remove goodall and its local settings:

        ```
        uv tool uninstall goodall
        rm -rf ~/.config/goodall
        ```

- [ ] **In each macOS user account that ran ActivityWatch:** turn off its
      start-at-login option in its menu-bar menu, then quit it from there.
      Then remove its settings, its recorded data and its logs. This
      deletes everything it recorded in that account:

      ```
      rm -rf ~/Library/Application\ Support/activitywatch ~/Library/Caches/activitywatch ~/Library/Logs/activitywatch
      rm -f ~/Library/LaunchAgents/net.activitywatch.aw-qt.plist
      ```

- [ ] **From an administrator account:** move `/Applications/ActivityWatch.app`
      and `/Applications/goodall.app` (the menu bar icon) to the Trash. In
      System Settings, Privacy & Security, Accessibility, select
      ActivityWatch and remove it with the minus button.
- [ ] **An old eval account** can be deleted in System Settings, Users &
      Groups, which also deletes its home folder. A fresh account is
      cleaner than reusing one: ActivityWatch's first copy into a new eval
      takes everything it holds, so an old account's earlier windows and
      pages would arrive in the new eval's first work block.

## Part 1: From an administrator macOS user account

An eval account cannot install software for the whole Mac, so these come
first.

- [ ] Create the eval account: System Settings, Users & Groups, Add User,
      with the account type **Standard**.
- [ ] Install, for every account on the Mac: `git`, ActivityWatch, and
      `uv` (0.13.4 or newer).
- [ ] Install the source applications the eval will use: the Claude desktop
      app, Antigravity, and the Codex desktop app (it installs as
      `ChatGPT.app`). Claude Code for the terminal is different: its
      installer puts it in one account's home folder, so it is installed in
      Part 2, inside the eval account.
- [ ] Install 1Password and its `op` command only if the eval will use
      secrets. An eval with no secrets does not need them.
- [ ] Build goodall's menu bar icon and put it in `/Applications`, where
      every account can run it and only an administrator can change it.
      Building needs Apple's Command Line Tools (`xcode-select --install`
      if `xcode-select -p` prints nothing):

      ```
      git clone https://github.com/lukert33/goodall.git ~/goodall-source
      ~/goodall-source/menu-bar-icon/build.sh
      cp -R ~/goodall-source/menu-bar-icon/build/goodall.app /Applications/
      ```

      Nothing starts it at sign-in yet: `goodall start` starts it
      ([menu-bar-icon.md](menu-bar-icon.md)).
- [ ] If ActivityWatch also runs in another macOS user account on this Mac,
      quit it there until Part 3's step 3 is done, then start it again on
      its usual port, 5600. Part 3 says why, and checks that the eval
      account's copy keeps clear of it.

## Part 2: In the eval account, install and sign in

Sign in to the Mac as the eval account for everything from here on.

- [ ] Run `command -v uv`. It should print a path. If `uv` was installed with
      Homebrew from the administrator account, every account finds it,
      because Homebrew adds its folder to every account's PATH. If nothing is
      printed, install uv inside this account with its own installer.
- [ ] Install goodall:

      ```
      git clone https://github.com/lukert33/goodall.git
      uv tool install ./goodall
      ```

- [ ] uv puts the `goodall` command in `~/.local/bin`, which a new account's
      PATH does not include, and says so in a warning. Run this once, then
      close Terminal and open a new window:

      ```
      uv tool update-shell
      ```

- [ ] Run `command -v goodall` and keep the full path it prints. Part 5
      needs it.
- [ ] Tell git who you are in this account. Goodall commits every record to
      the record repo under this name and email, and refuses to write a
      record without them. The commits stay on this Mac (ADR 0023).

      ```
      git config --global user.name "Ada Lovelace"
      git config --global user.email "ada@example.org"
      ```

- [ ] Install Claude Code for the terminal in this account, with the
      installer from Claude Code's own documentation:

      ```
      curl -fsSL https://claude.ai/install.sh | bash
      ```

      Then open a new Terminal window and run `claude --version`. A copy
      installed in another account is not on this account's PATH.
- [ ] Run `goodall status`. It should run, and tell you local settings are
      missing. **Not yet confirmed:** that a tool installed this way inside a
      standard account works without an administrator.
- [ ] Sign in to each source application: Claude, Antigravity, Codex.
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

- [ ] Create the record repo, a git repository of its own, with no
      remote. Goodall commits records to it and never pushes them.

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

      At least one `[[scope]]` entry is required, and its `repository` must
      look like `host/owner/name`. Goodall checks it but does not use it
      yet, so for an eval whose work is not in a repository, name the one
      you would keep its files in.

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

### Mixed account mode: trying goodall in an account you also use for other things

Everything above sets up **dedicated account mode**: an eval account, where
signing in means recording. That is the recommended setup. **Mixed account
mode** is a second choice, for trying goodall out on your own work in a
macOS user account you also use for other things (ADR 0021). To use it, do
Parts 2, 4, 5 and 6 in that account, skip Part 3, and add one line to local
settings:

```toml
account_mode = "mixed"
```

The other value is `"dedicated"`, which is also what leaving the line out
means. Anything else is refused, with the correct line shown.

What changes in mixed account mode:

- **The prompt gate is off.** Your prompts are never refused for want of a
  work block. Nothing is recorded unless you run `goodall start`, and work
  outside a work block is left alone.
- **Any agent conversation held during a work block is copied, whatever it
  was about.** If any part of a conversation with an agent happens while a
  work block is active, goodall copies that whole conversation when the
  work block ends, including the parts from before you started and anything
  personal in it. Run `goodall stop` before you turn to something that is
  not for the eval, and start a new conversation for it.
- **ActivityWatch is not used**, even when `eval.toml` says
  `activitywatch = true`, because it records the whole account all the
  time. Goodall asks it nothing and copies nothing from it, and the
  `activitywatch_port` and `activitywatch_device_id` lines are not needed.
- **`goodall doctor` warns, and does not fail,** when the account is an
  administrator account. An eval account is still the recommended setup.
- Every work block record says which mode it was recorded in
  (`account_mode`), and `goodall status` shows the mode.

Of the safeguards ADR 0021 plans for mixed account mode, the menu bar icon
is built: it turns orange while a work block is active. So are the
notifications, shown when recording starts or stops without your own
command (docs/work-block-endings.md). The automatic timeout is not built
yet, so nothing tells you a work block has been left running until goodall
closes it as idle; glance at the icon, or run `goodall status`, when
unsure.

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
      it again. One warning is expected today and can be left: there is no
      shared env file (`.env.shared`) in the record repo.
- [ ] With no work block active, send any prompt in Claude Code in a
      terminal. It should be refused with a message telling you to run
      `goodall start`. (In mixed account mode it should go through, and
      nothing should appear in the log directory.)
- [ ] Do the same in the Claude desktop app's Code tab. Note whether it is
      refused.
- [ ] Run `goodall start`, send a prompt in each, then run `goodall stop`.
      Expect `goodall stop` to say `Telemetry  missing`. Claude Code sends
      telemetry only when it is started with goodall's settings, and nothing
      starts it that way yet; the transcripts are still copied.
- [ ] Check that the log directory's `transcripts/` folder holds a copy of
      each session, and that the record repo's `work-blocks/` folder holds
      one new file.
- [ ] Check the menu bar icon. After your first `goodall start` in the eval
      account, goodall's binoculars should appear in the menu bar, orange
      while recording. Open its menu: the first line says what is being
      recorded. Choose **Pause**, then **Record work block** and a
      workstream, then **Stop recording**, and check after each that
      `goodall status` in Terminal agrees. A menu item that did not work
      shows a `!` beside the icon, and its details are in
      `~/Library/Logs/goodall/menu-bar-icon.log`.
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
- [ ] See for yourself what ActivityWatch recorded of your browsing. This
      prints, for each window that had a page address, the time (in UTC), how many
      seconds it was in front, the app, the window title and the address.
      It is the raw record, so it shows what you searched for and which
      pages you read. **Read it on your own screen only; never paste it
      anywhere.**

      ```
      cd ~/evals/domain-test-logs.noindex
      jq -r '.[] | select(.data.url != null) | [.timestamp[11:19], (.duration | floor), .data.app, .data.title, .data.url] | @tsv' activitywatch/*/events/*window*.json | sort | less -S
      ```

      What to look for: a search shows up as the search page's address,
      with the search words in it, and usually in its title too. Only the
      window in front is recorded, about once a second, so a page that was
      in front for under a second may be missing, and a background tab
      never appears. Safari's private windows appear like any other.
      Without the Automation permission from Part 3, step 5, Safari's
      windows appear with no title and no address, and this prints nothing
      for them. `goodall timeline` lists the same moments as
      `application_focus` lines naming only the app.
- [ ] To see the warning, quit ActivityWatch from its menu-bar icon, run
      `goodall start` and `goodall stop`: stop should still succeed, with a
      warning that ActivityWatch's data was not copied. Start ActivityWatch
      again and run `goodall status`: it should say it copied the data.

- [ ] Read the timeline. `goodall stop` should have shown a line `Timeline
      built`. Then run:

      ```
      goodall timeline
      ```

      Expect the work block you just stopped, one line for each thing that
      happened, in time order: `human typed_prompt` for each prompt you
      sent, `ai tool_call` and `harness tool_result` lines for what the
      agent did, `human journal_entry` and `human intervention` for what you
      wrote, and, if the eval includes ActivityWatch, `human
      application_focus` lines naming the application that was in front.
      Expect no text of yours on any line: no prompt, no window title, no
      page address. An application or tool goodall has no name for shows as
      `other_application` or `other_tool`.
- [ ] Follow one pointer. Run `goodall timeline --pointers`, pick a
      `typed_prompt` line, and note its file and line number. This prints
      the start of that line of the raw record, which should be the prompt
      you typed:

      ```
      cd ~/evals/domain-test-logs.noindex
      sed -n '<line number>p' '<path after "log_directory: ">' | cut -c1-300
      ```

- [ ] Check that the timeline holds none of your words. Pick a word that was
      in a prompt, a window title or a page address during the work block
      (the name of the site you visited will do). This should print `0`:

      ```
      grep -c '<the word>' timeline/timeline.sqlite3
      ```

      The same search over `transcripts/` and `activitywatch/` will find it:
      those are the raw records, which keep everything. That includes
      anything typed into a web form that ends up in a page's address or
      title, such as a search.
- [ ] Check that it can be rebuilt. `ls -l timeline/` should show one file,
      `-rw-------`. Delete it with `rm -f timeline/timeline.sqlite3`, run
      `goodall timeline` again, and expect the same lines.

## What goodall records today, and what it does not

| Source | Recorded today? |
|---|---|
| Claude Code in a terminal | Yes: transcripts, and the prompt gate applies |
| Claude desktop app, Code tab | Expected to be the same; Part 6 confirms |
| Claude desktop app, Cowork and chat | No. Their sessions are kept in your Claude account, not on the Mac (ADR 0019) |
| Antigravity | Not yet (goodall-fs9.24) |
| Codex | Not yet (goodall-fs9.25) |
| ActivityWatch (apps, windows, page addresses) | Yes, copied when a work block ends, if the eval config says `activitywatch = true`. Not yet confirmed on a real eval account. The timeline lists the copies |
| Journal entries and interventions | Yes |

`goodall timeline` shows a work block in time order (`docs/timeline.md`);
it has not been run in a real eval account yet. There is no `goodall
dry-run` and no `goodall teardown`.
