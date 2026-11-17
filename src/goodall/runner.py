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

Two ways of running a program are planned but deliberately not built yet,
because nothing calls them yet: handing the terminal over to a program
(a new `OutputMode`, with no timeout, since it is a person's working
session), and starting a program that outlives one goodall command (a
separate method returning a record that can be saved and used later to stop
it).

This module imports only the standard library, because the hook path will
use it (ADR 0005); tests/test_hook_path_imports.py proves it.
"""

import os
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

# How long a short command may run before goodall gives up on it.
DEFAULT_TIMEOUT_SECONDS = 10.0

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
    - `working_directory` is the folder it runs in; None means the folder
      goodall was run from.
    """

    arguments: list[str]
    output: OutputMode = OutputMode.CAPTURE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    extra_environment: dict[str, str] = field(default_factory=dict)
    working_directory: Path | None = None

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
        environment = dict(os.environ)
        environment.update(command.extra_environment)
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


def kill_process_group(process: "subprocess.Popen[str]") -> None:
    """Kill `process` and everything it started, which share its process group.

    A program that deliberately leaves its process group (as a daemon does)
    escapes this; none of the short commands goodall runs does that.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # Every process in the group had already exited.
        pass
