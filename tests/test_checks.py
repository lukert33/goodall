"""Tests for goodall.checks, the small machinery every diagnostic check runs through.

`goodall doctor`'s own tests (tests/test_doctor.py) prove the behaviour a
contributor sees. These pin down the rules the later checks rely on, with
made-up checks, so the rules hold whatever checks exist: a failure skips
what depends on it, a skip names the failure that caused it, and a warning
skips nothing.
"""

import pytest

from goodall.checks import Check, CheckContext, CheckFunction, CheckOutcome, failed, passed, run_checks, warned
from goodall.fake_runner import FakeRunner
from goodall.terminal import CheckStatus, Failure

PROBLEM = Failure(kind="example", what_happened="It broke.", why="Because.", what_to_do_next="Mend it.")


def always(outcome: CheckOutcome) -> CheckFunction:
    """A check function that ignores the context and returns `outcome`."""

    def run(context: CheckContext) -> CheckOutcome:
        return outcome

    return run


def make_check(id: str, outcome: CheckOutcome, prerequisites: list[str] | None = None) -> Check:
    return Check(
        id=id,
        group="Example",
        summary=f"Summary of {id}",
        prerequisites=prerequisites or [],
        run=always(outcome),
    )


def statuses_and_waits(checks: list[Check]) -> list[tuple[str, CheckStatus, str | None]]:
    results = run_checks(checks, CheckContext(runner=FakeRunner()))
    return [(result.id, result.status, result.waiting_for) for result in results]


def test_a_failed_check_skips_the_checks_that_depend_on_it_and_names_it() -> None:
    checks = [
        make_check("first", failed([PROBLEM])),
        make_check("second", passed(), prerequisites=["first"]),
        make_check("independent", passed()),
    ]
    assert statuses_and_waits(checks) == [
        ("first", CheckStatus.FAIL, None),
        ("second", CheckStatus.SKIP, "first"),
        ("independent", CheckStatus.PASS, None),
    ]


def test_a_skip_further_down_names_the_check_that_actually_failed() -> None:
    # One broken thing shows as one failure: the contributor is pointed
    # straight at it, not at a chain of skipped checks.
    checks = [
        make_check("first", failed([PROBLEM])),
        make_check("second", passed(), prerequisites=["first"]),
        make_check("third", passed(), prerequisites=["second"]),
    ]
    assert statuses_and_waits(checks)[2] == ("third", CheckStatus.SKIP, "first")


def test_a_warning_does_not_skip_the_checks_that_depend_on_it() -> None:
    checks = [
        make_check("first", warned([PROBLEM])),
        make_check("second", passed(), prerequisites=["first"]),
    ]
    assert statuses_and_waits(checks) == [
        ("first", CheckStatus.WARN, None),
        ("second", CheckStatus.PASS, None),
    ]


def test_a_skipped_check_is_never_run() -> None:
    ran: list[str] = []

    def record_run(context: CheckContext) -> CheckOutcome:
        ran.append("second")
        return passed()

    checks = [
        make_check("first", failed([PROBLEM])),
        Check(id="second", group="Example", summary="Second", prerequisites=["first"], run=record_run),
    ]
    run_checks(checks, CheckContext(runner=FakeRunner()))
    assert ran == []


def test_a_prerequisite_must_come_earlier_in_the_list() -> None:
    checks = [make_check("second", passed(), prerequisites=["first"]), make_check("first", passed())]
    with pytest.raises(ValueError, match="first"):
        run_checks(checks, CheckContext(runner=FakeRunner()))


def test_a_failure_or_warning_without_a_next_step_is_refused() -> None:
    with pytest.raises(ValueError):
        failed([])
    with pytest.raises(ValueError):
        warned([])
