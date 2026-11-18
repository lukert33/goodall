"""Tests for `.claude/hooks/branch_merge_status.py`, the Claude Code hook that
stops agents committing to a branch whose pull request is already merged.

These are outside-in tests: each one runs the hook script as a separate
process, the way Claude Code does, feeding it the JSON a real hook receives on
standard input. The git repository is real (git behavior is part of what is
being checked). GitHub is not: a fake `gh` program is placed first on PATH and
answers from a file, so no test touches the network.

The hook is repository tooling, not part of the `goodall` package, so these
tests call `subprocess` directly instead of going through the package's runner
gateway.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "branch_merge_status.py"

MERGED_PULL_REQUEST = {
    "number": 7,
    "state": "MERGED",
    "url": "https://example.test/pull/7",
    "mergedAt": "2026-11-16T17:02:15Z",
}
OPEN_PULL_REQUEST = {"number": 8, "state": "OPEN", "url": "https://example.test/pull/8", "mergedAt": None}
CLOSED_PULL_REQUEST = {"number": 9, "state": "CLOSED", "url": "https://example.test/pull/9", "mergedAt": None}

# The fake `gh`: prints the canned answer, counts how often it was called, and
# can be told to fail. It reads its instructions from files beside itself so
# each test controls it without environment variables leaking between tests.
# The hook calls `gh pr list --head <branch> ...`, so the branch is the fourth
# argument; a branch with its own answer file gets that answer, every other
# branch gets the shared one.
FAKE_GH_SOURCE = """#!/bin/sh
here="$(dirname "$0")"
echo called >> "$here/gh-calls.log"
if [ -f "$here/gh-fails" ]; then
  echo "gh: could not reach GitHub" >&2
  exit 1
fi
if [ -f "$here/gh-answer-for-$4.json" ]; then
  cat "$here/gh-answer-for-$4.json"
else
  cat "$here/gh-answer.json"
