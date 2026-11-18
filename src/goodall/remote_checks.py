"""The diagnostic checks of where the record repo is shared: its remote, gh's sign-in to GitHub, and its visibility.

`goodall doctor` runs `REMOTE_CHECKS` after the program checks
(goodall.doctor). Three checks, in order:

- "The record repo is shared through a remote" (Record repo): git names a
  remote the records are pushed to. Learns which host that remote is on
  (goodall.remote_addresses) and keeps it in the check context.
- "gh is signed in to GitHub" (Tools): asked only when that remote is on
  GitHub. Checks the token is accepted, and that it carries the `repo`
  scope gh needs to see a private repository.
- "The shared record repo is not public" (Record repo): asks gh for the
  repository's visibility, again only on GitHub.

Why these are warnings rather than failures:

- No remote means the records stay on this Mac and reach nobody else, but
  every record is still safe in a local commit and goodall still records,
  so goodall works in a reduced way: the definition of a warning. A
  rehearsal or a one-person eval may keep its records local on purpose.
- A public record repo is the researcher's choice to make (AGENTS.md
  invariant 6: people administer git hosting, goodall never changes it),
  and publishing the record is the point of an eval. It warns because
  every record is public the moment it is pushed, before any redaction.
- A token without the `repo` scope still signs gh in; it only stops gh
  seeing a private record repo, which the visibility check then reports.

A token GitHub refuses, or gh not signed in at all, is a failure: the owner
decided gh stays a requirement (2026-11-17), and a broken sign-in is fixed
in one command.

How gh is asked, and why (ADR 0011: goodall makes no network calls of its
own, so GitHub is reached only through gh, through the runner):

- Two questions only, both read-only: `gh auth status ... --no-refresh`
  and `gh repo view ... --json visibility`. Trap: without `--no-refresh`
  gh renews an expiring token, which changes a stored credential, and
  doctor promises to change nothing (AGENTS.md invariant 5).
  `GH_NO_UPDATE_NOTIFIER` stops gh writing its update-check file, and
  `GH_PROMPT_DISABLED` stops it waiting for an answer nobody can give.
- gh's output is read for a few fixed fields and never shown. Its error
  text, its account name and anything else it prints are left out of every
  message, because gh can echo a token or a person's login. A message
  describes what happened in goodall's own words instead.
- A short timeout, `GH_TIMEOUT_SECONDS`, so a Mac with the network down
  finishes doctor quickly. When the sign-in check cannot reach GitHub, the
  visibility check does not try again.
- `gh auth status --json` exits 0 whatever it finds, and reports a token
  GitHub refused and a GitHub it could not reach both as state "error".
  They are told apart by the HTTP status 401 in gh's error text: refused
  is a failure with a next step, not reached is unverified, because the
  Mac may simply be offline. A gh too old to know `--json` is unverified
  too, with the command to run by hand.
- `goodall doctor --offline` asks GitHub nothing, and reports both GitHub
  checks as unverified.
"""

import json

from goodall.checks import Check, CheckContext, CheckOutcome, could_not_verify, failed, passed, warned
from goodall.program_checks import GH_INSTALLED, RECORD_REPO_GROUP, RECORD_REPO_IS_GIT_REPOSITORY, TOOLS_GROUP
from goodall.record_repo import find_push_target, run_git
from goodall.remote_addresses import RecordRepoRemote, RemoteHost, describe_remote_address
from goodall.runner import Command, CommandOutcome, CommandResult, OutputMode
from goodall.settings_checks import valid_local_settings
from goodall.terminal import Failure

RECORD_REPO_HAS_REMOTE = "record_repo.has_remote"
GH_SIGNED_IN = "tools.gh_signed_in"
RECORD_REPO_NOT_PUBLIC = "record_repo.not_public"

# How long each gh command may take. Short, because with the network down
# each one waits this long before goodall gives up on it.
GH_TIMEOUT_SECONDS = 5.0

# Set for every gh command: no update check (it writes a file) and no prompts.
GH_ENVIRONMENT = {"GH_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1"}

