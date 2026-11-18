"""The runner: the only code in goodall that starts other programs.

Every program goodall runs (git, gh, the secrets manager's command, `id`)
goes through a `Runner` (ADR 0011, AGENTS.md invariant 9). A caller
describes what to run as a `Command` and reads back a `CommandResult`. The
runner also says whether a program is installed (`find_program`) without
running it, so a test decides that too.

Rules this module keeps, and why:

- A missing program, a timeout and a non-zero exit are results, never
  exceptions. Each is an ordinary state of someone's machine, and the
  caller (usually a diagnostic check) has to turn it into a message with a
  next step. `CommandResult.problem` is a ready-made one-line description
  of what went wrong, such as "`git` was not found", for that message.
- Arguments are always a list, and no shell is ever used, so nothing in an
  argument (a folder name with a space or a `;` in it) can be read as
  another command.
- Every command says what happens to its output (`OutputMode`): captured,
  or discarded for anything that might print a secret, so a secret value
  never reaches goodall's memory, a message or a test.
- Short commands time out after `DEFAULT_TIMEOUT_SECONDS` unless the
  caller asks for another limit, so a hung program can never hang goodall.
  A program that times out is killed together with everything it started.
- A program never reads from goodall's standard input: goodall never asks a
  question in the terminal (ADR 0012), and a program waiting for an answer
  nobody can see would only hang until its timeout.
- Secrets reach programs only through the secrets manager (ADR 0010), so a
  `Command` refuses extra environment variables whose names say they hold a
  secret. Nothing can check that an argument is not a secret; never put one
  there, because arguments are visible to every program on the machine.

There are two adapters at the one seam: `RealRunner` here, and
`goodall.fake_runner.FakeRunner` for tests. `cli.main` takes a runner and
passes it down, so a test can drive any command without starting a real
program it did not allow.

Background programs (ADR 0011; the telemetry receiver is the first): a
program that outlives the goodall command that started it. `start_background`
starts one, waits for the first line it prints (its "ready line", such as
the receiver's port), and returns a `BackgroundProgram`, a record a caller
saves to a file. A later goodall command reads that record back and passes
it to `check_background` or `stop_background`. Decisions a reader would
otherwise reverse-engineer:

- Telling the recorded process from a stranger. Process ids are reused: once
  a program has ended, the system may give its id to anything else, and a
  later `goodall stop` must never kill that. So the record holds the
  process's start time as the system reports it (`ps -o lstart`, asked with
  TZ=UTC and LC_ALL=C so the text is the same whoever asks), and a process
  counts as the recorded one only when both its id and its start time
  match. A different start time means REPLACED, and nothing is sent to it.
  The start time has one-second resolution; a new process taking the same
  id within the same second the original started is not a real risk.
- The program starts in a session of its own, so closing the terminal that
  ran `goodall start` does not end it, and stopping it signals its whole
  process group. Its standard input is empty, and its output goes to
  temporary files that are unlinked at once, so it can print whatever it
  likes without filling a pipe nobody reads; goodall reads only the first
  line, and keeps nothing.
- It is started with `os.posix_spawnp` rather than subprocess.Popen, because
  a Popen object for a program meant to outlive goodall would warn that it
  was still running when goodall drops it. The trap: while goodall itself
  is still running (in tests, chiefly), an ended background program is its
  child and lingers as a zombie until reaped, so every check reaps first.
- Handing the terminal over to a program (a new `OutputMode`, with no
  timeout, since it is a person's working session) is planned but not built,
  because nothing calls it yet.

This module imports only the standard library, because the hook path will
use it (ADR 0005); tests/test_hook_path_imports.py proves it.
"""

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

# How long a short command may run before goodall gives up on it. For a
# background program, how long goodall waits for its ready line, and how
# long it waits for it to end once asked to stop before killing it.
DEFAULT_TIMEOUT_SECONDS = 10.0

# How often goodall looks again while it waits for a background program.
BACKGROUND_POLL_SECONDS = 0.02

