# Goodall

Goodall records what a person and their AI agents each did while doing real work together, so that the record can be analyzed and published. These are the terms it uses for that record and the setup around it.

## Language

### Evals and accounts

**Open-world eval**:
A study of what AI can do on real work in the real world, with a person working alongside the agents, as opposed to a benchmark run in a sandbox.
_Avoid_: Benchmark, experiment

**Construct**:
The question an eval sets out to measure, written down before the work starts.
_Avoid_: Research question, hypothesis

**macOS user account**:
One login on a Mac with its own home folder, the kind created under Users & Groups in System Settings. Written out in full; shortened to "account" only where the same paragraph has already said "macOS user account".
_Avoid_: Profile, user profile, macOS account, login

**Eval account**:
A standard (non-admin) macOS user account used for one eval and nothing else. It is the boundary of what the eval records.
_Avoid_: Project account, eval machine, eval profile, sandbox

**Development account**:
An ordinary account where goodall itself is developed. It is outside any eval.

**Contributor**:
A person taking part in an eval, identified on every record their eval account produces. Agents are not contributors; records identify them by model and tool.
_Avoid_: User, participant, subject, agent contributor

**Roster**:
The list of an eval's contributors, each with an id and a display name.
_Avoid_: Team list, members

**Workstream**:
A named division of an eval's work, such as engineering or go-to-market, used to attribute effort and cost.
_Avoid_: Lane, track, category

### Recording

**Work block**:
A logged stretch of one contributor's work on one workstream, with a start and an end. Many agent sessions can fall inside one work block.
_Avoid_: Session (when a work block is meant), shift, timebox

**Claude session**:
One conversation with Claude Code, identified by Claude Code's own session id.
_Avoid_: Session (unqualified), run, chat

**Prompt gate**:
The check that refuses a prompt to an agent while no work block is active.
_Avoid_: Session gate, guard, lock

**Heartbeat**:
Evidence, left each time a hook fires, that a work block was still active at that moment.

**Inferred end**:
The end of a work block that nobody closed, taken from its last heartbeat.

### Records

**Record repo**:
The git repository, separate from goodall and from the work itself, that holds one eval's own records and that goodall operates.
_Avoid_: Eval repo, project repo, instance repo

**Log directory**:
The folder, outside any repository, where an eval's raw records are kept.
_Avoid_: Log volume, logs folder

**Raw record**:
Something captured exactly as its source produced it: a transcript or a telemetry file.
_Avoid_: Raw log, source data

**Derived record**:
Something computed from raw records and goodall's own records, which can always be regenerated.
_Avoid_: Processed data, output

**Transcript**:
Claude Code's own file recording one Claude session.
_Avoid_: Log, conversation log, chat history

**Telemetry**:
The stream of events, metrics and traces Claude Code emits about a session as it runs.
_Avoid_: Metrics, OTel data, logs

**Telemetry receiver**:
Goodall's own program that listens on 127.0.0.1 for Claude Code's telemetry and keeps each request as one line, from `goodall start` to `goodall stop`, one per work block (ADR 0002).
_Avoid_: Collector, OTel server, listener

**Receipt log**:
The file `receipts.jsonl` beside the telemetry files, where goodall's receiver records each request it stored: a sequence number, when it arrived, where its line is, and the length and hash of the body as sent (ADR 0013). A raw record, because none of it can be regenerated.
_Avoid_: Index, manifest, audit log

**Journal entry**:
A contributor's written note about what was decided or done, and by whom.
_Avoid_: Log entry, diary entry, note

**Eval config**:
The settings that are true for every contributor to one eval: its name, roster, workstreams, declared scope and intervention categories.
_Avoid_: Project config, project file, settings

**Local settings**:
The settings true for one eval account on one machine: where its record repo and log directory are, which contributor it is and their main workstream, and which secrets manager it uses.
_Avoid_: Local config, machine config, pointer file

**Setup page**:
The page goodall opens in a browser to ask a person the questions needed to set up an eval account. Nothing is listening behind it.
_Avoid_: Wizard, onboarding form, web app, dashboard

**Answers file**:
The file holding one person's answers to the setup questions, produced by the setup page or written by hand for unattended setup.
_Avoid_: Config file, setup file, response file

**Secrets manager**:
A program that stores secrets outside files and can hand one to a program it launches.
_Avoid_: Vault, password manager, keychain

**Secret reference**:
A pointer to a secret held in a secrets manager, which names the secret without containing it.
_Avoid_: Secret, key, credential (for the pointer itself)

**Env file**:
The file in an eval account, named `.env`, that lists environment variable names and the secret reference each resolves to. It holds nothing else.
_Avoid_: Dotenv, secrets file, credentials file

**Shared env file**:
The file in the record repo, named `.env.shared`, that lists the non-secret environment variables every contributor's launched programs need.
_Avoid_: Public env file, plain env file, dev env file

**Declared scope**:
The repositories, and paths inside them, that count as an eval's dataset. Repositories are named by identity, not by where a copy sits on one machine.
_Avoid_: Dataset boundary, org

### Attribution

**Intervention**:
A moment when a person overrode, corrected, redirected or rescued an agent, or had to act because the agent could not.
_Avoid_: Override, correction, human-in-the-loop event

**Actor label**:
The answer, for one entry in a record, to who produced it: human, AI or harness.
_Avoid_: Attribution tag, author, role

**Harness**:
The software around the model that produces entries neither the person nor the model wrote, such as tool results and injected instructions.
_Avoid_: System, scaffold, tool

**Corroboration**:
Checking an eval's own records against records someone else holds and the researcher cannot edit.
_Avoid_: Verification, validation, audit