# Whether gh is signed in to github.com, for the account it would use.
GH_AUTH_STATUS_COMMAND = [
    "gh",
    "auth",
    "status",
    "--hostname",
    "github.com",
    "--active",
    "--json",
    "hosts",
    "--no-refresh",
]

# The scope a classic GitHub token needs to see a private repository.
REPOSITORY_SCOPE = "repo"


def gh_repo_view_command(github_repository: str) -> list[str]:
    """The gh command that asks whether `github_repository` (owner/name) is public, private or internal."""
    return ["gh", "repo", "view", f"github.com/{github_repository}", "--json", "visibility"]


def run_gh(context: CheckContext, arguments: list[str]) -> CommandResult:
    """Run one gh command through the runner, with goodall's short timeout and no prompts."""
    command = Command(
        arguments,
        output=OutputMode.CAPTURE,
        timeout_seconds=GH_TIMEOUT_SECONDS,
        extra_environment=GH_ENVIRONMENT,
    )
    return context.runner.run(command)


def check_record_repo_has_remote(context: CheckContext) -> CheckOutcome:
    """The record repo has a remote, and goodall can tell which one records are pushed to.

    Asks git the same way `goodall journal` and the work-block commands do
    when they push (goodall.record_repo.find_push_target), so doctor and
    those commands agree on where records go. Only reads git's settings.
    """
    record_repo = valid_local_settings(context).record_repo.path
    push_target = find_push_target(context.runner, record_repo)
    if push_target is None:
        context.record_repo_remote = RecordRepoRemote(host=RemoteHost.NONE)
        return warned([record_repo_has_no_remote_failure(context)])
    if isinstance(push_target, Failure):
        context.record_repo_remote = RecordRepoRemote(host=RemoteHost.UNCLEAR)
        return warned([push_destination_unknown_failure(context, push_target.kind)])

    address_result = run_git(context.runner, record_repo, ["remote", "get-url", "--push", push_target.remote])
    if not address_result.succeeded:
        context.record_repo_remote = RecordRepoRemote(host=RemoteHost.UNCLEAR)
        return warned([push_destination_unknown_failure(context, "record_repo_remote_unreadable")])
    context.record_repo_remote = describe_remote_address(address_result.standard_output.strip())
    return passed()


def record_repo_has_no_remote_failure(context: CheckContext) -> Failure:
    """The warning for a record repo with no remote, whose records reach nobody else."""
    record_repo = valid_local_settings(context).record_repo
    return Failure(
        kind="record_repo_has_no_remote",
        what_happened=(
            f"The record repo {record_repo.as_written} is not connected to a shared record repo, so its records "
            "are only on this Mac."
        ),
        why=(
            "The researcher and the other contributors see records only once goodall pushes them to the shared "
            "record repo. Every record is still kept here, in a local commit."
        ),
        what_to_do_next=(
            "Ask the researcher who runs the eval for the address of the shared record repo, then connect it: "
            f"in {record_repo.path}, run git remote add origin ADDRESS, with ADDRESS that address. Goodall sends "
            "the waiting records with the next one it writes. If this eval keeps its records on this Mac on "
            "purpose, nothing. Then run goodall doctor again."
        ),
    )


def push_destination_unknown_failure(context: CheckContext, kind: str) -> Failure:
    """The warning for a record repo whose remote goodall cannot pick out, so it cannot push."""
    record_repo = valid_local_settings(context).record_repo
    return Failure(
        kind=kind,
        what_happened=(
            f"Goodall cannot tell which shared record repo the record repo {record_repo.as_written} sends its "
            "records to, so new records stay on this Mac."
        ),
        why=(
            "Goodall sends records where the record repo's own git settings say, and never guesses or changes "
            "those settings itself."
        ),
        what_to_do_next=(
            f"Run git status and git remote -v in {record_repo.path} and show the output to the researcher who "
            "runs the eval: the record repo should be on a branch that follows one remote. Then run goodall "
            "doctor again."
        ),
    )


