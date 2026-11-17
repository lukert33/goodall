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
- does the item each secret reference points at exist?
  (`look_up_referenced_items`)
- and, for each manager, why the field a reference names cannot be checked
  (`WHY_FIELDS_CANNOT_BE_CHECKED`).

Launching a program with secrets in its environment will be added here too.

The rule every question that runs the secrets manager keeps: no secret
value may ever enter goodall's process (ADR 0010). An answer that is in the
exit code alone is asked with the runner's discard output mode, so nothing
the secrets manager prints reaches goodall at all. An answer that is in
what the secrets manager prints is asked only of a command documented to
print no secret value, and what it prints is used to answer and then
dropped: never shown, never stored, never quoted in a message. Where no
such command exists, the answer is "could not verify"; goodall never asks
for a value and throws it away, because the value would still have been in
goodall's memory.

Messages never name a reference or any part of it (a reference names a
vault and an item, which tell a reader where to look), so the answers here
carry the reference only for the caller to match them to its variables.

1Password, the one secrets manager today (ADR 0006), is asked through its
command, op. `op whoami` "Returns the currently active account or service
account" and "returns an error if no accounts are currently authenticated"
(https://www.1password.dev/cli/reference/commands/whoami/, read
2026-11-16). It prints who is signed in, never a secret, and goodall
discards even that. A 1Password community forum answer (not the reference
documentation) suggests it for checking a session without setting off a
Touch ID prompt, which matters because doctor must not ask a person
anything.

To find out whether an item exists, goodall lists vaults (`op vault list`)
and the items in a vault (`op item list --vault`). 1Password's reference
says only that these return "all vaults the current user has read access
to" and "a list of all items the account has read access to"
(https://www.1password.dev/cli/reference/management-commands/vault/ and
.../item/, read 2026-11-16); it sends a reader to `op item get` and
`op read` for fields' contents. That the listings' JSON holds no field
value is from op 2's observed output, not a documented promise: it is the
first thing to confirm against a real 1Password account.

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

import json
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

# The scheme that starts each secrets manager's references, in lower case.
# 1Password's op:// is the one kind supported today (ADR 0006).
SECRET_REFERENCE_SCHEMES = ["op://"]

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