fi
"""


class Workspace:
    """A throwaway git repository on a work branch, plus a fake `gh`."""

    def __init__(self, base_directory: Path) -> None:
        self.repository = base_directory / "repository"
        self.fake_bin = base_directory / "fake-bin"
        self.repository.mkdir()
        self.fake_bin.mkdir()

        fake_gh = self.fake_bin / "gh"
        fake_gh.write_text(FAKE_GH_SOURCE)
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)
        self.set_pull_requests([])

        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Test Person")
        self.git("config", "user.email", "test@example.test")
        self.git("commit", "--quiet", "--allow-empty", "-m", "first commit")
        self.git("switch", "--quiet", "-c", "work-branch")

    def git(self, *arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=self.repository, check=True, capture_output=True)

    def set_pull_requests(self, pull_requests: list[dict[str, object]]) -> None:
        (self.fake_bin / "gh-answer.json").write_text(json.dumps(pull_requests))

    def set_pull_requests_for_branch(self, branch: str, pull_requests: list[dict[str, object]]) -> None:
        """Give one branch its own answer; other branches keep the shared one."""
        (self.fake_bin / f"gh-answer-for-{branch}.json").write_text(json.dumps(pull_requests))

    def make_gh_fail(self) -> None:
        (self.fake_bin / "gh-fails").write_text("")

    def remove_gh(self) -> None:
        (self.fake_bin / "gh").unlink()

    def gh_call_count(self) -> int:
        log = self.fake_bin / "gh-calls.log"
        if not log.exists():
            return 0
        return len(log.read_text().splitlines())

    def run_hook(
        self, mode: str, hook_input: dict[str, object], *, include_real_path: bool = True
    ) -> "subprocess.CompletedProcess[str]":
        """Run the hook as Claude Code would: mode as the argument, JSON on stdin."""
        environment = dict(os.environ)
        search_path = str(self.fake_bin)
        if include_real_path:
            search_path = search_path + os.pathsep + environment.get("PATH", "")
        else:
            # Keep git reachable while hiding any real `gh` on this machine.
            search_path = search_path + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        environment["PATH"] = search_path
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT), mode],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def run_gate(self, command: str, *, working_directory: Path | None = None) -> "subprocess.CompletedProcess[str]":
        """Run the commit gate for one Bash command."""
        hook_input: dict[str, object] = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(working_directory or self.repository),
        }
        return self.run_hook("gate", hook_input)


def make_workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


# --- The commit gate (PreToolUse on Bash) ----------------------------------


def test_commit_is_blocked_when_the_branch_pull_request_is_merged(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_gate('git commit -m "one more thing"')

    assert result.returncode == 2
    assert "work-branch" in result.stderr
    assert "#7" in result.stderr
    assert "merged" in result.stderr.lower()


def test_blocked_message_says_how_to_start_a_new_branch(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_gate("git commit -m more")

    assert "git fetch origin" in result.stderr
    assert "git switch -c" in result.stderr


def test_push_is_blocked_when_the_branch_pull_request_is_merged(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_gate("git push origin work-branch")

    assert result.returncode == 2


def test_commit_is_allowed_when_the_pull_request_is_still_open(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([OPEN_PULL_REQUEST])

    result = workspace.run_gate("git commit -m more")

    assert result.returncode == 0


def test_commit_is_allowed_when_the_branch_has_no_pull_request(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)

    result = workspace.run_gate("git commit -m more")

    assert result.returncode == 0


def test_commit_is_allowed_when_a_reused_branch_has_a_new_open_pull_request(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST, OPEN_PULL_REQUEST])

    result = workspace.run_gate("git commit -m more")

    assert result.returncode == 0


def test_commit_on_the_default_branch_is_not_this_hooks_business(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.git("switch", "--quiet", "main")
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_gate("git commit -m more")

    assert result.returncode == 0
    assert workspace.gh_call_count() == 0


def test_commands_that_do_not_commit_or_push_never_ask_github(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    for harmless_command in ["ls -la", "git status", "git log --oneline", "echo git commit", "uv run pytest"]:
        result = workspace.run_gate(harmless_command)
        assert result.returncode == 0, harmless_command

    assert workspace.gh_call_count() == 0


def test_commit_after_other_commands_in_one_line_is_still_seen(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_gate("git add notes.txt && git commit -m more")

    assert result.returncode == 2


def test_cd_earlier_in_the_command_decides_which_repository_is_checked(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = workspace.run_gate(f"cd {workspace.repository} && git commit -m more", working_directory=elsewhere)

    assert result.returncode == 2


def test_git_dash_c_decides_which_repository_is_checked(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = workspace.run_gate(f"git -C {workspace.repository} commit -m more", working_directory=elsewhere)

    assert result.returncode == 2


def test_commit_outside_any_git_repository_is_allowed(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])
    not_a_repository = tmp_path / "plain-folder"
    not_a_repository.mkdir()

    result = workspace.run_gate("git commit -m more", working_directory=not_a_repository)

    assert result.returncode == 0


def test_gate_allows_the_commit_and_says_so_when_github_cannot_be_reached(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])
    workspace.make_gh_fail()

    result = workspace.run_gate("git commit -m more")

    assert result.returncode == 0
    assert "could not check" in result.stderr.lower()


def test_gate_allows_the_commit_when_gh_is_not_installed(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.remove_gh()
    hook_input: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m more"},
        "cwd": str(workspace.repository),
    }

    result = workspace.run_hook("gate", hook_input, include_real_path=False)

    assert result.returncode == 0
    assert "could not check" in result.stderr.lower()


def test_gate_ignores_input_it_cannot_understand(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)

    result = workspace.run_hook("gate", {"unexpected": "shape"})

    assert result.returncode == 0


# --- A branch switch earlier in the same command ----------------------------
#
# The gate runs before the command does, so the checkout is still on the old
# branch. When the command itself first moves to another branch, a later
# commit lands there, and that is the branch to judge.


def test_commit_after_switch_create_in_the_same_command_is_allowed(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])

    result = workspace.run_gate("git fetch origin && git switch -c next-branch main && git commit -m more")

    assert result.returncode == 0, result.stderr


def test_commit_after_checkout_create_in_the_same_command_is_allowed(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])

    result = workspace.run_gate("git checkout -b next-branch main && git commit -m more")

    assert result.returncode == 0, result.stderr


def test_switch_create_with_git_dash_c_is_followed_into_that_repository(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    repository = workspace.repository

    result = workspace.run_gate(
        f"git -C {repository} switch -c next-branch main && git -C {repository} commit -m more",
        working_directory=elsewhere,
    )

    assert result.returncode == 0, result.stderr


def test_commit_after_switching_to_an_existing_merged_branch_is_blocked(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.git("branch", "finished-branch")
    workspace.set_pull_requests_for_branch("finished-branch", [MERGED_PULL_REQUEST])

    result = workspace.run_gate("git switch finished-branch && git commit -m more")

    assert result.returncode == 2
    assert "finished-branch" in result.stderr


def test_commit_after_switching_from_a_merged_branch_to_an_unmerged_one_is_allowed(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.git("branch", "live-branch")
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])
    workspace.set_pull_requests_for_branch("live-branch", [OPEN_PULL_REQUEST])

    result = workspace.run_gate("git checkout live-branch && git commit -m more")

    assert result.returncode == 0, result.stderr


def test_a_later_switch_back_to_a_merged_branch_is_still_blocked(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])

    result = workspace.run_gate("git switch -c next-branch main && git switch work-branch && git commit -m more")

    assert result.returncode == 2


def test_switch_create_reusing_a_merged_branch_name_is_blocked(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests_for_branch("reused-name", [MERGED_PULL_REQUEST])

    result = workspace.run_gate("git switch -c reused-name main && git commit -m more")

    assert result.returncode == 2


def test_unrecognised_switch_forms_fall_back_to_the_current_branch(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.git("branch", "live-branch")
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])

    for unsure_switch in [
        "git switch --detach main",
        "git switch -",
        "git checkout -- notes.txt",
        "git checkout notes.txt",
        "git switch --track origin/live-branch",
        "git switch -c next-branch main && git checkout -",
    ]:
        result = workspace.run_gate(f"{unsure_switch} && git commit -m more")
        assert result.returncode == 2, unsure_switch


def test_a_switch_not_joined_to_the_commit_by_and_is_not_trusted(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests_for_branch("work-branch", [MERGED_PULL_REQUEST])

    # If the switch failed, these would still run the commit on work-branch.
    for command in [
        "git switch -c next-branch main ; git commit -m more",
        "git switch -c next-branch main || git commit -m more",
        "git switch -c next-branch main && git add . ; git commit -m more",
    ]:
        result = workspace.run_gate(command)
        assert result.returncode == 2, command


# --- Claude session start (tell the agent the status up front) ---------------------


def test_claude_session_start_tells_the_agent_when_the_branch_is_already_merged(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_hook("session-start", {"hook_event_name": "SessionStart", "cwd": str(workspace.repository)})

    assert result.returncode == 0
    output = json.loads(result.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "work-branch" in context
    assert "#7" in context
    assert "git switch -c" in context


def test_claude_session_start_says_nothing_when_the_branch_is_not_merged(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([OPEN_PULL_REQUEST])

    result = workspace.run_hook("session-start", {"hook_event_name": "SessionStart", "cwd": str(workspace.repository)})

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_claude_session_start_mentions_a_pull_request_closed_without_merging(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([CLOSED_PULL_REQUEST])

    result = workspace.run_hook("session-start", {"hook_event_name": "SessionStart", "cwd": str(workspace.repository)})

    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "#9" in context
    assert "closed" in context.lower()


# --- The periodic reminder (on each prompt, without hammering GitHub) -------


def test_prompt_reminder_tells_the_agent_when_the_branch_is_merged(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])

    result = workspace.run_hook(
        "prompt-reminder", {"hook_event_name": "UserPromptSubmit", "cwd": str(workspace.repository)}
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "#7" in output["hookSpecificOutput"]["additionalContext"]


def test_prompt_reminder_never_blocks_the_prompt(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([MERGED_PULL_REQUEST])
    workspace.make_gh_fail()

    result = workspace.run_hook(
        "prompt-reminder", {"hook_event_name": "UserPromptSubmit", "cwd": str(workspace.repository)}
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_prompt_reminder_reuses_a_recent_answer_instead_of_asking_github_again(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([OPEN_PULL_REQUEST])
    hook_input: dict[str, object] = {"hook_event_name": "UserPromptSubmit", "cwd": str(workspace.repository)}

    workspace.run_hook("prompt-reminder", hook_input)
    workspace.run_hook("prompt-reminder", hook_input)
    workspace.run_hook("prompt-reminder", hook_input)

    assert workspace.gh_call_count() == 1


def test_the_commit_gate_always_asks_github_afresh(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    workspace.set_pull_requests([OPEN_PULL_REQUEST])
    workspace.run_hook("prompt-reminder", {"hook_event_name": "UserPromptSubmit", "cwd": str(workspace.repository)})

    # The pull request is merged a moment after the reminder cached "open".
    workspace.set_pull_requests([MERGED_PULL_REQUEST])
    result = workspace.run_gate("git commit -m more")

    assert result.returncode == 2
