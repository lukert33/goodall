#!/usr/bin/env python3
"""Claude Code hook: close a Beads issue once its pull request has merged into main.

Why this exists. The project rule is that an issue stays in progress while its
pull request is open, and is closed only when that pull request merges. The
owner merges on GitHub, often long after the agent that opened the pull
request has finished, so somebody has to come back and close the issue. This
hook is that somebody.

How an issue is linked to its pull request. The agent that opens the pull
request sets the issue's external reference to the pull request's URL:
`bd update <id> --external-ref https://github.com/<owner>/<repo>/pull/<n>`.
Issues without such a link are ignored.

Who calls it, and when. Claude Code, through `.claude/settings.json`:

- `session-start`, when a session starts: closes merged issues and lists the
  issues still waiting on an open pull request, so a fresh session knows what
  is in review.
- `prompt-check`, before a prompt, at most once every ten minutes across all
  worktrees of this repository: closes merged issues and reports only what
  changed. Most prompts cost one file check and nothing else.

What it does for each linked issue in progress, by asking `gh pr view`:

- merged into main: `bd close` with the pull request in the reason, then one
  `bd dolt push` for all of them, because the Beads push hook only watches
  commands an agent runs, not this hook.
- merged into another branch (a stacked pull request): left in progress, and
  reported, because the work is not on main yet.
- closed without merging: left in progress and reported, because whether to
  reopen the pull request or put the issue back to open is the owner's call.
- open: left alone, and listed at session start only.

Design decisions a reader would otherwise have to reverse-engineer:

- It never refuses anything and always exits 0. It reports through
  `additionalContext`, which reaches the agent, and `systemMessage`, which the
  person sees.
- No `bd`, no `gh`, no network, or input it cannot read: it changes nothing.
  GitHub being unreachable is said once, so nobody assumes issues were checked.
- The ten-minute stamp lives in the repository's shared git folder, so every
  worktree's sessions share one schedule instead of each asking GitHub.
- Standard library only, so it runs without goodall installed.
"""

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# The branch whose merge counts as finished. Stacked pull requests merge into
# another branch first, and their issue must wait until the stack reaches main.
MAIN_BRANCH = "main"

# How often the before-a-prompt check may ask GitHub, in seconds.
PROMPT_CHECK_INTERVAL_SECONDS = 10 * 60

# Name of the stamp file inside the repository's shared git folder.
STAMP_FILE_NAME = "goodall-pull-request-check"

# How long to wait for each program before giving up on it.
COMMAND_TIMEOUT_SECONDS = 20

PULL_REQUEST_URL = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/[0-9]+$")


@dataclass
class LinkedIssue:
    """An issue in progress whose external reference is a pull request URL."""

    issue_id: str
    pull_request_url: str


@dataclass
class Report:
    """What this run found, kept apart so each mode can decide what to say."""

    closed: list[LinkedIssue]
    merged_elsewhere: list[tuple[LinkedIssue, str]]
    closed_without_merging: list[LinkedIssue]
    still_open: list[LinkedIssue]
    unchecked: list[LinkedIssue]
    push_problem: str | None


def read_working_folder() -> Path:
    """The folder Claude Code says the session is in, or the current folder when the input is unreadable."""
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return Path.cwd()
    if isinstance(hook_input, dict) and isinstance(hook_input.get("cwd"), str):
        return Path(hook_input["cwd"])
    return Path.cwd()


def run(arguments: list[str], folder: Path) -> subprocess.CompletedProcess[str] | None:
    """Run a program and return what it did, or None when it could not be run at all."""
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            cwd=folder,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def stamp_path(folder: Path) -> Path | None:
    """Where the ten-minute stamp lives: the git folder every worktree of this repository shares."""
    git_program = shutil.which("git")
    if git_program is None:
        return None
    completed = run([git_program, "rev-parse", "--git-common-dir"], folder)
    if completed is None or completed.returncode != 0:
        return None
    common_folder = Path(completed.stdout.strip())
    if not common_folder.is_absolute():
        common_folder = folder / common_folder
    return common_folder / STAMP_FILE_NAME


def checked_recently(folder: Path) -> bool:
    """True when a prompt check ran within the interval. Marks this run as the latest otherwise."""
    stamp = stamp_path(folder)
    if stamp is None:
        return False
    now = time.time()
    if stamp.exists() and now - stamp.stat().st_mtime < PROMPT_CHECK_INTERVAL_SECONDS:
        return True
    try:
        stamp.write_text(str(int(now)))
    except OSError:
        pass
    return False


def linked_issues_in_progress(bd_program: str, folder: Path) -> list[LinkedIssue]:
    """Issues in progress whose external reference is a GitHub pull request URL."""
    completed = run([bd_program, "list", "--status", "in_progress", "--json"], folder)
    if completed is None or completed.returncode != 0:
        return []
    try:
        issues = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(issues, list):
        return []
    linked = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        external_ref = issue.get("external_ref")
        issue_id = issue.get("id")
        if not isinstance(external_ref, str) or not isinstance(issue_id, str):
            continue
        if PULL_REQUEST_URL.match(external_ref.strip()):
            linked.append(LinkedIssue(issue_id=issue_id, pull_request_url=external_ref.strip()))
    return linked


