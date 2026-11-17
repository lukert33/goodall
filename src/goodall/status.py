"""`goodall status`: tells a contributor whether a work block is active.

Called by `cli.main` when someone runs `goodall status` or
`goodall status --json`. Agents use the exit code and the JSON document to
decide whether to proceed, so both are part of goodall's stable contract.

What is built so far, in the order goodall checks:

1. Local settings are found, read and checked (goodall.local_settings).
   Everything wrong with the file is reported at once.
2. The record repo folder exists, and the log directory can be written to.
   An unreachable log directory is reported as "not recording" (ADR 0008):
   when goodall cannot write raw records, no work block can be active, so
   "cannot write records" and "not recording" are the same observable state.
3. The eval config is read from the record repo and checked, and local
   settings are checked against it: this account's contributor must be in
   the roster, and its main workstream one of the eval's workstreams
   (goodall.eval_config).
4. With everything in order, goodall reports the eval, the contributor and
   that no work block is active. Work blocks are not built yet, so that is
   the only answer possible, and the exit code is 1: 0 is reserved for an
   active work block.

Each result is built once as records and then shown either as text (results
on standard output, failures on standard error) or as one JSON document on
standard output, so the two renderings cannot drift apart. The JSON document
always has the keys `work_block_active`, `eval`, `contributor` and
`failures`; `eval` and `contributor` are null whenever `failures` is not
empty.

The folder checks themselves live in goodall.folders, because
`goodall doctor` asks the same questions and must word the answers the same
way.
"""

from goodall import exit_codes
from goodall.eval_config import EvalForThisAccount, read_eval_for_this_account
from goodall.folders import describe_folder_problem, log_directory_unreachable_failure, record_repo_missing_failure
from goodall.local_settings import read_local_settings
from goodall.terminal import Failure, Terminal


def run_status(terminal: Terminal, as_json: bool) -> int:
    """Report the state of this macOS user account's eval and return the exit code."""
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        return report_failures(terminal, settings_reading, as_json)
    settings = settings_reading

    # The two folders do not depend on each other, so both are checked and
    # both problems shown at once.
    folder_failures: list[Failure] = []
    record_repo_problem = describe_folder_problem(settings.record_repo.path, needs_writing=False)
    if record_repo_problem is not None:
        folder_failures.append(record_repo_missing_failure(settings, record_repo_problem))
    log_directory_problem = describe_folder_problem(settings.log_directory.path, needs_writing=True)
    if log_directory_problem is not None:
        folder_failures.append(log_directory_unreachable_failure(settings, log_directory_problem))
    if folder_failures:
        return report_failures(terminal, folder_failures, as_json)

    eval_reading = read_eval_for_this_account(settings)
    if isinstance(eval_reading, list):
        return report_failures(terminal, eval_reading, as_json)
    return report_no_work_block(terminal, eval_reading, as_json)


def report_no_work_block(terminal: Terminal, eval_for_this_account: EvalForThisAccount, as_json: bool) -> int:
    """Report that everything is in order and no work block is active.

    Returns 1, not 0: `goodall status` exits 0 only while a work block is
    active, so an agent can branch on the exit code alone. No work block can
    exist yet, so this is the best answer goodall can give today.
    """
    contributor = eval_for_this_account.contributor
    main_workstream = eval_for_this_account.local_settings.main_workstream
    if as_json:
        terminal.write_json(
            {
                "work_block_active": False,
                "eval": {"name": eval_for_this_account.eval_config.name},
                "contributor": {
                    "id": contributor.id,
                    "display_name": contributor.display_name,
                    "main_workstream": main_workstream,
                },
                "failures": [],
            }
        )
    else:
        terminal.show_facts(
            [
                ("Eval", eval_for_this_account.eval_config.name),
                ("Contributor", f"{contributor.display_name} ({contributor.id})"),
                ("Workstream", f"{main_workstream}, your main workstream"),
            ]
        )
        terminal.show_paragraph("No work block is active, so nothing is being recorded.")
    return exit_codes.ERROR


def report_failures(terminal: Terminal, failures: list[Failure], as_json: bool) -> int:
    """Show `failures` as text or as JSON, and return the error exit code.

    The JSON document has the same keys whatever went wrong. No work block
    can be active while goodall is failing, so `work_block_active` is false,
    and `eval` and `contributor` are null because goodall has not confirmed
    them.
    """
    if as_json:
        failure_documents = []
        for failure in failures:
            failure_documents.append(failure.as_json())
        terminal.write_json(
            {"work_block_active": False, "eval": None, "contributor": None, "failures": failure_documents}
        )
    else:
        terminal.show_failures(failures)
    return exit_codes.ERROR
