"""Tests for the real runner (goodall.runner), against harmless real programs.

Every other test hands goodall a fake runner, so these are the only tests
that prove the real one does what the fake pretends: a program that
succeeds, one that exits non-zero, one that does not exist and one that
outlasts its timeout each come back as a result, never an exception.

The harmless real program is the Python interpreter running these tests
(`sys.executable`), because it exists on every machine the tests run on.
"""

import dataclasses
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from goodall.runner import (
    BackgroundOutcome,
    BackgroundProgram,
    BackgroundStart,
    BackgroundState,
    BackgroundStop,
    Command,
    CommandOutcome,
    OutputMode,
    RealRunner,
    environment_for,
)

PYTHON = sys.executable


def test_a_program_that_succeeds_returns_its_output() -> None:
    result = RealRunner().run(Command([PYTHON, "-c", "print('hello')"]))

    assert result.outcome == CommandOutcome.COMPLETED
    assert result.exit_code == 0
    assert result.standard_output == "hello\n"
    assert result.succeeded
    assert result.problem is None


def test_a_program_that_exits_non_zero_is_a_result_whose_problem_quotes_its_error() -> None:
    # No single quotes in the script, so the expected quoting below is plain.
    script = 'import sys; sys.stderr.write("fatal: something broke\\nmore detail\\n"); sys.exit(3)'
    result = RealRunner().run(Command([PYTHON, "-c", script]))

    assert result.outcome == CommandOutcome.COMPLETED
    assert result.exit_code == 3
    assert not result.succeeded
    assert result.problem == f"`{PYTHON} -c '{script}'` exited with code 3: fatal: something broke"


def test_a_program_that_does_not_exist_is_a_result_not_an_exception() -> None:
    result = RealRunner().run(Command(["goodall-test-no-such-program", "--version"]))

    assert result.outcome == CommandOutcome.NOT_FOUND
    assert result.exit_code is None
    assert not result.succeeded
    assert result.problem == "`goodall-test-no-such-program` was not found"


# Starts a second program that would sleep for a minute, writes that
# program's process id to the file named by its first argument, then sleeps
# for a minute itself. Both must be killed when the runner gives up.
START_A_SLEEPER_AND_SLEEP = """
import subprocess, sys, time
sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1], "w") as pid_file:
    pid_file.write(str(sleeper.pid))
time.sleep(60)
"""


def process_is_running(process_id: int) -> bool:
    """True while a process with this id exists. Signal 0 only asks; it sends nothing."""
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_program_that_outlasts_its_timeout_is_killed_with_everything_it_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "sleeper.pid"
    command = Command([PYTHON, "-c", START_A_SLEEPER_AND_SLEEP, str(pid_file)], timeout_seconds=1)

    started_at = time.monotonic()
    result = RealRunner().run(command)
    elapsed_seconds = time.monotonic() - started_at

    assert result.outcome == CommandOutcome.TIMED_OUT
    assert result.exit_code is None
    assert result.problem is not None
    assert result.problem.endswith("` timed out after 1 s")
    assert elapsed_seconds < 5
    # The program it started was killed too. It may take a moment for the
    # system to clear it away once it is dead.
    sleeper_process_id = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while process_is_running(sleeper_process_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_is_running(sleeper_process_id)


def test_discarded_output_never_reaches_goodall() -> None:
    # Stands in for a command that might print a secret: neither stream is kept.
    script = 'import sys; print("pretend-secret-value"); sys.stderr.write("pretend-secret-value\\n"); sys.exit(1)'
    result = RealRunner().run(Command([PYTHON, "-c", script], output=OutputMode.DISCARD))

    assert result.exit_code == 1
    assert result.standard_output == ""
    assert result.standard_error == ""
    # The problem quotes no error line, because none was kept.
    assert result.problem is not None
    assert result.problem.endswith("` exited with code 1")


def test_a_program_that_cannot_be_started_is_a_result_not_an_exception(tmp_path: Path) -> None:
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("#!/bin/sh\necho hello\n")
    not_executable.chmod(0o644)

    result = RealRunner().run(Command([str(not_executable)]))

    assert result.outcome == CommandOutcome.COULD_NOT_START
    assert result.problem == f"`{not_executable}` could not be started: Permission denied"


def test_extra_environment_variables_reach_the_program_alongside_goodalls_own() -> None:
    script = 'import os; print(os.environ["GOODALL_TEST_SETTING"], "PATH" in os.environ)'
    command = Command([PYTHON, "-c", script], extra_environment={"GOODALL_TEST_SETTING": "on"})

    result = RealRunner().run(command)

    assert result.standard_output == "on True\n"


def test_environment_variables_with_a_removed_prefix_never_reach_the_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOODALL_STRAY_ONE", "from outside")
    monkeypatch.setenv("GOODALL_STRAY_TWO", "from outside")
    script = (
        'import os; print(sorted(name for name in os.environ if name.startswith("GOODALL_STRAY")), '
        '"PATH" in os.environ)'
    )
    command = Command(
        [PYTHON, "-c", script],
        removed_environment_prefixes=["GOODALL_STRAY"],
        extra_environment={"GOODALL_STRAY_KEPT": "set by goodall"},
    )

    result = RealRunner().run(command)

    assert result.standard_output == "['GOODALL_STRAY_KEPT'] True\n"


def test_a_command_runs_in_the_folder_it_names(tmp_path: Path) -> None:
    command = Command([PYTHON, "-c", "import os; print(os.getcwd())"], working_directory=tmp_path)

    result = RealRunner().run(command)

    # resolve(), because on macOS the temporary folder is reached through a link.
    assert Path(result.standard_output.strip()).resolve() == tmp_path.resolve()


def test_a_missing_working_folder_is_not_mistaken_for_a_missing_program(tmp_path: Path) -> None:
    missing_folder = tmp_path / "not-here"
    result = RealRunner().run(Command([PYTHON, "-c", "pass"], working_directory=missing_folder))

    assert result.outcome == CommandOutcome.COULD_NOT_START
    assert result.problem == f"`{PYTHON}` could not be started: the folder it runs in, {missing_folder}, does not exist"


def test_finding_a_program_says_where_it_is_or_that_it_is_missing() -> None:
    runner = RealRunner()

    found = runner.find_program("sh")
    assert found is not None
    assert found.is_absolute()
    assert runner.find_program("goodall-test-no-such-program") is None


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "GH_TOKEN", "CLIENT_SECRET", "anthropic_api_key"])
def test_an_extra_environment_variable_that_looks_like_a_secret_is_refused(name: str) -> None:
    # ADR 0010: secret values reach programs only through the secrets
    # manager, never through goodall. Refusing is goodall's own mistake, not
    # the machine's, so it is an exception rather than a result.
    with pytest.raises(ValueError, match=name):
        Command(["true"], extra_environment={name: "op://Eval/item/credential"})


