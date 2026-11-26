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
  While recording is paused (`goodall pause`) no work block is active, so
  the prompt is refused the same way, with a message that says so (in
  dedicated account mode; in mixed account mode a pause refuses nothing).
  When a work block is active but what Claude Code sent cannot be read, the
  prompt still goes through (see below).
- Every other event leaves a heartbeat when a work block is active, and
  always exits 0 with nothing printed. These events must never disrupt
  Claude Code: exit 2 from a Stop hook, for one, would stop Claude from
  finishing its reply. They also remember, in the log directory, which
  Claude config folder this Claude Code uses (goodall.claude_family), so
  goodall finds its session files when a work block ends. Hooks copy
  nothing: agent sessions are copied when a work block ends, the same way
  for every source application (goodall.agent_session_copies, ADR 0019).

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
  JSON cannot be read, so it can still refuse when no work block is active.
  When the two disagree, the command line wins.
- With no event name on the command line (owner decision 2026-11-17, Q56),
  the event is taken from `hook_event_name` in the input, and the hook then
  behaves exactly as if it had been named, so a UserPromptSubmit hook
  written as bare `goodall hook` is still a working prompt gate. Exit 1 only
  when neither names an event goodall handles: 1 is shown by Claude Code
  but stops nothing, while 2 would refuse a prompt or keep Claude from
  stopping when the event is exactly what is unknown. Nothing is printed
  (stderr would appear in Claude Code's transcript), and the occurrence is
  noted in the log directory, by category only (goodall.hook_problems), so
  `goodall status` warns that the hook settings need checking. Only when a
  person runs `goodall hook` at a terminal is the fix printed.
- An event goodall does not handle, named on the command line (a hook
  installed on PreToolUse, say), is treated as every non-gate event is:
  a heartbeat, exit 0, nothing printed. It is noted the same way, since it
  means the settings differ from docs/claude-code-hooks.md.
- In mixed account mode the gate is off (ADR 0021): the account is also
  used for things that are not the eval, so refusing a prompt is the worse
  failure there. A prompt with no work block active goes through with
  nothing printed and nothing written, and so does one when the log
  directory cannot be reached or goodall hits an unexpected error. The
  other events write nothing outside a work block either. One refusal
  stays: missing or unreadable local settings, because without them goodall
  cannot know the mode, and dedicated account mode is the safe assumption.
  Everything below about refusing describes dedicated account mode.
- The gate fails closed. Missing or unreadable local settings, an
  unreachable log directory (ADR 0008), no active work block, and any
  unexpected error all refuse the prompt, because in an eval account
  unrecorded work is the worse failure. Each has its own next step.
- Input goodall cannot read (empty, not JSON, or without a valid Claude
  session id) does NOT refuse the prompt while a work block is active: the
  owner decided (2026-11-17) to record with an empty value that can be
  reconciled later and to warn, so a Claude Code that changes its input
  shape cannot block every prompt until goodall is updated. The heartbeat
  names no Claude session, a line in the log directory notes the moment,
  the event, the work block and what was unreadable, as a fixed word and
  never the input itself (goodall.hook_problems, which also says how the
  Claude session can be tied back to the work block), and the next
  `goodall status` warns. With no work block active the prompt is refused
  exactly as it would be with readable input: unreadable input never opens
  a gate that would otherwise be closed.
- The warning at the moment it happens is the JSON field `systemMessage`
  on standard output, which Claude Code's hooks documentation describes as
  a warning shown to the person; only `additionalContext` and plain-text
  standard output are added to Claude's context, so the hook prints that
  one JSON object and nothing else. Checked against the documentation on
  2026-11-17, not yet by hand in Claude Code.
- The prompt gate also checks the work block's telemetry receiver, because
  Claude Code drops telemetry silently when nothing listens (ADR 0002). It
  tries the lock the receiver holds on its receipt log (no program, no
  network call; goodall.telemetry_capture). A receiver found down never
  refuses the prompt: it is noted in the log directory, so `goodall status`
  warns and the work block record says its telemetry is incomplete, and the
  first time only the person is told through `systemMessage`.
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

from goodall import claude_family, exit_codes, hook_problems, records, telemetry_capture, work_blocks
from goodall.folders import describe_folder_problem
from goodall.local_settings import MIXED_ACCOUNT_MODE, LocalSettings, read_local_settings
from goodall.terminal import Terminal

# The one event that can refuse what the person asked for.
PROMPT_GATE_EVENT = "UserPromptSubmit"

# Every event goodall's hooks are installed on (docs/claude-code-hooks.md).
KNOWN_EVENTS = [PROMPT_GATE_EVENT, "PostToolUse", "Stop", "SessionEnd", "SessionStart"]

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
# The same refusal while recording is paused (`goodall pause`, ADR 0021):
# no work block is active then either, and the person is told why.
PAUSED_REFUSAL = (
    "Goodall is paused, so it is not recording and this prompt was not sent. "
    "Open a terminal, run goodall start to record again, then send your prompt again."
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
# Shown inside Claude Code, through the hook's JSON `systemMessage`, when a
# prompt went through although goodall could not read what came with it.
# One plain sentence; it reaches the person, not Claude.
UNREADABLE_INPUT_NOTICE = (
    "Goodall could not read the details Claude Code sent with this prompt, so it sent the prompt and kept your "
    "work block recording without linking this Claude session to it; please tell your eval's researcher, who may "
    "need to update goodall."
)
# Shown inside Claude Code the first time the prompt gate finds the work
# block's telemetry receiver not running. Once only: status and stop say it
# again, and a notice on every prompt would teach people to ignore notices.
RECEIVER_DOWN_NOTICE = (
    "Goodall's telemetry receiver has stopped, so Claude Code's telemetry is not being kept; your prompt was sent "
    "and your transcripts are still recorded. Run goodall start in a terminal to start the receiver again."
)
UNEXPECTED_PROBLEM_REFUSAL = (
    "Goodall hit an unexpected problem while checking this prompt, so it was not sent. "
    "Run goodall status in a terminal to see what is wrong, and if it shows nothing, tell your eval's researcher."
)

# Shown only to a person who runs `goodall hook` at a terminal with no event
# name. Inside Claude Code nothing is printed; the problem is noted instead.
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


@dataclass(frozen=True)
class GateDecision:
    """What the prompt gate decided: refuse with `refusal`, or let the prompt through, with `notice` if any.

    `notice` is what the person is shown inside Claude Code when the prompt
    goes through but something about it was not recorded fully.
    """

    refusal: str | None = None
    notice: str | None = None


def unreadable_input_category(text: str | None) -> str | None:
    """Which of hook_problems.INPUT_PROBLEMS makes `text` unreadable, or None when it can be read.

    The answer is one fixed word, so what is noted holds nothing of the
    input, which carries the person's prompt.
    """
    if text is None or text.strip() == "":
        return "empty"
    try:
        fields = json.loads(text)
    except json.JSONDecodeError:
        return "not_json"
    if not isinstance(fields, dict):
        return "not_json"
    if "session_id" not in fields:
        return "no_session_id"
    claude_session = fields["session_id"]
    if not isinstance(claude_session, str) or not records.CLAUDE_SESSION_ID_PATTERN.fullmatch(claude_session):
        return "session_id_not_uuid"
    return None


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

    Called by goodall.entry_point. One argument, the event's name, is
    expected. With none, or more than one (a mistake in Claude Code's
    settings), the event is taken from what Claude Code sends instead.
    """
    terminal = Terminal()
    if len(arguments) != 1:
        return run_hook(None, terminal, real_clock)
    return run_hook(arguments[0], terminal, real_clock)


def run_hook(event_name: str | None, terminal: Terminal, clock: Clock) -> int:
    """Handle one hook event and return the exit code Claude Code will act on.

    `event_name` is the event named on the command line, or None when it
    named none.
    """
    if event_name is None:
        return run_hook_without_event_name(terminal, clock)
    return handle_event(event_name, lambda: terminal.read_piped_text(HOOK_INPUT_LIMIT), terminal, clock)


def run_hook_without_event_name(terminal: Terminal, clock: Clock) -> int:
    """Take the event from `hook_event_name` in what Claude Code sent; exit 1, noted, when it names no known one."""
    try:
        hook_input_text = terminal.read_piped_text(HOOK_INPUT_LIMIT)
    except Exception:
        hook_input_text = None
    event_name, problem = event_named_in_input(hook_input_text)
    if event_name is not None:
        return handle_event(event_name, lambda: hook_input_text, terminal, clock)
    if terminal.standard_input is not None and terminal.standard_input.isatty():
        # A person at a terminal, not Claude Code, which always sends input.
        terminal.show_hook_message(MISSING_EVENT_MESSAGE)
        return exit_codes.ERROR
    try:
        note_hook_problem(clock(), None, problem)
    except Exception:
        # Silent: whatever goes wrong here must not reach Claude Code.
        pass
    return exit_codes.ERROR


def event_named_in_input(hook_input_text: str | None) -> tuple[str | None, str]:
    """The known event `hook_event_name` names, or None with the hook_problems word for why not."""
    try:
        fields = json.loads(hook_input_text or "")
    except json.JSONDecodeError:
        return None, "no_event_name"
    if not isinstance(fields, dict) or not isinstance(fields.get("hook_event_name"), str):
        return None, "no_event_name"
    if fields["hook_event_name"] not in KNOWN_EVENTS:
        return None, "event_not_known"
    return fields["hook_event_name"], ""


def note_hook_problem(now: datetime, event_name: str | None, problem: str) -> None:
    """Note `problem` in the log directory, with the active work block if any; silently nothing without one."""
    settings = read_local_settings()
    if not isinstance(settings, LocalSettings):
        return
    log_directory = settings.log_directory.path
    if describe_folder_problem(log_directory, needs_writing=True) is not None:
        return
    work_block_id = None
    active = work_blocks.active_work_block(log_directory, now)
    if active is not None:
        work_block_id = active.id
    elif settings.account_mode == MIXED_ACCOUNT_MODE:
        # Outside a work block this account is not the eval's business, and
        # the note would hold when the person used Claude Code (ADR 0021).
        return
    hook_problems.record_hook_problem(log_directory, now, event_name, work_block_id, problem)


def handle_event(event_name: str, read_input: Callable[[], str | None], terminal: Terminal, clock: Clock) -> int:
    """Handle one event whose name is known, reading Claude Code's input with `read_input`."""
    if event_name == PROMPT_GATE_EVENT:
        try:
            decision = check_prompt(read_input(), clock())
        except Exception:
            # Trap: this catches goodall's own bugs too. That is deliberate:
            # a bug must close the gate, not open it.
            decision = GateDecision(refusal=UNEXPECTED_PROBLEM_REFUSAL)
            if prompt_gate_is_off():
                decision = GateDecision()
        if decision.refusal is not None:
            terminal.show_hook_message(decision.refusal)
            return exit_codes.CLAUDE_CODE_REFUSES_PROMPT
        if decision.notice is not None:
            terminal.show_hook_notice(decision.notice)
        return exit_codes.SUCCESS

    try:
        record_activity(event_name, read_input(), clock())
    except Exception:
        # Deliberately silent: see "Every other event fails open" above.
        pass
    return exit_codes.SUCCESS


def prompt_gate_is_off() -> bool:
    """True when local settings can be read and say mixed account mode, where no prompt is ever refused.

    Asked only after an unexpected error, so it reads local settings again
    and trusts nothing the failed check worked out. Not knowing means the
    gate stays on.
    """
    try:
        settings = read_local_settings()
    except Exception:
        return False
    return isinstance(settings, LocalSettings) and settings.account_mode == MIXED_ACCOUNT_MODE


def check_prompt(hook_input_text: str | None, now: datetime) -> GateDecision:
    """The prompt gate: leave a heartbeat and let the prompt through, or say why it is refused.

    Checked in the order a person would fix things: goodall set up at all,
    then the log directory, then the work block. What Claude Code sent is
    read last, and only decides which Claude session the heartbeat names,
    so it can never open a gate the checks before it closed.

    In mixed account mode the last two never refuse: the prompt goes
    through silently, recorded only when a work block is active (ADR 0021).
    """
    settings = read_local_settings()
    if not isinstance(settings, LocalSettings):
        for failure in settings:
            if failure.kind == "local_settings_missing":
                return GateDecision(refusal=LOCAL_SETTINGS_MISSING_REFUSAL)
        return GateDecision(refusal=LOCAL_SETTINGS_UNREADABLE_REFUSAL)

    gate_is_off = settings.account_mode == MIXED_ACCOUNT_MODE
    log_directory = settings.log_directory.path
    if describe_folder_problem(log_directory, needs_writing=True) is not None:
        if gate_is_off:
            return GateDecision()
        return GateDecision(refusal=LOG_DIRECTORY_UNREACHABLE_REFUSAL)

    hook_input = read_hook_input(hook_input_text)
    claude_session = None
    if hook_input is not None:
        claude_session = hook_input.claude_session

    # record_heartbeat answers "is a work block active?" and records the
    # heartbeat in one step. None covers no work block, an overdue one, and a
    # heartbeat that could not be written: in each, this prompt would go
    # unrecorded, which ADR 0008 treats as the same observable state.
    work_block = work_blocks.record_heartbeat(log_directory, now, claude_session)
    if work_block is None:
        if gate_is_off:
            # Mixed account mode: paused or not, nothing is refused and
            # nothing is written, and the pause is not even read.
            return GateDecision()
        # Asked only now, so a prompt that goes through reads no extra file.
        if work_blocks.read_pause(log_directory) is not None:
            return GateDecision(refusal=PAUSED_REFUSAL)
        return GateDecision(refusal=NO_WORK_BLOCK_REFUSAL)
    notices = []
    if hook_input is None:
        problem = unreadable_input_category(hook_input_text)
        if problem is None:
            # Readable JSON that read_hook_input still refused cannot happen
            # today; noted under the broadest word rather than not at all.
            problem = "not_json"
        hook_problems.record_hook_problem(log_directory, now, PROMPT_GATE_EVENT, work_block.id, problem)
        notices.append(UNREADABLE_INPUT_NOTICE)
    if receiver_newly_down(log_directory, work_block.id, now):
        notices.append(RECEIVER_DOWN_NOTICE)
    if notices:
        return GateDecision(notice=" ".join(notices))
    return GateDecision()


def receiver_newly_down(log_directory: Path, work_block_id: str, now: datetime) -> bool:
    """Note the work block's telemetry receiver down if it is; True only the first time.

    Cheap enough for every prompt: one file opened and a lock tried, no
    program and no network call (goodall.telemetry_capture). Any error is
    swallowed, because telemetry must never decide whether a prompt goes
    through: the gate is about recording the work, and transcripts still are.
    """
    try:
        return telemetry_capture.notice_receiver_down(log_directory, work_block_id, now)
    except Exception:
        return False


def record_activity(event_name: str, hook_input_text: str | None, now: datetime) -> None:
    """Leave a heartbeat if a work block is active, and remember which Claude config folder this Claude Code uses.

    Unreadable input still leaves a heartbeat, without a Claude session: the
    moment is evidence even when the session is unknown.

    The config folder is remembered because a terminal running `goodall
    stop` may not have this Claude Code's CLAUDE_CONFIG_DIR, and goodall
    must still find the session files to copy (goodall.claude_family). It
    comes from this process's own environment, never from the hook's
    input. A heartbeat that fails must not stop that, so each is tried on
    its own. In mixed account mode none of this happens outside a work
    block; the folder is remembered by the first event inside one.
    """
    settings = read_local_settings()
    if not isinstance(settings, LocalSettings):
        return
    log_directory = settings.log_directory.path
    hook_input = read_hook_input(hook_input_text)
    claude_session = None
    if hook_input is not None:
        claude_session = hook_input.claude_session
    active = None
    try:
        active = work_blocks.record_heartbeat(log_directory, now, claude_session)
    except Exception:
        # Silent, as every event but the prompt gate is; see the module docstring.
        pass
    if active is None and settings.account_mode == MIXED_ACCOUNT_MODE:
        # Outside a work block, mixed account mode writes nothing at all (ADR 0021).
        return
    if event_name not in KNOWN_EVENTS:
        work_block_id = None
        if active is not None:
            work_block_id = active.id
        hook_problems.record_hook_problem(log_directory, now, None, work_block_id, "event_not_known")
    try:
        config_folder = claude_family.claude_config_folder().resolve(strict=True)
        claude_family.remember_claude_config_folder(log_directory, config_folder)
    except Exception:
        # Silent too. Goodall still looks in `~/.claude` and in the folder
        # the terminal's own environment names.
        pass


def real_clock() -> datetime:
    """The time now, in UTC: the clock the hook uses outside tests."""
    return datetime.now(UTC)
