# AGENTS.md

Goodall is an open-source tool for instrumenting open-world evals: it records
what a human and their AI agents each did while doing real work together, so
the record can be analysed and published. It is being rebuilt from scratch;
most of what is described below does not exist in code yet.

This file is the single source of guidance for every agent and human
contributor.

## Where guidance lives

- **Conventions and invariants**: this file. A subdirectory that needs its own
  guidance gets its own `AGENTS.md`.
- **`CLAUDE.md` is a pointer.** In every directory it contains only an
  `@AGENTS.md` import. Put new guidance in `AGENTS.md`.
- **Decisions and their reasoning**: `docs/adr/`. Read the ADRs for an area
  before changing it. Record a new one, as a short paragraph, only when the
  decision is hard to reverse, surprising without context, and the result of
  a real trade-off.
- **Vocabulary**: `GLOSSARY.md`. Use its terms in code, tests, issues and
  prose.
- **Working notes and spike findings**: Beads memory (`bd memories <keyword>`,
  `bd remember "..."`).
- **Tasks**: `bd` (see the Beads sections below).

## Agent skills

### Issue tracker

Issues live in Beads, driven by the `bd` command; this repo does not use
GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels, applied as Beads labels. See
`docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` at the repo root and ADRs in `docs/adr/`.
See `docs/agents/domain.md`.

## Two kinds of machine account

Goodall imposes strict rules on an **eval account**: a standard (non-admin)
macOS user account used only for one eval. Those rules are product behaviour, to be
enforced by `goodall doctor` and `goodall onboard`.

Developing goodall happens in an ordinary **development account** and is
outside any eval. Eval-account restrictions apply to what goodall checks, and
the sections below are what apply to you while building it.

## Invariants

Each of these is a contract. A change that breaks one needs a new decision
first.

1. **Goodall holds no instance data.** An eval's config, construct, journal
   and intervention log live in that eval's record repo; raw logs live in its
   log directory.
2. **Raw records stay verbatim.** Captured transcripts and collector output
   are copied, kept in their original layout, and left unmodified. Everything
   computed from them (labels, a Scout database) is derived and regenerable.
3. **Labels come from versioned rules.** Actor labels (`human`, `ai`,
   `harness`) are computed from raw data by documented rules that carry a
   version.
4. **Secrets stay in the secrets manager.** Files hold only references into
   it (1Password's `op://` references are the one kind supported today); raw
   secret values given as input are rejected.
5. **Diagnostics are read-only.** `goodall doctor` and its checks leave files,
   credentials and remote state untouched.
6. **Goodall operates git, people administer it.** Goodall commits and pushes
   to the record repo; creating remote repos and setting their visibility is
   the researcher's job.
7. **The hook path is standard library only.** Code run by a Claude Code hook
   imports nothing third-party, because the prompt gate runs on every prompt.
8. **Capture is independent of analysis.** `inspect-ai` and `inspect-scout`
   are an optional extra, imported only by export code.
9. **One gateway each.** All subprocess calls go through one runner module
   with a fake for tests, and goodall makes no network calls of its own. All
   terminal output goes through one UI module. The terminal never asks a
   question: the setup page is the only place goodall asks a person for
   answers, and unattended setup reads an answers file.

## Command-line conventions

- One command, `goodall`, with subcommands.
- Commands that agents call offer `--json` with a stable shape.
- Exit codes: `0` success, `1` error, `2` usage error, `3` the prompt gate is
  closed.
- Every failure message says what to do next.

## Code style

The reader is a researcher who writes analysis scripts and has never shipped
production software. They should be able to read any file top to bottom and
understand why each function exists.

- Plain loops over dense one-liners; a comprehension only when it has a single
  simple clause.
- Names written out in full: `command_result`, `exit_code`,
  `for check in checks`. Accepted short forms are `id`, `url`, `api`, `env`,
  `yml`.
- Module docstring first: what the file is for, who calls it, and any design
  decision a reader would otherwise reverse-engineer.
- Docstrings and comments give the reason and the trap, and are updated in the
  same edit as the behaviour they describe.
- Type hints on every signature, `@dataclass` for records, `pathlib.Path` for
  paths, `yaml.safe_load` for YAML.
- One obvious path: add an abstraction when a second real case exists.

## Testing

- **Test first, outside-in.** Begin every change with a failing test, starting
  from the outside: first a test that drives `goodall` the way a person or a
  hook would, then a failing unit test for each piece needed to make it pass.
  Run each test and watch it fail for the right reason before writing the code
  that satisfies it. This is the project owner's strong preference for every
  agent.
- The quality gates, all of which pass before a change is handed over:
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`.
- CI (`.github/workflows/quality-gates.yml`) runs the same four gates on every
  pull request.
- Tests use the fake runner and `tmp_path`; the real network, the real
  1Password sign-in (`op`) and the user's real files stay out of reach.
- Real `git` is used in tests, because git behaviour is what they verify.
- Test names state the behaviour protected:
  `test_tracked_env_fails_even_when_ignored`.
- A bug fix arrives with a test that fails without it.

## Dependencies

Standard library first. To add a package, give the human a request containing
its maintainer, repository, advisory history, licence and dependency count,
each checked live today, plus anything you could not verify. Add it only after
approval, together with its row in `docs/DEPENDENCIES.md` and the updated
`uv.lock`. A recommendation from a model is a lead to check, and the checked
facts are the provenance.

## Git

- **Commit freely in a git context you own.** An agent working on a branch or
  in a worktree created for its own task commits there without asking. This
  repository rule takes precedence over the default in the Beads sections
  below. Pushing, opening a pull request, and committing to `main` or to a
  branch someone else is working on still wait for the owner's go-ahead.
- **A merged branch is finished.** Once a branch's pull request is merged,
  further work starts on a new branch from `origin/main`. Claude Code hooks
  (`.claude/hooks/branch_merge_status.py`) report a merged branch when a Claude
  session starts and on each prompt, and refuse `git commit` and `git push`
  there. In other tools, check with `gh pr list --head <branch> --state all`
  before committing.
- Prefer smaller commits: one logical change each, made as soon as that change
  passes the quality gates. Imperative subject; the body gives the reason.
- AI-authored commits keep their `Co-Authored-By` trailer, and branches are
  merged with merge commits so authorship survives.
- Pull request descriptions fill in every heading of
  `.github/pull_request_template.md`.

## Upstream contributions

`inspect_ai` and `inspect_scout` close pull requests that lack a
maintainer-accepted issue. Open the issue first, and write code intended for
upstream in their style from the start.

## Shell

Pass non-interactive flags (`cp -f`, `mv -f`, `rm -f`, `ssh -o BatchMode=yes`)
so a command never waits on a prompt.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
