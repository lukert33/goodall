#!/usr/bin/env python3
"""Claude Code hook: stop agents adding commits to a branch that is finished.

Why this exists. A pull request gets merged while an agent session is still
open. The agent does not notice, keeps committing to the same branch, and the
new commits land on a branch nobody will merge again. This hook tells the
agent the branch's status and refuses the commit, with the fix.

Who calls it. Claude Code, through `.claude/settings.json`, with the mode as
the one argument and the hook's JSON on standard input:

- `gate` (PreToolUse on Bash): if the command would `git commit` or
  `git push` on a branch whose pull request is merged, exit 2 with the reason
  on standard error. Claude Code blocks the command and shows the agent the
  reason. Always asks GitHub afresh.
- `session-start` (SessionStart): if the branch is merged, or its pull request
  was closed, say so in the context the agent starts with.
- `prompt-reminder` (UserPromptSubmit): the same message on each prompt, so a
  merge that happens mid-session is noticed. Reuses a recent answer for a few
  minutes so that prompts stay fast.

Design decisions a reader would otherwise have to reverse-engineer:

- GitHub is the authority, asked through the `gh` command. Git alone cannot
  tell a merged branch from a brand-new branch that has no commits yet.
- When GitHub cannot be asked (no `gh`, no network, not signed in) the hook
  allows the command and says it could not check. A broken check must not
  stop all work.
- A branch with an OPEN pull request is fine even if an older one from the
  same branch name was merged: that is a reused name, not a finished branch.
- The default branch is left alone. Whether an agent may commit there is a
  separate rule (AGENTS.md, "Git").
- The gate runs before the command does, so a command such as
  `git switch -c next origin/main && git commit ...` would otherwise be judged
  by the branch it is leaving. A `git switch` or `git checkout` earlier in the
  same line, joined to the commit by `&&` all the way, moves the judgment to
  the branch it switches to. Only a few plain forms are recognized; anything
  else (unknown options, detached checkouts, `git switch -`, checking out
  files) falls back to the branch checked out now. Wrongly refusing a commit
  is cheap; wrongly allowing one onto a finished branch is what this hook
  exists to prevent.
- Standard library only, and it calls `subprocess` directly. This file is
  repository tooling that runs before the `goodall` package is importable, so
  the package's runner gateway (AGENTS.md invariant 9) does not apply to it.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

EXIT_ALLOW = 0
# Claude Code treats exit code 2 from a PreToolUse hook as "block this tool
# call and show standard error to the agent".
EXIT_BLOCK = 2

GITHUB_TIMEOUT_SECONDS = 15
GIT_TIMEOUT_SECONDS = 10
REMINDER_CACHE_SECONDS = 300
REMINDER_CACHE_FILENAME = "goodall-branch-merge-status.json"

DEFAULT_BRANCH_NAMES = {"main", "master"}
GUARDED_GIT_SUBCOMMANDS = {"commit", "push"}

# The options of `git switch` and `git checkout` that create (or reset) a
# branch, followed by its name: `git switch -c <name> [<start-point>]`.
BRANCH_CREATING_OPTIONS = {
    "switch": {"-c", "--create", "-C", "--force-create"},
    "checkout": {"-b", "-B"},
}
# Options that change nothing about which branch ends up checked out.
QUIET_OPTIONS = {"-q", "--quiet"}

# git options that take a separate value, so the value is not mistaken for
# the subcommand: `git -C some/dir commit`, `git -c user.name=x commit`.
GIT_OPTIONS_WITH_A_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# Splits one shell line into its separate commands, keeping the separators
# (the brackets make `re.split` return them too). Good enough for the
# commands agents write; it does not try to be a full shell parser.
SHELL_COMMAND_SEPARATORS = re.compile(r"(&&|\|\||;|\||\n)")
# Only after `&&` is the previous command known to have succeeded, so only
# then can a branch switch earlier in the line be trusted to have happened.
SEPARATOR_THAT_NEEDS_SUCCESS = "&&"


@dataclass
class GuardedGitCall:
    """A `git commit` or `git push` found in a Bash command.

    `branch` is the branch an earlier `git switch` / `git checkout` in the
    same command will have moved to, or "" to judge the branch checked out
    in `directory` now.
    """

    subcommand: str
    directory: Path
    branch: str = ""


@dataclass
class PullRequest:
    """The few facts about a pull request this hook needs."""

    number: int
    state: str  # "OPEN", "MERGED" or "CLOSED", as GitHub reports it
    url: str
    merged_at: str


@dataclass
class BranchStatus:
    """What is known about the current branch's pull requests.

    `verdict` is one of:
    - "merged": a pull request from this branch was merged and none is open
    - "closed": a pull request was closed without merging and none is open
    - "active": nothing suggests the branch is finished
    - "unknown": GitHub could not be asked
    - "not-applicable": no branch to judge (default branch, detached HEAD,
      or not a git repository)
    """

    verdict: str
    branch: str = ""
    pull_request: PullRequest | None = None
    problem: str = ""


# --- Reading the Bash command ----------------------------------------------


def split_into_words(shell_command: str) -> list[str]:
    """Split one command into words the way a shell would, forgivingly.

    `shlex` raises on an unfinished quote. A hook must not crash on odd
    input, so fall back to splitting on whitespace.
    """
    try:
        return shlex.split(shell_command)
    except ValueError:
        return shell_command.split()


def find_git_subcommand(words: list[str]) -> tuple[str, str, list[str]]:
    """Return (subcommand, directory given with -C, the subcommand's arguments).

    Returns ("", "", []) when the words are not a git command. The directory
    is "" when no `-C` was given.
    """
    if not words or Path(words[0]).name != "git":
        return "", "", []

    directory = ""
    index = 1
    while index < len(words):
        word = words[index]
        if word in GIT_OPTIONS_WITH_A_VALUE:
            if word == "-C" and index + 1 < len(words):
                directory = words[index + 1]
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return word, directory, words[index + 1 :]
    return "", directory, []


def branch_after_switch(subcommand: str, arguments: list[str], directory: Path) -> str:
    """The branch a `git switch` / `git checkout` leaves checked out, or "" if unsure.

    Recognizes only these forms, optionally with `-q`:
      git switch -c <name> [<start-point>]     (also -C, --create, --force-create)
      git checkout -b <name> [<start-point>]   (also -B)
      git switch <name>
      git checkout <name>   only when <name> is an existing local branch,
                            because `git checkout <path>` changes files, not
                            the branch.
    Everything else returns "", which means "judge the branch checked out
    now". A created branch is still looked up on GitHub, because a new local
    branch can reuse the name of one whose pull request was merged.
    """
    creating_options = BRANCH_CREATING_OPTIONS[subcommand]
    created_branch = ""
    other_words: list[str] = []

    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in QUIET_OPTIONS:
            index += 1
            continue
        # The name must come straight after the creating option, and before
        # any start point, as in the documented form.
        creating_comes_first = not created_branch and not other_words
        if argument in creating_options and creating_comes_first and index + 1 < len(arguments):
            created_branch = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("-"):
            # An option not listed above, `-` (the previous branch) or `--`
            # (paths follow).
            return ""
        other_words.append(argument)
        index += 1

    if created_branch:
        if created_branch.startswith("-") or len(other_words) > 1:
            return ""
        return created_branch

    if len(other_words) != 1:
        return ""
    target = other_words[0]
    if subcommand == "checkout" and not local_branch_exists(target, directory):
        return ""
    return target


def find_guarded_git_calls(bash_command: str, starting_directory: Path) -> list[GuardedGitCall]:
    """Find every `git commit` / `git push` in a Bash command line.

    Follows `cd some/dir` earlier in the same line, and `git -C some/dir`,
    because agents usually write `cd <worktree> && git commit ...` and the
    branch that matters is the one in that directory.

    Also follows `git switch` / `git checkout` earlier in the line (see
    `branch_after_switch`), remembering per directory which branch the
    command will have moved to. That memory is dropped at any separator
    other than `&&`: after `;`, `||` or `|` the switch may have failed and
    the commit would then land on the branch checked out now.
    """
    calls: list[GuardedGitCall] = []
    current_directory = starting_directory
    # Keyed by the resolved directory, so `cd repo && git -C repo ...` agree.
    branch_after_switch_by_directory: dict[Path, str] = {}

    # `re.split` with a bracketed pattern gives [command, separator, command, ...].
    pieces = SHELL_COMMAND_SEPARATORS.split(bash_command)
    for index in range(0, len(pieces), 2):
        if index > 0 and pieces[index - 1] != SEPARATOR_THAT_NEEDS_SUCCESS:
            branch_after_switch_by_directory.clear()

        words = split_into_words(pieces[index].strip())
        if not words:
            continue

        if words[0] == "cd" and len(words) >= 2:
            current_directory = (current_directory / words[1]).expanduser()
            continue

        subcommand, dash_c_directory, arguments = find_git_subcommand(words)
        directory = current_directory
        if dash_c_directory:
            directory = (current_directory / dash_c_directory).expanduser()

        if subcommand in BRANCH_CREATING_OPTIONS:
            target_branch = branch_after_switch(subcommand, arguments, directory)
            if target_branch:
                branch_after_switch_by_directory[directory.resolve()] = target_branch
            else:
                # Unsure where this switch leads, so forget any earlier one
                # and judge the branch checked out now.
                branch_after_switch_by_directory.pop(directory.resolve(), None)

        if subcommand in GUARDED_GIT_SUBCOMMANDS:
            branch = branch_after_switch_by_directory.get(directory.resolve(), "")
            calls.append(GuardedGitCall(subcommand=subcommand, directory=directory, branch=branch))

    return calls


# --- Asking git and GitHub --------------------------------------------------


def run_command(arguments: list[str], directory: Path, timeout_seconds: int) -> tuple[bool, str, str]:
    """Run a command and return (succeeded, standard output, problem).

    Never raises: a missing program, a missing directory and a timeout are
    all reported as "did not succeed", because the caller treats every one
    of them the same way.
    """
    try:
        completed = subprocess.run(
            arguments,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, "", str(error)

    if completed.returncode != 0:
        return False, completed.stdout, completed.stderr.strip()
    return True, completed.stdout, ""


def current_branch(directory: Path) -> str:
    """The branch checked out in `directory`, or "" if there is none.

    "" covers three cases the hook leaves alone: not a git repository, a
    directory that does not exist, and a detached HEAD.
    """
    succeeded, output, _problem = run_command(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], directory, GIT_TIMEOUT_SECONDS
    )
    if not succeeded:
        return ""
    return output.strip()


def local_branch_exists(branch: str, directory: Path) -> bool:
    """Whether `branch` is a local branch in the repository at `directory`."""
    succeeded, _output, _problem = run_command(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], directory, GIT_TIMEOUT_SECONDS
    )
    return succeeded


def git_directory(directory: Path) -> Path | None:
    """Where git keeps this checkout's private files (differs per worktree)."""
    succeeded, output, _problem = run_command(
        ["git", "rev-parse", "--absolute-git-dir"], directory, GIT_TIMEOUT_SECONDS
    )
    if not succeeded or not output.strip():
        return None
    return Path(output.strip())


def parse_pull_requests(github_output: str) -> list[PullRequest] | None:
    """Turn `gh pr list --json ...` output into records, or None if unreadable."""
    try:
        raw_pull_requests = json.loads(github_output)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_pull_requests, list):
        return None

    pull_requests: list[PullRequest] = []
    for raw in raw_pull_requests:
        if not isinstance(raw, dict):
            continue
        pull_requests.append(
            PullRequest(
                number=int(raw.get("number") or 0),
                state=str(raw.get("state") or "").upper(),
                url=str(raw.get("url") or ""),
                merged_at=str(raw.get("mergedAt") or ""),
            )
        )
    return pull_requests


