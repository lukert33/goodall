"""Putting a record into the record repo: write its file, commit it, push it.

`goodall journal`, `goodall intervene` and the work-block commands call
`write_record` with the runner, this account's record repo folder (from
local settings) and a record from goodall.records. It writes the record's
file, commits that one file, and pushes when the record repo has a remote.
It returns a `RecordWriteOutcome` saying how far it got, and never raises
for anything about the machine, the repository or the network.

Rules this module keeps, and why:

- Goodall operates git; people administer it (AGENTS.md invariant 6, ADR
  0001). It commits and pushes, and never creates a remote, changes a
  remote's address or visibility, or needs to know where the remote is
  hosted. The remote's address lives only in git (ADR 0009).
- One file per record, and a record is never edited or deleted by goodall
  once written: a file that already exists is refused, never overwritten.
- Only the file written is staged and committed. Anything else in the
  record repo, staged or not, belongs to the contributor and is left
  exactly as it was.
- The order is write, commit, then push. A push that fails or cannot be
  made never undoes the write: the record is safe in a local commit and the
  outcome says the push is pending. The next write pushes everything
  pending, because a push sends the whole branch.
- All git goes through the runner (ADR 0011).
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from goodall import records
from goodall.runner import Command, CommandResult, Runner
from goodall.terminal import Failure


class PushState(StrEnum):
    """Where a write left the record repo's commits, as against its remote."""

    # Everything committed locally is on the remote.
    PUSHED = "pushed"
    # Some commits are only in this account's copy; the next write pushes them.
    PENDING = "pending"
    # The record repo has no remote, so there is nowhere to push.
    NO_REMOTE = "no_remote"
    # Nothing was committed, so no push was tried.
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class RecordWriteOutcome:
    """How far putting one record into the record repo got.

    - `path` is the record's file, whether or not it was written.
    - `written` is True when this call created the file.
    - `committed` is True when the file is in a local commit. From then on
      the record is safe, whatever happened to the push.
    - `push_state` says whether the commits reached the remote.
    - `pending_commits` is how many commits are not yet on the remote.
    - `failures` says what went wrong, if anything, each with a next step.
    """

    path: Path
    written: bool
    committed: bool
    push_state: PushState
    pending_commits: int = 0
    failures: list[Failure] = field(default_factory=list)


# Each kind of record, named for a person reading the repository's history.
RECORD_TYPE_NAMES = {
    records.WorkBlockRecord: "work block record",
    records.JournalEntry: "journal entry",
    records.Intervention: "intervention",
}


def write_record(runner: Runner, record_repo: Path, record: records.Record) -> RecordWriteOutcome:
    """Write `record`'s file into the record repo at `record_repo`, commit it, and push if there is a remote."""
    text = records.render_record(record)
    relative_path = records.relative_path_for(record)
    path = record_repo / relative_path

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as record_file:
        record_file.write(text)

    run_git(runner, record_repo, ["add", "--", str(relative_path)])
    run_git(runner, record_repo, ["commit", "--quiet", "-m", commit_message(record), "--", str(relative_path)])
    push_outcome = push_pending(runner, record_repo)
    return RecordWriteOutcome(
        path=path,
        written=True,
        committed=True,
        push_state=push_outcome.push_state,
        pending_commits=push_outcome.pending_commits,
        failures=push_outcome.failures,
    )


@dataclass(frozen=True)
class PushOutcome:
    """What pushing the record repo's pending commits did.

    `push_state` is never NOT_ATTEMPTED here. `pending_commits` is counted
    by git after the push, so it cannot disagree with the repository.
    """

    push_state: PushState
    pending_commits: int = 0
    failures: list[Failure] = field(default_factory=list)


@dataclass(frozen=True)
class PushTarget:
    """Where the current branch of the record repo is pushed: a remote's name and a branch on it."""

    remote: str
    branch: str

    @property
    def tracking_reference(self) -> str:
        """git's local copy of where that branch stood on the remote when last fetched or pushed."""
        return f"refs/remotes/{self.remote}/{self.branch}"


def push_pending(runner: Runner, record_repo: Path) -> PushOutcome:
    """Push every commit on the record repo's current branch that the remote does not have yet."""
    target = find_push_target(runner, record_repo)
    if target is None:
        return PushOutcome(push_state=PushState.NO_REMOTE)
    run_git(runner, record_repo, ["push", "--quiet", target.remote, f"HEAD:refs/heads/{target.branch}"])
    return PushOutcome(push_state=PushState.PUSHED)


def find_push_target(runner: Runner, record_repo: Path) -> PushTarget | None:
    """Where the current branch is pushed, or None when the record repo has no remote."""
    branch = run_git(runner, record_repo, ["symbolic-ref", "--quiet", "--short", "HEAD"]).standard_output.strip()
    remote = run_git(runner, record_repo, ["config", "--get", f"branch.{branch}.remote"]).standard_output.strip()
    if not remote:
        return None
    return PushTarget(remote=remote, branch=branch)


def commit_message(record: records.Record) -> str:
    """The message of the commit that adds `record`, naming its kind and id for someone browsing history."""
    record_type_name = RECORD_TYPE_NAMES[type(record)]
    return f"Add {record_type_name} {record.id}\n\nWritten by goodall for contributor {record.contributor}."


def run_git(runner: Runner, record_repo: Path, arguments: list[str]) -> CommandResult:
    """Run git with `arguments` in the record repo."""
    return runner.run(Command(["git", *arguments], working_directory=record_repo))