# What `ps` is asked for, and in which environment, to tell one process from
# another that was later given the same id (see the module docstring).
PROCESS_START_TIME_ENVIRONMENT = {"TZ": "UTC", "LC_ALL": "C"}

# An environment variable whose name ends in one of these is taken to hold a
# secret (ADR 0010).
SECRET_NAME_ENDINGS = ["_KEY", "_TOKEN", "_SECRET"]


def looks_like_a_secret_name(name: str) -> bool:
    """True when an environment variable's name ends in _KEY, _TOKEN or _SECRET, in any case."""
    for ending in SECRET_NAME_ENDINGS:
        if name.upper().endswith(ending):
            return True
    return False


@dataclass(frozen=True)
class RequiredProgram:
    """A program goodall needs, and what to tell a contributor when it is missing.

    - `name` is the command, as found on the PATH.
    - `described` names it for a person, at the start of a sentence, such
      as "gh, GitHub's command-line tool,".
    - `why` says what goodall uses it for.
    - `how_to_install` says where to get it, as the end of a sentence.

    It lives here, beside `Runner.find_program`, because both the program
    checks (goodall.program_checks) and the secrets manager module
    (goodall.secrets_manager) describe programs with it, and the secrets
    manager module must not import the checks.
    """

    name: str
    described: str
    why: str
    how_to_install: str


class OutputMode(StrEnum):
    """What happens to what a program prints."""

    # Kept, so the caller can read it.
    CAPTURE = "capture"
    # Thrown away unread, both standard output and standard error, for any
    # command that might print a secret. Standard error goes too, because
    # a program can echo what it was given in an error message. The caller
    # still gets the outcome and the exit code.
    DISCARD = "discard"


class CommandOutcome(StrEnum):
    """How running a command ended.

    COMPLETED means the program ran and exited, whatever its exit code;
    `CommandResult.succeeded` says whether that exit was a success.
    """

    COMPLETED = "completed"
    # No program by that name could be found on the PATH, or at that path.
    NOT_FOUND = "not_found"
    # The program is there but the system would not start it, for example
    # because the file is not marked as a program that may be run.
    COULD_NOT_START = "could_not_start"
    # It ran for longer than its timeout, and was killed with everything it started.
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class Command:
    """One program to run, and how to run it.

    - `arguments` is the program's name (or full path) followed by its
      arguments, one list item each.
    - `output` says what happens to what the program prints.
    - `timeout_seconds` is how long it may run before it is killed.
    - `extra_environment` holds environment variables the program gets on
      top of goodall's own environment.
    - `removed_environment_prefixes`: any variable in goodall's own
      environment whose name starts with one of these is left out of the
      program's, so a stray setting (git's `GIT_DIR`, say) cannot steer it.
      `extra_environment` is applied afterwards, so it can still set one.
    - `working_directory` is the folder it runs in; None means the folder
      goodall was run from.
    """

    arguments: list[str]
    output: OutputMode = OutputMode.CAPTURE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    extra_environment: dict[str, str] = field(default_factory=dict)
    working_directory: Path | None = None
    removed_environment_prefixes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Refuse an extra environment variable whose name says it holds a secret.

        Raises ValueError, because passing one is a mistake in goodall's own
        code, not something about the machine: secret values reach programs
        only through the secrets manager (ADR 0010). Checked here, when the
        command is made, so the real and the fake runner both refuse it.
        """
        for name in self.extra_environment:
            if looks_like_a_secret_name(name):
                raise ValueError(
                    f"The environment variable {name} looks like it holds a secret. Secrets reach programs only "
                    "through the secrets manager (ADR 0010), never as an extra environment variable."
                )

    @property
    def as_typed(self) -> str:
        """The command as a person would type it in a terminal, for messages."""
        return shlex.join(self.arguments)


@dataclass(frozen=True)
class CommandResult:
    """What happened when a command ran.

    - `outcome` says how it ended.
    - `exit_code` is the program's exit code when it completed, else None.
    - `standard_output` and `standard_error` are what it printed; both are
      empty when its output was discarded or it never completed.
    - `start_error` is the system's reason, when the outcome is
      COULD_NOT_START.
    """

    command: Command
    outcome: CommandOutcome
    exit_code: int | None = None
    standard_output: str = ""
    standard_error: str = ""
    start_error: str | None = None

    @property
    def succeeded(self) -> bool:
        """True when the program ran and exited with code 0."""
        return self.outcome == CommandOutcome.COMPLETED and self.exit_code == 0

    @property
    def problem(self) -> str | None:
        """A one-line description of what went wrong, or None when the command succeeded.

        It names the command in backticks and, when the program printed an
        error, quotes the first line of it, which is usually the line that
        says what is wrong.
        """
        if self.succeeded:
            return None
        if self.outcome == CommandOutcome.NOT_FOUND:
            return f"`{self.command.arguments[0]}` was not found"
        if self.outcome == CommandOutcome.COULD_NOT_START:
            return f"`{self.command.arguments[0]}` could not be started: {self.start_error}"
        described_command = f"`{self.command.as_typed}`"
        if self.outcome == CommandOutcome.TIMED_OUT:
            # :g writes 10.0 as "10" and 0.5 as "0.5".
            return f"{described_command} timed out after {self.command.timeout_seconds:g} s"
        description = f"{described_command} exited with code {self.exit_code}"
        first_error_line = first_non_blank_line(self.standard_error)
        if first_error_line is not None:
            description += f": {first_error_line}"
        return description


def first_non_blank_line(text: str) -> str | None:
    """The first line of `text` that is not blank, without surrounding spaces, or None."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