def pull_request_state(gh_program: str, url: str, folder: Path) -> tuple[str, str] | None:
    """The pull request's state (OPEN, CLOSED, MERGED) and base branch, or None when GitHub did not answer."""
    completed = run([gh_program, "pr", "view", url, "--json", "state,baseRefName"], folder)
    if completed is None or completed.returncode != 0:
        return None
    try:
        answer = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(answer, dict):
        return None
    state = answer.get("state")
    base = answer.get("baseRefName")
    if not isinstance(state, str) or not isinstance(base, str):
        return None
    return state, base


def close_issue(bd_program: str, linked_issue: LinkedIssue, folder: Path) -> bool:
    """Close the issue, naming the pull request. True when bd says it worked."""
    reason = f"Merged into {MAIN_BRANCH} in {linked_issue.pull_request_url} (closed by the pull request status hook)."
    completed = run([bd_program, "close", linked_issue.issue_id, "--force", "--reason", reason], folder)
    return completed is not None and completed.returncode == 0


def push_beads_database(bd_program: str, folder: Path) -> str | None:
    """Run `bd dolt push`. Return None when it worked, or what went wrong."""
    completed = run([bd_program, "dolt", "push"], folder)
    if completed is None:
        return "bd dolt push could not be run or did not finish"
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or completed.stdout).strip()
    return detail if detail != "" else f"bd dolt push exited with code {completed.returncode}"


def check_pull_requests(bd_program: str, gh_program: str, folder: Path) -> Report:
    report = Report(
        closed=[],
        merged_elsewhere=[],
        closed_without_merging=[],
        still_open=[],
        unchecked=[],
        push_problem=None,
    )
    for linked_issue in linked_issues_in_progress(bd_program, folder):
        answer = pull_request_state(gh_program, linked_issue.pull_request_url, folder)
        if answer is None:
            report.unchecked.append(linked_issue)
            continue
        state, base = answer
        if state == "MERGED" and base == MAIN_BRANCH:
            if close_issue(bd_program, linked_issue, folder):
                report.closed.append(linked_issue)
            else:
                report.unchecked.append(linked_issue)
        elif state == "MERGED":
            report.merged_elsewhere.append((linked_issue, base))
        elif state == "CLOSED":
            report.closed_without_merging.append(linked_issue)
        else:
            report.still_open.append(linked_issue)
    if report.closed:
        report.push_problem = push_beads_database(bd_program, folder)
    return report


def describe(report: Report, include_open: bool) -> str:
    """Plain-words lines for what the run found; empty when there is nothing worth saying."""
    lines = []
    for linked_issue in report.closed:
        lines.append(f"Closed {linked_issue.issue_id}: {linked_issue.pull_request_url} has merged into {MAIN_BRANCH}.")
    if report.closed and report.push_problem is not None:
        lines.append(
            f"The Beads push after closing failed ({report.push_problem}). The closes are saved on this Mac; "
            "run bd dolt push before you finish."
        )
    for linked_issue, base in report.merged_elsewhere:
        lines.append(
            f"{linked_issue.issue_id} stays in progress: {linked_issue.pull_request_url} merged into {base}, "
            f"not {MAIN_BRANCH}, so its work is not on {MAIN_BRANCH} yet."
        )
    for linked_issue in report.closed_without_merging:
        lines.append(
            f"{linked_issue.issue_id} stays in progress: {linked_issue.pull_request_url} was closed without merging. "
            "Ask the owner whether to reopen the pull request or put the issue back to open."
        )
    if include_open and report.still_open:
        waiting = []
        for linked_issue in report.still_open:
            waiting.append(f"{linked_issue.issue_id} ({linked_issue.pull_request_url})")
        lines.append("Waiting on an open pull request: " + ", ".join(waiting) + ".")
    if report.unchecked:
        names = []
        for linked_issue in report.unchecked:
            names.append(linked_issue.issue_id)
        lines.append(
            "These issues' pull requests could not be checked (GitHub or bd did not answer): "
            + ", ".join(names)
            + ". Nothing was changed for them."
        )
    return "\n".join(lines)


def say(event_name: str, message: str) -> None:
    """Hand the message to the agent as added context and show it to the person."""
    output = {
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": message},
        "systemMessage": message,
    }
    print(json.dumps(output))


def main(arguments: list[str]) -> int:
    mode = arguments[0] if arguments else ""
    folder = read_working_folder()
    if mode not in ("session-start", "prompt-check"):
        print(
            f"pull_request_status.py: unknown mode '{mode}'. Use session-start or prompt-check.",
            file=sys.stderr,
        )
        return 0
    bd_program = shutil.which("bd")
    gh_program = shutil.which("gh")
    if bd_program is None or gh_program is None:
        return 0
    if mode == "prompt-check" and checked_recently(folder):
        return 0
    report = check_pull_requests(bd_program, gh_program, folder)
    message = describe(report, include_open=(mode == "session-start"))
    if message != "":
        event_name = "SessionStart" if mode == "session-start" else "UserPromptSubmit"
        say(event_name, message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
