# The administrator install

The administrator install creates the **eval account** for you and stages
its **first sign-in setup**, so the person who will record never has to find
Users & Groups in System Settings or open a terminal in the new account
(ADR 0022). Words used here are defined in [GLOSSARY.md](../GLOSSARY.md).

**This is a sketch until it has been run on a real Mac.** Everything below
is built and tested with goodall's fake runner only: no test creates an
account or installs anything. The last section is the checklist for the
owner's hand test (`goodall-ayy.11`).

It is three steps:

1. **In an administrator account**, install goodall and the menu bar icon
   for every account (below).
2. **In the same administrator account**, run `goodall onboard --answers`
   with an answers file that has an `[eval_account]` section. Goodall shows
   every change and command, then runs them: it stages the first sign-in
   setup and creates the eval account as a **standard** account.
3. **Sign in to the eval account.** After macOS's own setup screens, goodall
   finishes setting up the account by itself and starts recording. Every
   later sign-in starts recording too.

Creating the eval account by hand (Part 1 of
[setup-by-hand.md](setup-by-hand.md)) still works; then nothing here is
needed.

## 1. Install goodall and the menu bar icon for every account

The eval account runs goodall at every sign-in, from its Claude Code hooks
and from the menu bar icon, so goodall must be where every account can run
it and **only root can change it**: otherwise the eval account, or an agent
working in it, could change what records it.

Goodall goes in `/usr/local/goodall`, with its command at
`/usr/local/goodall/bin/goodall`. `/usr/local` is where an administrator
puts software for the whole Mac; on a Mac with an Apple chip Homebrew uses
`/opt/homebrew` instead, so `/usr/local` belongs to root. Goodall's own
folder keeps its Python and its whole environment together, owned by root,
and easy to remove. Goodall cannot install itself there. From an
administrator account, with `uv` installed (Part 1 of setup-by-hand.md):

```
git clone https://github.com/lukert33/goodall.git ~/goodall-source
sudo env UV_TOOL_DIR=/usr/local/goodall/tools UV_TOOL_BIN_DIR=/usr/local/goodall/bin \
  UV_PYTHON_INSTALL_DIR=/usr/local/goodall/python \
  "$(command -v uv)" tool install --managed-python ~/goodall-source
```

`--managed-python` and `UV_PYTHON_INSTALL_DIR` put the Python goodall runs
on inside the same root-owned folder; a Python from Homebrew would be one
the administrator account can change. `"$(command -v uv)"` is there because
`sudo` does not search Homebrew's folder.

Then build the menu bar icon and copy it to `/Applications`, as Part 1 of
[setup-by-hand.md](setup-by-hand.md) shows. The administrator install
refuses to go on without either, and says so.

## 2. Run the administrator install

The easy way: run plain `goodall onboard` in the administrator account,
choose an eval account and a new eval on the setup page, and answer "Create
it for me" with the new account's name ([setup-page.md](setup-page.md)).
The page writes the answers file below for you, and goodall runs this same
install when the file arrives.

Or write an answers file as [answers-file.md](answers-file.md) shows, for the
eval account (its folders are in the eval account's home, so write them
with `~/`; goodall refuses them otherwise), and add:

```toml
[eval_account]
create = true
name = "goodall-eval"
```

`account_mode` must be `"dedicated"` and `eval` must be `"new"`. Leave out
`[claude_code] goodall_program`, or set it to `/usr/local/goodall/bin/goodall`:
the eval account's hooks run the goodall installed for every account.

```
goodall onboard --answers ~/goodall-answers.toml --plan
goodall onboard --answers ~/goodall-answers.toml
```

Run in any account but the one the answers name, this does **not** set up
the account it runs in. Before changing anything it checks:

- this is an administrator account (`/usr/bin/id -Gn` lists `admin`);
- goodall is at `/usr/local/goodall/bin/goodall`, owned by root, and no
  other account can change it;
- `[claude_code] goodall_program`, if given, is that goodall;
- the menu bar icon is at `/Applications/goodall.app`;
- no account has the name: not in `/usr/bin/dscl . -list /Users` (which only
  reads), and no home folder `/Users/<name>` left by a deleted account.

Then it shows the plan and every command, and runs them in this order,
each with `sudo` and the terminal handed over to it, so macOS asks for the
passwords and goodall never sees one. With `name = "goodall-eval"`, run
from the administrator account `goodall-administrator`, and `<temporary>`
standing for a folder only that account can open, which goodall removes
afterwards:

