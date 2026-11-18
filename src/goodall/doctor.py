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

A healthy eval with secrets always has one unverified check, because the
field a secret reference names cannot be checked without reading the
secret. So when nothing failed or warned and only unverified checks
remain, the text report says "All checks passed." above the counts, and
why the rest were not checked (`closing_line`). The check keeps its
unverified status, in the report and in `--json`, so "not checked" is
never mistaken for "fine"; `--json` gets no closing line, because an agent
reads the counts.
Skips are not counted either, because a check is only ever skipped when
another check in the same report failed (goodall.checks), so a report with
a skip already exits 1.

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
adding a key does not raise it. Nor does adding a status (and its count),
because an agent should treat any status other than "pass" and "warn" as
needing attention, and so reads a new one correctly.
"""

from goodall import exit_codes
from goodall.checks import Check, CheckContext, run_checks
from goodall.claude_code_checks import CLAUDE_CODE_CHECKS
from goodall.program_checks import PROGRAM_CHECKS
from goodall.remote_checks import REMOTE_CHECKS
from goodall.runner import Runner
from goodall.secrets_checks import CHECKS_UNVERIFIABLE_WITHOUT_A_SECRET, SECRETS_CHECKS
from goodall.settings_checks import SETTINGS_CHECKS
from goodall.terminal import CheckResult, CheckStatus, Terminal, count_checks

# The layout of `goodall doctor --json`.
DOCTOR_JSON_SCHEMA_VERSION = 1

# Every check doctor runs, in order. The settings checks come first, because
# they say which eval this account belongs to; then the checks that run
# other programs (tools, the eval account, the record repo); then where the
# record repo is shared (its remote, gh's sign-in, its visibility); then the
# checks of the env file and the shared env file (secrets); last, Claude
# Code's hooks. A check is shown under its group's heading wherever it runs:
# gh's sign-in under "Tools", and the secrets manager's command, which waits
# for the env file, too.
DOCTOR_CHECKS: list[Check] = [
    *SETTINGS_CHECKS,
    *PROGRAM_CHECKS,
    *REMOTE_CHECKS,
    *SECRETS_CHECKS,
    *CLAUDE_CODE_CHECKS,
]


def run_doctor(terminal: Terminal, runner: Runner, as_json: bool, offline: bool = False) -> int:
    """Run every check, show the results, and return the exit code.

    `runner` is how the checks start other programs (goodall.runner).
    `offline` leaves out every question to GitHub: those checks are
    reported as unverified, which neither passes nor fails them.
    """
    results = run_checks(DOCTOR_CHECKS, CheckContext(runner=runner, offline=offline))
    if as_json:
        terminal.write_json(doctor_document(results))
    else:
        terminal.show_check_report(results, closing_line(results))

    if count_checks(results).failed > 0:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def closing_line(results: list[CheckResult]) -> str | None:
    """The sentence above the counts when nothing failed or warned but some checks are unverified, else None.

    It says the report is healthy, then why the rest were not checked:
    because checking would mean reading a secret, when that is the only
    reason, and otherwise only that they could not be checked (a secrets
    manager that did not answer is worth a second look). When every check
    passed, or anything failed or warned, the counts say it all.
    """
    counts = count_checks(results)
    if counts.failed > 0 or counts.warned > 0 or counts.unverified == 0:
        return None

    all_need_a_secret = True
    for result in results:
        if result.status == CheckStatus.UNVERIFIED and result.id not in CHECKS_UNVERIFIABLE_WITHOUT_A_SECRET:
            all_need_a_secret = False

    if counts.unverified == 1:
        things = "One thing goodall"
        verb = "is"
    else:
        things = f"{counts.unverified} things goodall"
        verb = "are"
    if all_need_a_secret:
        return f"All checks passed. {things} cannot check without reading a secret."
    return f"All checks passed. {things} could not check {verb} marked unverified."


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
