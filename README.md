# Goodall

Goodall records what a person and their AI agents each did while doing real
work together, so that the record can be analyzed. It also includes tools for
applying a redaction policy to the raw record, to protect personally 
identifiable information (PII) or trade secrets.

It is for researchers who run **open-world evals**: studies of what AI can do
on real work in the real world, with a person working alongside the agents,
rather than a benchmark run in a sandbox. You do not need to be a software
engineer to use it, but you do need a Mac, a terminal, and someone (possibly
you) who can install programs on that Mac.

## Current scope of goodall

Goodall is an instrument. It measures **delegation**: who did what, and
where each mistake was caught.

Its first analytic priority is **where mistakes are caught**: at review,
before the work left the person's hands, or downstream, after it had
effects.

For the tools and studies that already do part of this, see
[docs/related-work.md](docs/related-work.md).

## Status: being rebuilt after a closed source prototype phase

Goodall is being rebuilt from scratch after I validated the security posture with a prototype. 
Much of what this page describes is planned, not built. Do not rely on goodall for a real eval yet.

These commands work today:

| Command | What it does |
|---|---|
| `goodall status` | Shows whether a work block is active, and what is wrong with the setup if anything. |
| `goodall start` | Begins a **work block**: a logged stretch of your work on one workstream. This opens the prompt gate. |
| `goodall stop` | Ends the current work block, copies every agent session that has a record inside it into the log directory, and builds the timeline. This closes the prompt gate. |
| `goodall pause` | Ends the current work block, meaning to continue it, and does everything `goodall stop` does. The next `goodall start` begins a new work block whose record names the paused one. |
| `goodall fixblock` | Corrects which workstream a work block was for, by recording a fix beside it. |
| `goodall journal` | Adds a **journal entry**: your written note about what was decided or done. |
| `goodall intervene` | Records an **intervention**: a moment you overrode, corrected or rescued an agent. |
| `goodall timeline` | Shows one ended work block in time order: who did what and when, with no private text ([docs/timeline.md](docs/timeline.md)). |
| `goodall doctor` | Checks the account and setup, and changes nothing. |
| `goodall hook` | Called by Claude Code, not by people. It is the prompt gate, and it leaves heartbeats. |

These commands exist but are not built yet: `onboard` (setup), `sync`, `logs`,
`export scout`, `dry-run`, `teardown` and `release`. Each tells you so when you
run it, and `goodall onboard` says how to do its job by hand.
The full by-hand steps are in [docs/setup-by-hand.md](docs/setup-by-hand.md).

For an eval that includes ActivityWatch, goodall copies what apps and windows
were in use into the log directory when a work block ends
(`docs/activitywatch-rules.md`), and the timeline lists them. The
telemetry receiver is written but not yet started and stopped by a command.
Copying agent sessions works for the Claude family:
the Claude desktop app's Code tab and Claude Code in a terminal
(`docs/source-applications.md`).

## Words used here

- **Eval account**: a standard (non-admin) macOS user account used for one
  eval and nothing else. It is the boundary of what the eval records:
  nothing outside it is recorded, and inside it only work in a work block
  is. You can sign in there with an AI provider account you already have,
  such as your own Claude account; it does not need to be made for the eval.
- **Work block**: a logged stretch of one contributor's work on one
  workstream, with a start and an end.
- **Prompt gate**: the check that refuses a prompt to an agent while no work
  block is active. This is deliberate: unrecorded work is the worse failure.
  It is on in **dedicated account mode**, the recommended setup, where
  goodall records in an eval account. In **mixed account mode**, for trying
  goodall in a macOS user account you also use for other things, it is off:
  no prompt is refused, and only work inside a work block is recorded. Any
  agent conversation held during a work block is copied whatever it was
  about. The mode is one line in local settings
  ([docs/setup-by-hand.md](docs/setup-by-hand.md)).
- **Transcript**: Claude Code's own file recording one conversation (a
  **Claude session**).
- **Telemetry**: the stream of events Claude Code reports about a session as
  it runs.
- **Raw record**: something captured exactly as its source produced it.
- **Derived record**: something computed from raw records, which can always
  be made again.

The full list is in [GLOSSARY.md](GLOSSARY.md).

## Install

You need Python 3.12 or newer. The Python that comes with macOS is older
than that, so most Macs need a new one. The installer **uv** fetches a
suitable Python for you, which is why it is the documented route.

Goodall is not on a package index (PyPI) yet, so today you install it from a
copy of this repository:

