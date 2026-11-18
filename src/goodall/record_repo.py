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
- All git goes through the runner (ADR 0011). Retrying a push is decided
  here, not in the runner, because it involves git policy.

Decisions a reader would otherwise have to reverse-engineer (all
provisional, chosen while the project owner was away):

- Commits are made with the identity configured in git for this
  repository (its own setting or the contributor's global one). With none,
  goodall refuses before writing anything rather than let git invent one.
  There is no AI co-author trailer: goodall the tool commits on a
  contributor's behalf.
- When a push is refused because a collaborator pushed first, goodall
  fetches their branch and merges it with a merge commit (never a rebase,
  never a forced push), then pushes again, up to `MAXIMUM_PUSH_RETRIES`
  times. It merges only when no tracked file has uncommitted changes, and
  undoes a merge that stops part-way, so the local commit always survives.
- "Pending" is not stored anywhere: it is the commits on the branch that
  git's remote-tracking branch does not have.
- Push and fetch give up after `NETWORK_TIMEOUT_SECONDS`; git cannot
  prompt, and stray `GIT_*` variables are ignored (see `run_git`).
- `push_pending` is public so a later command can push what is waiting
  without writing a record.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from goodall import records
from goodall.runner import DEFAULT_TIMEOUT_SECONDS, Command, CommandOutcome, CommandResult, Runner
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

# How long a push or fetch may take. A remote that cannot be reached must
# not hang a command a person is waiting on; the record is already safe in a
# local commit by then, so giving up costs only a later push. Long enough
# for a slow connection to send a few small text files.
NETWORK_TIMEOUT_SECONDS = 30.0

# The remote pushed to when the branch names no upstream: git's own name
# for the remote a repository was cloned from.
DEFAULT_REMOTE = "origin"

# How long a commit may take. Longer than a short command's limit, because
# the contributor's own hooks and commit signing run inside it.
COMMIT_TIMEOUT_SECONDS = 60.0

# The git settings that say who makes a commit. Both must be set.
IDENTITY_SETTINGS = ["user.name", "user.email"]

# Each kind of record, named for a person reading the repository's history.
RECORD_TYPE_NAMES = {
    records.WorkBlockRecord: "work block record",
    records.JournalEntry: "journal entry",
    records.Intervention: "intervention",
    records.WorkBlockFix: "work block fix",
}


def write_record(runner: Runner, record_repo: Path, record: records.Record) -> RecordWriteOutcome:
    """Write `record`'s file into the record repo at `record_repo`, commit it, and push if there is a remote."""
    text = records.render_record(record)
    relative_path = records.relative_path_for(record)
    path = record_repo / relative_path

    # Checked before anything is written, so a record repo goodall cannot
    # commit in never gets a stray uncommitted file.
    readiness_failure = check_record_repo_ready(runner, record_repo)
    if readiness_failure is not None:
        return not_written(path, readiness_failure)

    # Trap: a link anywhere on the way (a `journal` folder that is really a
    # link to another folder, say) would carry the record out of the record
    # repo, where it would be neither committed nor safe. So the folder must
    # be exactly where it appears to be: resolving links must change nothing.
    expected_folder = record_repo.resolve() / relative_path.parent
    if path.parent.resolve() != expected_folder:
        return not_written(path, record_path_outside_record_repo_failure(record_repo, path))
    # "x" creates the file only if nothing is there, in one step, so an
    # existing record (or anything else at that name) is never overwritten,
    # even by a second goodall writing at the same moment.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as record_file:
            record_file.write(text)
    except FileExistsError:
        return not_written(path, record_already_exists_failure(path))
    except OSError as write_error:
        # No permission, a full disk, a folder where a file should be: all
        # states of the machine, so a failure with a next step, not a crash.
        return not_written(path, record_not_written_failure(path, write_error))

    # Stage and commit only this one file. Naming it after `--` makes the
    # commit take that path alone (git's "--only"), so anything else the
    # contributor has staged stays staged and out of goodall's commit.
    # The contributor's own hooks and signing settings apply, as they would
    # to any commit of theirs; goodall reports what they refuse.
    add_result = run_git(runner, record_repo, ["add", "--", str(relative_path)])
    if not add_result.succeeded:
        return written_not_committed(path, record_commit_failed_failure(record_repo, path, add_result))
    commit_result = run_git(
        runner,
        record_repo,
        ["commit", "--quiet", "-m", commit_message(record), "--", str(relative_path)],
        timeout_seconds=COMMIT_TIMEOUT_SECONDS,
    )
    if not commit_result.succeeded:
        return written_not_committed(path, record_commit_failed_failure(record_repo, path, commit_result))
    push_outcome = push_pending(runner, record_repo)
    return RecordWriteOutcome(
        path=path,
        written=True,
        committed=True,
        push_state=push_outcome.push_state,
        pending_commits=push_outcome.pending_commits,
        failures=push_outcome.failures,
    )


def check_record_repo_ready(runner: Runner, record_repo: Path) -> Failure | None:
    """Say why goodall cannot commit in `record_repo`, or None when it can.

    It must be the top folder of a git repository of its own, and git must
    have an identity to sign the commit with.
    """
    top_folder_result = run_git(runner, record_repo, ["rev-parse", "--show-toplevel"])
    if not top_folder_result.succeeded:
        return record_repo_not_git_repository_failure(record_repo, top_folder_result.problem or "")
    # Trap: git also answers for a plain folder inside some other
    # repository (a home folder kept in git, say), naming that one's top
    # folder; committing there would put records in the wrong repository.
    # resolve() both, because git names the real path and the folder given
    # may pass through a link (on macOS, /var is /private/var).
    top_folder = Path(top_folder_result.standard_output.strip())
    if top_folder.resolve() != record_repo.resolve():
        return record_repo_not_git_repository_failure(
            record_repo, f"it is a folder inside the git repository {top_folder}"
        )

    # Trap: with no name or email configured, git may make one up from the
    # account and machine names, and the commit would carry an address that
    # reaches nobody. Goodall never invents an identity either, so it
    # insists on one that git was told.
    for setting in IDENTITY_SETTINGS:
        identity_result = run_git(runner, record_repo, ["config", "--get", setting])
        if not identity_result.standard_output.strip():
            return identity_missing_failure(record_repo)
    return None


def record_repo_not_git_repository_failure(record_repo: Path, problem: str) -> Failure:
    """The failure for a record repo folder that is not a git repository goodall can commit in."""
    return Failure(
        kind="record_repo_not_git_repository",
        what_happened=f"The record repo folder {record_repo} is not a git repository of its own: {problem}.",
        why="Goodall keeps the eval's records by committing them in the record repo, so it must be one.",
        what_to_do_next=(
            "Run goodall doctor, which says what is wrong with the record repo folder and how to fix it. "
            "No record was written."
        ),
    )


def identity_missing_failure(record_repo: Path) -> Failure:
    """The failure for a record repo where git has no name or email to put on commits."""
    return Failure(
        kind="record_repo_identity_missing",
        what_happened=(
            f"git has no name or email to put on commits in {record_repo}, so goodall did not write the record."
        ),
        why=(
            "Every commit in the record repo names who made it. Goodall uses the name and email you gave git, "
            "and never makes one up."
        ),
        what_to_do_next=(
            'Tell git who you are by running, in Terminal: git config --global user.name "Your Name" and then '
            'git config --global user.email "you@example.org", with your own name and the email you use for '
            "this eval. Then run the command again."
        ),
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
    if isinstance(target, Failure):
        return PushOutcome(
            push_state=PushState.PENDING,
            pending_commits=count_commits_on_no_remote(runner, record_repo),
            failures=[target],
        )
    push_result = push_branch(runner, record_repo, target)
    for _retry in range(MAXIMUM_PUSH_RETRIES):
        if push_result.succeeded:
            break
        # A push that never completed (it timed out, say) told us nothing
        # about the remote, so there is nothing to combine.
        if push_result.outcome != CommandOutcome.COMPLETED:
            break
        # Was the push refused because a collaborator pushed first? Fetch
        # the branch and look: if everything on the remote is already in
        # this copy, the refusal was for some other reason (no permission,
        # a protected branch, the remote unreachable), and merging cannot
        # help.
        fetch_result = fetch_branch(runner, record_repo, target)
        if not fetch_result.succeeded:
            break
        if remote_branch_already_included(runner, record_repo, target):
            break
        merge_failure = merge_remote_branch(runner, record_repo, target)
        if merge_failure is not None:
            return pending_outcome(runner, record_repo, target, merge_failure)
        push_result = push_branch(runner, record_repo, target)

    if push_result.succeeded:
        return PushOutcome(push_state=PushState.PUSHED)
    pending_commits = count_pending_commits(runner, record_repo, target)
    return PushOutcome(
        push_state=PushState.PENDING,
        pending_commits=pending_commits,
        failures=[push_failed_failure(record_repo, target, push_result, pending_commits)],
    )


def push_branch(runner: Runner, record_repo: Path, target: PushTarget) -> CommandResult:
    """Push the current branch, with every commit on it, to its branch on the remote.

    The destination is written out in full, so the push goes where the
    branch's upstream says whatever the contributor's `push.default` is,
    and goodall never changes the repository's settings to make it so.
    A plain push, never a forced one: the remote's history is never
    rewritten.
    """
    return run_git(
        runner,
        record_repo,
        ["push", "--quiet", target.remote, f"HEAD:refs/heads/{target.branch}"],
        timeout_seconds=NETWORK_TIMEOUT_SECONDS,
    )


def pending_outcome(runner: Runner, record_repo: Path, target: PushTarget, failure: Failure) -> PushOutcome:
    """A push outcome saying commits are still waiting, because of `failure`."""
    return PushOutcome(
        push_state=PushState.PENDING,
        pending_commits=count_pending_commits(runner, record_repo, target),
        failures=[failure],
    )


def count_commits_on_no_remote(runner: Runner, record_repo: Path) -> int:
    """How many commits in this copy's history git has seen on no remote at all.

    Used when there is no one branch to compare with, such as when the
    record repo is not on a branch.
    """
    count_result = run_git(runner, record_repo, ["rev-list", "--count", "HEAD", "--not", "--remotes"])
    return int(count_result.standard_output.strip() or "0")


def fetch_branch(runner: Runner, record_repo: Path, target: PushTarget) -> CommandResult:
    """Bring git's copy of the remote branch (its remote-tracking branch) up to date.

    The refspec is written out, rather than left to the remote's configured
    one, so the tracking branch is updated however the clone was set up.
    The leading `+` lets it follow a remote branch that was rewritten:
    the tracking branch is only git's note of where the remote stands.
    """
    refspec = f"+refs/heads/{target.branch}:{target.tracking_reference}"
    return run_git(
        runner,
        record_repo,
        ["fetch", "--quiet", "--no-tags", target.remote, refspec],
        timeout_seconds=NETWORK_TIMEOUT_SECONDS,
    )


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
    # The merge commit runs the contributor's hooks and signing, like any
    # commit, so it gets a commit's time limit. `--no-ff` because a setting
    # of `merge.ff = only` would otherwise refuse the merge outright.
    merge_result = run_git(
        runner,
        record_repo,
        ["merge", "--quiet", "--no-ff", "--no-edit", "-m", merge_message, target.tracking_reference],
        timeout_seconds=COMMIT_TIMEOUT_SECONDS,
    )
    if merge_result.succeeded:
        return None
    # A merge that stopped part-way leaves MERGE_HEAD behind; undo it. One
    # that refused before starting (an untracked file in the way, say)
    # changed nothing and leaves nothing to undo.
    undone = True
    merge_in_progress = run_git(runner, record_repo, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"])
    if merge_in_progress.succeeded:
        abort_result = run_git(runner, record_repo, ["merge", "--abort"])
        undone = abort_result.succeeded
    return merge_failed_failure(record_repo, target, merge_result.problem or "git merge failed", undone)


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


def merge_failed_failure(record_repo: Path, target: PushTarget, problem: str, undone: bool = True) -> Failure:
    """The failure for a merge with the remote branch that did not complete.

    `undone` says whether the attempt was rolled back; the message must
    never claim it was when it was not.
    """
    what_was_left = "Goodall undid the attempt; your commits are as they were."
    if not undone:
        what_was_left = (
            "Goodall could not undo the attempt, so the record repo is part-way through a merge; your commits "
            "are still there."
        )
    return Failure(
        kind="record_merge_failed",
        what_happened=(
            f"The record is saved and committed in {record_repo}, but not pushed: another contributor pushed to "
            f"{target.remote} first, and combining their commits with yours failed ({problem}). {what_was_left}"
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


def find_push_target(runner: Runner, record_repo: Path) -> PushTarget | Failure | None:
    """Where the current branch is pushed; None when the record repo has no remote; a Failure when unclear.

    The branch's upstream, set by `git clone`, says which remote and which
    branch there. A record repo whose remote was added by hand may have no
    upstream; then the remote called `origin`, git's usual name, gets the
    branch under the same name. With several remotes and no upstream,
    goodall does not guess. Nothing here is stored outside git (ADR 0009).
    """
    remotes_result = run_git(runner, record_repo, ["remote"])
    remotes = remotes_result.standard_output.split()
    if not remotes:
        return None
    branch_result = run_git(runner, record_repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    branch = branch_result.standard_output.strip()
    if not branch_result.succeeded or not branch:
        return not_on_a_branch_failure(record_repo)

    upstream_remote = run_git(runner, record_repo, ["config", "--get", f"branch.{branch}.remote"])
    upstream_branch = run_git(runner, record_repo, ["config", "--get", f"branch.{branch}.merge"])
    remote = upstream_remote.standard_output.strip()
    remote_branch = upstream_branch.standard_output.strip().removeprefix("refs/heads/")
    if remote in remotes and remote_branch:
        return PushTarget(remote=remote, branch=remote_branch)
    if DEFAULT_REMOTE in remotes:
        return PushTarget(remote=DEFAULT_REMOTE, branch=branch)
    return no_push_destination_failure(record_repo, branch)


def not_on_a_branch_failure(record_repo: Path) -> Failure:
    """The failure for a record repo whose copy is not on a branch, so there is no branch to push."""
    return Failure(
        kind="record_repo_not_on_a_branch",
        what_happened=(
            f"The record is saved and committed in {record_repo}, but not pushed: the record repo is not on a "
            "branch (git calls this a detached HEAD), so goodall does not know where on the remote it belongs."
        ),
        why="Records reach other contributors only once they are pushed to the eval's branch on the remote.",
        what_to_do_next=(
            f"Ask the researcher who runs the eval to put the record repo in {record_repo} back on its branch "
            "(git status shows where it is). The waiting commits are pushed with the next record goodall writes."
        ),
    )


def no_push_destination_failure(record_repo: Path, branch: str) -> Failure:
    """The failure for a record repo with remotes but none that goodall can tell is the one to push to."""
    return Failure(
        kind="record_repo_push_destination_unknown",
        what_happened=(
            f"The record is saved and committed in {record_repo}, but not pushed: the branch {branch} has no "
            "upstream and there is no remote called origin, so goodall does not know where to push it."
        ),
        why="Goodall pushes where git says to, and never guesses or changes the repository's settings itself.",
        what_to_do_next=(
            f"In {record_repo}, run git push --set-upstream <remote> {branch} once, naming the eval's remote. "
            "Goodall pushes there from then on."
        ),
    )


def not_written(path: Path, failure: Failure) -> RecordWriteOutcome:
    """The outcome for a record whose file was not written, because of `failure`."""
    return RecordWriteOutcome(
        path=path, written=False, committed=False, push_state=PushState.NOT_ATTEMPTED, failures=[failure]
    )


def written_not_committed(path: Path, failure: Failure) -> RecordWriteOutcome:
    """The outcome for a record whose file was written but could not be committed."""
    return RecordWriteOutcome(
        path=path, written=True, committed=False, push_state=PushState.NOT_ATTEMPTED, failures=[failure]
    )


def record_commit_failed_failure(record_repo: Path, path: Path, result: CommandResult) -> Failure:
    """The failure for a record that was written but that git would not commit.

    The file is left where it is: it holds what the contributor recorded,
    and goodall never deletes a record.
    """
    return Failure(
        kind="record_commit_failed",
        what_happened=f"The record was written to {path}, but git did not commit it: {result.problem}.",
        why=(
            "A record reaches other contributors only once it is committed and pushed. Your own git settings, "
            "such as a hook or commit signing, can refuse a commit; goodall does not get round them."
        ),
        what_to_do_next=(
            f"Fix what git complained about, then commit the file yourself: in {record_repo}, run git commit "
            f'-m "Add record" -- {path.relative_to(record_repo)}. The record itself is safe on this Mac.'
        ),
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


def record_not_written_failure(path: Path, write_error: OSError) -> Failure:
    """The failure for a record file the system would not let goodall create."""
    return Failure(
        kind="record_not_written",
        what_happened=f"Goodall could not write the record to {path}: {write_error.strerror}.",
        why="Goodall keeps each record as a file in the record repo, so it must be able to create one there.",
        what_to_do_next=(
            f"Check that this macOS user account may write in {path.parent} and that the disk has space, then "
            "run the command again."
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


def run_git(
    runner: Runner, record_repo: Path, arguments: list[str], timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> CommandResult:
    """Run git with `arguments` in the record repo, shielded from stray settings and unable to prompt.

    - Every `GIT_*` variable in goodall's environment is left out. Trap:
      git obeys `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and their
      siblings over the folder it runs in, and they are set, for instance,
      while a git hook of some other repository runs; goodall would commit
      into that repository. The contributor's git settings in their home
      folder still apply, because those are theirs to choose.
    - Nothing may ask for a password or passphrase: goodall never asks
      questions in the terminal, and a prompt nobody sees hangs until the
      timeout. `GIT_TERMINAL_PROMPT=0` stops git's own prompts;
      `credential.interactive=never` asks credential helpers that honour it
      (Git Credential Manager does) not to open a window; `SSH_ASKPASS` is
      left out so ssh has no program to ask with. ssh cannot ask in the
      terminal either, because the runner starts git with no terminal
      attached. A helper that ignores all this is still stopped by the
      timeout.
    """
    command = Command(
        ["git", "-c", "credential.interactive=never", *arguments],
        working_directory=record_repo,
        timeout_seconds=timeout_seconds,
        removed_environment_prefixes=["GIT_", "SSH_ASKPASS"],
        extra_environment={"GIT_TERMINAL_PROMPT": "0"},
    )
    return runner.run(command)