```
/usr/bin/sudo /usr/bin/install -d -o root -g wheel -m 0755 '/Library/Application Support/goodall'
/usr/bin/sudo /usr/bin/install -o root -g wheel -m 0644 <temporary>/eval-account-answers.toml '/Library/Application Support/goodall/eval-account-answers.toml'
/usr/bin/sudo /usr/bin/install -o root -g wheel -m 0755 <temporary>/first-run '/Library/Application Support/goodall/first-run'
/usr/bin/sudo /usr/bin/install -o root -g wheel -m 0644 <temporary>/io.github.lukert33.goodall.first-run.plist /Library/LaunchAgents/io.github.lukert33.goodall.first-run.plist
/usr/bin/sudo /usr/bin/install -o root -g wheel -m 0644 <temporary>/io.github.lukert33.goodall.menu-bar-icon.plist /Library/LaunchAgents/io.github.lukert33.goodall.menu-bar-icon.plist
/usr/bin/sudo /usr/sbin/sysadminctl -addUser goodall-eval -password - -adminUser goodall-administrator -adminPassword -
```

| What | Where | Owner | Permissions |
|---|---|---|---|
| Staging folder | `/Library/Application Support/goodall` | root:wheel | `0755` |
| Answers for the eval account, a byte-for-byte copy of your answers file | `/Library/Application Support/goodall/eval-account-answers.toml` | root:wheel | `0644` |
| First-run script | `/Library/Application Support/goodall/first-run` | root:wheel | `0755` |
| First-run LaunchAgent | `/Library/LaunchAgents/io.github.lukert33.goodall.first-run.plist` | root:wheel | `0644` |
| Menu bar icon LaunchAgent | `/Library/LaunchAgents/io.github.lukert33.goodall.menu-bar-icon.plist` | root:wheel | `0644` |
| The eval account, a standard account | `/Users/goodall-eval` | the new account | macOS's own |

- **Never `/Users/Shared`**: every account can write there.
- **No password is on any command line**, where any program on the Mac
  could read it. `-password -` makes `sysadminctl` ask for the new
  account's password; `-adminPassword -` makes it ask for yours, which it
  needs to give the new account a secure token on a Mac with FileVault.
  `sudo` asks for yours first.
- **A standard account**: `sysadminctl` makes an administrator only with
  `-admin`, which goodall never passes.
- **Every staged file is readable by every account.** The answers file holds
  no secret (goodall refuses one), but it does hold your secret
  *references*, which name vaults and items in 1Password.
- **The files come first and the account last.** If creating the account
  fails, for example after a mistyped password, run the same command again:
  it creates only the account. Run again once everything is in place, it
  says `Nothing to change`.

What the staged files say:

- The **first-run script** is a few lines of comment and one command,
  `exec "/usr/local/goodall/bin/goodall" sign-in`. Every decision is in
  `goodall sign-in`, in Python.
- The **first-run LaunchAgent** runs the script at every sign-in to every
  account (`RunAtLoad`, `LimitLoadToSessionType` Aqua).
- The **menu bar icon LaunchAgent** runs
  `/Applications/goodall.app/Contents/MacOS/goodall-menu-bar-icon` at every
  sign-in to every account, with `GOODALL_COMMAND` set to
  `/usr/local/goodall/bin/goodall` ([menu-bar-icon.md](menu-bar-icon.md)),
  and `KeepAlive` with `SuccessfulExit` false: it brings the icon back if
  it crashes, but not after **Quit goodall**, which exits cleanly. In an
  account where goodall is not set up, the icon says so in its menu.

## 3. The first sign-in, and every sign-in after

The first-run script runs `goodall sign-in` at every sign-in to every
account:

- **In any other account** it stops at once, having read the eval
  account's name from the staged answers file and changed nothing.
- **In the eval account, the first time**, it sets the account up from the
  staged answers file, exactly as `goodall onboard --answers` would, showing
  each change before making it. It refuses the staged file unless root owns
  it and no other account can change it. When setup succeeds it notes so in
  `~/.config/goodall/first-sign-in-done`, and no later sign-in repeats a
  setup step. When setup fails, nothing is noted and the next sign-in tries
  again.
- **In the eval account, every time**, it starts a work block on your main
  workstream, as a background start, with a notification saying recording
  started because you signed in. If a work block is already active it
  starts nothing.

launchd shows a program's output to nobody, so in the eval account
everything `goodall sign-in` prints also goes to
`~/Library/Logs/goodall/sign-in.log`, readable by that account only. After
the first sign-in, and whenever setup fails, goodall opens that log in
your text editor, so you can read what it did and what to do next.