```
git clone https://github.com/lukert33/goodall.git
uv tool install ./goodall
```

Then run `goodall status` to check that the command works. Use uv 0.13.4 or
newer: older versions trust a certificate authority whose keys were
compromised.

Other routes are allowed but have not been tested: **pipx** or **pip** in a
virtual environment, each with a Python 3.12 or newer installed separately.
The choice belongs to whoever owns the Mac.

**Placeholder:** where on the Mac goodall should be installed so that an eval
account can run it has not been settled. An eval account is a standard
account and cannot install software for the whole Mac. This will be decided
when the first eval account is set up on a real Mac. Until then, install it
somewhere the eval account can run it, and check with `command -v goodall`
signed in as that account.

### What you need, required and optional

Goodall itself has no Python packages it depends on. These are the other
programs it works with. The "who installs it" column matters because an eval
account cannot install software for the whole Mac: anything marked
administrator must be installed from an administrator macOS user account on
the same Mac, before the eval account can use it. This column is written from
how each program is normally installed. It has not yet been tried in a real
eval account.

| Program | Required? | Who installs it | What it is for |
|---|---|---|---|
| Python 3.12 or newer | Required | Comes with the installer | Runs goodall. |
| uv, pipx or pip | Required, to install | An administrator if installed with Homebrew or for every account. uv can also be installed inside one account. | Puts goodall on the Mac. |
| goodall | Required | Whoever ran the installer | Records the eval. |
| git | Required | Administrator | Saves the eval's records as commits in the record repo, on this Mac. |
| Claude Code | Required | Not yet checked | The agent tool whose work goodall records. |
| 1Password's `op` | Required only if the eval uses secrets. An eval with no secrets does not need it. | Administrator | Looks up secrets the eval needs, without goodall storing them. |
| The export extra | Optional | Whoever installed goodall | Turns records into the form the Inspect analysis tools read. It brings in `inspect-ai` and `inspect-scout`, about 95 packages, so install it only if you export. **Placeholder:** the install command is not decided, and the export is not built. |
| ActivityWatch | Required in every eval account when the eval config says `activitywatch = true`; otherwise not used | Administrator | Notes which app and window were in use, to back up what the human did. Goodall copies its data into the log directory when a work block ends. It records window titles and page addresses, which can contain private text. |

Signing in is separate from installing, and always happens inside the eval
account: Claude Code and, when the eval uses secrets, 1Password.

For the checked facts behind each program, see
[docs/DEPENDENCIES.md](docs/DEPENDENCIES.md).

## Where your data goes

Goodall holds no data about any eval. Everything an eval produces lives in
one of three places.

1. **The tool.** Goodall's own code, installed on the Mac. It contains none
   of your eval's data, so updating it never touches your records.

2. **The record repo.** A git repository, separate from goodall and from the
   work being studied, that holds the eval's own records as readable files:
   the eval config (`eval.toml`: name, roster of contributors, workstreams,
   declared scope, and whether the eval includes ActivityWatch), the construct (`CONSTRUCT.md`, the question the eval
   measures), a shared env file (`.env.shared`, non-secret variables), and
   one file per work block, journal entry, intervention and work block fix.
   Goodall commits to it and never pushes it anywhere: it is a git
   repository on this Mac, and its history shows any later edit to a
   record. If you add a remote yourself, goodall leaves it alone and
   `goodall doctor` says so. There is no copy off the Mac unless you make
   one, so back the record repo up with whatever you already use for your
   Mac.

3. **The log directory.** A folder on the Mac, outside any repository, that
   holds the raw records: copies of each source application's own session
   files, in that application's own layout, and telemetry. For the Claude
   family that is its transcripts in `transcripts/` and a few allow-listed
   files from Claude Code's config folder in `claude-config/` (the prompt
   history and file-edit backups, never settings or credentials). An agent
   session is copied when a work block ends, if any of its records carries
   a time inside the work block (`docs/source-applications.md`). Goodall's
   own working notes sit beside them in separate folders, and so does the
   timeline, in `timeline/`: a derived database that lists what happened in
   time order and holds no private text, only pointers into the raw records
   (`docs/timeline.md`). It can be deleted and built again at any time.

Two small files sit in the eval account's home folder and belong to neither
place: the local settings (`~/.config/goodall/local-settings.toml`: where the
record repo and log directory are, which contributor this account is, and,
when the eval includes ActivityWatch, this account's ActivityWatch port and
identifier) and the env file (`.env`), which holds secret *references* only.

