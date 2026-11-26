"""The diagnostic checks of Claude Code's hooks, under the heading "Claude Code".

`goodall doctor` runs `CLAUDE_CODE_CHECKS` last (goodall.doctor). Two
checks:

- "Claude Code's user settings run goodall's hooks": Claude Code's user
  settings file has a `goodall hook <event>` command for every event goodall
  handles (goodall.hook.KNOWN_EVENTS), and does not turn hooks off.
- "The goodall that the hooks run starts": each goodall program those
  commands name runs `goodall --version` successfully. A hook whose goodall
  cannot start leaves the prompt gate open (Claude Code lets a prompt
  through when a hook cannot run), so the eval would record nothing.

Which settings file counts, and why: only Claude Code's user settings,
`settings.json` in Claude Code's own folder (`~/.claude`, or the folder
CLAUDE_CONFIG_DIR names; goodall.claude_family.claude_config_folder), which is
where docs/claude-code-hooks.md tells a person to put the hooks. A project's
settings (`.claude/settings.json` or `.claude/settings.local.json` inside a
project folder) are not read: they apply only while Claude Code works in
that folder, so a prompt gate there leaves every other folder in the eval
account ungated, and doctor cannot know which folders the contributor will
work in. Settings an administrator manages for every account on the Mac are
not read either; hooks put there would be reported missing (a judgment
call recorded for the owner).

What is read, and what is never shown: from the whole settings file, only
`hooks` and `disableAllHooks` are looked at. The file also holds things
such as environment variables, which may include keys, so no value from it
ever appears in a message: not a command, not the path of the goodall a
command names, not the line JSON could not read. Messages name the
settings file, the events and the line number instead.

How a hook command is recognized (`read_goodall_hook_command`): split as a
shell would split it, it is a program called `goodall` followed by `hook`
and, optionally, one event name. Anything else, such as a sound played on
Stop, is some other hook and is left alone. A goodall command that names a
different event from the one it is installed on does not count for that
event, because goodall trusts the name on the command line over Claude
Code's (goodall.hook).

Two things work but warn:

- A bare `goodall hook`, with no event, takes the event from what Claude
  Code sends (owner decision 2026-11-17, Q56), but when that cannot be read
  it exits 1 and the prompt goes through unrecorded.
- A goodall named without its full path may not be found, because Claude
  Code can run hooks without the folders a terminal adds to PATH. It is
  still started by the second check, from doctor's own PATH, so a hook
  that cannot start is reported only when doctor cannot start it either.
  `~/` counts as a full path, because Claude Code runs hook commands
  through a shell, which expands it.

Like every check, these only look (AGENTS.md invariant 5): the settings
file is read and never written, and `goodall --version` changes nothing.
Both wait for valid local settings, because hooks matter only in an
account set up for an eval, and so that missing local settings stay one
failure.
"""

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from goodall.checks import Check, CheckContext, CheckOutcome, failed, passed, warned
from goodall.claude_family import claude_config_folder
from goodall.hook import KNOWN_EVENTS
from goodall.runner import Command, CommandOutcome, CommandResult, OutputMode
from goodall.settings_checks import LOCAL_SETTINGS_VALID
from goodall.terminal import Failure

GROUP = "Claude Code"

HOOKS_INSTALLED = "claude_code.hooks_installed"
HOOKS_RUN_GOODALL = "claude_code.hooks_run_goodall"

# The name of Claude Code's user settings file, inside its own folder.
CLAUDE_SETTINGS_FILE_NAME = "settings.json"

# How long the goodall a hook names may take to print its version. It does
# nothing else, so a goodall that takes longer would also slow every prompt.
GOODALL_START_TIMEOUT_SECONDS = 5.0

# Where a person finds the hook settings to copy.
HOOKS_DOCUMENT = "docs/claude-code-hooks.md in goodall's documentation"


@dataclass(frozen=True)
class GoodallHookCommand:
    """One `goodall hook` command from Claude Code's settings.

    - `program` is the goodall it runs, as written (a full path, a path
      starting with `~/`, or just `goodall`). It is never shown.
    - `event_name` is the event it names, or None for a bare `goodall hook`.
    """

    program: str
    event_name: str | None


