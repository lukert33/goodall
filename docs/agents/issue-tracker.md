# Issue tracker: Beads

Issues, specs and tickets for this repo live in Beads, a local issue database
driven by the `bd` command. Use `bd` for every operation; this repo does not
use GitHub Issues. Run `bd prime` for the full command reference.

Issue ids look like `goodall-yjt` (top level) and `goodall-yjt.3` (a child).

## Conventions

- **Create an issue**:
  `bd create --title "..." --description "..." --type task --priority 2`.
  Types: `task`, `bug`, `feature`, `epic`, `spike`, `decision`, `chore`.
  Priority is `0` (highest) to `4`. Add `--acceptance "..."` for acceptance
  criteria and `--labels a,b` for labels. `--silent` prints only the new id.
  For a long body, pass `--body-file <path>` (or `--stdin`).
- **Read an issue**: `bd show <id>`, or `bd show <id> --json` for all fields.
  Comments are separate: `bd comments <id>`.
- **List issues**: `bd list --status open`, narrowed with `--label <name>`,
  `--no-labels`, `--type <type>` or `--parent <id>`. Add `--json` to parse.
- **Make an issue a child of a parent**: `bd create --parent <parent-id> ...`.
  A child inherits its parent's labels unless you pass `--no-inherit-labels`.
  List children with `bd children <parent-id>`.
- **Comment on an issue**: `bd comments add <id> "..."`, or
  `bd comments add <id> -f <file>` for a long comment.
- **Apply / remove labels**: `bd label add <id> <label>` /
  `bd label remove <id> <label>`. See labels with `bd label list <id>`.
- **Block one issue on another**: `bd dep add <issue> <depends-on>` (the first
  waits for the second), or `--deps <id>,<id>` at creation. Beads tracks this
  natively: `bd blocked` lists blocked issues and `bd ready` lists the rest.
- **Claim**: `bd update <id> --claim`, before any work.
- **Close**: `bd close <id> --reason "..."`.

Avoid `bd edit`: it opens an editor and hangs an agent. Change fields with
`bd update <id> --title/--description/--acceptance/--append-notes`.

## Where issue data lives

The database is in `.beads/` of the main checkout and is shared by every
worktree, so an issue created on any branch is visible from all of them. A
pull request therefore never "contains" issues. Name the issue ids a change
addresses in its commit messages and pull request description.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo starts treating
outside pull requests as feature requests; `/triage` reads this flag.)_

When set to `yes`, read a pull request with `gh pr view <number> --comments`
and `gh pr diff <number>`, and record its triage state on a Beads issue
created for it that links the pull request.

## When a skill says "publish to the issue tracker"

Create a Beads issue. A spec is an issue of type `epic` or `feature` whose
tickets are its children.

## When a skill says "fetch the relevant ticket"

`bd show <id>` and `bd comments <id>`.

## How work is closed

`bd close <id> --reason "..."` once the change is committed and the quality
gates in `AGENTS.md` pass. Closing does not depend on a pull request merging.

## Wayfinding operations

Used by `/wayfinder`. The **map** is one issue; its tickets are its children.

- **Map**: `bd create --type epic --labels wayfinder:map --title "..."`, with
  the Destination / Notes / Decisions-so-far / Not-yet-specified body as its
  description. Update the body with `bd update <map-id> --description "..."`.
- **Child ticket**:
  `bd create --parent <map-id> --no-inherit-labels --labels wayfinder:<type>`
  where `<type>` is `research`, `prototype`, `grilling` or `task`. The
  `--no-inherit-labels` flag matters: without it the child also carries
  `wayfinder:map` and looks like a second map.
- **Blocking**: Beads' native dependencies, `bd dep add <child> <blocker>`,
  wired in a second pass once every ticket has an id.
- **Frontier query**: `bd ready --parent <map-id> --unassigned` lists the
  map's open, unblocked, unclaimed children. First listed wins.
- **Claim**: `bd update <id> --claim`, the session's first write.
- **Resolve**: `bd comments add <id> "<answer>"`, then
  `bd close <id> --reason "<one-line gist>"`, then add a line for it to the
  map's Decisions so far.
