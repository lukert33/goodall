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
from goodall.runner import Command, CommandOutcome, CommandResult, Runner
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


# How many times a push refused because a collaborator pushed first is
# retried after combining their commits. More than one, because a third
# contributor can push in between; small, because each retry is a round trip
# to the remote while a person waits.
MAXIMUM_PUSH_RETRIES = 3

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

    # Trap: a link anywhere on the way (a `journal` folder that is really a
    # link to another folder, say) would carry the record out of the record
    # repo, where it would be neither committed nor safe. So the folder must
    # be exactly where it appears to be: resolving links must change nothing.
    expected_folder = record_repo.resolve() / relative_path.parent
    if path.parent.resolve() != expected_folder:
        return not_written(path, record_path_outside_record_repo_failure(record_repo, path))
    path.parent.mkdir(parents=True, exist_ok=True)
    # "x" creates the file only if nothing is there, in one step, so an
    # existing record (or anything else at that name) is never overwritten,
    # even by a second goodall writing at the same moment.
    try:
        with path.open("x", encoding="utf-8") as record_file:
            record_file.write(text)
    except FileExistsError:
        return not_written(path, record_already_exists_failure(path))

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
    push_result = run_git(runner, record_repo, ["push", "--quiet", target.remote, f"HEAD:refs/heads/{target.branch}"])
    for _retry in range(MAXIMUM_PUSH_RETRIES):
        if push_result.succeeded:
            return PushOutcome(push_state=PushState.PUSHED)
        # A program that never completed (a timeout, say) told us nothing
        # about the remote, so there is nothing to combine.
        if push_result.outcome != CommandOutcome.COMPLETED:
            break
        # Was the push refused because a collaborator pushed first? Fetch
        # the branch and look: if everything on the remote is already in
        # this copy, the refusal was for some other reason, and merging
        # cannot help.
        fetch_result = fetch_branch(runner, record_repo, target)
        if not fetch_result.succeeded:
            break
        if remote_branch_already_included(runner, record_repo, target):
            break
        merge_failure = merge_remote_branch(runner, record_repo, target)
        if merge_failure is not None:
            return pending_outcome(runner, record_repo, target, merge_failure)
        push_result = run_git(
            runner, record_repo, ["push", "--quiet", target.remote, f"HEAD:refs/heads/{target.branch}"]
        )
    if push_result.succeeded:
        return PushOutcome(push_state=PushState.PUSHED)
    pending_commits = count_pending_commits(runner, record_repo, target)
    return PushOutcome(
        push_state=PushState.PENDING,
        pending_commits=pending_commits,
        failures=[push_failed_failure(record_repo, target, push_result, pending_commits)],
    )


def pending_outcome(runner: Runner, record_repo: Path, target: PushTarget, failure: Failure) -> PushOutcome:
    """A push outcome saying commits are still waiting, because of `failure`."""
    return PushOutcome(
        push_state=PushState.PENDING,
        pending_commits=count_pending_commits(runner, record_repo, target),
        failures=[failure],
    )


def fetch_branch(runner: Runner, record_repo: Path, target: PushTarget) -> CommandResult:
    """Bring git's copy of the remote branch (its remote-tracking branch) up to date.

    The refspec is written out, rather than left to the remote's configured
    one, so the tracking branch is updated however the clone was set up.
    The leading `+` lets it follow a remote branch that was rewritten:
    the tracking branch is only git's note of where the remote stands.
    """
    refspec = f"+refs/heads/{target.branch}:{target.tracking_reference}"
    return run_git(runner, record_repo, ["fetch", "--quiet", "--no-tags", target.remote, refspec])


def remote_branch_already_included(runner: Runner, record_repo: Path, target: PushTarget) -> bool:
    """True when every commit on the remote branch, as last fetched, is already in this copy's history."""
    ancestor_result = run_git(runner, record_repo, ["merge-base", "--is-ancestor", target.tracking_reference, "HEAD"])
    return ancestor_result.succeeded


