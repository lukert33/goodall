"""Tests for `.claude/hooks/beads_push.py`, the Claude Code hook that pushes
the Beads database to its remote whenever an agent starts or finishes an issue.

These are outside-in tests: each one runs the hook script as a separate
process, the way Claude Code does, feeding it the JSON a real PostToolUse hook
receives on standard input. A fake `bd` program is placed first on PATH and
records how it was called, so no test touches the real Beads database or the
network.

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

import pytest

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "beads_push.py"

# The fake `bd`: writes its arguments to a log beside itself, and fails when a
# file named `bd-fails` sits beside it. It never contacts anything.
FAKE_BD_SOURCE = """#!/bin/sh
here="$(dirname "$0")"
echo "$@" >> "$here/bd-calls.log"
if [ -f "$here/bd-fails" ]; then
  echo "fatal: could not reach the remote" >&2
  exit 1
fi
echo "Push complete."
"""


class Workspace:
    """A throwaway folder to run the hook in, plus a fake `bd` on PATH."""

    def __init__(self, base_directory: Path) -> None:
        self.project = base_directory / "project"
        self.fake_bin = base_directory / "fake-bin"
        self.project.mkdir()
        self.fake_bin.mkdir()
        fake_bd = self.fake_bin / "bd"
        fake_bd.write_text(FAKE_BD_SOURCE)
        fake_bd.chmod(fake_bd.stat().st_mode | stat.S_IXUSR)

    def make_bd_fail(self) -> None:
        (self.fake_bin / "bd-fails").write_text("")

    def bd_calls(self) -> list[str]:
        log = self.fake_bin / "bd-calls.log"
        if not log.exists():
            return []
        return log.read_text().splitlines()

    def run_hook(self, tool_name: str, command: str) -> subprocess.CompletedProcess[str]:
        """Run the hook as Claude Code would after a tool call has finished."""
        hook_input = {
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": {"command": command},
            "cwd": str(self.project),
        }
        environment = dict(os.environ)
        environment["PATH"] = str(self.fake_bin) + os.pathsep + environment["PATH"]
        return subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            cwd=self.project,
            env=environment,
            timeout=30,
            check=False,
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "bd close goodall-fs9.4",
        'bd close goodall-fs9.4 --force --reason "Built in the pull request"',
        "bd update goodall-fs9.4 --claim",
        "cd /somewhere/else && bd update goodall-fs9.4 --claim",
        "bd update goodall-a --claim && bd close goodall-b",
        'bd remember --key a-finding "what was observed"',
    ],
)
def test_starting_finishing_or_remembering_pushes_the_beads_database_once(workspace: Workspace, command: str) -> None:
    completed = workspace.run_hook("Bash", command)

    assert completed.returncode == 0
    assert workspace.bd_calls() == ["dolt push"]
    assert completed.stdout == ""
    assert completed.stderr == ""


@pytest.mark.parametrize(
    "command",
    [
        "bd show goodall-fs9.4",
        "bd ready",
        "bd list --status open",
        'bd update goodall-fs9.4 --append-notes "a note"',
        "bd dolt push",
        "git commit -m 'bd close is mentioned here but nothing ran bd'",
        "uv run pytest",
    ],
)
def test_other_commands_push_nothing(workspace: Workspace, command: str) -> None:
    completed = workspace.run_hook("Bash", command)

    assert completed.returncode == 0
    assert workspace.bd_calls() == []


def test_a_tool_other_than_bash_pushes_nothing(workspace: Workspace) -> None:
    completed = workspace.run_hook("Write", "bd close goodall-fs9.4")

    assert completed.returncode == 0
    assert workspace.bd_calls() == []


def test_a_failed_push_tells_the_agent_what_to_do_without_undoing_its_work(workspace: Workspace) -> None:
    workspace.make_bd_fail()

    completed = workspace.run_hook("Bash", "bd close goodall-fs9.4")

    # Exit 2 is how a PostToolUse hook hands its message to the agent. The
    # command it follows has already run, so nothing is blocked or undone.
    assert completed.returncode == 2
    assert "was not pushed" in completed.stderr
    assert "bd dolt push" in completed.stderr
    assert "could not reach the remote" in completed.stderr


def test_input_the_hook_cannot_read_pushes_nothing_and_does_not_fail(workspace: Workspace) -> None:
    environment = dict(os.environ)
    environment["PATH"] = str(workspace.fake_bin) + os.pathsep + environment["PATH"]

    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="this is not JSON",
        capture_output=True,
        text=True,
        cwd=workspace.project,
        env=environment,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert workspace.bd_calls() == []


def test_a_machine_without_bd_is_left_alone(workspace: Workspace) -> None:
    (workspace.fake_bin / "bd").unlink()
    hook_input = {
        "tool_name": "Bash",
        "tool_input": {"command": "bd close goodall-fs9.4"},
        "cwd": str(workspace.project),
    }
    environment = dict(os.environ)
    # A PATH with no `bd` on it at all, but still able to find nothing else the hook needs.
    environment["PATH"] = str(workspace.fake_bin)

    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        cwd=workspace.project,
        env=environment,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