def read_goodall_hook_command(command: str) -> GoodallHookCommand | None:
    """The goodall program and event in one hook command, or None when it is not a `goodall hook` command."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) not in [2, 3]:
        return None
    if Path(words[0]).name != "goodall" or words[1] != "hook":
        return None
    if len(words) == 2:
        return GoodallHookCommand(program=words[0], event_name=None)
    return GoodallHookCommand(program=words[0], event_name=words[2])


def claude_user_settings_file() -> Path:
    """Claude Code's user settings file, in the folder Claude Code keeps its own files in."""
    return claude_config_folder() / CLAUDE_SETTINGS_FILE_NAME


def check_hooks_installed(context: CheckContext) -> CheckOutcome:
    """Claude Code's user settings run a `goodall hook` command on every event goodall handles."""
    settings_file = claude_user_settings_file()
    reading = read_claude_settings(settings_file)
    if isinstance(reading, Failure):
        return failed([reading])
    if reading.get("disableAllHooks") is True:
        return failed([hooks_disabled_failure(settings_file)])

    commands_by_event = goodall_hook_commands_by_event(reading.get("hooks"))
    missing_events = []
    for event in KNOWN_EVENTS:
        if not commands_by_event[event]:
            missing_events.append(event)
    if missing_events:
        return failed([hooks_missing_failure(settings_file, missing_events)])

    programs: dict[str, list[str]] = {}
    events_without_their_name = []
    events_without_a_full_path = []
    for event in KNOWN_EVENTS:
        names_its_event = False
        for hook_command in commands_by_event[event]:
            events_for_program = programs.setdefault(hook_command.program, [])
            if event not in events_for_program:
                events_for_program.append(event)
            if hook_command.event_name is not None:
                names_its_event = True
            if not is_full_path(hook_command.program) and event not in events_without_a_full_path:
                events_without_a_full_path.append(event)
        if not names_its_event:
            events_without_their_name.append(event)
    context.hook_goodall_programs = programs

    warnings = []
    if events_without_their_name:
        warnings.append(hook_without_event_failure(settings_file, events_without_their_name))
    if events_without_a_full_path:
        warnings.append(hook_not_full_path_failure(settings_file, events_without_a_full_path))
    if warnings:
        return warned(warnings)
    return passed()


def read_claude_settings(settings_file: Path) -> dict[str, object] | Failure:
    """Claude Code's settings as a dictionary, or the failure saying why they could not be read.

    Trap: the reason JSON gives for a parse error can quote the text it
    stopped at, which may be a key, so only its line number is kept.
    """
    try:
        settings_bytes = settings_file.read_bytes()
    except FileNotFoundError:
        return claude_settings_missing_failure(settings_file)
    except IsADirectoryError:
        return claude_settings_missing_failure(settings_file)
    except OSError as read_error:
        return claude_settings_unreadable_failure(settings_file, read_error.strerror or "it could not be read")
    try:
        settings = json.loads(settings_bytes)
    except json.JSONDecodeError as parse_error:
        return claude_settings_not_json_failure(settings_file, f"line {parse_error.lineno}")
    except ValueError:
        return claude_settings_not_json_failure(settings_file, "its text encoding")
    if not isinstance(settings, dict):
        return claude_settings_not_json_failure(settings_file, "its top level, which is not a {...} table")
    return settings


def goodall_hook_commands_by_event(hooks: object) -> dict[str, list[GoodallHookCommand]]:
    """Every `goodall hook` command in Claude Code's `hooks` table, by the event it is installed on.

    The table's shape is Claude Code's: each event holds a list of matcher
    groups, each with a list of hooks, each a {"type": "command",
    "command": "..."}. Anything in another shape is passed over, because
    Claude Code would not run it either.
    """
    found: dict[str, list[GoodallHookCommand]] = {}
    for event in KNOWN_EVENTS:
        found[event] = []
    if not isinstance(hooks, dict):
        return found
    for event in KNOWN_EVENTS:
        matcher_groups = hooks.get(event)
        if not isinstance(matcher_groups, list):
            continue
        for matcher_group in matcher_groups:
            if not isinstance(matcher_group, dict) or not isinstance(matcher_group.get("hooks"), list):
                continue
            for hook in matcher_group["hooks"]:
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    continue
                command = hook.get("command")
                if not isinstance(command, str):
                    continue
                hook_command = read_goodall_hook_command(command)
                if hook_command is None:
                    continue
                if hook_command.event_name is not None and hook_command.event_name != event:
                    continue
                found[event].append(hook_command)
    return found