def known_remote(context: CheckContext) -> RecordRepoRemote:
    """The record repo's remote, as the remote check found it.

    Trap: missing only when a check lists the wrong prerequisites, a
    mistake in goodall, so this raises rather than reporting it.
    """
    if context.record_repo_remote is None:
        raise RuntimeError(f"A check that needs the record repo's remote must list {RECORD_REPO_HAS_REMOTE}.")
    return context.record_repo_remote


def check_gh_signed_in(context: CheckContext) -> CheckOutcome:
    """gh is signed in to github.com with a token GitHub accepts, when the record repo is on GitHub."""
    remote = known_remote(context)
    if remote.host == RemoteHost.NONE:
        return passed(note="The record repo has no remote, so goodall has nothing to ask GitHub.")
    if remote.host == RemoteHost.ELSEWHERE:
        return passed(note="The record repo is shared somewhere not on GitHub, so gh does not need to be signed in.")
    if remote.host == RemoteHost.UNCLEAR:
        return could_not_verify([remote_unclear_failure("whether gh needs to be signed in to GitHub")])
    if context.offline:
        return could_not_verify([offline_failure("Whether gh is signed in to GitHub")])

    result = run_gh(context, GH_AUTH_STATUS_COMMAND)
    if not result.succeeded:
        return could_not_verify([gh_sign_in_unanswered_failure(result)])
    account = read_active_github_account(result.standard_output)
    if account is None:
        return could_not_verify([gh_sign_in_unanswered_failure(result)])
    if account == {}:
        return failed([gh_not_signed_in_failure()])

    state = account.get("state")
    error = account.get("error")
    if state == "success":
        context.github_signed_in = True
        return outcome_for_scopes(account.get("scopes"))
    # Trap: gh reports a refused token and an unreachable GitHub with the
    # same state, "error"; only the 401 in its error text tells them apart.
    if state == "error" and isinstance(error, str) and "401" in error:
        return failed([gh_token_refused_failure()])
    return could_not_verify([github_not_reached_failure()])


def read_active_github_account(gh_output: str) -> dict[str, object] | None:
    """The account gh would use on github.com, from `gh auth status --json hosts`.

    Returns an empty dictionary when gh is signed in to no account there,
    and None when the answer cannot be read, which is a gh that changed
    its answer's shape: not knowing, never a pass.
    """
    try:
        document = json.loads(gh_output)
    except ValueError:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("hosts"), dict):
        return None
    accounts = document["hosts"].get("github.com")
    if accounts is None or accounts == []:
        return {}
    if not isinstance(accounts, list):
        return None
    for account in accounts:
        if isinstance(account, dict) and account.get("active") is True:
            return account
    first_account = accounts[0]
    if not isinstance(first_account, dict):
        return None
    return first_account


def outcome_for_scopes(scopes: object) -> CheckOutcome:
    """Pass when gh's token can see a private repository, warn when it lacks the `repo` scope.

    gh lists scopes only for a classic token or its own sign-in. A
    fine-grained token, or one given in GH_TOKEN, lists none, and its
    access cannot be read from here, so that passes with a note; the
    visibility check finds out whether it can see the record repo.
    """
    if not isinstance(scopes, str) or scopes.strip() == "":
        return passed(
            note=(
                "gh's token lists no scopes (a fine-grained token, for one, has none to list), so goodall cannot "
                "tell from here whether it may see a private record repo. The next check finds out."
            )
        )
    scope_names = []
    for scope in scopes.split(","):
        scope_names.append(scope.strip())
    if REPOSITORY_SCOPE not in scope_names:
        return warned([gh_token_missing_repo_scope_failure()])
    return passed()


def gh_not_signed_in_failure() -> Failure:
    """The failure for gh signed in to no account on github.com."""
    return Failure(
        kind="gh_not_signed_in",
        what_happened="gh is not signed in to GitHub in this macOS user account.",
        why="The record repo is shared on GitHub, and goodall asks GitHub about it only through gh.",
        what_to_do_next=(
            "Run gh auth login in Terminal in this eval account and follow its steps, choosing GitHub.com. "
            "Then run goodall doctor again."
        ),
    )