Setup commits the record repo's first files, so git needs a name and email,
and a new account has none. Goodall sets them in the record repo's own
settings only: your display name, and your contributor id at
`goodall.invalid`, an address that reaches nobody. The eval account's global
git settings are left alone ([answers-file.md](answers-file.md), "The record
repo's git identity").

### Turning off recording at sign-in

Starting a work block at sign-in is on by default in an eval account: the
account is the consent boundary, so signing in means recording (ADR 0021).
To turn it off, add this line to `~/.config/goodall/local-settings.toml` in
the eval account:

```toml
start_at_sign_in = false
```

Leave it out, or write `true`, to turn it back on. Nothing ever starts at
sign-in in mixed account mode.

### Signing out

Signing out should stop the work block as a background stop. The stop is
built: `goodall stop --background sign-out` ends it and shows a notification
saying so. **What runs it at sign-out is not built**, because whether macOS
lets anything run dependably at sign-out is unknown until the hand test.
Until then a work block nobody stopped is closed by the three-hour idle
rule at its last activity. Switching to another account, and closing the
lid, do not stop it, on purpose: agents keep working there (ADR 0021).

## What goodall doctor checks

In any account, `goodall doctor` checks, and only checks, under
**First sign-in**:

- goodall at `/usr/local/goodall/bin/goodall` is owned by root and no other
  account can change it;
- the staging folder, the first-run script and the staged answers file are
  owned by root and no other account can change them;
- both LaunchAgents are owned by root, no other account can change them, and
  they still run the first-run script and the menu bar icon, with
  `GOODALL_COMMAND` and `KeepAlive` as above.

On a Mac with none of these files, all three pass with a note. A failure
gives the `sudo chown` and `sudo chmod` commands to fix it from an
administrator account, or says to run the administrator install again,
which writes each file back exactly.

Not built yet: `goodall teardown` (ADR 0022 says it removes these files),
and anything that runs at sign-out.

## Not yet confirmed: the hand test checklist

Everything above was built without running any of it on a Mac. Tick each
item during the hand test (`goodall-ayy.11`), in a throwaway account, and
note what really happened. The same list is a comment on that issue.

- [ ] 1. `sudo env UV_TOOL_DIR=... UV_TOOL_BIN_DIR=... UV_PYTHON_INSTALL_DIR=... uv tool install --managed-python` installs goodall at `/usr/local/goodall/bin/goodall`, with its Python under `/usr/local/goodall/python`, every file owned by root and not writable by others, and a standard account can run it.
- [ ] 2. `/usr/sbin/sysadminctl -addUser <name> -password - -adminUser <administrator> -adminPassword -`, run with `sudo` and the terminal handed over, asks in the terminal for the administrator's password and for the new account's password (once or twice?), and creates the account. Note exactly what it prints. If it does not, try `interactive` in place of `-adminUser ... -adminPassword -`.
- [ ] 3. The account it creates is a standard account (`id -Gn <name>` has no `admin`), with its home folder at `/Users/<name>`.
- [ ] 4. On a Mac with FileVault: `sysadminctl -secureTokenStatus <name>` says the new account has a secure token, and it can unlock the disk at startup (Q79).
- [ ] 5. `sudo` asks for the password once and remembers it for the five `install` commands and `sysadminctl`, so the administrator is asked at most three times in all.
- [ ] 6. `/usr/bin/install -o root -g wheel -m <mode>` leaves each staged file owned by root:wheel with that mode, and `install -d` makes `/Library/Application Support/goodall` the same way.
- [ ] 7. What macOS shows at the new account's first sign-in (its own setup screens), and whether the first-run LaunchAgent runs only after them.
- [ ] 8. A LaunchAgent in `/Library/LaunchAgents`, owned by root, runs the root-owned first-run script at sign-in for a standard account, with `HOME` set to that account's home folder.
- [ ] 9. In every other account, the first-run script runs and changes nothing: no `~/Library/Logs/goodall/sign-in.log` appears there.
- [ ] 10. `open -t` from the first-run LaunchAgent opens the sign-in log in TextEdit in front of the person, after macOS's setup screens.
- [ ] 11. The first sign-in, in an account with no git settings, finishes setup; `git log` in the record repo shows the display name and `<contributor id>@goodall.invalid`, and `~/.gitconfig` was not created.
- [ ] 12. The menu bar icon's LaunchAgent starts the helper at sign-in in a standard account, the helper finds goodall through `GOODALL_COMMAND`, and Quit goodall (exit 0) is not undone while a crash is.
- [ ] 13. The notification at a sign-in start appears under goodall's name, and whether the helper's notification permission question appears first (ADR 0024, Q80).
- [ ] 14. In an administrator account, and any other account where goodall is not set up, the menu bar icon's menu says goodall is not set up there, and nothing else happens.
- [ ] 15. Whether anything runs dependably at sign-out, so `goodall stop --background sign-out` can be triggered; what fast user switching and closing the lid do to a work block started at sign-in (Q81).
- [ ] 16. `dscl . -list /Users` lists every local account, and a home folder left by a deleted account is found at `/Users/<name>`.
