"""`goodall doctor`: checks this macOS user account and its eval setup, and changes nothing.

Called by `cli.main` when someone runs `goodall doctor` or
`goodall doctor --json`. It runs every check in `DOCTOR_CHECKS`, in order
(goodall.checks explains how a failed check skips the ones that depend on
it), and shows the results either as a grouped report or as one JSON
document. Both are built from the same list of `CheckResult` records, so
they cannot disagree.

Exit code: 1 when any check failed, otherwise 0. Warnings do not fail it: a
warning means goodall works, in a reduced way. Nor does a check goodall
could not verify: it is reported as unverified, which is neither a pass nor
a failure, and the report says what the contributor can check by hand.
Skips are not counted
either, because a check is only ever skipped when another check in the same
report failed (goodall.checks), so a report with a skip already exits 1.

Doctor is strictly read-only (AGENTS.md invariant 5): it creates, changes
and deletes nothing, so a contributor can always run it first when
something seems wrong.

The JSON document, whose shape agents rely on:

    {
      "schema_version": 1,
      "summary": {"passed": 7, "warned": 0, "failed": 0, "skipped": 0, "unverified": 0},
      "checks": [CheckResult.as_json(), ...]
    }

`schema_version` is raised whenever a key changes meaning or disappears;
adding a key does not raise it. Adding the status "unverified" (and its
count) did not raise it either: an agent that treats any status other than
"pass" and "warn" as needing attention reads it correctly.
"""

from goodall import exit_codes
from goodall.checks import Check, CheckContext, run_checks
from goodall.program_checks import PROGRAM_CHECKS
from goodall.runner import Runner
from goodall.secrets_checks import SECRETS_CHECKS
from goodall.settings_checks import SETTINGS_CHECKS
from goodall.terminal import CheckResult, Terminal, count_checks

# The layout of `goodall doctor --json`.
DOCTOR_JSON_SCHEMA_VERSION = 1

# Every check doctor runs, in order. The settings checks come first, because
# they say which eval this account belongs to; then the checks that run
# other programs (tools, the eval account, the record repo); then the checks
# of the env file and the shared env file (secrets).
DOCTOR_CHECKS: list[Check] = [*SETTINGS_CHECKS, *PROGRAM_CHECKS, *SECRETS_CHECKS]


def run_doctor(terminal: Terminal, runner: Runner, as_json: bool) -> int:
    """Run every check, show the results, and return the exit code.

    `runner` is how the checks start other programs (goodall.runner).
    """
    results = run_checks(DOCTOR_CHECKS, CheckContext(runner=runner))
    if as_json:
        terminal.write_json(doctor_document(results))
    else:
        terminal.show_check_report(results)

    if count_checks(results).failed > 0:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def doctor_document(results: list[CheckResult]) -> dict[str, object]:
    """Build the `--json` document from the same results the report shows."""
    counts = count_checks(results)
    check_documents = []
    for result in results:
        check_documents.append(result.as_json())
    return {
        "schema_version": DOCTOR_JSON_SCHEMA_VERSION,
        "summary": {
            "passed": counts.passed,
            "warned": counts.warned,
            "failed": counts.failed,
            "skipped": counts.skipped,
            "unverified": counts.unverified,
        },
        "checks": check_documents,
    }
