# The menu bar icon

Goodall puts one icon in the Mac's menu bar (ADR 0024). It is the recording
indicator: it shows, for as long as you are signed in, whether goodall is
recording. Its menu is the way to start, pause and stop a work block without
a terminal.

This page describes the first version: what it shows, the command it reads
its state from, how to build and try it by hand, and what is not built yet.
**It is not installed by anything yet.** Until onboarding installs it, the
only way to see it is to build it yourself, as below.

## What it shows

| Look | Meaning |
| --- | --- |
| Orange binoculars with a filled dot in each lens | Goodall is recording. |
| Binoculars with an upright bar in each lens | Recording is paused. Goodall is not recording. |
| Binoculars with empty lenses | Goodall is not recording. |

The icon shows no running time. Hold the pointer over it, or open the menu,
to read which workstream is being recorded and since when.

A `!` beside the icon means the last thing you chose from the menu did not
work. It goes away when you open the menu.

## The menu

| Item | The one command it runs |
| --- | --- |
| A gray status line, such as "Recording research since 09:30" | none |
| **Record work block** ▸ one item per workstream, your main workstream first and marked "(main)" | `goodall start --workstream NAME` |
| **Pause** | `goodall pause` |
| **Stop recording** | `goodall stop` |
| **Settings…** | `goodall settings` |
| **Quit goodall** | `goodall stop` if goodall is recording or paused, then the icon leaves |

- Items that make no sense right now are gray: you cannot pause when nothing
  is recording, or record while a work block is already active.
