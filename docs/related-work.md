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

- MIT licence; written in Go; created 2026-01-02; about 5,300 stars; newest
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

Not confirmed from the README: line-level attribution of human versus agent
authorship. The README describes a checkpoint trailer on each commit. The
attribution claim comes from the SWE-chat paper, which mentions it without
giving the method in the part we read.

**Decision (owner, 2026-11-19): evaluate building goodall on top of Entire's
session logging and attribution, not from scratch.** This is an evaluation,
not an adoption. Two things the evaluation must settle first:

1. **Entire pushes session transcripts to the git remote by default.**
   Goodall's rule is that raw records stay on the Mac (ADR 0017). Either
   Entire can be configured to keep checkpoints local, or goodall cannot use
   its storage.
2. **Entire would be a program goodall runs**, in the eval account, with the
   account's access. It needs the same provenance check as git or `op`
   before it is adopted (`docs/DEPENDENCIES.md`), including reading its two
   advisories.

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

## Davidson: a sceptic's reading of gradual disempowerment

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
  a reason goodall records where errors are caught and judges nothing
  (ADR 0018).
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
- **Errors under real stakes.** Where each error was caught: before it left
  the person's hands, or after it had effects (ADR 0018).
- **One eval, start to finish, with a pre-registered construct**, in the
  CRUX sense of an open-world eval.

## Limits to say out loud

- **One kind of user so far.** A technical founder who keeps strategy in git
  is not a typical founder. Other people running goodall, especially outside
  coding, is part of the plan, and the first group is not yet named.
- **Goodall is an instrument.** It records delegation and where errors were
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
