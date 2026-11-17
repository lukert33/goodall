"""The fake runner: the runner every test hands to goodall instead of the real one.

`cli.main` accepts a runner (goodall.runner), so a test can drive any
command with this fake and decide what every program "does". It lives in
the package, beside the real runner, so any test, including those of later
tickets, can import it as `goodall.fake_runner`.

What it promises, and why (ADR 0011):

- A command it was not told about fails the test, with an AssertionError
  naming the command. So no test can reach the real `gh` or the real
  secrets manager by accident: forgetting to script one is a loud failure,
  never a silent call to the real world.
- A test scripts an answer for an exact list of arguments with `answer`,
  and gets back the same answer every time that command runs.
- Every command it is asked to run is kept in `calls`, in order.
- The programs named in `real_programs` (git, by default) run for real,
  through the real runner, because git's actual behaviour is what tests
  verify (AGENTS.md, "Testing"). Use them only in temporary folders.
- `find_program` finds exactly the programs listed in `installed_programs`
  and no others. A real program is found where it really is; any other is
  given a made-up location, since nothing will run it.
- It keeps the real runner's promises about output: a command whose output
  is discarded gets none of the scripted output, so no test can pass only
  because the fake handed goodall something the real runner would not.
- A command scripted as never finishing comes back at once as timed out,
  just as the real runner would report it after the command's own timeout.
  tests/test_runner.py proves the real runner really does kill it.
"""

from dataclasses import dataclass, field
from pathlib import Path

from goodall.runner import Command, CommandOutcome, CommandResult, OutputMode, RealRunner

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


@dataclass
class FakeRunner:
    """A runner whose programs do only what a test scripted.

    - `installed_programs` are the programs `find_program` finds.
    - `real_programs` are the programs that run for real.
    - `calls` is every command it was asked to run, in order, real ones too.
    """

    installed_programs: list[str] = field(default_factory=list)
    real_programs: list[str] = field(default_factory=lambda: ["git"])
    calls: list[Command] = field(default_factory=list)
    answers: dict[tuple[str, ...], ScriptedAnswer] = field(default_factory=dict)

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

    def run(self, command: Command) -> CommandResult:
        """Answer `command` as scripted, or fail the test when it was not scripted."""
        self.calls.append(command)
        if command.arguments[0] in self.real_programs:
            return RealRunner().run(command)
        scripted_answer = self.answers.get(tuple(command.arguments))
        if scripted_answer is None:
            raise AssertionError(
                f"The fake runner was asked to run `{command.as_typed}`, which the test did not script. "
                "Script it with FakeRunner.answer(...) or one of its siblings."
            )
        if command.output == OutputMode.DISCARD:
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
