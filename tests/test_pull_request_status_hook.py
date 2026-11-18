"""Tests for `.claude/hooks/pull_request_status.py`, the Claude Code hook that
closes a Beads issue once the pull request linked to it has merged into main.

The project rule: an issue stays in progress while its pull request is open,
and is closed only when that pull request merges. The link is the issue's
external reference, set to the pull request's URL. This hook does the closing,
so nobody has to come back after the owner merges.

These are outside-in tests: each one runs the hook script as a separate
process, the way Claude Code does, with the JSON a real hook receives on
standard input. Fake `bd` and `gh` programs sit first on PATH. The fake `bd`
answers `bd list` from a file and records every call; the fake `gh` answers
`gh pr view` from a file of pull request states. No test touches the real
Beads database, GitHub or the network.

The hook is repository tooling, not part of the `goodall` package, so these
tests call `subprocess` directly instead of going through the package's runner
gateway. Real `git` is used, because the hook keeps its throttle stamp in the
repository's git folder.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "pull_request_status.py"

PULL_REQUEST_47 = "https://github.com/lukert33/goodall/pull/47"
PULL_REQUEST_48 = "https://github.com/lukert33/goodall/pull/48"

# The fake `bd`: logs its arguments, answers `list` from in-progress.json,
# and fails a push when a file named `push-fails` sits beside it.
FAKE_BD_SOURCE = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).parent
with open(here / "bd-calls.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
arguments = sys.argv[1:]
if arguments[:1] == ["list"]:
    print((here / "in-progress.json").read_text())
elif arguments[:2] == ["dolt", "push"] and (here / "push-fails").exists():
    print("fatal: could not reach the remote", file=sys.stderr)
    sys.exit(1)
"""

# The fake `gh`: logs its arguments and answers `pr view <url>` from
# pull-requests.json, or fails like gh does when the pull request is unknown.
FAKE_GH_SOURCE = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).parent
with open(here / "gh-calls.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if (here / "gh-fails").exists():
    print("error connecting to api.github.com", file=sys.stderr)
    sys.exit(1)
pull_requests = json.loads((here / "pull-requests.json").read_text())
url = sys.argv[3]
if url not in pull_requests:
    print("GraphQL: Could not resolve to a PullRequest", file=sys.stderr)
    sys.exit(1)
print(json.dumps(pull_requests[url]))
"""


class Workspace:
    """A throwaway git repository to run the hook in, with fake `bd` and `gh` on PATH."""

    def __init__(self, base_directory: Path) -> None:
        self.project = base_directory / "project"
        self.fake_bin = base_directory / "fake-bin"
        self.project.mkdir()
        self.fake_bin.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        for name, source in (("bd", FAKE_BD_SOURCE), ("gh", FAKE_GH_SOURCE)):
            program = self.fake_bin / name
            program.write_text(source.format(python=sys.executable))
            program.chmod(program.stat().st_mode | stat.S_IXUSR)
        self.set_issues([])
        self.set_pull_requests({})

    def set_issues(self, issues: list[dict[str, object]]) -> None:
        (self.fake_bin / "in-progress.json").write_text(json.dumps(issues))

    def set_pull_requests(self, pull_requests: dict[str, dict[str, object]]) -> None:
        (self.fake_bin / "pull-requests.json").write_text(json.dumps(pull_requests))

    def calls_to(self, program: str) -> list[list[str]]:
        log = self.fake_bin / f"{program}-calls.log"
        if not log.exists():
            return []
        calls = []
        for line in log.read_text().splitlines():
            calls.append(json.loads(line))
        return calls

    def close_calls(self) -> list[list[str]]:
        closes = []
        for call in self.calls_to("bd"):
            if call[:1] == ["close"]:
                closes.append(call)
        return closes

    def run_hook(self, mode: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run the hook as Claude Code would, at session start or before a prompt."""
        event_names = {"session-start": "SessionStart", "prompt-check": "UserPromptSubmit"}
        hook_input = {"hook_event_name": event_names.get(mode, mode), "cwd": str(self.project)}
        environment = dict(os.environ)
        environment["PATH"] = path if path is not None else str(self.fake_bin) + os.pathsep + environment["PATH"]
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT), mode],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            cwd=self.project,
            env=environment,
            timeout=60,
            check=False,
        )


def issue(issue_id: str, external_ref: str | None) -> dict[str, object]:
    record: dict[str, object] = {"id": issue_id, "title": f"Title of {issue_id}", "status": "in_progress"}
    if external_ref is not None:
        record["external_ref"] = external_ref
    return record


def pull_request(state: str, base: str = "main") -> dict[str, object]:
    return {"state": state, "baseRefName": base}