def look_up_branch_status(directory: Path, branch: str = "") -> BranchStatus:
    """Ask GitHub about a branch's pull requests.

    `branch` defaults to the one checked out in `directory`, asked of git.
    """
    if not branch:
        branch = current_branch(directory)
    if not branch or branch in DEFAULT_BRANCH_NAMES:
        return BranchStatus(verdict="not-applicable", branch=branch)

    succeeded, output, problem = run_command(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "number,state,url,mergedAt"],
        directory,
        GITHUB_TIMEOUT_SECONDS,
    )
    if not succeeded:
        return BranchStatus(verdict="unknown", branch=branch, problem=problem or "gh did not answer")

    pull_requests = parse_pull_requests(output)
    if pull_requests is None:
        return BranchStatus(verdict="unknown", branch=branch, problem="gh gave an answer that could not be read")

    merged: PullRequest | None = None
    closed: PullRequest | None = None
    for pull_request in pull_requests:
        if pull_request.state == "OPEN":
            # An open pull request means the branch is in use, whatever
            # happened to earlier pull requests from the same branch name.
            return BranchStatus(verdict="active", branch=branch, pull_request=pull_request)
        if pull_request.state == "MERGED" and merged is None:
            merged = pull_request
        if pull_request.state == "CLOSED" and closed is None:
            closed = pull_request

    if merged is not None:
        return BranchStatus(verdict="merged", branch=branch, pull_request=merged)
    if closed is not None:
        return BranchStatus(verdict="closed", branch=branch, pull_request=closed)
    return BranchStatus(verdict="active", branch=branch)