@dataclass(frozen=True)
class BackgroundProgram:
    """A program running in the background, as a later goodall command needs to find it again.

    - `process_id` is the system's id for the process.
    - `process_started_at` is when the system says that process started,
      which tells it apart from a later process given the same id.
    - `arguments` is what was run, for messages.

    `as_json` and `from_json` turn it into, and back from, what a caller
    saves in a file.
    """

    process_id: int
    process_started_at: str
    arguments: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "process_started_at": self.process_started_at,
            "arguments": list(self.arguments),
        }

    @staticmethod
    def from_json(values: object) -> "BackgroundProgram | None":
        """Read a saved record back; None when it is not one, so a damaged file never stops anything."""
        if not isinstance(values, dict):
            return None
        process_id = values.get("process_id")
        process_started_at = values.get("process_started_at")
        arguments = values.get("arguments")
        # bool is a kind of int in Python, so `true` would otherwise pass.
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            return None
        if not isinstance(process_started_at, str) or process_started_at == "":
            return None
        if not isinstance(arguments, list) or arguments == []:
            return None
        for argument in arguments:
            if not isinstance(argument, str):
                return None
        return BackgroundProgram(process_id, process_started_at, tuple(arguments))


class BackgroundOutcome(StrEnum):
    """How starting a background program ended."""

    # It printed its ready line and is running.
    STARTED = "started"
    # It exited before printing its ready line.
    EXITED = "exited"
    NOT_FOUND = "not_found"
    COULD_NOT_START = "could_not_start"
    # It printed no ready line before its command's timeout, and was killed.
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class BackgroundStart:
    """What happened when goodall started a background program.

    `program` is set only when the outcome is STARTED; `ready_line` is the
    first line it printed, without its line ending. When it exited first,
    `exit_code` and `standard_error` say how.
    """

    command: Command
    outcome: BackgroundOutcome
    program: BackgroundProgram | None = None
    ready_line: str = ""
    exit_code: int | None = None
    standard_error: str = ""
    start_error: str | None = None

    @property
    def problem(self) -> str | None:
        """A one-line description of what went wrong, as CommandResult.problem gives it, or None once started."""
        if self.outcome == BackgroundOutcome.STARTED:
            return None
        if self.outcome == BackgroundOutcome.NOT_FOUND:
            return f"`{self.command.arguments[0]}` was not found"
        if self.outcome == BackgroundOutcome.COULD_NOT_START:
            return f"`{self.command.arguments[0]}` could not be started: {self.start_error}"
        described_command = f"`{self.command.as_typed}`"
        if self.outcome == BackgroundOutcome.TIMED_OUT:
            return f"{described_command} did not say it was ready within {self.command.timeout_seconds:g} s"
        description = f"{described_command} exited with code {self.exit_code}"
        first_error_line = first_non_blank_line(self.standard_error)
        if first_error_line is not None:
            description += f": {first_error_line}"
        return description