def is_full_path(program: str) -> bool:
    """True when `program` names one file wherever it is run from: from `/`, or from `~/`, which a shell expands."""
    return program.startswith("/") or program.startswith("~/")


def names_joined(names: list[str]) -> str:
    """`names` in a sentence: "Stop", "Stop and SessionEnd", "PostToolUse, Stop and SessionEnd"."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


WHY_HOOKS_MATTER = (
    "Goodall records an eval through Claude Code's hooks: the UserPromptSubmit hook is the prompt gate, which "
    "refuses a prompt while no work block is active, and the other hooks show the work block was active and "
    "copy each Claude session's transcript."
)


def claude_settings_missing_failure(settings_file: Path) -> Failure:
    """The failure for an account whose Claude Code has no user settings file."""
    return Failure(
        kind="claude_code_settings_missing",
        what_happened=(
            f"Claude Code has no user settings file at {settings_file}, so none of goodall's hooks are installed "
            "and nothing stops a prompt while no work block is active."
        ),
        why=WHY_HOOKS_MATTER,
        what_to_do_next=(
            f"Run goodall onboard, or add the hooks by hand as {HOOKS_DOCUMENT} shows. Hooks in a project's own "
            ".claude folder do not count, because they apply only inside that project. Then run goodall doctor "
            "again."
        ),
    )


def claude_settings_unreadable_failure(settings_file: Path, reason: str) -> Failure:
    """The failure for a Claude Code settings file this account cannot read."""
    return Failure(
        kind="claude_code_settings_unreadable",
        what_happened=f"Goodall could not read Claude Code's user settings file {settings_file}: {reason}.",
        why=WHY_HOOKS_MATTER,
        what_to_do_next=(
            f"Check that this macOS user account owns {settings_file} and may read it (ls -l shows both). Then run "
            "goodall doctor again."
        ),
    )


def claude_settings_not_json_failure(settings_file: Path, where: str) -> Failure:
    """The failure for a Claude Code settings file that is not a JSON table; `where` says where, never what."""
    return Failure(
        kind="claude_code_settings_not_json",
        what_happened=(
            f"Claude Code's user settings file {settings_file} is not valid JSON (the problem is at {where}), so "
            "Claude Code cannot read goodall's hooks from it."
        ),
        why=WHY_HOOKS_MATTER,
        what_to_do_next=(
            f"Open {settings_file} in a text editor and fix it there: a missing comma or quotation mark is the "
            f"usual cause. {HOOKS_DOCUMENT[0].upper() + HOOKS_DOCUMENT[1:]} shows the hooks as they should look. "
            "Then run goodall doctor again."
        ),
    )


def hooks_disabled_failure(settings_file: Path) -> Failure:
    """The failure for Claude Code settings that turn every hook off."""
    return Failure(
        kind="claude_code_hooks_disabled",
        what_happened=(
            f"Claude Code's user settings file {settings_file} turns every hook off, so none of goodall's hooks run "
            "and nothing stops a prompt while no work block is active."
        ),
        why=WHY_HOOKS_MATTER,
        what_to_do_next=(
            f'Open {settings_file} in a text editor and delete the line "disableAllHooks": true, or set it to '
            "false. Then run goodall doctor again."
        ),
    )


def hooks_missing_failure(settings_file: Path, missing_events: list[str]) -> Failure:
    """The failure for Claude Code settings with no goodall hook on some events goodall handles."""
    return Failure(
        kind="claude_code_hooks_missing",
        what_happened=(
            f"Claude Code's user settings file {settings_file} runs no goodall hook on {names_joined(missing_events)}."
        ),
        why=WHY_HOOKS_MATTER,
        what_to_do_next=(
            f"Add the hook for each of those events to {settings_file} as {HOOKS_DOCUMENT} shows, beside any hooks "
            "already there. Each command names its own event, as in goodall hook Stop for Stop. Then run goodall "
            "doctor again."
        ),
    )


def hook_without_event_failure(settings_file: Path, events: list[str]) -> Failure:
    """The warning for goodall hooks that do not name their event."""
    return Failure(
        kind="claude_code_hook_without_event",
        what_happened=(
            f"The goodall hooks on {names_joined(events)} in {settings_file} do not name their event. They work, "
            "but whenever goodall cannot read what Claude Code sends, they record nothing, and a prompt on "
            "UserPromptSubmit goes through ungated."
        ),
        why=(
            "Goodall takes the event from the hook command first, so it still knows the event when Claude Code's "
            "input cannot be read."
        ),
        what_to_do_next=(
            f"Add each event's name to the end of its goodall hook command in {settings_file}, as in goodall hook "
            "UserPromptSubmit. Then run goodall doctor again."
        ),
    )


def hook_not_full_path_failure(settings_file: Path, events: list[str]) -> Failure:
    """The warning for goodall hooks that name goodall without saying where it is."""
    return Failure(
        kind="claude_code_hook_not_full_path",
        what_happened=(
            f"The goodall hooks on {names_joined(events)} in {settings_file} name goodall without its full path. "
            "Claude Code may run hooks without the folders your terminal adds to PATH, and then cannot find it."
        ),
        why="A hook Claude Code cannot run lets every prompt through, unrecorded.",
        what_to_do_next=(
            "Run command -v goodall in Terminal in this eval account, and put the full path it prints in place of "
            f"goodall at the start of each of those commands in {settings_file}. Then run goodall doctor again."
        ),
    )


def check_hooks_run_goodall(context: CheckContext) -> CheckOutcome:
    """Each goodall the hooks name starts and prints its version.

    Each program is started once, however many events it is installed on,
    with its output discarded: only whether it started matters, and its
    output is not something goodall needs to hold.
    """
    programs = context.hook_goodall_programs
    if programs is None:
        raise RuntimeError(f"{HOOKS_RUN_GOODALL} needs {HOOKS_INSTALLED} as a prerequisite.")
    settings_file = claude_user_settings_file()
    failures = []
    for program, events in programs.items():
        command = Command(
            [str(Path(program).expanduser()), "--version"],
            output=OutputMode.DISCARD,
            timeout_seconds=GOODALL_START_TIMEOUT_SECONDS,
        )
        result = context.runner.run(command)
        if not result.succeeded:
            failures.append(hook_goodall_does_not_start_failure(settings_file, events, result))
    if failures:
        return failed(failures)
    return passed()


def goodall_start_described(result: CommandResult) -> str:
    """What happened when goodall was started, in goodall's words, without its path."""
    if result.outcome == CommandOutcome.NOT_FOUND:
        return "there is no program at the place the hook command names"
    if result.outcome == CommandOutcome.COULD_NOT_START:
        return "the system would not start it"
    if result.outcome == CommandOutcome.TIMED_OUT:
        return f"it did not finish within {GOODALL_START_TIMEOUT_SECONDS:g} seconds"
    return f"it stopped with exit code {result.exit_code}"


