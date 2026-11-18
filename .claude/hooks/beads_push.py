#!/usr/bin/env python3
"""Claude Code hook: push the Beads database whenever an issue is started or finished.

Why this exists. Issues and memories live in a Dolt database on this Mac.
They reach GitHub only when someone runs `bd dolt push`, and for the first
days of this project nobody did, so the only copy of the project's decisions
was local. This hook makes the push happen at the moments that matter, so
nobody has to remember.

Who calls it. Claude Code, through `.claude/settings.json`, after every Bash
command an agent runs (PostToolUse), with the hook's JSON on standard input.

What it does. If the command the agent just ran claimed an issue
(`bd update <id> --claim`), closed one (`bd close`), or saved a memory
(`bd remember`), it runs `bd dolt push` once and waits for it. Every other
command is ignored.

Design decisions a reader would otherwise have to reverse-engineer:

- It runs after the command, never before, so it cannot stop an agent's work.
  When the push fails it exits 2, which is how a PostToolUse hook hands a
  message to the agent: the agent is told the data is safe locally and what
  to run later. Nothing is undone.
- It waits for the push (about two seconds when there is nothing new) instead
  of starting it in the background. Claude Code stops background hook work
  when it exits, and a push that silently never happened is the failure this
  hook exists to prevent.
- Memories are pushed as well as issues, although the owner asked only for
  "started or completed". Memories hold the project's decisions and are left
  out of the `.beads/issues.jsonl` export, so they have no other copy.
- A command is judged by its first word, part by part. `git commit -m "bd
  close"` mentions bd but does not run it, so it pushes nothing.
- A machine without `bd`, or input this hook cannot read, is left alone. A
  broken hook must not get in the way of unrelated work.
- Standard library only, so it runs without goodall installed.
"""

import json
import re
import shlex
import shutil
import subprocess
import sys

# How long to wait for the push before giving up and telling the agent.
PUSH_TIMEOUT_SECONDS = 60

# Exit code that makes Claude Code show a PostToolUse hook's message to the agent.
TELL_THE_AGENT = 2

# Shell punctuation that separates one command from the next on a line.
COMMAND_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")


def command_from_hook_input(hook_input_text: str) -> str | None:
    """Return the Bash command the agent just ran, or None when this was not a Bash command."""
    try:
        hook_input = json.loads(hook_input_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(hook_input, dict):
        return None
    if hook_input.get("tool_name") != "Bash":
        return None
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    return command


def words_of(command_part: str) -> list[str]:
    """Split one command into words the way a shell would, or roughly so when its quoting is unusual."""
    try:
        return shlex.split(command_part)
    except ValueError:
        return command_part.split()


def changes_what_should_be_pushed(command: str) -> bool:
    """True when the command claimed an issue, closed one, or saved a memory."""
    for command_part in COMMAND_SEPARATORS.split(command):
        words = words_of(command_part)
        if len(words) < 2 or words[0] != "bd":
            continue
        subcommand = words[1]
        if subcommand in ("close", "remember"):
            return True
        if subcommand == "update" and "--claim" in words:
            return True
    return False


def push_beads_database(bd_program: str) -> str | None:
    """Run `bd dolt push`. Return None when it worked, or what went wrong."""
    try:
        completed = subprocess.run(
            [bd_program, "dolt", "push"],
            capture_output=True,
            text=True,
            timeout=PUSH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"it did not finish within {PUSH_TIMEOUT_SECONDS} seconds"
    except OSError as error:
        return f"bd could not be started ({error.strerror})"
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or completed.stdout).strip()
    if detail == "":
        detail = f"bd dolt push exited with code {completed.returncode}"
    return detail


def main() -> int:
    command = command_from_hook_input(sys.stdin.read())
    if command is None or not changes_what_should_be_pushed(command):
        return 0
    bd_program = shutil.which("bd")
    if bd_program is None:
        return 0
    problem = push_beads_database(bd_program)
    if problem is None:
        return 0
    print(
        "The Beads change is saved on this Mac but was not pushed to the remote: "
        f"{problem}. Nothing is lost and nothing needs to be redone. "
        "Run bd dolt push before you finish, and tell the owner if it fails again.",
        file=sys.stderr,
    )
    return TELL_THE_AGENT


if __name__ == "__main__":
    sys.exit(main())
