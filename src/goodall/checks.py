"""The machinery every diagnostic check runs through: what a check is, and how a list of them is run.

`goodall doctor` (goodall.doctor) builds one list of checks, in order, and
hands it to `run_checks`. Each check is a small function that looks at one
thing and returns a `CheckOutcome`; `run_checks` turns the outcomes into
`CheckResult` records (goodall.terminal), which the terminal report and the
`--json` document are both built from.

How dependencies work, and why:

- Each check names the checks it depends on, its `prerequisites`, by id.
  A prerequisite must come earlier in the list; the list's order is the
  order checks run in, so it can be read top to bottom.
- When a check fails, every check that depends on it is skipped instead of
  run, and its result names the failed check. A check that depends on a
  skipped check is skipped too, and names the same failed check. So one
  broken thing (missing local settings, say) shows as one failure, and the
  contributor is pointed straight at it.
- A warning skips nothing: a warning means goodall still works, in a
  reduced way, so whatever depends on it can still be checked. Nor does a
  check goodall could not verify (`could_not_verify`): not knowing is not a
  failure.
- A check function can only pass, fail, warn or say it could not verify
  (`passed`, `failed`, `warned`, `could_not_verify`). Skipping is decided
  here, never by a check, so a skip always
  means a failure elsewhere in the same report. That is why `goodall
  doctor`'s exit code can look at failures alone.

This is deliberately a list and a loop, not a plugin system: a new check is
a function plus one entry in a list.

Checks share what they learn through a `CheckContext`. A check that reads
local settings, for example, puts them there, and the checks that depend on
it read them back instead of reading the file again. That is safe because a
check runs only when every prerequisite passed or warned.

Checks only look. `goodall doctor` promises to change nothing on the
machine (AGENTS.md invariant 5), so no check may create, change or delete
a file.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from goodall.env_files import EnvEntry
from goodall.eval_config import EvalConfig
from goodall.local_settings import LocalSettings
from goodall.runner import Runner
from goodall.terminal import CheckResult, CheckStatus, Failure


@dataclass
class CheckContext:
    """What the checks have learned so far, for the checks that depend on them.

    `runner` is given when the context is made: it is how a check starts
    another program (goodall.runner), and a test gives a fake one.

    Every other field starts empty. A field is filled by the check that
    learns it, and read only by checks that list that check as a
    prerequisite.

    - `local_settings_reading` is the full answer from reading local
      settings: the settings, or every problem with them. One check decides
      whether the file was found, the next whether it was valid.
    - `local_settings` is set once local settings are known to be valid.
    - `eval_config` is set once the eval config is known to be valid.
    - `env_file_entries` is set once the env file is known to hold only
      secret references: its entries, or an empty list when there is no
      env file.
    - `shared_env_file_entries` is the same for the shared env file, once
      it is known to hold no secrets.
    """

    runner: Runner
    local_settings_reading: LocalSettings | list[Failure] | None = None
    local_settings: LocalSettings | None = None
    eval_config: EvalConfig | None = None
    env_file_entries: list[EnvEntry] | None = None
    shared_env_file_entries: list[EnvEntry] | None = None


@dataclass(frozen=True)
class CheckOutcome:
    """What one check function found: a status, and what is wrong when something is.

    Make one with `passed`, `failed`, `warned` or `could_not_verify` rather
    than directly, so anything but a pass always carries at least one next
    step, and only a pass carries a note.
    """

    status: CheckStatus
    failures: list[Failure] = field(default_factory=list)
    note: str | None = None


def passed(note: str | None = None) -> CheckOutcome:
    """The outcome of a check that found what it looked for.

    `note` is for a pass a person should still know about, such as "there
    is no env file, which is fine if this eval uses no secrets"; it is
    shown beneath the check. Anything that needs acting on is a warning
    instead.
    """
    return CheckOutcome(status=CheckStatus.PASS, note=note)


def failed(failures: list[Failure]) -> CheckOutcome:
    """The outcome of a check that found something goodall cannot work with.

    Raises ValueError when `failures` is empty, because every failed check
    must tell the contributor what to do next.
    """
    if not failures:
        raise ValueError("A failed check must say what is wrong and what to do next.")
    return CheckOutcome(status=CheckStatus.FAIL, failures=failures)


def warned(failures: list[Failure]) -> CheckOutcome:
    """The outcome of a check that found a problem goodall can work around, such as "not recording".

    Raises ValueError when `failures` is empty, for the same reason as `failed`.
    """
    if not failures:
        raise ValueError("A check that warns must say what is wrong and what to do next.")
    return CheckOutcome(status=CheckStatus.WARN, failures=failures)


def could_not_verify(failures: list[Failure]) -> CheckOutcome:
    """The outcome of a check that could not find out the answer, such as when finding out would mean reading a secret.

    It is neither a pass nor a failure, so "not checked" is never mistaken
    for "fine". Each of `failures` says what could not be checked, why, and
    how the contributor can check it themselves. Raises ValueError when
    `failures` is empty, for the same reason as `failed`.
    """
    if not failures:
        raise ValueError("A check that could not verify must say what it could not check and what to do next.")
    return CheckOutcome(status=CheckStatus.UNVERIFIED, failures=failures)


# A check function looks at one thing, using what earlier checks put in the
# context, and returns what it found.
CheckFunction = Callable[[CheckContext], CheckOutcome]


@dataclass(frozen=True)
class Check:
    """One diagnostic check.

    - `id` is stable and dotted, group first, such as
      "settings.local_settings_valid". Agents branch on it, so it is never
      renamed once released.
    - `group` is the heading the check appears under in the report.
    - `summary` is the one line a person reads, written as the statement the
      check confirms, such as "Local settings are valid". The status word in
      front of it says whether it held.
    - `prerequisites` are the ids of the checks this one depends on, all of
      which must come earlier in the list.
    - `run` is the check function.
    """

    id: str
    group: str
    summary: str
    prerequisites: list[str]
    run: CheckFunction


def run_checks(checks: list[Check], context: CheckContext) -> list[CheckResult]:
    """Run `checks` in order and return one result for each, in the same order.

    A check whose prerequisite failed, or was itself skipped, is not run; its
    result is SKIP, naming the check that failed. Raises ValueError when a
    check names a prerequisite that has not run before it, which is a
    mistake in goodall's own list of checks, not in the contributor's setup.
    """
    results_by_id: dict[str, CheckResult] = {}
    results: list[CheckResult] = []
    for check in checks:
        failed_prerequisite = find_failed_prerequisite(check, results_by_id)
        if failed_prerequisite is None:
            outcome = check.run(context)
            result = CheckResult(
                id=check.id,
                group=check.group,
                status=outcome.status,
                summary=check.summary,
                failures=outcome.failures,
                note=outcome.note,
            )
        else:
            result = CheckResult(
                id=check.id,
                group=check.group,
                status=CheckStatus.SKIP,
                summary=check.summary,
                waiting_for=failed_prerequisite,
            )
        results_by_id[check.id] = result
        results.append(result)
    return results


def find_failed_prerequisite(check: Check, results_by_id: dict[str, CheckResult]) -> str | None:
    """Return the id of the failed check that stops `check` running, or None if it can run.

    For a prerequisite that was itself skipped, this is the check that
    prerequisite was waiting for, so every skip names the real cause.
    """
    for prerequisite_id in check.prerequisites:
        if prerequisite_id not in results_by_id:
            raise ValueError(
                f"The check {check.id} depends on {prerequisite_id}, which must come before it in the list of checks."
            )
        prerequisite_result = results_by_id[prerequisite_id]
        if prerequisite_result.status == CheckStatus.FAIL:
            return prerequisite_id
        if prerequisite_result.status == CheckStatus.SKIP:
            return prerequisite_result.waiting_for
    return None