class BackgroundState(StrEnum):
    """Whether a recorded background program is still running."""

    RUNNING = "running"
    # Nothing has that process id any more: the program has ended.
    ENDED = "ended"
    # A different process now has that id, so the recorded program has ended.
    REPLACED = "replaced"


class BackgroundStop(StrEnum):
    """What came of asking a recorded background program to stop."""

    # It ended when asked (SIGTERM).
    STOPPED = "stopped"
    # It had not ended within the timeout, so it was killed (SIGKILL).
    KILLED = "killed"
    # It had already ended; nothing was sent.
    ALREADY_ENDED = "already_ended"
    # The process with that id is not the recorded program; nothing was sent.
    NOT_OURS = "not_ours"


def environment_for(command: Command) -> dict[str, str]:
    """The environment `command`'s program runs with: goodall's own, plus its extra variables.

    Nothing is taken out unless the command asks for it by naming prefixes
    in `removed_environment_prefixes` (record repo operations remove git's
    own variables that way). That matters for 1Password: on a Mac that
    runs unattended, op signs in with a service account token it reads
    from OP_SERVICE_ACCOUNT_TOKEN, so that variable must reach op
    unchanged. Goodall never reads the value; it only passes it on, as it
    passes on PATH.

    Built only as the program starts, and never stored in a `Command` or a
    `CommandResult`, so no message or exception made from those can hold a
    secret from the environment. A function of its own so a test can see
    what the real runner would give a command that the fake runner ran.
    """
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith(tuple(command.removed_environment_prefixes)):
            environment[name] = value
    environment.update(command.extra_environment)
    return environment


