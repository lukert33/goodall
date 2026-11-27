"""The fake runner: the runner every test hands to goodall instead of the real one.

`cli.main` accepts a runner (goodall.runner), so a test can drive any
command with this fake and decide what every program "does". It lives in
the package, beside the real runner, so any test, including those of later
tickets, can import it as `goodall.fake_runner`.

What it promises, and why (ADR 0011):

- A command it was not told about fails the test, with an AssertionError
  naming the command. So no test can reach the real secrets manager, or
  any other real program, by accident: forgetting to script one is a loud failure,
  never a silent call to the real world.
- A test scripts an answer for an exact list of arguments with `answer`,
  and gets back the same answer every time that command runs.
- A test can instead script a whole program with a function
  (`answer_with`), which is given each command for that program that has no
  exact answer and returns what it "does". The function may write a file
  the command names, as `curl --output FILE` does, so a test can stand in
  for a local server that goodall asks through `curl`. A program with no
  such function and no exact answer still fails the test.
- Every command it is asked to run is kept in `calls`, in order.
- The programs named in `real_programs` (git, by default) run for real,
  through the real runner, because git's actual behavior is what tests
  verify (AGENTS.md, "Testing"). Use them only in temporary folders.
- `find_program` finds exactly the programs listed in `installed_programs`
  and no others. A real program is found where it really is; any other is
  given a made-up location, since nothing will run it.
- It keeps the real runner's promises about output: a command whose output
  is discarded, or that was handed the terminal, gets none of the scripted
  output, so no test can pass only because the fake handed goodall
  something the real runner would not.
- A command scripted as never finishing comes back at once as timed out,
  just as the real runner would report it after the command's own timeout.
  tests/test_runner.py proves the real runner really does kill it.

Background programs (`start_background`, `check_background`,
`stop_background`) follow the same rules: one the test did not script fails
the test, and one named in `real_programs` really starts. A scripted one is
a simulation (`SimulatedProgram`) that the test supplies, matched by the
start of its arguments, because the rest (a folder named after a work
block, say) is not known in advance. Each goodall command a test runs gets
a runner of its own, but a background program outlives the command that
started it, so simulations live in a `FakeBackgroundPrograms` registry that
the test hands to every runner; a later command's runner finds the program
there by its process id. Two simulations come with the fake:
`PretendProgram`, which only says it is running, and
`InProcessTelemetryReceiver`, a real telemetry receiver run in the test's
own process, so telemetry sent to it really lands on disk while the test
stays in one process.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from goodall import telemetry_receiver
from goodall.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    BackgroundOutcome,
    BackgroundProgram,
    BackgroundStart,
    BackgroundState,
    BackgroundStop,
    Command,
    CommandOutcome,
    CommandResult,
    OutputMode,
    RealRunner,
)

# The start time every simulated program's record carries. No real process
# reports it, so a record of a simulation is never mistaken for a real one.
SIMULATED_START_TIME = "simulated"

# Simulated process ids start here, far above what a test machine hands out,
# and count up, so each simulation is told apart from the others.
FIRST_SIMULATED_PROCESS_ID = 4_000_001

# Where the fake says a scripted program is installed. It exists on no
# machine, so nothing can run a program from there by mistake.
FAKE_PROGRAM_FOLDER = Path("/nonexistent-goodall-test/bin")


@dataclass(frozen=True)
class ScriptedAnswer:
    """What a scripted command "does" when the fake runs it."""

    outcome: CommandOutcome
    exit_code: int | None = None
    standard_output: str = ""
    standard_error: str = ""


@dataclass(frozen=True)
class SimulatedExit:
    """What a simulated background program "does" when it exits before saying it is ready."""

    exit_code: int
    standard_error: str = ""


class SimulatedProgram(Protocol):
    """A background program a test simulates in place of a real one."""

    def start(self, command: Command) -> str | SimulatedExit:
        """Come up and return the ready line, or say how it exited instead."""
        ...

    def is_running(self) -> bool: ...

    def stop(self) -> None:
        """End, as a real program would when asked to stop."""
        ...


@dataclass
class PretendProgram:
    """A simulation that does nothing but say it is ready and then that it is running, until it is stopped.

    Give `exit_code` for one that exits before it is ready, with
    `standard_error` as what it printed. `end` makes it end by itself, as a
    crash would.
    """

    ready_line: str = "ready"
    exit_code: int | None = None
    standard_error: str = ""
    running: bool = False

    def start(self, command: Command) -> str | SimulatedExit:
        if self.exit_code is not None:
            return SimulatedExit(self.exit_code, self.standard_error)
        self.running = True
        return self.ready_line

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False

    def end(self) -> None:
        self.running = False


@dataclass
class InProcessTelemetryReceiver:
    """A real telemetry receiver, run in the test's own process in place of `python -m goodall.telemetry_receiver`.

    It reads the directory and port from the command's arguments as the
    real program would, listens on a real port, writes real files, and
    holds its real lock on the receipt log, so anything that checks the
    receiver sees what it would see of a real one. `crash` ends it as a
    killed receiver ends: whatever it had acknowledged is already on disk.
    A port it cannot have makes it exit with code 1, as the real one does.
    """

    running_receiver: telemetry_receiver.RunningReceiver | None = None

    def start(self, command: Command) -> str | SimulatedExit:
        parsed_arguments = telemetry_receiver.parse_arguments(command.arguments[command.arguments.index("-m") + 2 :])
        try:
            self.running_receiver = telemetry_receiver.start_receiver(
                parsed_arguments.telemetry_directory, port=parsed_arguments.port
            )
        except (OSError, ValueError) as start_error:
            return SimulatedExit(1, f"The telemetry receiver did not start: {start_error}\n")
        return telemetry_receiver.ready_line(self.running_receiver)

    def is_running(self) -> bool:
        return self.running_receiver is not None

    def stop(self) -> None:
        if self.running_receiver is not None:
            self.running_receiver.stop()
            self.running_receiver = None

    def crash(self) -> None:
        self.stop()


@dataclass
class FakeBackgroundPrograms:
    """The simulated background programs of one test, shared by every runner the test makes.

    `scripts` maps the start of a program's arguments to what makes its
    simulation; `started` maps each simulated process id to its simulation.
    `stop_all` ends every one still running; call it when the test ends, so
    an in-process receiver never outlives its test.
    """

    scripts: dict[tuple[str, ...], Callable[[], SimulatedProgram]] = field(default_factory=dict)
    started: dict[int, SimulatedProgram] = field(default_factory=dict)
    next_process_id: int = FIRST_SIMULATED_PROCESS_ID

    def take_process_id(self) -> int:
        process_id = self.next_process_id
        self.next_process_id += 1
        return process_id

    def script_for(self, arguments: list[str]) -> Callable[[], SimulatedProgram] | None:
        """The script whose arguments are the longest start of `arguments`, or None."""
        best_match: tuple[str, ...] | None = None
        for scripted_arguments in self.scripts:
            if tuple(arguments[: len(scripted_arguments)]) != scripted_arguments:
                continue
            if best_match is None or len(scripted_arguments) > len(best_match):
                best_match = scripted_arguments
        if best_match is None:
            return None
        return self.scripts[best_match]

    def stop_all(self) -> None:
        for simulation in self.started.values():
            if simulation.is_running():
                simulation.stop()


@dataclass
class FakeRunner:
    """A runner whose programs do only what a test scripted.

    - `installed_programs` are the programs `find_program` finds.
    - `real_programs` are the programs that run for real.
    - `calls` is every command it was asked to run, in order, real ones too,
      background programs included.
    - `answers` holds the answer scripted for each exact list of arguments,
      and `programs` the function scripted for each whole program.
    - `background` holds the simulated background programs; hand the same
      one to every runner in a test.
    """

    installed_programs: list[str] = field(default_factory=list)
    real_programs: list[str] = field(default_factory=lambda: ["git"])
    calls: list[Command] = field(default_factory=list)
    answers: dict[tuple[str, ...], ScriptedAnswer] = field(default_factory=dict)
    programs: dict[str, Callable[[Command], ScriptedAnswer]] = field(default_factory=dict)
    background: FakeBackgroundPrograms = field(default_factory=FakeBackgroundPrograms)

    def answer_background(self, arguments_start: list[str], make_program: Callable[[], SimulatedProgram]) -> None:
        """Script every background program whose arguments start with `arguments_start` as a simulation.

        `make_program` is called once per start, so a test can hand back the
        same simulation each time or a new one.
        """
        self.background.scripts[tuple(arguments_start)] = make_program

    def start_background(self, command: Command) -> BackgroundStart:
        """Start a real program, or the simulation scripted for it; fail the test when there is neither."""
        self.calls.append(command)
        if command.arguments[0] in self.real_programs:
            return RealRunner().start_background(command)
        make_program = self.background.script_for(command.arguments)
        if make_program is None:
            raise AssertionError(
                f"The fake runner was asked to start `{command.as_typed}` in the background, which the test did "
                "not script. Script it with FakeRunner.answer_background(...)."
            )
        simulation = make_program()
        ready_line = simulation.start(command)
        if isinstance(ready_line, SimulatedExit):
            return BackgroundStart(
                command=command,
                outcome=BackgroundOutcome.EXITED,
                exit_code=ready_line.exit_code,
                standard_error=ready_line.standard_error,
            )
        process_id = self.background.take_process_id()
        self.background.started[process_id] = simulation
        program = BackgroundProgram(process_id, SIMULATED_START_TIME, tuple(command.arguments))
        return BackgroundStart(
            command=command, outcome=BackgroundOutcome.STARTED, program=program, ready_line=ready_line
        )

    def check_background(self, program: BackgroundProgram) -> BackgroundState:
        """Ask the simulation whether it is running; a real program is checked for real."""
        if program.process_started_at != SIMULATED_START_TIME:
            return RealRunner().check_background(program)
        simulation = self.background.started.get(program.process_id)
        if simulation is None or not simulation.is_running():
            return BackgroundState.ENDED
        return BackgroundState.RUNNING

    def stop_background(
        self, program: BackgroundProgram, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> BackgroundStop:
        """Stop the simulation; a real program is stopped for real."""
        if program.process_started_at != SIMULATED_START_TIME:
            return RealRunner().stop_background(program, timeout_seconds)
        if self.check_background(program) == BackgroundState.ENDED:
            return BackgroundStop.ALREADY_ENDED
        self.background.started[program.process_id].stop()
        return BackgroundStop.STOPPED

    def answer(
        self, arguments: list[str], exit_code: int = 0, standard_output: str = "", standard_error: str = ""
    ) -> None:
        """Script `arguments` to run and exit with `exit_code`, having printed what is given."""
        self.answers[tuple(arguments)] = ScriptedAnswer(
            outcome=CommandOutcome.COMPLETED,
            exit_code=exit_code,
            standard_output=standard_output,
            standard_error=standard_error,
        )

    def answer_not_found(self, arguments: list[str]) -> None:
        """Script `arguments` as naming a program that is not installed."""
        self.answers[tuple(arguments)] = ScriptedAnswer(outcome=CommandOutcome.NOT_FOUND)

    def answer_timed_out(self, arguments: list[str]) -> None:
        """Script `arguments` as a program that never finishes, so it times out."""
        self.answers[tuple(arguments)] = ScriptedAnswer(outcome=CommandOutcome.TIMED_OUT)

    def answer_with(self, program_name: str, respond: Callable[[Command], ScriptedAnswer]) -> None:
        """Script every command that runs `program_name` and has no exact answer: `respond` says what it "does".

        `respond` is given the command and may write a file the command
        names, as the real program would.
        """
        self.programs[program_name] = respond

    def run(self, command: Command) -> CommandResult:
        """Answer `command` as scripted, or fail the test when it was not scripted."""
        self.calls.append(command)
        if command.arguments[0] in self.real_programs:
            return RealRunner().run(command)
        scripted_answer = self.answers.get(tuple(command.arguments))
        if scripted_answer is None and command.arguments[0] in self.programs:
            scripted_answer = self.programs[command.arguments[0]](command)
        if scripted_answer is None:
            raise AssertionError(
                f"The fake runner was asked to run `{command.as_typed}`, which the test did not script. "
                "Script it with FakeRunner.answer(...) or one of its siblings."
            )
        if command.output in [OutputMode.DISCARD, OutputMode.HAND_OVER]:
            return CommandResult(command=command, outcome=scripted_answer.outcome, exit_code=scripted_answer.exit_code)
        return CommandResult(
            command=command,
            outcome=scripted_answer.outcome,
            exit_code=scripted_answer.exit_code,
            standard_output=scripted_answer.standard_output,
            standard_error=scripted_answer.standard_error,
        )

    def find_program(self, name: str) -> Path | None:
        """Where `name` is installed, if the test listed it in `installed_programs`; otherwise None."""
        if name not in self.installed_programs:
            return None
        if name in self.real_programs:
            return RealRunner().find_program(name)
        return FAKE_PROGRAM_FOLDER / name
