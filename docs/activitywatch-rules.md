# ActivityWatch human-action rules, version 1

These are the rules `goodall.activitywatch` uses to find what the person did
outside any agent, in an ActivityWatch export. Each rule is a function in
`src/goodall/activitywatch.py`, named below. Change a rule, its function and
this page in the same commit, and raise `RULES_VERSION` (ADR 0004).

ActivityWatch is optional and recommended; goodall does not depend on it.
Goodall does not yet capture its data into the log directory (Beads issue
`goodall-fs9.15`); this is the reading side only.

## Private text: read this first

**These actions carry some of the most private text goodall handles**: window
titles, page addresses and titles, file and project paths. They belong to the
**pre-redaction record**. Raw records are kept verbatim and redaction is a
later, separate step, so the parser keeps these values as they are. It never
puts them in a message, an error or a report: they are left out of each
record's printed form (`repr`), an unfamiliar record is described only by
bucket id, position and a reason, and an unreadable file is named by path
only. Anything that publishes or shares these actions must redact them first.

## Who did it

Every action here is labeled as done by a human, with no claim about which
human. ActivityWatch records what happened at one macOS user account; it does
not know who was at the keyboard. This follows the owner's direction that
humans are humans, harnesses are harnesses and agents are agents, whatever
the identity inside each.

## What comes back

`parse_activitywatch_export(path)` returns `actions`, oldest first across all
buckets, and `unfamiliar_records`. Every action has:

- `source`: always `activitywatch_export`.
- `kind` and `rule`: below.
- `bucket_id`, `hostname`, `event_index` (the event's position in its bucket
  as exported) and `event_id` (only the Rust server exports event ids): where
  it was found.
- `timestamp`: the event's start, as written (UTC).
- `duration_seconds`: the event's duration, as written.

| kind | rule | bucket type (watcher) | extra fields |
|---|---|---|---|
| `application_focus` | `application_focus_from_window_event` | `currentwindow` (aw-watcher-window) | `app`, `title`†, `url`† |
| `browser_page_focus` | `browser_page_focus_from_web_event` | `web.tab.current` (aw-watcher-web) | `url`†, `title`†, `audible`, `incognito`, `tab_count` |
| `editor_file_focus` | `editor_file_focus_from_editor_event` | `app.editor.activity` (aw-watcher-vscode and others) | `file`†, `project`†, `language`, `branch`† |
| `away_from_keyboard` | `keyboard_presence_from_afk_event` | `afkstatus` (aw-watcher-afk), `status: afk` | none |
| `active_at_keyboard` | `keyboard_presence_from_afk_event` | `afkstatus`, `status: not-afk` | none |

† private text, left out of the printed form.

## Rules