- While a command runs, the first line says what it is doing ("Preparing to
  record…", "Pausing…", "Stopping…", "Opening settings…") and every item is
  gray, so a second click starts nothing. An open menu changes to the new
  state as soon as goodall has said what the command changed.
- The menu never asks a question, and it never opens a window or a dialog.
- **Quit goodall** stops any recording cleanly, exactly as **Stop recording**
  would, and then removes the icon. If stopping fails the icon stays, because
  goodall would still be recording with nothing on screen to say so. The
  icon comes back when you next run `goodall start`, and, once onboarding
  installs its LaunchAgent, when you next sign in.
- **Settings…** will reopen the setup page with your current answers. The
  setup page is not built, so `goodall settings` is a stub: choosing the item
  counts as a command that did not work, and the stub's message, which says
  how to change your settings by hand, is in the log file named below.

### When a command does not work

Nothing pops up. The menu gains two gray lines: which item did not work, and
where the details are. The icon shows a `!` until you open the menu. Everything
goodall printed is added to the log file
`~/Library/Logs/goodall/menu-bar-icon.log`, readable by your macOS user
account only. Goodall's own message is not shown in the menu because the menu
has room for a line, not a paragraph, and because those messages use words
the menu must not (ADR 0021). Running the same command in a terminal shows
the full message with its next step.

## Where the logic is

The icon is a small program written in Swift, because an icon in the menu
bar cannot be made with Python's standard library. Its source is two
commented files, written to be read by someone who does not know Swift:
[`menu-bar-icon/MenuModel.swift`](../menu-bar-icon/MenuModel.swift) decides
what to show, and [`menu-bar-icon/main.swift`](../menu-bar-icon/main.swift)
draws it and runs goodall (ADR 0025).

It decides as little as it can. It runs `goodall icon-state --json`, draws
what the answer says, and runs one goodall command per menu item. The words
of the status line, which items are enabled, the list of workstreams and
which is the main one all come from the Python (`src/goodall/icon_state.py`).
To change what the icon says, change the Python. What the helper does
decide (the line shown while a command runs, when a failure shows, what
Quit goodall does first) is in `MenuModel.swift`, and
`tests/test_menu_bar_icon_model.py` tests it by running it.

It asks goodall for the state:

- when it starts;
- whenever the folder named in `watch_folder` changes, which is how a
  `goodall start` typed in a terminal shows in the icon within a second;
- after every menu command;
- once a minute, in case a change was missed, and so that a work block left
  idle past the idle limit stops showing as recording.

Killing the helper ends nothing: a work block is a file in the log
directory, and goodall's telemetry receiver is a separate program.

## `goodall icon-state`

`goodall icon-state --json` prints one JSON document saying what the icon
should show. Without `--json` it prints the status line as text.

It is a command of its own, and the icon does not use `goodall status`,
because `goodall status` settles what earlier work blocks left behind: it
can close a forgotten work block, commit to the record repo and copy agent
sessions. The icon asks every minute without anyone asking it to, so its
command **only reads**. It writes no file, starts no program, and closes
nothing. A flag on `goodall status` would have given one command two
promises, two exit code rules and two documents.

Exit code: `0` when goodall can be used in this account, `1` when it cannot.
The document is printed either way.

```json
{
  "schema_version": 1,
  "state": "recording",
  "status_line": "Recording research since 09:30",
  "account_mode": "dedicated",
  "work_block": {
    "id": "20261116T093000Z-ada-lovelace-1a2b3c4d",
    "workstream": "research",
    "started_at": "2026-11-16T09:30:00Z"
  },
  "paused_work_block": null,
  "workstreams": [
    {"name": "research", "main": true},
    {"name": "engineering", "main": false}
  ],
  "enabled": {"record_work_block": false, "pause": true, "stop_recording": true},
  "setup": {"usable": true, "reason": null},
  "watch_folder": "/Volumes/EvalLogs/goodall-pilot/work-blocks"
}
```

| Key | What it holds |
| --- | --- |
| `schema_version` | `1`. Raised when a key changes meaning or goes away. |
| `state` | `recording`, `paused` or `not_recording`. A work block left idle past the idle limit is `not_recording`, as it is for the prompt gate; this command leaves it for the next `goodall start`, `stop` or `status` to close. |
| `status_line` | The first line of the menu, in words for a person. Times are in this Mac's local time. |
| `account_mode` | `dedicated` or `mixed`, from local settings. `null` when setup is not usable. |
| `work_block` | While recording: the active work block's `id`, `workstream` and `started_at` (UTC). Otherwise `null`. |
| `paused_work_block` | While paused: the paused work block's `id`, `workstream` and `paused_at` (UTC). Otherwise `null`. |
| `workstreams` | Every workstream in the eval config, as `name` and `main`. The contributor's main workstream is first and is the only one with `main` true. Empty when setup is not usable. |
| `enabled` | Which menu items make sense now: `record_work_block`, `pause`, `stop_recording`. All false when setup is not usable. |
| `setup` | `usable` is false when goodall cannot record in this account at all, for example with no local settings or an unreachable log directory. `reason` is then one sentence for a person, which points at `goodall status`; otherwise `null`. |
| `watch_folder` | The folder whose changes mean "ask again": the one holding `active-work-block.json` and `pause.json`, or, before the first work block has made that folder, the log directory it will appear in. `null` when setup is not usable. |

Nothing in `status_line` or `setup.reason` says "eval": the icon shows them
as they are.

## How the helper finds goodall

Whoever starts the helper tells it where goodall is, in the environment
variable `GOODALL_COMMAND`: the full path of the `goodall` program. The
helper is started by `open` or by launchd, which give it almost no `PATH`,
so it cannot look goodall up, and it never searches.

- `goodall start` sets the variable when it brings the icon back: it names
  the `goodall` beside the Python that is running it, or failing that the
  one on its `PATH`.
- The LaunchAgent that will start the icon at sign-in must set it too, in
  its `EnvironmentVariables`. That LaunchAgent is not built yet.

A helper started without the variable, for example by double-clicking
goodall.app, says so in its menu and offers only **Quit goodall**.

## Where it is installed

`/Applications/goodall.app`, the folder the whole Mac shares. One copy
serves every macOS user account, in both account modes. Only an
administrator can change what is in that folder, so an eval account, and
the agents working in it, cannot swap the recording indicator for one that
shows something else. The administrator install will put it there
(ADR 0022). A copy in `~/Applications` is not looked for.

The place also has to be stable: macOS remembers notification permission by
where an application is, and the spike was refused permission from a
temporary folder. The spike was given permission from `~/Applications`;
`/Applications` has not been tried yet.

`goodall start` looks for the helper there. If it finds it, it runs
`open -g --env GOODALL_COMMAND=... /Applications/goodall.app`, which starts
the helper without taking the keyboard, and does nothing to one that is
already running. If it does not find it, it starts no program at all, so
`goodall start` is no slower on a Mac without the helper. A helper that
cannot be started is a warning; the work block has started regardless.

## Build it and try it by hand

Building needs Apple's Command Line Tools (`xcode-select --install`), not
Xcode, and a Mac with an Apple chip.

```sh
menu-bar-icon/build.sh
```

This compiles `menu-bar-icon/main.swift` into
`menu-bar-icon/build/goodall.app` and signs it on this Mac. The `build`
folder is ignored by git; no compiled program is committed.

To try it against the eval this account is set up for, without installing
anything:

```sh
GOODALL_COMMAND="$(command -v goodall)" \
    menu-bar-icon/build/goodall.app/Contents/MacOS/goodall-menu-bar-icon
```

The icon appears and the terminal stays busy; press Control-C, or choose
**Quit goodall**, to end it. Run `goodall start` and `goodall stop` in
another terminal and watch the icon follow.

To try it the way `goodall start` will find it, from an administrator
account:

```sh
cp -R menu-bar-icon/build/goodall.app /Applications/
goodall start
```

Remove it again with `rm -rf /Applications/goodall.app` after choosing
**Quit goodall**.

## What is not built yet

- **Installing.** Nothing copies goodall.app to `/Applications`, and
  nothing installs the LaunchAgent that starts it at sign-in and brings it
  back if it dies (`KeepAlive` with `SuccessfulExit` false, so **Quit
  goodall** is not undone). Both belong to onboarding.
- **Shipping.** How the built helper travels inside goodall's package, and
  what that does to the package's platform tag, is not decided (ADR 0024).
- **Notifications.** A background start or stop will show a notification
  under goodall's name from this same helper. The bundle is named and
  identified for that, but the helper sends none yet.
- **The timeout** for mixed account mode, and its "Keep recording"
  notification.
- **Settings…** needs the setup page.
- **`goodall doctor`** does not check the helper yet.
- **Tried by a person once.** On 2026-11-27 every menu item was clicked
  against a throwaway eval, in light and dark menu bars, from a development
  account. It has not been run in a standard account or from a LaunchAgent.
  What the menu shows after each event is tested automatically (ADR 0025);
  what AppKit does with it is not. After changing `main.swift`, check by
  hand that an open menu changes when a command finishes, and that the
  three looks are right in light and dark menu bars.
