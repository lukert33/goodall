"""Putting a record into the record repo: write its file and commit it.

`goodall journal`, `goodall intervene`, `goodall fixblock` and the
work-block commands call `write_record` with the runner, this account's
record repo folder (from local settings) and a record from goodall.records.
It writes the record's file and commits that one file. It returns a
`RecordWriteOutcome` saying how far it got, and never raises for anything
about the machine or the repository.

Rules this module keeps, and why:

- The record repo is local and never pushed (ADR 0023). Goodall commits
  and never pushes, fetches, merges from elsewhere or adds a remote. A
  remote someone added by hand is left alone, and `goodall doctor` says
  that goodall does not push to it. The local history is the point: it is
  what makes a later edit to one of goodall's records visible.
- One file per record, and a record is never edited or deleted by goodall
  once written: a file that already exists is refused, never overwritten.
  The one exception is `replace_with_deleted_work_block`, for a work block
  whose captured data a person deleted (ADR 0021).
- Only the file written is staged and committed. Anything else in the
  record repo, staged or not, belongs to the contributor and is left
  exactly as it was.
- All git goes through the runner (ADR 0011).

Decisions a reader would otherwise have to reverse-engineer (all
provisional, chosen while the project owner was away):

- Commits are made with the identity configured in git for this
  repository (its own setting or the contributor's global one). With none,
  goodall refuses before writing anything rather than let git invent one.
  There is no AI co-author trailer: goodall the tool commits on a
  contributor's behalf.
- git cannot prompt, and stray `GIT_*` variables are ignored (see
  `run_git`).
"""

from dataclasses import dataclass, field
from pathlib import Path

from goodall import records
from goodall.runner import DEFAULT_TIMEOUT_SECONDS, Command, CommandResult, Runner
from goodall.terminal import Failure


@dataclass(frozen=True)
class RecordWriteOutcome:
    """How far putting one record into the record repo got.

    - `path` is the record's file, whether or not it was written.
    - `written` is True when this call created the file.
    - `committed` is True when the file is in a local commit. From then on
      the record is safe, and `failures` is empty.
    - `failures` says what went wrong, if anything, each with a next step.
    """

    path: Path
    written: bool
    committed: bool
    failures: list[Failure] = field(default_factory=list)


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
    records.DeletedWorkBlock: "deleted work block",
}


def write_record(runner: Runner, record_repo: Path, record: records.Record) -> RecordWriteOutcome:
    """Write `record`'s file into the record repo at `record_repo`, and commit it."""
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
    return RecordWriteOutcome(path=path, written=True, committed=True)


def replace_with_deleted_work_block(
    runner: Runner, record_repo: Path, deleted: records.DeletedWorkBlock, fix_files: list[Path]
) -> RecordWriteOutcome:
    """Put a deleted work block in place of its work block's record, remove its fixes, and commit both at once.

    The one time goodall replaces a record, decided by the project owner
    (ADR 0021): a person deleted what the work block captured. The file
    keeps its name, so whatever names the work block still names something.
    `fix_files` are the work block's fixes, removed in the same commit
    because each names the workstream the replacement no longer gives. Git
    history is left alone: the commit adds to it, and the record as it was
    stays in the commits before.

    The new text is written to a temporary file and renamed over the old,
    so the folder never holds half a record. Only these paths are staged
    and committed, as `write_record` does.
    """
    text = records.render_record(deleted)
    relative_path = records.relative_path_for(deleted)
    path = record_repo / relative_path
    readiness_failure = check_record_repo_ready(runner, record_repo)
    if readiness_failure is not None:
        return not_written(path, readiness_failure)
    expected_folder = record_repo.resolve() / relative_path.parent
    if path.parent.resolve() != expected_folder:
        return not_written(path, record_path_outside_record_repo_failure(record_repo, path))
    temporary_file = path.parent / f".{path.name}.goodall-tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_file.write_text(text, encoding="utf-8")
        temporary_file.replace(path)
    except OSError as write_error:
        temporary_file.unlink(missing_ok=True)
        return not_written(path, record_not_written_failure(path, write_error))

    paths_to_commit = [str(relative_path)]
    for fix_file in fix_files:
        fix_relative_path = str(fix_file.relative_to(record_repo))
        # Trap: naming a path git does not know makes the commit fail, so
        # a fix that was never committed is simply removed from the folder.
        if not run_git(runner, record_repo, ["ls-files", "--error-unmatch", "--", fix_relative_path]).succeeded:
            fix_file.unlink(missing_ok=True)
            continue
        remove_result = run_git(runner, record_repo, ["rm", "--quiet", "--", fix_relative_path])
        if not remove_result.succeeded:
            return written_not_committed(path, record_commit_failed_failure(record_repo, path, remove_result))
        paths_to_commit.append(fix_relative_path)
    add_result = run_git(runner, record_repo, ["add", "--", str(relative_path)])
    if not add_result.succeeded:
        return written_not_committed(path, record_commit_failed_failure(record_repo, path, add_result))
    commit_result = run_git(
        runner,
        record_repo,
        ["commit", "--quiet", "-m", replacement_commit_message(deleted), "--", *paths_to_commit],
        timeout_seconds=COMMIT_TIMEOUT_SECONDS,
    )
    if not commit_result.succeeded:
        return written_not_committed(path, record_commit_failed_failure(record_repo, path, commit_result))
    return RecordWriteOutcome(path=path, written=True, committed=True)


def replacement_commit_message(deleted: records.DeletedWorkBlock) -> str:
    """The message of the commit that puts a deleted work block in place of its record."""
    return (
        f"Replace work block record {deleted.id} with a deleted work block\n\n"
        f"Written by goodall at the request of contributor {deleted.contributor}, who deleted what the work block "
        "captured."
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


def not_written(path: Path, failure: Failure) -> RecordWriteOutcome:
    """The outcome for a record whose file was not written, because of `failure`."""
    return RecordWriteOutcome(path=path, written=False, committed=False, failures=[failure])


def written_not_committed(path: Path, failure: Failure) -> RecordWriteOutcome:
    """The outcome for a record whose file was written but could not be committed."""
    return RecordWriteOutcome(path=path, written=True, committed=False, failures=[failure])


def record_commit_failed_failure(record_repo: Path, path: Path, result: CommandResult) -> Failure:
    """The failure for a record that was written but that git would not commit.

    The file is left where it is: it holds what the contributor recorded,
    and goodall never deletes a record.
    """
    return Failure(
        kind="record_commit_failed",
        what_happened=f"The record was written to {path}, but git did not commit it: {result.problem}.",
        why=(
            "A record is kept in the record repo only once it is committed. Your own git settings, "
            "such as a hook or commit signing, can refuse a commit; goodall does not get around them."
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
      `credential.interactive=never` asks credential helpers that honor it
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