1. **The file** (`parse_activitywatch_export`). A JSON object with
   `buckets`, either an object keyed by bucket id (what both servers write)
   or a list (what ActivityWatch's JSON schema describes). Anything else
   raises `ActivityWatchExportError`, whose message names the file and what
   to do, and quotes nothing from it. One bucket's export has the same
   wrapper as an export of all buckets.
2. **The bucket** (`actions_from_bucket`). Its `type` picks the rule
   (`RULES_BY_BUCKET_TYPE`); an unknown type is unfamiliar, and the bucket is
   skipped. Each event needs a `timestamp` that `datetime.fromisoformat` reads
   (both `Z` and `+00:00` are accepted; a time with no zone is taken as UTC)
   and a `data` object.
3. **Application focus** (`application_focus_from_window_event`). An event
   with a string `data.app`. `title` may be "excluded" (ActivityWatch's own
   filter), empty or missing; `url` appears only from the macOS watcher, only
   for a browser in front, and is left out (not written as null) otherwise.
   A `url` can come with an empty `title`.
4. **Browser page focus** (`browser_page_focus_from_web_event`). An event
   with a string `data.url`. The browser watcher records the active tab even
   while the browser is not in front; lining it up with application focus is
   a later step.
5. **Editor file focus** (`editor_file_focus_from_editor_event`). An event
   with a string `data.file`. The VS Code watcher writes "unknown" for any
   value it could not read.
6. **Keyboard presence** (`keyboard_presence_from_afk_event`). `data.status`
   "afk" is `away_from_keyboard` and "not-afk" is `active_at_keyboard`; any
   other status is unfamiliar. The AFK watcher decides from keyboard and
   mouse input only, so reading without touching either shows as away.

Durations are kept as written. Events that overlap (an application focus and
a browser page at the same time) are separate actions; nothing is merged.

## What this was based on

The format was read on 2026-11-17 from the `master` branch of each repository below (commit
hashes could not be fetched), plus the documentation site.

- Documentation: https://docs.activitywatch.net/ , the "Buckets and events"
  page (`docs/src/buckets-and-events.rst`) and the REST API page
  (`docs/src/api/rest.rst`), which gives the per-bucket export shape
  `{"buckets": {<bucket_id>: {..., "events": [...]}}}`.
- `ActivityWatch/aw-server`: `aw_server/rest.py` and `aw_server/api.py`
  (`export_bucket`, `export_all`; event `id` is removed on export, and there
  is no `last_updated`).
- `ActivityWatch/aw-core`: `aw_core/models.py` (event `timestamp` as UTC
  ISO 8601, `duration` in seconds), `aw_datastore/storages/peewee.py`
  (bucket keys `id`, `created`, `name`, `type`, `client`, `hostname`,
  `data`), and `aw_core/schemas/export.json` (which describes `buckets` as a
  list).
- `ActivityWatch/aw-server-rust`: `aw-models/src/bucket.rs`, `export.rs`,
  `event.rs`, `duration.rs`, and `aw-datastore/src/export.rs` (bucket keys
  include `metadata` and `last_updated` and no `name`; events keep `id`;
  events newest first).
- `ActivityWatch/aw-watcher-window`: `aw_watcher_window/main.py`, `lib.py`,
  `config.py`, `macos.swift`, `macos_jxa.py` (type `currentwindow`, data
  `app`, `title`, and `url` on macOS).
- `ActivityWatch/aw-watcher-afk`: `aw_watcher_afk/afk.py` (type
  `afkstatus`, data `status` "afk" or "not-afk").
- `ActivityWatch/aw-watcher-web`: `src/background/heartbeat.ts`,
  `client.ts`, `helpers.ts` (type `web.tab.current`, data `url`, `title`,
  `audible`, `incognito`, `tabCount`, and sometimes `profile`, `tab_group`,
  `tab_group_color`, which are not kept).
- `ActivityWatch/aw-watcher-vscode`: `src/extension.ts` (type
  `app.editor.activity`, data `language`, `project`, `file`, `branch`).
- `ActivityWatch/aw-webui`: `src/views/Buckets.vue` (the Export button
  downloads the same endpoints' responses unchanged).

## Checked against a running ActivityWatch

On 2026-11-26 the hand test in Beads issue `goodall-fs9.19` ran ActivityWatch
0.14.3 on macOS 26, in a standard account, and confirmed:

- The Python server (`aw-server`) runs by default, not the Rust one.
- Its export is `{"buckets": {<bucket_id>: {...}}}`, an object keyed by
  bucket id, with bucket keys `client`, `created`, `data`, `events`,
  `hostname`, `id`, `name`, `type`. Exported events have no `id`.
- Timestamps are written `2026-11-26T15:04:05.123456+00:00`: six
  fractional digits and `+00:00`, never `Z`.
- Window events always have `app` and `title`. `title` can be empty (for
  example Safari with its Automation permission denied). `url` is left out,
  not written as null, when there is none. Safari and Chrome give a `url`
  once ActivityWatch has the Automation permission for them; Firefox gives
  one through Accessibility, with no prompt. Safari's private windows give
  a `url` too, and nothing in the event marks them private.
- AFK events have `status` only.

## Not confirmed

- The Rust server's export shape and exact timestamp text; `Z` and
  `+00:00` are both accepted.
- Older servers' export shapes.
- Chrome with its Automation permission denied (expected to match Safari:
  empty title, no `url`).
- Editor watchers other than VS Code's (vim, JetBrains, Neovim) and whether
  they write the same type and keys.
- Any watcher not listed here: its buckets are reported as unfamiliar.
