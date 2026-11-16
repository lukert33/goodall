"""The secrets manager module: everything goodall knows about the secrets manager that local settings name.

ADR 0011 makes this the one purpose-built module over the runner: adding a
second secrets manager should mean changing this module and nothing else.
Every public function takes the secrets manager's name, as local settings
write it, and answers for that manager; callers never build a secrets
manager command line themselves. Today it answers:

- is a value a well-formed secret reference? (`is_well_formed_secret_reference`,
  which runs nothing). `goodall doctor` asks it of every value in the env
  file (goodall.secrets_checks), and the setup page will apply the same rule
  (ADR 0012).
- is the secrets manager signed in and usable in this macOS user account?
  (`check_signed_in`)

Launching a program with secrets in its environment will be added here too.

The rule every question that runs the secrets manager keeps: no secret
value may ever enter goodall's process (ADR 0010). An answer that is in the
exit code alone is asked with the runner's discard output mode, so nothing
the secrets manager prints reaches goodall at all.

1Password, the one secrets manager today (ADR 0006), is asked through its
command, op. `op whoami` "Returns the currently active account or service
account" and "returns an error if no accounts are currently authenticated"
(https://www.1password.dev/cli/reference/commands/whoami/, read
2026-11-16). It prints who is signed in, never a secret, and goodall
discards even that. 1Password's staff recommend it for checking a session
without setting off a Touch ID prompt, which matters because doctor must
not ask a person anything.

Why only the form is checked, and why strictly: every value in the env file
must be a reference (ADR 0010), so anything that is not one is treated as a
possible raw secret pasted by mistake. A strict form costs a contributor
with an unusual item name one extra step (using the item's id, which
1Password's own syntax asks for anyway), and in return a pasted key can
never pass for a reference.

1Password's form follows its documented secret reference syntax
(https://www.1password.dev/cli/secret-reference-syntax/, read 2026-11-16):

    op://vault/item/field
    op://vault/item/section/field

- Each of vault, item, section and field is a name or an id. A name may hold
  only letters a to z and A to Z, digits, hyphens, underscores, periods and
  spaces; 1Password says a name with any other character must be replaced by
  its id, which is letters and digits.
- Goodall also requires that no part is empty or starts or ends with a
  space, because a stray space is a typo, not a name.
- Goodall refuses the query parameters 1Password allows after the field
  (`?attribute=otp`, `?ssh-format=openssh`), because none of the secrets
  goodall holds references for (model API keys) needs them. Allow them here
  when one does.
- The scheme must be written `op://` in lower case, as 1Password writes it.

This module imports only the standard library, because the code that
launches programs will use it, as the hook path may.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from goodall.runner import Command, CommandOutcome, OutputMode, RequiredProgram, Runner

# One part of a 1Password reference: allowed characters only, and no space
# at either end. A single character is allowed, since a vault can be called "a".
ONE_PASSWORD_REFERENCE_PART = r"[A-Za-z0-9_.\-](?:[A-Za-z0-9_.\- ]*[A-Za-z0-9_.\-])?"

# A whole 1Password reference: vault, item, optional section, field.
ONE_PASSWORD_REFERENCE_PATTERN = re.compile(
    rf"op://{ONE_PASSWORD_REFERENCE_PART}/{ONE_PASSWORD_REFERENCE_PART}"
    rf"(?:/{ONE_PASSWORD_REFERENCE_PART})?/{ONE_PASSWORD_REFERENCE_PART}"
)

# Each secrets manager's name as a person writes it, for messages; local
# settings name it in lower case.
DISPLAY_NAMES = {
    "1password": "1Password",
}

# What a well-formed reference looks like, for each secrets manager, said so
# a person can copy it. Messages show these instead of the value written,
# which is never repeated. The example is the agreed one from goodall-xes.3.
REFERENCE_FORMS = {
    "1password": (
        "op://vault/item/field, or op://vault/item/section/field, for example "
        "op://Eval/anthropic-engineering/credential"
    ),
}

# Where a contributor finds a secret's reference in each secrets manager.
WHERE_TO_FIND_A_REFERENCE = {
    "1password": "In the 1Password app, open the item and choose Copy Secret Reference from the field's menu",
}

# The command of each secrets manager local settings can name, and what to
# tell a contributor when it is missing. Local settings accept only these
# (goodall.local_settings), so a lookup cannot miss.
SECRETS_MANAGER_PROGRAMS = {
    "1password": RequiredProgram(
        name="op",
        described="op, 1Password's command-line tool,",
        why=(
            "This eval's secrets are kept in 1Password, and goodall reaches them only through op: op checks "
            "that each secret reference points at a real item, and launches programs with the secrets they need."
        ),
        how_to_install="following https://developer.1password.com/docs/cli/get-started/",
    ),
}

# How a contributor signs in to each secrets manager, as a next step.
SIGN_IN_STEPS = {
    "1password": (
        "Open the 1Password app and unlock it. If you use the app to sign in op (Settings, then Developer, then "
        "Integrate with 1Password CLI), run op signin in Terminal and approve the request; otherwise run "
        "eval $(op signin) in Terminal and run goodall doctor from that same window. If it still fails, run "
        "op whoami in Terminal to see what op says"
    ),
}

# The command that answers "is 1Password signed in?" by its exit code alone.
ONE_PASSWORD_SIGNED_IN_COMMAND = ["op", "whoami"]


class SignInState(StrEnum):
    """Whether the secrets manager can be used in this macOS user account right now."""

    SIGNED_IN = "signed_in"
    # It answered, and said it is not signed in.
    SIGNED_OUT = "signed_out"
    # It did not answer at all: it timed out, or could not be started.
    NO_ANSWER = "no_answer"


@dataclass(frozen=True)
class SignInAnswer:
    """The secrets manager's answer to "are you signed in?".

    `problem` is set when the state is not SIGNED_IN: a one-line description
    of what happened, such as "`op whoami` timed out after 10 s", safe to
    show, because it holds nothing the secrets manager printed.
    """

    state: SignInState
    problem: str | None = None


def check_signed_in(secrets_manager: str, runner: Runner) -> SignInAnswer:
    """Ask `secrets_manager` whether it is signed in, without bringing any of its output into goodall.

    Raises ValueError for a secrets manager this module does not know,
    because local settings accept only the ones it does, so that would be a
    mistake in goodall's own code.
    """
    if secrets_manager != "1password":
        raise ValueError(f"goodall.secrets_manager does not know the secrets manager {secrets_manager!r}.")
    result = runner.run(Command(ONE_PASSWORD_SIGNED_IN_COMMAND, output=OutputMode.DISCARD))
    if result.succeeded:
        return SignInAnswer(state=SignInState.SIGNED_IN)
    # The output was discarded, so `problem` quotes nothing op printed.
    if result.outcome == CommandOutcome.COMPLETED:
        return SignInAnswer(state=SignInState.SIGNED_OUT, problem=result.problem)
    return SignInAnswer(state=SignInState.NO_ANSWER, problem=result.problem)


def is_well_formed_secret_reference(secrets_manager: str, value: str) -> bool:
    """True when `value` has the form of a secret reference for `secrets_manager`.

    Only the form is checked; whether the item exists is a question for the
    secrets manager itself. A secrets manager this module does not know
    accepts nothing, so a gap here can never let a raw secret through.
    """
    if secrets_manager == "1password":
        return ONE_PASSWORD_REFERENCE_PATTERN.fullmatch(value) is not None
    return False


def looks_like_a_secret_reference(value: str) -> bool:
    """True when `value` looks like it is meant as a secret reference for any secrets manager goodall knows.

    Used to catch a reference in the shared env file, where it would be
    handed to a program as plain text instead of being resolved, and would
    reveal vault and item names in a repository that may be published.
    Deliberately loose: anything starting with a manager's scheme counts,
    well formed or not.
    """
    return value.lower().startswith("op://")
