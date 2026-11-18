"""Tests for the fake runner (goodall.fake_runner) that every other test hands to goodall.

The fake is what keeps tests away from the real world, so its promises are
tested on their own: a command it was not told about fails the test and
names the command, scripted answers come back as results, every call is
recorded, and git runs for real.
"""

from pathlib import Path

import pytest

from goodall.fake_runner import FakeRunner
from goodall.runner import Command, CommandOutcome, OutputMode


def test_a_command_the_fake_was_not_told_about_fails_the_test_and_names_the_command() -> None:
    runner = FakeRunner()

    with pytest.raises(AssertionError, match="`op item get 'API key'`"):
        runner.run(Command(["op", "item", "get", "API key"]))


def test_a_scripted_answer_comes_back_as_a_result_and_every_call_is_recorded() -> None:
    runner = FakeRunner()
    runner.answer(["gh", "auth", "status"], exit_code=1, standard_error="You are not logged in\n")
    command = Command(["gh", "auth", "status"])

    first_result = runner.run(command)
    second_result = runner.run(command)

    assert first_result.exit_code == 1
    assert first_result.problem == "`gh auth status` exited with code 1: You are not logged in"
    assert second_result == first_result
    assert runner.calls == [command, command]


def test_a_command_whose_output_is_discarded_gets_none_of_the_scripted_output() -> None:
    # The fake keeps the real runner's promise, so a test cannot pass only
    # because the fake handed goodall output the real runner would discard.
    runner = FakeRunner()
    runner.answer(["op", "read", "op://Eval/item/credential"], standard_output="pretend-secret-value\n")

    result = runner.run(Command(["op", "read", "op://Eval/item/credential"], output=OutputMode.DISCARD))

    assert result.succeeded
    assert result.standard_output == ""


def test_a_command_can_be_scripted_as_missing_or_as_never_finishing() -> None:
    runner = FakeRunner()
    runner.answer_not_found(["gh", "--version"])
    runner.answer_timed_out(["/usr/bin/id", "-Gn"])

    not_found = runner.run(Command(["gh", "--version"]))
    timed_out = runner.run(Command(["/usr/bin/id", "-Gn"], timeout_seconds=3))

    assert not_found.outcome == CommandOutcome.NOT_FOUND
    assert not_found.problem == "`gh` was not found"
    # Reported at once, as the real runner would after the command's own timeout.
    assert timed_out.outcome == CommandOutcome.TIMED_OUT
    assert timed_out.problem == "`/usr/bin/id -Gn` timed out after 3 s"


def test_git_runs_for_real_so_tests_verify_gits_actual_behavior(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = runner.run(Command(["git", "init", "--quiet"], working_directory=tmp_path))

    assert result.succeeded
    assert (tmp_path / ".git").is_dir()
    assert runner.calls == [Command(["git", "init", "--quiet"], working_directory=tmp_path)]


def test_only_the_programs_a_test_lists_are_installed() -> None:
    runner = FakeRunner(installed_programs=["git", "gh"])

    gh_location = runner.find_program("gh")
    git_location = runner.find_program("git")

    assert gh_location is not None
    assert runner.find_program("op") is None
    # git is real, so it is found where it really is.
    assert git_location is not None
    assert git_location.is_file()