# --- The reminder's short-lived memory --------------------------------------


def status_to_cache_text(status: BranchStatus) -> str:
    pull_request = status.pull_request
    record = {
        "checked_at": time.time(),
        "verdict": status.verdict,
        "branch": status.branch,
        "pull_request": None
        if pull_request is None
        else {
            "number": pull_request.number,
            "state": pull_request.state,
            "url": pull_request.url,
            "merged_at": pull_request.merged_at,
        },
    }
    return json.dumps(record)


def read_cached_status(cache_file: Path, branch: str) -> BranchStatus | None:
    """Return a recent remembered status for this branch, or None."""
    try:
        record = json.loads(cache_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("branch") != branch:
        return None

    checked_at = record.get("checked_at")
    if not isinstance(checked_at, (int, float)) or time.time() - checked_at > REMINDER_CACHE_SECONDS:
        return None

    pull_request: PullRequest | None = None
    raw_pull_request = record.get("pull_request")
    if isinstance(raw_pull_request, dict):
        pull_request = PullRequest(
            number=int(raw_pull_request.get("number") or 0),
            state=str(raw_pull_request.get("state") or ""),
            url=str(raw_pull_request.get("url") or ""),
            merged_at=str(raw_pull_request.get("merged_at") or ""),
        )
    return BranchStatus(verdict=str(record.get("verdict") or "active"), branch=branch, pull_request=pull_request)


def look_up_branch_status_with_memory(directory: Path) -> BranchStatus:
    """Like `look_up_branch_status`, but reuse an answer a few minutes old.

    Used only by the per-prompt reminder. A failed lookup is not remembered,
    so the next prompt tries again.
    """
    branch = current_branch(directory)
    if not branch or branch in DEFAULT_BRANCH_NAMES:
        return BranchStatus(verdict="not-applicable", branch=branch)

    private_git_directory = git_directory(directory)
    cache_file = None if private_git_directory is None else private_git_directory / REMINDER_CACHE_FILENAME

    if cache_file is not None:
        remembered = read_cached_status(cache_file, branch)
        if remembered is not None:
            return remembered

    status = look_up_branch_status(directory)
    if cache_file is not None and status.verdict != "unknown":
        try:
            cache_file.write_text(status_to_cache_text(status))
        except OSError:
            pass  # Remembering is a convenience; the answer is still correct.
    return status


# --- What the agent is told --------------------------------------------------


def how_to_start_a_new_branch() -> str:
    return (
        "Start a new branch from the up-to-date default branch before doing more work:\n"
        "  git fetch origin\n"
        "  git switch -c <new-branch-name> origin/main\n"
        "Uncommitted changes come with you when you switch. If the owner asked for a "
        "follow-up on this same branch, check with them first."
    )


def describe_finished_branch(status: BranchStatus) -> str:
    """A message for the agent when the branch is merged or closed, else ""."""
    pull_request = status.pull_request
    if pull_request is None:
        return ""

    if status.verdict == "merged":
        when = f" on {pull_request.merged_at}" if pull_request.merged_at else ""
        return (
            f"Branch '{status.branch}' is finished: its pull request #{pull_request.number} was merged{when} "
            f"({pull_request.url}). New commits here would land on a branch nobody will merge again.\n"
            + how_to_start_a_new_branch()
        )
    if status.verdict == "closed":
        return (
            f"Branch '{status.branch}' may be finished: its pull request #{pull_request.number} was closed "
            f"without merging ({pull_request.url}). Ask the owner whether to keep working here.\n"
            + how_to_start_a_new_branch()
        )
    return ""


def print_context_for_agent(hook_event_name: str, message: str) -> None:
    """Hand text to Claude Code to place in the agent's context."""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": message}}))


# --- The three modes ---------------------------------------------------------


def directory_from_hook_input(hook_input: dict[str, object]) -> Path:
    working_directory = hook_input.get("cwd")
    if isinstance(working_directory, str) and working_directory:
        return Path(working_directory)
    return Path.cwd()


def run_gate(hook_input: dict[str, object]) -> int:
    """PreToolUse: block `git commit` / `git push` on a merged branch."""
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return EXIT_ALLOW
    bash_command = tool_input.get("command")
    if not isinstance(bash_command, str):
        return EXIT_ALLOW

    for call in find_guarded_git_calls(bash_command, directory_from_hook_input(hook_input)):
        status = look_up_branch_status(call.directory, call.branch)
        if status.verdict == "merged":
            print(f"Blocked `git {call.subcommand}`. " + describe_finished_branch(status), file=sys.stderr)
            return EXIT_BLOCK
        if status.verdict == "unknown":
            print(
                f"Note: could not check whether branch '{status.branch}' has a merged pull request "
                f"({status.problem}). Allowing `git {call.subcommand}`; check the branch yourself with "
                "`gh pr list --head <branch> --state all`.",
                file=sys.stderr,
            )
    return EXIT_ALLOW


def run_session_start(hook_input: dict[str, object]) -> int:
    """SessionStart: tell the agent up front if the branch is finished."""
    status = look_up_branch_status(directory_from_hook_input(hook_input))
    message = describe_finished_branch(status)
    if message:
        print_context_for_agent("SessionStart", message)
    return EXIT_ALLOW


def run_prompt_reminder(hook_input: dict[str, object]) -> int:
    """UserPromptSubmit: repeat the message while the branch stays finished.

    Always exits 0. On this event a non-zero exit could block the person's
    prompt, and a reminder must never do that.
    """
    status = look_up_branch_status_with_memory(directory_from_hook_input(hook_input))
    message = describe_finished_branch(status)
    if message:
        print_context_for_agent("UserPromptSubmit", message)
    return EXIT_ALLOW


def read_hook_input() -> dict[str, object]:
    """Read the JSON Claude Code sends on standard input; {} if unreadable."""
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(hook_input, dict):
        return {}
    return hook_input


def main(arguments: list[str]) -> int:
    mode = arguments[0] if arguments else ""
    hook_input = read_hook_input()

    if mode == "gate":
        return run_gate(hook_input)
    if mode == "session-start":
        return run_session_start(hook_input)
    if mode == "prompt-reminder":
        return run_prompt_reminder(hook_input)

    print(
        f"branch_merge_status.py: unknown mode '{mode}'. Use gate, session-start or prompt-reminder.",
        file=sys.stderr,
    )
    # Exit 1, not 2: a mistake in the hook's own configuration should be
    # reported without blocking the agent's command.
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