def gh_token_refused_failure() -> Failure:
    """The failure for a token GitHub no longer accepts: expired, revoked or mistyped."""
    return Failure(
        kind="gh_token_refused",
        what_happened="GitHub refused the token gh signs in with: it has expired or been withdrawn.",
        why="The record repo is shared on GitHub, and goodall asks GitHub about it only through gh.",
        what_to_do_next=(
            "Run gh auth login in Terminal in this eval account and follow its steps, choosing GitHub.com, to "
            "sign gh in again. If the token comes from the GH_TOKEN or GITHUB_TOKEN environment variable, ask "
            "whoever set it for a new one instead. Then run goodall doctor again."
        ),
    )


def gh_token_missing_repo_scope_failure() -> Failure:
    """The warning for a token without the `repo` scope, which cannot see a private repository."""
    return Failure(
        kind="gh_token_missing_repo_scope",
        what_happened=(
            "gh is signed in to GitHub, but its token lacks the repo scope, so gh cannot see a private record repo."
        ),
        why="Goodall asks gh whether the record repo is public; without the repo scope, gh cannot look.",
        what_to_do_next=(
            "Run gh auth refresh --scopes repo in Terminal in this eval account and follow its steps. Then run "
            "goodall doctor again."
        ),
    )


def gh_sign_in_unanswered_failure(result: CommandResult) -> Failure:
    """The unverified result for a gh that gave no answer goodall can read.

    Worded from the outcome alone, never from what gh printed (see the
    module docstring).
    """
    return Failure(
        kind="gh_sign_in_unanswered",
        what_happened=f"Goodall could not find out whether gh is signed in to GitHub: {gh_outcome_described(result)}.",
        why="The record repo is shared on GitHub, and goodall asks GitHub about it only through gh.",
        what_to_do_next=(
            "Run gh auth status in Terminal in this eval account: it should say it is logged in to github.com. "
            "If gh does not know a command, ask an administrator macOS user account on this Mac to update gh "
            "from https://cli.github.com. Then run goodall doctor again."
        ),
    )


def gh_outcome_described(result: CommandResult) -> str:
    """What happened when gh ran, in goodall's words: never gh's own, which may hold a token."""
    if result.outcome == CommandOutcome.TIMED_OUT:
        return f"gh did not answer within {GH_TIMEOUT_SECONDS:g} seconds"
    if result.outcome == CommandOutcome.NOT_FOUND:
        return "gh was not found"
    if result.outcome == CommandOutcome.COULD_NOT_START:
        return "gh could not be started"
    if result.succeeded:
        return "gh's answer was not in a form goodall can read"
    return f"gh stopped with exit code {result.exit_code}"


def github_not_reached_failure() -> Failure:
    """The unverified result for a gh that could not reach GitHub."""
    return Failure(
        kind="github_not_reached",
        what_happened="gh could not reach GitHub, so goodall could not check that gh is still signed in.",
        why="The record repo is shared on GitHub, and goodall asks GitHub about it only through gh.",
        what_to_do_next="Check that this Mac is online, then run goodall doctor again.",
    )


def remote_unclear_failure(what: str) -> Failure:
    """The unverified result for a check that needs to know where the record repo is shared, and cannot tell."""
    return Failure(
        kind="record_repo_remote_unclear",
        what_happened=f"Goodall could not check {what}, because it cannot tell which repository the records go to.",
        why="Goodall checks sign-in and visibility only for a record repo shared on GitHub.",
        what_to_do_next=(
            "Fix where the record repo sends its records first (see the record repo checks above), then run "
            "goodall doctor again."
        ),
    )


def offline_failure(what: str) -> Failure:
    """The unverified result for a check left out because doctor was run with --offline."""
    return Failure(
        kind="offline",
        what_happened=(
            f"{what} was not checked, because goodall doctor was run with --offline and checking means gh asking "
            "GitHub over the network."
        ),
        why="With --offline, goodall doctor asks nothing over the network.",
        what_to_do_next="Run goodall doctor without --offline when this Mac is online.",
    )


