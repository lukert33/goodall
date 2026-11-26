# Related work

What already exists that does part of what goodall does, what goodall adds,
and what we have and have not checked. Read this before claiming anything is
new.

Facts below were checked on **2026-11-19** against the pages named. Where a
page was read only in part, or a claim came from a summary, it says so.

## A correction

An earlier survey for this project (7 November 2026) said that no public tool
captures logs, interventions and human-versus-AI attribution during a live
run. **That is wrong for coding sessions.** Entire and the SWE-chat dataset
built on it do this at scale, and Anthropic has published measurements of
human oversight in real agent use. The survey itself is not in this
repository; wherever its claim is repeated, it should be replaced with the
statement under "What goodall adds" below.

## Entire

[entireio/cli](https://github.com/entireio/cli) is an open-source
command-line tool that hooks into git and into coding agents to record agent
sessions alongside the commits they led to.

Checked from the repository and its README:

- MIT license; written in Go; created 2026-01-02; about 5,300 stars; newest
  release v0.13.4 on 2026-11-13; several active contributors; two published
  security advisories (not yet read).
- `entire enable` installs the agent's hooks and Entire's own git hooks.
  No account is needed to capture. Signing in is for its hosted features.
- Sessions are stored as checkpoints under their own git refs
  (`refs/entire/checkpoints/...`) and **pushed to the repository's git remote
  alongside the code**. If the repository is public, anyone can read them.
- The README lists Claude Code, Codex, Cursor, Copilot CLI and Pi, "and
  more". The SWE-chat paper also lists Gemini CLI, OpenCode and Factory
  Droid.

Checked by running it, on 2026-11-25 and 2026-11-26: Entire v0.13.4 with
Claude Code, Codex and the Antigravity desktop app, in throwaway repos with
fake secrets, pushing only to a bare repo on the same Mac. Each agent's own
session files were compared with Entire's copies.

- **Capture is tied to commits.** A checkpoint is made when a commit follows
  agent work, and the commit gets an `Entire-Checkpoint:` trailer. Turns that
  no commit follows stay in a local working folder and never reach a
  checkpoint. Of an interrupted turn, Entire kept the prompt text at most.
- **A checkpoint is an edited copy, not the agent's record.** For Claude Code
  and Codex the system prompt is blanked and the text of instruction files
  is removed. Codex's encrypted reasoning is removed, and Claude Code's
  subagent transcripts are absent. For Antigravity the copy matches line for
  line, but Antigravity's own record already lacks the system prompt and the
  instruction files.
- **Redaction is always on, and uneven.** Token-shaped secrets were replaced.
  An AWS key ID, a plain password, an email address and a phone number
  survived the default settings; a setting for personal details exists.
  Turning every scanner off makes Entire's settings fail to load, and it
  then captures nothing without saying so. The local working copy, before a
  commit, is not redacted.
- **It can fail silently.** Until the project was trusted inside Codex, Codex
  ignored Entire's hooks: no trailer, no checkpoint, no message from Entire.
- **It can change the session it records.** With Codex, Entire's
  session-start hook adds a message telling the agent to run
  `entire agent-help`, and Codex did so before starting on the prompt.
- **Checkpoints can be kept local.** Uploading on `git push` is the default.
  A manual mode and a `--skip-push-sessions` option keep them on the Mac.
- **Attribution is per commit, not per line.** `entire blame` labels each
  line with the agent of the checkpoint attached to that line's commit, and
  says itself that this is not necessarily who wrote the line. We found no
  record of which lines the person wrote and which the agent wrote.

Not tested: Cursor, Copilot CLI, Pi and the other agents; a command the
person refuses, with Codex or Antigravity (neither asked for permission).
The two security advisories are still unread.

**Decision (owner, 2026-11-19): evaluate building goodall on top of Entire's
session logging and attribution, not from scratch.** This was an evaluation,
not an adoption. It had two things to settle:

1. **Entire pushes session transcripts to the git remote by default.**
   Goodall's rule is that raw records stay on the Mac (ADR 0017). Settled:
   Entire can keep checkpoints local.
2. **Entire would be a program goodall runs**, in the eval account, with the
   account's access. It needs the same provenance check as git or `op`
   before it is adopted (`docs/DEPENDENCIES.md`), including reading its two
   advisories. Not done, because Entire was not adopted.

**Outcome (owner, 2026-11-26): goodall keeps its own capture and is not
built on Entire.** Goodall's rule is that raw records stay verbatim, and
Entire's checkpoints are edited copies that miss whatever no commit follows.
Goodall's copy of a Claude Code session was byte for byte the agent's own
files, subagent transcripts included. Two uses of Entire stay open: as an
extra raw source for agents goodall does not yet capture, with uploading
turned off and after the provenance check above; and its redaction layers
as a reference for `goodall release`.

## SWE-chat

[SWE-chat](https://arxiv.org/abs/2604.20779) (Baumann, Padmakumar, Li, Yang,
Yang and Koyejo, Stanford; version 2 of 1 October 2026; accepted at COLM
2026) is a dataset of real coding-agent sessions collected from public
repositories whose developers opted in to Entire's logging.

Checked from the abstract and the first part of the paper:

- Almost 18,000 sessions, more than 229,000 user prompts, 2 million agent
  tool calls. Claude Code is about 69% of the data.
- "Only 59% of all agent-produced code survives into user commits" (abstract).
- Users push back in 50% of turns (abstract); the introduction gives 45.7%
  pushback and 4.5% interruptions per turn.
- Vibe-coded code has roughly 3.8 times more security vulnerabilities per
  committed line than code people write themselves.
- Section 5 is titled "Autonomy is outpacing oversight". Its body was not
  read.

Not confirmed: that version 1 reported 44% where version 2 reports 59%. A
summary of the unread part suggests the two figures may measure different
things. Do not quote the comparison until the paper has been read in full.

The part read is entirely about code. Whether it says anything about prose or
business documents kept in git was not found.

## Anthropic: measuring agent autonomy in practice

[Measuring AI agent autonomy in practice](https://www.anthropic.com/research/measuring-agent-autonomy)
(Anthropic, 18 February 2026) reports on human oversight in real use of
Claude Code and the API. Among its figures: new users run with full
auto-approve in roughly 20% of sessions, rising to over 40% by 750 sessions;
experienced users interrupt more often (about 9% of turns) than new ones
(about 5%).

## Davidson: a skeptic's reading of gradual disempowerment

[Thoughts on Gradual Disempowerment](https://alignmentforum.org/posts/ct6SMDuexe9uBwDoL/thoughts-on-gradual-disempowerment)
(Tom Davidson, AI Alignment Forum, 15 August 2025) is a critique of the
gradual disempowerment paper, marked by its author as very rough notes. Read
in full on 2026-11-20 from a PDF of the post.

- It makes no call for data collection. It argues the opposite of urgency: a
  gradual risk can be studied as it unfolds, so work on it can wait. Do not
  cite it as support for "better data is needed".
- It treats thin oversight as the competitive default and not as harm in
  itself: an owner can let their AIs act with no checking in and still keep
  the gains, and handing decisions to well-aligned AI could be good. This is
  a reason goodall's research output records where mistakes are caught and
  carries no oversight score (ADR 0018).
- Its main complaint is that the paper stays abstract and never shows how
  strong the competitive pressure is. A firm-level record is the kind of
  concrete evidence it says is missing, though it does not ask for one.

## What goodall adds

Goodall is **not the first measurement of human oversight of AI agents in
real work**. SWE-chat and Anthropic's study both do that, at a scale one
person cannot match. What is new in goodall:

- **Work beyond coding.** Strategy documents and other business writing kept
  in git, so that line attribution covers them too. SWE-chat's data is code.
- **Off-agent activity.** What the person did outside any agent session:
  searching, reading, writing instructions for agents. Captured with
  ActivityWatch (ADR 0016).
- **Mistakes under real stakes.** Where each mistake was caught: before it
  left the person's hands, or after it had effects (ADR 0018).
- **One eval, start to finish, with a pre-registered construct**, in the
  CRUX sense of an open-world eval.

## Limits to say out loud

- **One kind of user so far.** A technical founder who keeps strategy in git
  is not a typical founder. Other people running goodall, especially outside
  coding, is part of the plan, and the first group is not yet named.
- **Goodall is an instrument.** It records delegation and where mistakes were
  caught. It does not claim a framework for telling amplification from
  disempowerment; that analysis belongs to whoever uses the data.

## Not yet checked

Cited in project notes but not opened, or read only in summary. Do not cite
them in anything published until they have been read.

- Drago and Laine, "The Intelligence Curse": not opened.
- Kulveit and others, "Gradual Disempowerment" (2025): sections 1 to 4 read
  directly on 2026-11-17; sections 5 to 8, including its proposed metrics, in
  summary only.
- Jain and others, "Human-AI Complementarity: A Goal for Amplified Oversight"
  (2025, revised 2026): first part read directly; the rest in summary.