### ActivityWatch settings

ActivityWatch is decided for the whole eval, in `eval.toml`:

```toml
activitywatch = true   # every contributor runs ActivityWatch; leave it out, or write false, for an eval without it
```

When it is `true`, each eval account's local settings record that account's
own copy of ActivityWatch:

```toml
activitywatch_port = 5610                                          # any free port except 5600
activitywatch_device_id = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"   # this account's own, made-up here
```

The port may not be 5600, ActivityWatch's default. Ports are shared by every
macOS user account on the Mac, and ActivityWatch silently joins any copy
already answering on its port, so two accounts on 5600 would mix their
records. Set the same port in ActivityWatch's own settings for its server
and its watchers. The identifier is the `device_id` ActivityWatch shows at
`/api/0/info` on that port. `goodall doctor` fails when the eval includes
ActivityWatch and either setting is missing; other commands keep working.
In an eval without ActivityWatch the two settings are ignored.

Secrets never go in a file. The env file holds references such as
`op://vault/item/field`, which name a secret in 1Password without containing
it. Goodall rejects a raw secret value given as input.

Goodall makes no network calls of its own and never pushes the record repo,
so nothing leaves the Mac unless you send it yourself.

### What you share

Raw records stay on the Mac. You share only a **redacted** layer or a
**derived** layer, never the raw one. None of this is built yet; the decision
is recorded in
[ADR 0017](docs/adr/0017-raw-records-stay-local-and-every-export-carries-its-rule-versions.md).

## What goodall promises about your data

- **Raw records stay verbatim.** Transcripts and telemetry are copied as
  they are, in the original folder layout, and never modified. If Claude
  Code later rewrites a source, goodall keeps the earlier copy beside the new
  one, and nothing is deleted.
- **Everything computed is derived and can be remade.** Labels and any
  analysis database are made from the raw records, never the other way around.
- **Labels come from versioned rules.** Each entry in a record is labeled
  `human`, `ai` or `harness` (the software around the model, producing tool
  results and injected instructions) after the fact, by documented rules
  with a version number. A better rule later means relabeling the same data.
- **Capture does not depend on analysis.** Recording works with none of the
  Inspect packages installed.
- **The record repo stays on this Mac.** Goodall commits records to it and
  never pushes, fetches or adds a remote, so nothing is uploaded.

### Honest limits

- **Telemetry may have a line break replaced.** Telemetry is stored one
  request per line. If a request arrives containing a line break, goodall
  replaces it with a space to keep the line intact, and says so in a receipt
  log kept beside the data. The receipt holds a fingerprint (SHA-256) of the
  original, so the change can be detected but the original cannot be
  restored. Claude Code sends compact JSON, so this has not happened in
  practice. Telemetry is therefore verbatim "unless its receipt says
  otherwise". The receipt log is kept and published with the raw record.
- **Transcripts are in Claude Code's internal format.** It is not documented,
  not versioned, and changes between Claude Code releases. Goodall keeps
  copies exactly as written because a copy can then always be checked against
  its original, but what the copies contain will change as Claude Code does.
  Labeling rules were drawn from Claude Code 2.1.x transcripts and are
  provisional.
- **Telemetry can be lost silently.** Claude Code drops telemetry when nothing
  is listening. Transcripts are copied regardless, and are the record of
  last resort. A gap in the receipt log's sequence numbers shows that
  something went missing.
- **Records are corrected, not edited.** If a work block's workstream was
  damaged and recorded as `unknown`, the correction is a new file beside it
  (`goodall fixblock`), and analysis reads both.

## What is not supported in this version

- **Two evals in one macOS user account.** One eval account holds exactly one
  eval, so goodall never has to guess which eval a command belongs to.
- **Secrets managers other than 1Password.** The design allows another that
  keeps secrets out of files and can launch a program with the secret already
  set. Only 1Password's `op://` references work today. Contributions are
  welcome.
- **Email.** Sent email is the record of what actually went out to
  customers and suppliers, and it lives outside git. How to capture it is
  undecided.
- **Anything off macOS.** Eval accounts are macOS user accounts.

## Contributing

Guidance for people and agents who work on goodall is in [AGENTS.md](AGENTS.md).
Decisions and their reasons are in [docs/adr/](docs/adr/). Dependencies are
added only after a recorded check, as [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md)
describes.

Goodall is released under the license in [LICENSE](LICENSE).