def hook_goodall_does_not_start_failure(settings_file: Path, events: list[str], result: CommandResult) -> Failure:
    """The failure for a goodall, named in the hooks on `events`, that does not start.

    Names the events and not the program's path: the path is a value from
    Claude Code's settings (see the module docstring), and the person can
    find it beside the event's name in the settings file.
    """
    return Failure(
        kind="hook_goodall_does_not_start",
        what_happened=(
            f"The goodall that Claude Code's hooks on {names_joined(events)} run did not start: "
            f"{goodall_start_described(result)}. Claude Code lets a prompt through when its hook cannot run, so "
            "nothing is gated or recorded."
        ),
        why=WHY_HOOKS_MATTER,
        what_to_do_next=(
            "Run command -v goodall in Terminal in this eval account, and put the full path it prints in place of "
            f"the program at the start of each goodall hook command in {settings_file}. If it prints nothing, "
            "goodall is not installed for this macOS user account: ask whoever installed goodall on this Mac. "
            "Then run goodall doctor again."
        ),
    )


CLAUDE_CODE_CHECKS = [
    Check(
        id=HOOKS_INSTALLED,
        group=GROUP,
        summary="Claude Code's user settings run goodall's hooks",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_hooks_installed,
    ),
    Check(
        id=HOOKS_RUN_GOODALL,
        group=GROUP,
        summary="The goodall that the hooks run starts",
        prerequisites=[HOOKS_INSTALLED],
        run=check_hooks_run_goodall,
    ),
]