def merge_remote_branch(runner: Runner, record_repo: Path, target: PushTarget) -> Failure | None:
    """Combine a collaborator's pushed commits with this copy's, with a merge commit; None when it worked.

    A merge, not a rebase: the project never rewrites history, and a merge
    keeps every contributor's commits exactly as they made them. Because
    every record is a file of its own, the two sides add different files
    and cannot conflict over a record.

    It merges only when no tracked file has uncommitted changes. Trap: a
    merge that stops part-way can be undone with `git merge --abort`, but
    git warns that the undo may not restore uncommitted changes. With none
    present, the undo always puts the copy back exactly as it was, so a
    failed merge leaves the local commit in place and nothing else changed.
    """
    status_result = run_git(runner, record_repo, ["status", "--porcelain", "--untracked-files=no"])
    if not status_result.succeeded:
        return merge_failed_failure(record_repo, target, status_result.problem or "git status failed")
    if status_result.standard_output.strip():
        return local_changes_block_merge_failure(record_repo, target)
    merge_message = (
        f"Merge records from {target.remote}/{target.branch}\n\n"
        "Written by goodall: another contributor pushed records first, and this joins their commits with "
        "this copy's so both can be pushed."
    )
    merge_result = run_git(
        runner,
        record_repo,
        ["merge", "--quiet", "--no-ff", "--no-edit", "-m", merge_message, target.tracking_reference],
    )
    if merge_result.succeeded:
        return None
    # A merge that stopped part-way leaves MERGE_HEAD behind; undo it. One
    # that refused before starting (an untracked file in the way, say)
    # changed nothing and leaves nothing to undo.
    merge_in_progress = run_git(runner, record_repo, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"])
    if merge_in_progress.succeeded:
        run_git(runner, record_repo, ["merge", "--abort"])
    return merge_failed_failure(record_repo, target, merge_result.problem or "git merge failed")


def local_changes_block_merge_failure(record_repo: Path, target: PushTarget) -> Failure:
    """The failure for a merge goodall will not start, because the contributor has uncommitted changes."""
    return Failure(
        kind="record_push_blocked_by_local_changes",
        what_happened=(
            f"The record is saved and committed in {record_repo}, but not pushed: another contributor pushed to "
            f"{target.remote} first, and goodall will not combine their records with yours while files in the "
            "record repo have changes that are not committed."
        ),
        why=(
            "Combining the two histories changes files in the record repo. Goodall does that only when it can "
            "always put everything back as it was, which uncommitted changes would prevent."
        ),
        what_to_do_next=(
            f"Run git status in {record_repo} to see the changed files, then commit them or put them back as "
            "they were. Goodall will push the waiting commits with the next record it writes."
        ),
    )


def merge_failed_failure(record_repo: Path, target: PushTarget, problem: str) -> Failure:
    """The failure for a merge with the remote branch that did not complete, and was undone."""
    return Failure(
        kind="record_merge_failed",
        what_happened=(
            f"The record is saved and committed in {record_repo}, but not pushed: another contributor pushed to "
            f"{target.remote} first, and combining their commits with yours failed ({problem}). Goodall undid "
            "the attempt; your commits are as they were."
        ),
        why=(
            "Records are separate files, so this should not happen; it usually means a file other than a "
            "record was changed on both sides."
        ),
        what_to_do_next=(
            f"Ask the researcher who runs the eval to look at the record repo in {record_repo} (git status and "
            "git log show the state). Goodall will push the waiting commits with the next record it writes "
            "once the histories can be combined."
        ),
    )


def count_pending_commits(runner: Runner, record_repo: Path, target: PushTarget) -> int:
    """How many commits on the current branch are not on the remote, as far as git last saw it.

    Worked out from git itself (the remote-tracking branch, which git moves
    on every successful push and fetch), so it can never disagree with the
    repository the way a separate list could. When git has never seen the
    branch on the remote, every commit on it is pending.
    """
    tracking_known = run_git(runner, record_repo, ["rev-parse", "--verify", "--quiet", target.tracking_reference])
    revisions = ["HEAD"]
    if tracking_known.succeeded:
        revisions = [f"{target.tracking_reference}..HEAD"]
    count_result = run_git(runner, record_repo, ["rev-list", "--count", *revisions])
    return int(count_result.standard_output.strip() or "0")


def push_failed_failure(
    record_repo: Path, target: PushTarget, push_result: CommandResult, pending_commits: int
) -> Failure:
    """The failure for a push that did not happen; the record itself is safe in a local commit."""
    return Failure(
        kind="record_push_failed",
        what_happened=(
            f"The record is saved and committed in {record_repo}, but could not be pushed to the remote "
            f"{target.remote}: {push_result.problem}. {pending_commits} commit(s) are waiting to be pushed."
        ),
        why=(
            "Other contributors and the researcher see records only once they reach the remote. Nothing is lost: "
            "the next record goodall writes pushes everything waiting."
        ),
        what_to_do_next=(
            f"Check this Mac is online and can reach the remote (run git push in {record_repo} to see git's own "
            "message). Goodall will push the waiting commits with the next record it writes."
        ),
    )


def find_push_target(runner: Runner, record_repo: Path) -> PushTarget | None:
    """Where the current branch is pushed, or None when the record repo has no remote."""
    branch = run_git(runner, record_repo, ["symbolic-ref", "--quiet", "--short", "HEAD"]).standard_output.strip()
    remote = run_git(runner, record_repo, ["config", "--get", f"branch.{branch}.remote"]).standard_output.strip()
    if not remote:
        return None
    return PushTarget(remote=remote, branch=branch)


def not_written(path: Path, failure: Failure) -> RecordWriteOutcome:
    """The outcome for a record whose file was not written, because of `failure`."""
    return RecordWriteOutcome(
        path=path, written=False, committed=False, push_state=PushState.NOT_ATTEMPTED, failures=[failure]
    )


def record_path_outside_record_repo_failure(record_repo: Path, path: Path) -> Failure:
    """The failure for a record whose folder is a link leading somewhere other than inside the record repo."""
    return Failure(
        kind="record_path_outside_record_repo",
        what_happened=(
            f"Goodall did not write the record at {path}: a folder on the way there is a link to somewhere else, "
            f"so the record would not have landed inside the record repo {record_repo}."
        ),
        why="Goodall writes records only inside the record repo, where they are committed and kept.",
        what_to_do_next=(
            f"Look at the folders in {record_repo} (ls -l shows links) and replace the link with a real folder, "
            "or ask the researcher who runs the eval. Then run the command again."
        ),
    )


def record_already_exists_failure(path: Path) -> Failure:
    """The failure for a record whose file is already in the record repo."""
    return Failure(
        kind="record_already_exists",
        what_happened=f"A file is already at {path}, so goodall did not write the record there.",
        why=(
            "Goodall never edits or replaces a record once it is written, so the record repo stays an honest "
            "account of what was recorded when. Each record's file name is new, so this should not happen."
        ),
        what_to_do_next=(
            "Run the command again: the new record gets a fresh file name. If this keeps happening, report it "
            "to the researcher who runs the eval, with the path above."
        ),
    )


def commit_message(record: records.Record) -> str:
    """The message of the commit that adds `record`, naming its kind and id for someone browsing history."""
    record_type_name = RECORD_TYPE_NAMES[type(record)]
    return f"Add {record_type_name} {record.id}\n\nWritten by goodall for contributor {record.contributor}."


def run_git(runner: Runner, record_repo: Path, arguments: list[str]) -> CommandResult:
    """Run git with `arguments` in the record repo."""
    return runner.run(Command(["git", *arguments], working_directory=record_repo))