# A synthetic 1Password service account token, built from pieces so a secret
# scanner does not mistake it for a leak.
SYNTHETIC_SERVICE_ACCOUNT_TOKEN = "ops_" + "SYNTHETICserviceACCOUNT" * 4


def test_a_program_gets_goodalls_whole_environment_with_the_service_account_token_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # op signs in with a service account through this variable alone, so
    # nothing may strip it, or any other OP_ variable, on the way.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", SYNTHETIC_SERVICE_ACCOUNT_TOKEN)
    command = Command(["op", "whoami"], extra_environment={"GOODALL_TEST_SETTING": "on"})

    environment = environment_for(command)

    assert environment["OP_SERVICE_ACCOUNT_TOKEN"] == SYNTHETIC_SERVICE_ACCOUNT_TOKEN
    assert environment["GOODALL_TEST_SETTING"] == "on"
    assert environment["PATH"] == os.environ["PATH"]


def test_the_service_account_token_reaches_a_real_program(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", SYNTHETIC_SERVICE_ACCOUNT_TOKEN)
    # The program says only whether it got the same token, never the token.
    script = f'import os; print(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN") == "{SYNTHETIC_SERVICE_ACCOUNT_TOKEN}")'

    result = RealRunner().run(Command([PYTHON, "-c", script]))

    assert result.standard_output == "True\n"


@pytest.mark.parametrize("ending", ["fails", "times out"])
def test_the_service_account_token_is_in_no_command_or_result_goodall_keeps(
    monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    # The environment is built only as the program starts, so a command, a
    # result, its problem, and anything an exception would print from them
    # (their repr) hold no copy of the token.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", SYNTHETIC_SERVICE_ACCOUNT_TOKEN)
    if ending == "fails":
        command = Command([PYTHON, "-c", "import sys; sys.exit(1)"])
    else:
        command = Command([PYTHON, "-c", "import time; time.sleep(60)"], timeout_seconds=0.5)

    result = RealRunner().run(command)

    assert not result.succeeded
    for kept in [repr(command), repr(result), str(result.problem)]:
        assert "SYNTHETICservice" not in kept


# ---------------------------------------------------------------------------
# Background programs: started by one goodall command, stopped by a later one
# ---------------------------------------------------------------------------

# Says it is ready, then waits to be stopped, as the telemetry receiver does.
READY_THEN_WAIT = 'import time; print("{\\"ready\\": true}", flush=True); time.sleep(60)'

# The same, but ignores the polite request to stop, so it has to be killed.
READY_THEN_IGNORE_SIGTERM = (
    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); time.sleep(60)'
)


@pytest.fixture
def background_programs() -> Iterator[list[BackgroundProgram]]:
    """Background programs a test started, every one of them killed afterwards, even after a failing test."""
    started: list[BackgroundProgram] = []
    yield started
    for program in started:
        RealRunner().stop_background(program, timeout_seconds=1)


def start_in_background(
    started: list[BackgroundProgram], arguments: list[str], timeout_seconds: float = 10
) -> BackgroundStart:
    """Start a background program for real, keep its record for clean-up, and return the start's result."""
    start = RealRunner().start_background(Command(arguments, timeout_seconds=timeout_seconds))
    if start.program is not None:
        started.append(start.program)
    return start


def start_running(started: list[BackgroundProgram], arguments: list[str]) -> BackgroundProgram:
    """Start a background program that must come up, and return its record."""
    start = start_in_background(started, arguments)
    assert start.program is not None, start.problem
    return start.program


def test_a_background_program_outlives_its_start_and_a_later_command_can_stop_it(
    background_programs: list[BackgroundProgram],
) -> None:
    start = start_in_background(background_programs, [PYTHON, "-c", READY_THEN_WAIT])

    assert start.outcome == BackgroundOutcome.STARTED
    assert start.ready_line == '{"ready": true}'
    assert start.problem is None
    program = start.program
    assert program is not None
    # A later goodall command has only the saved record, so the test goes
    # through the same round trip.
    saved = json.loads(json.dumps(program.as_json()))
    program_again = BackgroundProgram.from_json(saved)
    assert program_again is not None
    assert program_again == program
    assert RealRunner().check_background(program_again) == BackgroundState.RUNNING
    # In a session of its own, so closing the terminal that started it does not end it.
    assert os.getsid(program.process_id) == program.process_id

    assert RealRunner().stop_background(program_again) == BackgroundStop.STOPPED
    assert RealRunner().check_background(program_again) == BackgroundState.ENDED
    assert RealRunner().stop_background(program_again) == BackgroundStop.ALREADY_ENDED


def test_stopping_refuses_a_process_that_is_not_the_one_recorded(
    background_programs: list[BackgroundProgram],
) -> None:
    # Process ids are reused: the record of a program that has ended may
    # name a process id the system has since given to something else. The
    # record's start time tells them apart, so stop never kills that.
    program = start_running(background_programs, [PYTHON, "-c", READY_THEN_WAIT])
    other_process = dataclasses.replace(program, process_started_at="Thu Jan  1 00:00:00 1970")

    assert RealRunner().check_background(other_process) == BackgroundState.REPLACED
    assert RealRunner().stop_background(other_process) == BackgroundStop.NOT_OURS
    assert RealRunner().check_background(program) == BackgroundState.RUNNING


def test_a_background_program_that_ignores_the_request_to_stop_is_killed(
    background_programs: list[BackgroundProgram],
) -> None:
    program = start_running(background_programs, [PYTHON, "-c", READY_THEN_IGNORE_SIGTERM])

    assert RealRunner().stop_background(program, timeout_seconds=0.5) == BackgroundStop.KILLED
    assert RealRunner().check_background(program) == BackgroundState.ENDED


def test_a_background_program_that_exits_before_it_is_ready_is_a_result_with_its_error(
    background_programs: list[BackgroundProgram],
) -> None:
    script = 'import sys; sys.stderr.write("port 4318 is already in use\\n"); sys.exit(1)'

    start = start_in_background(background_programs, [PYTHON, "-c", script])

    assert start.outcome == BackgroundOutcome.EXITED
    assert start.program is None
    assert start.exit_code == 1
    assert start.standard_error == "port 4318 is already in use\n"
    assert start.problem == f"`{PYTHON} -c '{script}'` exited with code 1: port 4318 is already in use"


def test_a_background_program_that_never_says_it_is_ready_is_killed_and_reported(
    background_programs: list[BackgroundProgram],
) -> None:
    started_at = time.monotonic()
    start = start_in_background(background_programs, [PYTHON, "-c", "import time; time.sleep(60)"], timeout_seconds=0.5)

    assert start.outcome == BackgroundOutcome.TIMED_OUT
    assert start.program is None
    assert start.problem is not None
    assert start.problem.endswith("did not say it was ready within 0.5 s")
    assert time.monotonic() - started_at < 5


def test_a_background_program_that_does_not_exist_is_a_result_not_an_exception(
    background_programs: list[BackgroundProgram],
) -> None:
    start = start_in_background(background_programs, ["goodall-test-no-such-program"])

    assert start.outcome == BackgroundOutcome.NOT_FOUND
    assert start.problem == "`goodall-test-no-such-program` was not found"


def test_a_saved_record_that_is_not_a_background_program_reads_as_none() -> None:
    assert BackgroundProgram.from_json({"process_id": "12"}) is None
    assert BackgroundProgram.from_json([]) is None
    assert BackgroundProgram.from_json({"process_id": 0, "process_started_at": "x", "arguments": ["a"]}) is None