def check_record_repo_not_public(context: CheckContext) -> CheckOutcome:
    """The record repo, when it is on GitHub, is not public."""
    remote = known_remote(context)
    if remote.host == RemoteHost.NONE:
        return passed()
    if remote.host == RemoteHost.ELSEWHERE:
        return passed(
            note=(
                "The record repo is shared somewhere not on GitHub, where goodall cannot see who may read it. "
                "Check that yourself with whoever hosts it."
            )
        )
    if remote.host == RemoteHost.UNCLEAR or remote.github_repository is None:
        return could_not_verify([remote_unclear_failure("whether the shared record repo is public")])
    if context.offline:
        return could_not_verify([offline_failure("Whether the shared record repo is public")])
    if not context.github_signed_in:
        return could_not_verify([visibility_unanswered_failure(remote.github_repository)])

    result = run_gh(context, gh_repo_view_command(remote.github_repository))
    visibility = read_visibility(result)
    if visibility == "PUBLIC":
        return warned([record_repo_public_failure()])
    if visibility in ["PRIVATE", "INTERNAL"]:
        return passed()
    return could_not_verify([visibility_unanswered_failure(remote.github_repository)])


def read_visibility(result: CommandResult) -> str | None:
    """The visibility gh named (PUBLIC, PRIVATE or INTERNAL), or None when it gave no readable answer."""
    if not result.succeeded:
        return None
    try:
        document = json.loads(result.standard_output)
    except ValueError:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("visibility"), str):
        return None
    visibility: str = document["visibility"]
    return visibility.upper()


def record_repo_public_failure() -> Failure:
    """The warning for a record repo anyone on the internet can read."""
    return Failure(
        kind="record_repo_public",
        what_happened=(
            "The shared record repo is public on GitHub: anyone can read every record as soon as it is pushed, "
            "before anything in it is redacted."
        ),
        why=(
            "Records hold contributors' journal entries and details of this Mac. Who may see them is the "
            "researcher's decision; goodall never changes a repository's visibility."
        ),
        what_to_do_next=(
            "If the eval is meant to be public from the start, nothing. Otherwise, ask the researcher who runs "
            "the eval to make the repository private in its settings on GitHub. Then run goodall doctor again."
        ),
    )


def visibility_unanswered_failure(github_repository: str) -> Failure:
    """The unverified result for a visibility gh could not tell goodall.

    `github_repository` is only owner/name (goodall.remote_addresses), so
    it holds no token and is safe to show.
    """
    return Failure(
        kind="record_repo_visibility_unanswered",
        what_happened="Goodall could not find out from gh whether the shared record repo is public.",
        why="Goodall asks GitHub about the record repo only through gh.",
        what_to_do_next=(
            f"Run gh repo view {github_repository} in Terminal in this eval account, or open the repository on "
            "GitHub, and look for the word Public or Private beside its name. If it says Public and should not, "
            "ask the researcher who runs the eval. Then run goodall doctor again."
        ),
    )


REMOTE_CHECKS = [
    Check(
        id=RECORD_REPO_HAS_REMOTE,
        group=RECORD_REPO_GROUP,
        summary="The record repo is shared through a remote",
        prerequisites=[RECORD_REPO_IS_GIT_REPOSITORY],
        run=check_record_repo_has_remote,
    ),
    Check(
        id=GH_SIGNED_IN,
        group=TOOLS_GROUP,
        summary="gh is signed in to GitHub",
        prerequisites=[GH_INSTALLED, RECORD_REPO_HAS_REMOTE],
        run=check_gh_signed_in,
    ),
    Check(
        id=RECORD_REPO_NOT_PUBLIC,
        group=RECORD_REPO_GROUP,
        summary="The shared record repo is not public",
        prerequisites=[RECORD_REPO_HAS_REMOTE, GH_SIGNED_IN],
        run=check_record_repo_not_public,
    ),
]
