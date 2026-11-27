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
A standard (non-admin) macOS user account used for one eval and nothing else. It is the boundary of what the eval records: nothing outside it is recorded, and inside it only work in a work block is. The AI provider accounts signed in there need not be made for the eval (ADR 0020).
_Avoid_: Project account, eval machine, eval profile, sandbox

**AI provider account**:
A person's account with a company that offers AI models, such as a Claude account. It may be shared between an eval and other uses; goodall neither requires one made for the eval nor checks which is signed in (ADR 0020). The one place "provider" is the right word: for an application, say source application or app family.
_Avoid_: Project account, eval login, model account

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

**Administrator account**:
A macOS user account that can install software for the whole Mac and create other accounts. Goodall is installed from one, and the eval account is created from one (ADR 0022). An eval account is never an administrator account.
_Avoid_: Admin account, admin user, root, owner account

**Dedicated account mode**:
Recording in an eval account. The recommended way to run an eval: signing in to the account means recording (ADR 0021).
_Avoid_: Eval mode, isolated mode, standard mode

**Mixed account mode**:
Recording work blocks in a macOS user account the person also uses for other things, offered as a second choice so a person can try goodall on their own work (ADR 0021). On the setup page it is labeled "Try it out".
_Avoid_: Own-account mode, try-it-out mode, personal mode, demo mode

**Project**:
The work output itself, made for the outside world: the product, the paper, the business. An eval is the record of its making. Goodall's own things are never named for the project.
_Avoid_: Project (for an eval), project config, project account, project settings

**First sign-in setup**:
The setup goodall completes by itself the first time a person signs in to a new eval account, from pieces the administrator install put in place beforehand (ADR 0022).
_Avoid_: First-login setup, first run, bootstrap

**Administrator install**:
What `goodall onboard` does in an administrator account when the answers file asks for an eval account: it stages first sign-in setup where only root can change it and creates the eval account as a standard account, running each privileged command with the terminal handed over (ADR 0022, docs/administrator-install.md). It sets up nothing in the administrator account itself.
_Avoid_: Admin install, system install, root setup

**First-run script**:
The short script, owned by root, that a LaunchAgent runs at every sign-in to every account. It runs `goodall sign-in`, which does nothing outside the eval account (ADR 0022).
_Avoid_: Login script, login hook, startup script

### Recording

**Recording**:
What goodall is doing while a work block is active. "Record" is the verb goodall uses with people ("goodall is recording", "record a work block"), chosen because it is the clearest word for consent (ADR 0021).
_Avoid_: Tracking, monitoring, logging, capturing (in words shown to a person)

**Menu bar icon**:
Goodall's icon in the Mac's menu bar. It is the recording indicator, showing whether goodall is recording, paused or not recording, and its menu starts, pauses and stops a work block (ADR 0024).
_Avoid_: Recording indicator (as a separate thing), tray icon, status item, menu bar app

**Pause**:
Ending the active work block with the intention of continuing. Recording again starts a new work block that names the one it continues. While paused, goodall is not recording.
_Avoid_: Suspend, break, hold

**Background start**, **background stop**:
A work block started or stopped by something other than the person's own command: signing in or out, a schedule or script, or goodall ending a block by itself. Each shows a notification (ADR 0021).
_Avoid_: Auto-start (as a noun for the event), silent start, automatic session

**Work block**:
A logged stretch of one contributor's work on one workstream, with a start and an end. Many agent sessions can fall inside one work block.
_Avoid_: Session (when a work block is meant), shift, timebox

**Source application**:
An application in which a contributor works with an agent, and whose own records of that work goodall copies: the Claude desktop app, for example.
_Avoid_: Source (unqualified), provider, vendor, tool

**App family**:
The source applications from one maker that keep their records of agent sessions in the same place and form. Named for the product its applications carry, never for a model or a company: "the Claude family", "the Antigravity family", "the Codex family".
_Avoid_: Provider, vendor, platform, ecosystem, OpenAI family, GPT family

**Agent session**:
One conversation with an agent in one source application, identified by that application's own id for it.
_Avoid_: Session (unqualified), run, chat, thread

**Claude session**:
An agent session in Claude Code, identified by Claude Code's own session id.
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
The git repository, separate from goodall and from the work itself, that holds one eval's own records. It is local: goodall commits to it and never pushes (ADR 0023).
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

**Timeline**:
The derived record, one per eval, that lists in time order every entry from every source application's transcripts, from ActivityWatch, and from journal entries and interventions. Each entry holds only times, numbers and values from fixed lists, plus a pointer to its place in a raw record; anything a person or a website could have written stays in the raw record.
_Avoid_: Rich local log, local log, event log, index

**Transcript**:
A source application's own file, or files, recording one agent session.
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
The settings that are true for every contributor to one eval: its name, roster, workstreams, declared scope, intervention categories, and whether it includes ActivityWatch.
_Avoid_: Project config, project file, settings

**Local settings**:
The settings true for one eval account on one machine: where its record repo and log directory are, which contributor it is and their main workstream, which secrets manager it uses, and, when the eval includes ActivityWatch, the port and identifier of the account's own ActivityWatch.
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

**Delegation**:
Handing a piece of work to an AI agent.
_Avoid_: Amplification, disempowerment, automation rate

**Mistake**:
Something wrong in the work that was later noticed. Each mistake is recorded with where it was caught, as one of two values: **caught at review**, before the work left the person's hands, or **caught downstream**, after it had effects. It has three subtypes, by who made it.
_Avoid_: Error, error catch, bug, failure, oversight score, over-reliance, quality of review

**Agent mistake**:
A mistake made in work an AI agent produced.
_Avoid_: AI error, hallucination, model failure

**Human mistake**:
A mistake made in work the person produced, including an instruction they gave an agent.
_Avoid_: User error, operator error

**Hybrid mistake**:
A mistake that the person and an agent both contributed to, such as an agent filling a gap in an unclear instruction the wrong way.
_Avoid_: Shared error, joint failure

**Off-agent work**:
What a person does for the eval outside any agent session, such as searching, reading, or writing instructions for an agent to follow.
_Avoid_: Manual work, human-only work

**Outcome record**:
A record of what actually went out into the world, such as a sent email, against which an agent's draft can be compared.
_Avoid_: Final output, deliverable

**Redaction policy**:
The versioned rules that decide what is removed from records before they are shared. Goodall has a default, and each eval may make it stricter or looser.
_Avoid_: Anonymization, scrubbing, sanitizing

**Export**:
A redacted or derived layer prepared for sharing, stamped with the versions of the redaction policy and derivation rules that produced it. A raw record is never an export.
_Avoid_: Release, dump, dataset