class Runner(Protocol):
    """The one seam for starting other programs. `RealRunner` and `FakeRunner` both fit it."""

    def run(self, command: Command) -> CommandResult:
        """Run `command` and say what happened. Never raises for anything the program did."""
        ...

    def find_program(self, name: str) -> Path | None:
        """Where the program `name` is on the PATH, or None when it is not installed.

        This only looks; it never runs the program, so it gives the same
        answer for a program that is installed but broken.
        """
        ...

    def start_background(self, command: Command) -> BackgroundStart:
        """Start `command` to outlive this goodall command, and return once it has printed its first line.

        `command.timeout_seconds` is how long to wait for that line. Never
        raises for anything the program did.
        """
        ...

    def check_background(self, program: BackgroundProgram) -> BackgroundState:
        """Whether the recorded program is still running. Only looks."""
        ...

    def stop_background(
        self, program: BackgroundProgram, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> BackgroundStop:
        """Ask the recorded program to stop, kill it after `timeout_seconds`, and never touch a stranger."""
        ...


@dataclass
class RealRunner:
    """The runner that starts real programs on this machine."""

    def run(self, command: Command) -> CommandResult:
        """Run `command`, wait for it to finish or time out, and say what happened.

        The program starts in a new session, which puts it and everything it
        starts in a process group of their own. On a timeout the whole group
        is killed. Trap: killing only the program is not enough, because a
        program it started can keep the output pipes open, and goodall would
        wait on them for as long as that program runs.
        """
        # DEVNULL for discarded output: the bytes go straight from the
        # program to nowhere, never through goodall's memory.
        output_destination = subprocess.PIPE
        if command.output == OutputMode.DISCARD:
            output_destination = subprocess.DEVNULL
        environment = environment_for(command)
        # Checked first, because Popen reports a missing working folder as
        # FileNotFoundError, which would read as "the program was not found".
        if command.working_directory is not None and not command.working_directory.is_dir():
            return CommandResult(
                command=command,
                outcome=CommandOutcome.COULD_NOT_START,
                start_error=f"the folder it runs in, {command.working_directory}, does not exist",
            )
        try:
            process = subprocess.Popen(
                command.arguments,
                cwd=command.working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output_destination,
                stderr=output_destination,
                text=True,
                # A program may print bytes that are not valid UTF-8 (an
                # odd file name, say); replace them rather than fail.
                errors="replace",
                start_new_session=True,
            )
        except FileNotFoundError:
            return CommandResult(command=command, outcome=CommandOutcome.NOT_FOUND)
        except OSError as start_error:
            # Anything else the system refuses (permission denied, not a
            # program at all) is a result too, so no caller has to catch it.
            return CommandResult(
                command=command, outcome=CommandOutcome.COULD_NOT_START, start_error=start_error.strerror
            )

        try:
            standard_output, standard_error = process.communicate(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            kill_process_group(process)
            # Collect what is left, so the killed program does not linger as a zombie.
            process.communicate()
            return CommandResult(command=command, outcome=CommandOutcome.TIMED_OUT)

        return CommandResult(
            command=command,
            outcome=CommandOutcome.COMPLETED,
            exit_code=process.returncode,
            # communicate() gives None for a stream that was not piped.
            standard_output=standard_output or "",
            standard_error=standard_error or "",
        )

    def find_program(self, name: str) -> Path | None:
        """Where the program `name` is on the PATH, or None when it is not installed."""
        location = shutil.which(name)
        if location is None:
            return None
        return Path(location)

    def start_background(self, command: Command) -> BackgroundStart:
        """Start `command` in a session of its own, wait for its first line, and return its record.

        Only captured output is supported, because the ready line has to be
        read, and the program runs in goodall's own folder, because
        posix_spawn cannot change folder. Asking for either is a mistake in
        goodall's code, so it raises ValueError.
        """
        if command.output != OutputMode.CAPTURE or command.working_directory is not None:
            raise ValueError("A background program needs captured output and runs in goodall's own folder.")
        # Unlinked as soon as they are made, so nothing the program prints
        # outlives it; only its first line is ever read.
        with tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as error_file:
            file_actions = [
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_DUP2, output_file.fileno(), 1),
                (os.POSIX_SPAWN_DUP2, error_file.fileno(), 2),
            ]
            try:
                process_id = os.posix_spawnp(
                    command.arguments[0],
                    command.arguments,
                    environment_for(command),
                    file_actions=file_actions,
                    setsid=True,
                )
            except FileNotFoundError:
                return BackgroundStart(command=command, outcome=BackgroundOutcome.NOT_FOUND)
            except OSError as start_error:
                return BackgroundStart(
                    command=command, outcome=BackgroundOutcome.COULD_NOT_START, start_error=start_error.strerror
                )
            return self.wait_until_ready(command, process_id, output_file.fileno(), error_file.fileno())

    def wait_until_ready(
        self, command: Command, process_id: int, output_descriptor: int, error_descriptor: int
    ) -> BackgroundStart:
        """Wait for a just-started program's first line, its exit, or the timeout, and say which came first."""
        deadline = time.monotonic() + command.timeout_seconds
        while True:
            printed = os.pread(output_descriptor, 65536, 0)
            if b"\n" in printed:
                ready_line = printed.split(b"\n")[0].decode("utf-8", errors="replace")
                process_started_at = self.process_started_at(process_id)
                if process_started_at is None:
                    # It ended straight after its ready line, or the system
                    # would not say when it started, so a later command could
                    # never tell it from a stranger. Not worth keeping either way.
                    kill_process_group_by_id(process_id)
                    reap(process_id)
                    return BackgroundStart(
                        command=command,
                        outcome=BackgroundOutcome.COULD_NOT_START,
                        start_error="goodall could not read when it started, so could not tell it apart later",
                    )
                program = BackgroundProgram(process_id, process_started_at, tuple(command.arguments))
                return BackgroundStart(
                    command=command, outcome=BackgroundOutcome.STARTED, program=program, ready_line=ready_line
                )
            exit_code = reap(process_id)
            if exit_code is not None:
                standard_error = os.pread(error_descriptor, 65536, 0).decode("utf-8", errors="replace")
                return BackgroundStart(
                    command=command,
                    outcome=BackgroundOutcome.EXITED,
                    exit_code=exit_code,
                    standard_error=standard_error,
                )
            if time.monotonic() > deadline:
                kill_process_group_by_id(process_id)
                os.waitpid(process_id, 0)
                return BackgroundStart(command=command, outcome=BackgroundOutcome.TIMED_OUT)
            time.sleep(BACKGROUND_POLL_SECONDS)

    def check_background(self, program: BackgroundProgram) -> BackgroundState:
        """Whether the recorded program is still running: same process id, same start time."""
        reap(program.process_id)
        process_started_at = self.process_started_at(program.process_id)
        if process_started_at is None:
            return BackgroundState.ENDED
        if process_started_at != program.process_started_at:
            return BackgroundState.REPLACED
        return BackgroundState.RUNNING

    def stop_background(
        self, program: BackgroundProgram, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> BackgroundStop:
        """Ask the recorded program to stop (SIGTERM), and kill it (SIGKILL) if it has not after `timeout_seconds`.

        Checked first, so a process that merely has the recorded id is never
        signalled. Trap: between that check and the signal the program could
        end and its id be reused; that takes far longer than the few
        milliseconds between them, so it is not guarded against.
        """
        state = self.check_background(program)
        if state == BackgroundState.ENDED:
            return BackgroundStop.ALREADY_ENDED
        if state == BackgroundState.REPLACED:
            return BackgroundStop.NOT_OURS
        signal_process_group(program.process_id, signal.SIGTERM)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not process_exists(program.process_id):
                return BackgroundStop.STOPPED
            time.sleep(BACKGROUND_POLL_SECONDS)
        kill_process_group_by_id(program.process_id)
        while process_exists(program.process_id):
            time.sleep(BACKGROUND_POLL_SECONDS)
        return BackgroundStop.KILLED

    def process_started_at(self, process_id: int) -> str | None:
        """When the system says process `process_id` started, or None when no running process has that id.

        Asked of `ps` through `run`, like every other program. A zombie (a
        process that has ended but not yet been reaped) counts as ended.
        """
        result = self.run(
            Command(
                ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(process_id)],
                extra_environment=PROCESS_START_TIME_ENVIRONMENT,
            )
        )
        if not result.succeeded:
            return None
        line = result.standard_output.strip()
        if line == "":
            return None
        process_state, _, started_at = line.partition(" ")
        if process_state.startswith("Z"):
            return None
        return " ".join(started_at.split())


def kill_process_group(process: "subprocess.Popen[str]") -> None:
    """Kill `process` and everything it started, which share its process group.

    A program that deliberately leaves its process group (as a daemon does)
    escapes this; none of the short commands goodall runs does that.
    """
    kill_process_group_by_id(process.pid)


def kill_process_group_by_id(process_id: int) -> None:
    """Kill the process group led by `process_id`; a group that has already gone is fine."""
    signal_process_group(process_id, signal.SIGKILL)


def signal_process_group(process_id: int, signal_number: int) -> None:
    """Send `signal_number` to the process group led by `process_id`, if it still exists."""
    try:
        os.killpg(process_id, signal_number)
    except ProcessLookupError:
        # Every process in the group had already exited.
        pass


def reap(process_id: int) -> int | None:
    """Collect `process_id`'s exit if it is goodall's own child and has ended; return its exit code, else None.

    A background program is goodall's child only while the goodall that
    started it is running (in tests, chiefly); otherwise this does nothing.
    """
    try:
        reaped_id, wait_status = os.waitpid(process_id, os.WNOHANG)
    except ChildProcessError:
        return None
    if reaped_id == 0:
        return None
    return os.waitstatus_to_exitcode(wait_status)


def process_exists(process_id: int) -> bool:
    """True while a process with this id exists. Signal 0 only asks; it sends nothing.

    An ended child of goodall's own is reaped first, so it does not count.
    """
    reap(process_id)
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists, but belongs to another user.
        return True
    return True