def context_of(completed: subprocess.CompletedProcess[str]) -> str:
    """The text the hook hands Claude Code as added context, or "" when it said nothing."""
    if completed.stdout.strip() == "":
        return ""
    output = json.loads(completed.stdout)
    return str(output["hookSpecificOutput"]["additionalContext"])


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_an_issue_whose_pull_request_merged_into_main_is_closed_and_pushed(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-yjt.5", PULL_REQUEST_47)])
    workspace.set_pull_requests({PULL_REQUEST_47: pull_request("MERGED")})

    completed = workspace.run_hook("session-start")

    assert completed.returncode == 0
    closes = workspace.close_calls()
    assert len(closes) == 1
    assert closes[0][1] == "goodall-yjt.5"
    assert PULL_REQUEST_47 in " ".join(closes[0])
    assert ["dolt", "push"] in workspace.calls_to("bd")
    assert "Closed goodall-yjt.5" in context_of(completed)
    assert PULL_REQUEST_47 in context_of(completed)


def test_an_open_pull_request_keeps_its_issue_in_progress_and_is_listed_at_session_start(
    workspace: Workspace,
) -> None:
    workspace.set_issues([issue("goodall-acp.6", PULL_REQUEST_48)])
    workspace.set_pull_requests({PULL_REQUEST_48: pull_request("OPEN")})

    completed = workspace.run_hook("session-start")

    assert completed.returncode == 0
    assert workspace.close_calls() == []
    assert "goodall-acp.6" in context_of(completed)
    assert PULL_REQUEST_48 in context_of(completed)


def test_a_pull_request_closed_without_merging_is_reported_and_its_issue_left_alone(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-acp.6", PULL_REQUEST_48)])
    workspace.set_pull_requests({PULL_REQUEST_48: pull_request("CLOSED")})

    completed = workspace.run_hook("session-start")

    assert workspace.close_calls() == []
    assert "closed without merging" in context_of(completed)
    assert "goodall-acp.6" in context_of(completed)


def test_a_stacked_pull_request_merged_into_another_branch_does_not_close_its_issue(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-fs9.7", PULL_REQUEST_48)])
    workspace.set_pull_requests({PULL_REQUEST_48: pull_request("MERGED", base="claude/telemetry-receipt-log")})

    completed = workspace.run_hook("session-start")

    assert workspace.close_calls() == []
    assert "claude/telemetry-receipt-log" in context_of(completed)
    assert "not main" in context_of(completed)


def test_issues_with_no_pull_request_link_are_ignored(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-acp.1", None), issue("goodall-x", "jira-ABC-1")])

    completed = workspace.run_hook("session-start")

    assert completed.returncode == 0
    assert workspace.calls_to("gh") == []
    assert workspace.close_calls() == []
    assert context_of(completed) == ""


def test_github_unreachable_changes_nothing_and_does_not_fail(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-yjt.5", PULL_REQUEST_47)])
    (workspace.fake_bin / "gh-fails").write_text("")

    completed = workspace.run_hook("session-start")

    assert completed.returncode == 0
    assert workspace.close_calls() == []
    assert "could not be checked" in context_of(completed)


def test_a_failed_push_after_closing_says_what_to_run(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-yjt.5", PULL_REQUEST_47)])
    workspace.set_pull_requests({PULL_REQUEST_47: pull_request("MERGED")})
    (workspace.fake_bin / "push-fails").write_text("")

    completed = workspace.run_hook("session-start")

    assert completed.returncode == 0
    assert "Closed goodall-yjt.5" in context_of(completed)
    assert "bd dolt push" in context_of(completed)


def test_the_prompt_check_runs_at_most_once_in_ten_minutes(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-acp.6", PULL_REQUEST_48)])
    workspace.set_pull_requests({PULL_REQUEST_48: pull_request("OPEN")})

    workspace.run_hook("prompt-check")
    workspace.run_hook("prompt-check")

    assert len(workspace.calls_to("gh")) == 1


def test_the_prompt_check_says_nothing_about_pull_requests_still_open(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-acp.6", PULL_REQUEST_48)])
    workspace.set_pull_requests({PULL_REQUEST_48: pull_request("OPEN")})

    completed = workspace.run_hook("prompt-check")

    assert completed.returncode == 0
    assert context_of(completed) == ""


def test_the_prompt_check_closes_a_merged_issue(workspace: Workspace) -> None:
    workspace.set_issues([issue("goodall-yjt.5", PULL_REQUEST_47)])
    workspace.set_pull_requests({PULL_REQUEST_47: pull_request("MERGED")})

    completed = workspace.run_hook("prompt-check")

    assert len(workspace.close_calls()) == 1
    assert "Closed goodall-yjt.5" in context_of(completed)


def test_a_machine_without_bd_or_gh_is_left_alone(workspace: Workspace) -> None:
    empty_bin = workspace.fake_bin.parent / "empty-bin"
    empty_bin.mkdir()
    git_folder = str(Path(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()).parent)

    completed = workspace.run_hook("session-start", path=str(empty_bin) + os.pathsep + git_folder)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert context_of(completed) == ""
