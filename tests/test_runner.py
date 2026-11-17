"""Tests for the real runner (goodall.runner), against harmless real programs.

Every other test hands goodall a fake runner, so these are the only tests
that prove the real one does what the fake pretends: a program that
succeeds, one that exits non-zero, one that does not exist and one that
outlasts its timeout each come back as a result, never an exception.

The harmless real program is the Python interpreter running these tests
(`sys.executable`), because it exists on every machine the tests run on.
"""

import os
import sys
import time
from pathlib import Path

import pytest

from goodall.runner import Command, CommandOutcome, OutputMode, RealRunner, environment_for

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
