"""`goodall hook <event>`: what Claude Code runs each time one of its hooks fires.

Claude Code starts `goodall hook UserPromptSubmit` (or PostToolUse, Stop,
SessionEnd, SessionStart) as a new process, sends the event's details as one
JSON object on standard input, and reads the exit code and standard error.
docs/claude-code-hooks.md shows the settings that make it do so. The command
is reached through goodall.entry_point, which hands `goodall hook` straight to
`run_hook_command` without loading the rest of goodall; goodall.cli also
routes it here, so both paths behave the same.

Two jobs, by event:

- UserPromptSubmit is the prompt gate. When a work block is active it leaves
  a heartbeat naming the Claude session, prints nothing and exits 0, and the
  prompt goes through. Otherwise it refuses the prompt: it writes one or two
  plain sentences to standard error saying what to do, and exits 2.
- Every other event leaves a heartbeat when a work block is active, and
  always exits 0 with nothing printed. These events must never disrupt
  Claude Code: exit 2 from a Stop hook, for one, would stop Claude from
  finishing its reply. PostToolUse, Stop and SessionEnd also copy the
  Claude session's transcript into the log directory (goodall.transcripts),
  with or without a work block.

Decisions a reader would otherwise reverse-engineer (PROVISIONAL unless the
owner says otherwise):

- Why exit 2, when AGENTS.md says goodall's code for a closed prompt gate is
  3: Claude Code refuses a prompt only when the hook exits 2 (any other
  non-zero code is shown as a hook error and the prompt goes through, which
  is the opposite of what the gate is for). So the hook returns
  exit_codes.CLAUDE_CODE_REFUSES_PROMPT, which is 2; PROMPT_GATE_CLOSED (3)
  stays for goodall's own commands. Exit 2 also means "usage error" to
  goodall's own commands, which is why the hook never uses it for one.
- Why the event name is on the command line rather than taken from the
  JSON's `hook_event_name`: the gate must know it is the gate even when the
  JSON cannot be read, so it can refuse rather than let the prompt through.
  When the two disagree, the command line wins.
- The gate fails closed. Missing or unreadable local settings, an
  unreachable log directory (ADR 0008), input that cannot be read, and any
  unexpected error all refuse the prompt, because in an eval account
  unrecorded work is the worse failure. Each has its own next step.
- Input without a Claude session id refuses the prompt too: without it, the
  Claude session's transcript cannot be tied to the work block, so the work
  would be recorded but not attributable.
- Every other event fails open and silent: any error is swallowed, because
  a broken heartbeat is a gap in evidence, while a broken hook would get in
  the person's way on every tool call. Standard output stays empty, because
  Claude Code adds a SessionStart hook's output to Claude's context.
- Refusals never quote a value from local settings, not even the log
  directory: what Claude Code shows can end up in a transcript (ADR 0006).
  `goodall status` and `goodall doctor`, run in a terminal, give the detail.
- Imports: standard library and hook-safe goodall modules only (ADR 0005,
  AGENTS.md invariant 7), and nothing that starts programs. A test in
  tests/test_hook_path_imports.py proves it in a fresh interpreter.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from goodall import exit_codes, records, transcripts, work_blocks
from goodall.folders import describe_folder_problem
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.terminal import Terminal

# The one event that can refuse what the person asked for.
PROMPT_GATE_EVENT = "UserPromptSubmit"

# The events after which the Claude session's transcript is copied into the
# log directory. PostToolUse fires after every tool call, so a long reply is
# copied as it goes, one step behind (the tool's result lands just after the
# hook); Stop catches the end of each reply and SessionEnd the end of the
# Claude session. A kill fires none of them, so `goodall start`, `stop` and
# `status` sweep up whatever is still behind (goodall.transcripts).
TRANSCRIPT_COPY_EVENTS = ["PostToolUse", "Stop", "SessionEnd"]

# The most hook input goodall reads, in characters. Hook input carries the
# whole prompt and, after a tool call, the tool's whole output, so the limit
# is generous; input longer than this is treated as unreadable.
HOOK_INPUT_LIMIT = 64 * 1024 * 1024

# What the person sees inside Claude Code when the gate refuses a prompt.
# One or two sentences each, naming the command to run; no setting's value.
NO_WORK_BLOCK_REFUSAL = (
    "Goodall is not recording, so this prompt was not sent. "
    "Open a terminal, run goodall start, then send your prompt again."
)
LOCAL_SETTINGS_MISSING_REFUSAL = (
    "Goodall is not set up in this macOS user account, so this prompt was not sent. "
    "Run goodall onboard in a terminal to set it up, then send your prompt again."
)
LOCAL_SETTINGS_UNREADABLE_REFUSAL = (
    "Goodall cannot read its local settings, so this prompt was not sent. "
    "Run goodall doctor in a terminal to see what to fix, then send your prompt again."
)
LOG_DIRECTORY_UNREACHABLE_REFUSAL = (
    "Goodall cannot reach its log directory, so nothing can be recorded and this prompt was not sent. "
    "If the log directory is on an external drive, connect it; then run goodall status in a terminal "
    "and send your prompt again."
)
HOOK_INPUT_UNREADABLE_REFUSAL = (
    "Goodall could not read the details Claude Code sent with this prompt, so it was not sent. "
    "Tell your eval's researcher, who may need to update goodall."
)
UNEXPECTED_PROBLEM_REFUSAL = (
    "Goodall hit an unexpected problem while checking this prompt, so it was not sent. "
    "Run goodall status in a terminal to see what is wrong, and if it shows nothing, tell your eval's researcher."
)

# Shown when Claude Code's settings run `goodall hook` without saying which
# event fired. Exit 1, not 2: Claude Code treats 1 as a hook error it shows
# but does not act on, while 2 would refuse prompts or keep Claude from
# stopping, and the event is exactly what is not known.
MISSING_EVENT_MESSAGE = (
    "goodall hook needs the name of the Claude Code hook event, for example goodall hook UserPromptSubmit. "
    "Correct the hook command in Claude Code's settings.json; docs/claude-code-hooks.md in goodall shows them."
)

# The type of a clock: asked once per hook for the time now.
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class HookInput:
    """What Claude Code said about the event, read from standard input.

    `claude_session` is the Claude session id, always a valid one.
    `transcript_path` is where Claude Code keeps that session's transcript,
    or None when it did not say; the file may not exist yet, since Claude
    Code creates it only after the first prompt. `fields` keeps the whole
    JSON object for anything else a caller needs.
    """

    claude_session: str
    transcript_path: Path | None
    fields: dict[str, object]


def read_hook_input(text: str | None) -> HookInput | None:
    """Read the JSON Claude Code sent, or None when it is not usable.

    Usable means a JSON object whose `session_id` is a Claude session id (a
    lowercase UUID). Never raises.
    """
    if text is None:
        return None
    try:
        fields = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(fields, dict):
        return None
    claude_session = fields.get("session_id")
    if not isinstance(claude_session, str) or not records.CLAUDE_SESSION_ID_PATTERN.fullmatch(claude_session):
        return None
    transcript_path = None
    transcript_text = fields.get("transcript_path")
    if isinstance(transcript_text, str) and transcript_text != "":
        transcript_path = Path(transcript_text)
    return HookInput(claude_session=claude_session, transcript_path=transcript_path, fields=fields)


def run_hook_command(arguments: list[str]) -> int:
    """Run `goodall hook` from the command line: `arguments` are the words after `hook`.

    Called by goodall.entry_point. Exactly one argument, the event's name, is
    expected; anything else is a mistake in Claude Code's settings.
    """
    terminal = Terminal()
    if len(arguments) != 1:
        terminal.show_hook_message(MISSING_EVENT_MESSAGE)
        return exit_codes.ERROR
    return run_hook(arguments[0], terminal, real_clock)


def run_hook(event_name: str | None, terminal: Terminal, clock: Clock) -> int:
    """Handle one hook event and return the exit code Claude Code will act on."""
    if event_name is None:
        terminal.show_hook_message(MISSING_EVENT_MESSAGE)
        return exit_codes.ERROR

    if event_name == PROMPT_GATE_EVENT:
        try:
            refusal = check_prompt(terminal.read_piped_text(HOOK_INPUT_LIMIT), clock())
        except Exception:
            # Trap: this catches goodall's own bugs too. That is deliberate:
            # a bug must close the gate, not open it.
            refusal = UNEXPECTED_PROBLEM_REFUSAL
        if refusal is None:
            return exit_codes.SUCCESS
        terminal.show_hook_message(refusal)
        return exit_codes.CLAUDE_CODE_REFUSES_PROMPT

    try:
        record_activity(event_name, terminal.read_piped_text(HOOK_INPUT_LIMIT), clock())
    except Exception:
        # Deliberately silent: see "Every other event fails open" above.
        pass
    return exit_codes.SUCCESS


def check_prompt(hook_input_text: str | None, now: datetime) -> str | None:
    """The prompt gate: leave a heartbeat and return None, or return why the prompt is refused.

    Checked in the order a person would fix things: goodall set up at all,
    then the log directory, then what Claude Code sent, then the work block.
    """
    settings = read_local_settings()
    if not isinstance(settings, LocalSettings):
        for failure in settings:
            if failure.kind == "local_settings_missing":
                return LOCAL_SETTINGS_MISSING_REFUSAL
        return LOCAL_SETTINGS_UNREADABLE_REFUSAL

    log_directory = settings.log_directory.path
    if describe_folder_problem(log_directory, needs_writing=True) is not None:
        return LOG_DIRECTORY_UNREACHABLE_REFUSAL

    hook_input = read_hook_input(hook_input_text)
    if hook_input is None:
        return HOOK_INPUT_UNREADABLE_REFUSAL

    # record_heartbeat answers "is a work block active?" and records the
    # heartbeat in one step. None covers no work block, an overdue one, and a
    # heartbeat that could not be written: in each, this prompt would go
    # unrecorded, which ADR 0008 treats as the same observable state.
    if work_blocks.record_heartbeat(log_directory, now, hook_input.claude_session) is None:
        return NO_WORK_BLOCK_REFUSAL
    return None


def record_activity(event_name: str, hook_input_text: str | None, now: datetime) -> None:
    """Leave a heartbeat if a work block is active, then copy the Claude session's transcript after some events.

    Unreadable input still leaves a heartbeat, without a Claude session: the
    moment is evidence even when the session is unknown.

    The transcript is copied after PostToolUse, Stop and SessionEnd whether
    or not a work block is active, because capture is unconditional: an
    eval account is used for nothing but the eval. A heartbeat that fails
    must not stop the copy, so it is tried on its own.
    """
    settings = read_local_settings()
    if not isinstance(settings, LocalSettings):
        return
    log_directory = settings.log_directory.path
    hook_input = read_hook_input(hook_input_text)
    claude_session = None
    if hook_input is not None:
        claude_session = hook_input.claude_session
    try:
        work_blocks.record_heartbeat(log_directory, now, claude_session)
    except Exception:
        # Silent, as every event but the prompt gate is; see the module docstring.
        pass
    if event_name in TRANSCRIPT_COPY_EVENTS and hook_input is not None and hook_input.transcript_path is not None:
        transcripts.copy_from_hook(
            transcripts.claude_projects_folder(),
            log_directory,
            hook_input.claude_session,
            hook_input.transcript_path,
            now,
        )


def real_clock() -> datetime:
    """The time now, in UTC: the clock the hook uses outside tests."""
    return datetime.now(UTC)