# Why each secrets manager cannot say whether the field a reference names
# exists, said so a researcher understands what goodall left unchecked.
# 1Password has no command that lists an item's fields without their
# values: `op item get` returns every field with its "value" (its documented
# JSON example shows them), and `op read` returns the value itself. Asking
# either and throwing the answer away would still bring the secret into
# goodall, which ADR 0010 forbids, so goodall does not ask.
WHY_FIELDS_CANNOT_BE_CHECKED = {
    "1password": (
        "1Password cannot tell goodall whether a field exists without also handing over the field's value, "
        "the secret itself, so goodall does not ask"
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


class ItemState(StrEnum):
    """What the secrets manager said about the item one secret reference points at."""

    EXISTS = "exists"
    # The vault exists, and holds no item with that name or id.
    MISSING = "missing"
    # This account can see no vault with that name or id.
    VAULT_MISSING = "vault_missing"
    # More than one vault, or more than one item in the vault, has the name
    # the reference uses, so which one the secrets manager would use is not
    # known. Naming it by its id instead settles it.
    AMBIGUOUS = "ambiguous"
    # Goodall could not find out safely, for the reason the answer gives.
    COULD_NOT_VERIFY = "could_not_verify"


@dataclass(frozen=True)
class ItemAnswer:
    """The answer for one secret reference: does the item it points at exist?

    - `reference` is the reference asked about, so a caller can match the
      answer to its variable. Never show it: it names a vault and an item.
    - `reason` is set when the state is AMBIGUOUS or COULD_NOT_VERIFY: why,
      as the end of a sentence starting "Goodall could not check it
      because". It never
      holds the reference, any part of it, or anything the secrets manager
      printed, so it is safe to show.

    An answer is about the item only. Whether the field exists too is a
    separate question (`FIELDS_CAN_BE_CHECKED`).
    """

    reference: str
    state: ItemState
    reason: str | None = None


def look_up_referenced_items(secrets_manager: str, references: list[str], runner: Runner) -> list[ItemAnswer]:
    """Ask `secrets_manager` whether the item each of `references` points at exists, without reading any secret.

    Returns one answer per reference, in the same order. Every reference
    must be well formed (`is_well_formed_secret_reference`). Raises
    ValueError for a secrets manager this module does not know, as
    `check_signed_in` does.
    """
    if secrets_manager != "1password":
        raise ValueError(f"goodall.secrets_manager does not know the secrets manager {secrets_manager!r}.")
    return check_one_password_items_exist(references, runner)


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
    handed to a program as plain text instead of being resolved, and in the
    eval config; either would reveal vault and item names in a repository
    that may be published. Deliberately loose: a manager's scheme anywhere
    in the value counts, in any case, well formed or not. This is the one
    definition; add a manager's scheme to SECRET_REFERENCE_SCHEMES.
    """
    lower_case_value = value.lower()
    for scheme in SECRET_REFERENCE_SCHEMES:
        if scheme in lower_case_value:
            return True
    return False


# 1Password's two value-free listings. `op vault list` lists the vaults this
# account can read; `op item list --vault` lists one vault's items. Both give
# each entry's id and name (an item's name is its "title") with a little
# more (dates, a category, and for an item a short "additional_information"
# such as a login's username), and neither gives any field's value: values
# come from `op item get` and `op read`, which goodall never runs. Goodall
# keeps only the ids and names, and only until it has answered.
ONE_PASSWORD_VAULT_LIST_COMMAND = ["op", "vault", "list", "--format", "json"]


def one_password_item_list_command(vault_id: str) -> list[str]:
    """The command that lists the items in one vault, named by its id.

    The id, not the name the reference uses, so the command line (which
    every program on the Mac can see while it runs) names no vault.
    """
    return ["op", "item", "list", "--vault", vault_id, "--format", "json"]


@dataclass(frozen=True)
class OnePasswordEntry:
    """A vault or an item as op lists it, cut down to the two things goodall needs: its id and its name."""

    id: str
    name: str


def check_one_password_items_exist(references: list[str], runner: Runner) -> list[ItemAnswer]:
    """Answer `look_up_referenced_items` for 1Password.

    One `op vault list`, then one `op item list` per vault the references
    use, so a typical env file costs two commands. Names are matched without
    regard to capital letters, because 1Password documents that "Secret
    references are case-insensitive"
    (https://www.1password.dev/cli/secret-reference-syntax/).
    """
    vaults = list_one_password_entries(ONE_PASSWORD_VAULT_LIST_COMMAND, "name", runner)
    if isinstance(vaults, str):
        answers = []
        for reference in references:
            answers.append(ItemAnswer(reference=reference, state=ItemState.COULD_NOT_VERIFY, reason=vaults))
        return answers

    # Each vault's items, listed the first time a reference needs them.
    items_by_vault_id: dict[str, list[OnePasswordEntry] | str] = {}
    answers = []
    for reference in references:
        vault_part, item_part = one_password_vault_and_item(reference)
        matching_vaults = find_entries_by_name_or_id(vaults, vault_part)
        if not matching_vaults:
            answers.append(ItemAnswer(reference=reference, state=ItemState.VAULT_MISSING))
            continue
        if len(matching_vaults) > 1:
            answers.append(
                ItemAnswer(
                    reference=reference,
                    state=ItemState.AMBIGUOUS,
                    reason=(
                        "more than one vault this account can see has the name the reference uses, so 1Password "
                        "may not know which is meant"
                    ),
                )
            )
            continue

        vault_id = matching_vaults[0].id
        if vault_id not in items_by_vault_id:
            items_by_vault_id[vault_id] = list_one_password_entries(
                one_password_item_list_command(vault_id), "title", runner
            )
        items = items_by_vault_id[vault_id]
        if isinstance(items, str):
            answers.append(ItemAnswer(reference=reference, state=ItemState.COULD_NOT_VERIFY, reason=items))
            continue

        matching_items = find_entries_by_name_or_id(items, item_part)
        if not matching_items:
            answers.append(ItemAnswer(reference=reference, state=ItemState.MISSING))
        elif len(matching_items) > 1:
            answers.append(
                ItemAnswer(
                    reference=reference,
                    state=ItemState.AMBIGUOUS,
                    reason=(
                        "more than one item in its vault has the name the reference uses, so 1Password may not "
                        "know which is meant"
                    ),
                )
            )
        else:
            answers.append(ItemAnswer(reference=reference, state=ItemState.EXISTS))
    return answers


def one_password_vault_and_item(reference: str) -> tuple[str, str]:
    """The vault and item parts of a well-formed 1Password reference, op://vault/item/[section/]field."""
    parts = reference.removeprefix("op://").split("/")
    return parts[0], parts[1]


def find_entries_by_name_or_id(entries: list[OnePasswordEntry], name_or_id: str) -> list[OnePasswordEntry]:
    """Every entry whose name or id is `name_or_id`, ignoring capital letters, as 1Password does."""
    wanted = name_or_id.casefold()
    matching_entries = []
    for entry in entries:
        if entry.name.casefold() == wanted or entry.id.casefold() == wanted:
            matching_entries.append(entry)
    return matching_entries


def list_one_password_entries(arguments: list[str], name_key: str, runner: Runner) -> list[OnePasswordEntry] | str:
    """Run one of op's value-free listings and keep only each entry's id and name.

    Returns the entries, or, when op did not give a usable answer, the
    reason as the end of a sentence. The reason never quotes what op
    printed, and never names a vault: `arguments[:3]` is "op vault list" or
    "op item list", which stop before the vault's id.

    Captured, not discarded, because the answer is in what op prints; that
    is safe only because these two listings never print a field's value.
    """
    command = Command(arguments, output=OutputMode.CAPTURE)
    result = runner.run(command)
    described_command = "`" + " ".join(arguments[:3]) + "`"
    if result.outcome == CommandOutcome.TIMED_OUT:
        return f"{described_command} did not answer within {command.timeout_seconds:g} s"
    if result.outcome != CommandOutcome.COMPLETED:
        return f"{described_command} could not be run"
    if not result.succeeded:
        return f"{described_command} exited with code {result.exit_code}"

    unreadable = f"goodall could not read the answer {described_command} gave"
    try:
        listing = json.loads(result.standard_output)
    except ValueError:
        return unreadable
    if not isinstance(listing, list):
        return unreadable
    entries = []
    for listed in listing:
        if not isinstance(listed, dict):
            return unreadable
        entry_id = listed.get("id")
        entry_name = listed.get(name_key)
        if not isinstance(entry_id, str) or not isinstance(entry_name, str):
            return unreadable
        entries.append(OnePasswordEntry(id=entry_id, name=entry_name))
    return entries
