"""The diagnostic checks for the env file and the shared env file, under the heading "Secrets".

`goodall doctor` runs `SECRETS_CHECKS` last (goodall.doctor). The env file
checks only read files. The checks that ask the secrets manager something
(is it signed in? does each referenced item exist?) do so only through
goodall.secrets_manager, which never lets a secret value into goodall; no
code here builds a secrets manager command line. ADR 0010 is the governing
decision:

- The env file, `.env` beside local settings, maps variable names to secret
  references and holds nothing else. Any other value is treated as a
  possible raw secret pasted by mistake.
- The shared env file, `.env.shared` at the top of the record repo, holds
  non-secret variables and is committed, so a variable whose name says it
  holds a secret is refused there.
- A name in both files is an error, so a contributor always knows which
  value a program receives.

Two further judgements, beyond what ADR 0010 settles:

- The env file holds references, not secrets, so another account being
  able to read it is a warning, not a failure (`check_env_file_private`).
- In the shared env file, a value that is a secret reference fails, because
  nothing would resolve it; a value shaped like a key or token only warns,
  because that is a guess (goodall.quoting.looks_like_a_random_token).
  The same goes for a variable name shaped like a key or token: a base64
  token pasted with its = padding reads as a name.

The most important rule in this module: no value from either file ever
appears in a message, because any of them could be a key pasted by mistake,
and goodall's output can end up in a terminal history or a transcript an
agent reads (ADR 0006). Messages name the variable and the line instead,
and leave out the variable's name too when it could itself be a secret
(goodall.env_files.variable_as_shown).
Lines the reader could not read are reported by goodall.env_files under the
same rule.

The dependency edges:

- The env file checks need valid local settings: the env file sits beside
  them, and they name the secrets manager whose references are expected.
- The shared env file check needs only the record repo folder.
- The check for a name in both files needs both files' checks to have
  passed or warned, because it compares what they read.
- The check that the secrets manager's command is installed needs the env
  file read, because the command is needed only when the env file refers
  to a secret: an eval with no secrets needs no secrets manager (ADR 0006).
  It is shown under "Tools" beside git and gh.
- The sign-in check needs the secrets manager's command installed and the
  env file read, because it too asks only when the env file refers to a
  secret.
"""

import shlex
import stat
from pathlib import Path

from goodall.checks import Check, CheckContext, CheckOutcome, could_not_verify, failed, passed, warned
from goodall.env_files import EnvEntry, EnvFileNotRead, EnvLineProblem, read_env_file, variable_as_shown
from goodall.local_settings import LocalSettings
from goodall.program_checks import TOOLS_GROUP, check_program_installed
from goodall.quoting import looks_like_a_random_token
from goodall.runner import looks_like_a_secret_name
from goodall.secrets_manager import (
    DISPLAY_NAMES,
    REFERENCE_FORMS,
    SECRETS_MANAGER_PROGRAMS,
    SIGN_IN_STEPS,
    WHERE_TO_FIND_A_REFERENCE,
    WHY_FIELDS_CANNOT_BE_CHECKED,
    ItemAnswer,
    ItemState,
    SignInAnswer,
    SignInState,
    check_signed_in,
    is_well_formed_secret_reference,
    look_up_referenced_items,
    looks_like_a_secret_reference,
)
from goodall.settings_checks import LOCAL_SETTINGS_VALID, RECORD_REPO_FOUND, valid_local_settings
from goodall.terminal import Failure

GROUP = "Secrets"

ENV_FILE_HOLDS_ONLY_REFERENCES = "secrets.env_file_holds_only_references"
ENV_FILE_PRIVATE = "secrets.env_file_private"
SHARED_ENV_FILE_HOLDS_NO_SECRETS = "secrets.shared_env_file_holds_no_secrets"
NO_VARIABLE_IN_BOTH_ENV_FILES = "secrets.no_variable_in_both_env_files"
SECRETS_MANAGER_SIGNED_IN = "secrets.secrets_manager_signed_in"
REFERENCED_ITEMS_EXIST = "secrets.referenced_items_exist"
REFERENCED_FIELDS_EXIST = "secrets.referenced_fields_exist"

# Shown under "Tools" with git and gh, and named like them, but run here,
# after the env file is read; see `check_secrets_manager_installed`.
SECRETS_MANAGER_INSTALLED = "tools.secrets_manager_installed"

# The permission bits that let accounts other than the file's owner read,
# change or run a file: everything for the group and for everyone else.
OTHER_ACCOUNTS_PERMISSION_BITS = stat.S_IRWXG | stat.S_IRWXO

# The env file's name, in the same folder as local settings (ADR 0010).
ENV_FILE_NAME = ".env"

# The shared env file's name, at the top of the record repo (ADR 0010).
SHARED_ENV_FILE_NAME = ".env.shared"


def env_file_path(local_settings: LocalSettings) -> Path:
    """Where the env file is: beside local settings."""
    return local_settings.settings_file.parent / ENV_FILE_NAME


def shared_env_file_path(local_settings: LocalSettings) -> Path:
    """Where the shared env file is: at the top of the record repo."""
    return local_settings.record_repo.path / SHARED_ENV_FILE_NAME


def check_env_file_holds_only_references(context: CheckContext) -> CheckOutcome:
    """Every line of the env file is NAME=reference, with a well-formed reference for the secrets manager."""
    local_settings = valid_local_settings(context)
    env_file = env_file_path(local_settings)
    reading = read_env_file(env_file)
    if isinstance(reading, EnvFileNotRead) and reading.missing:
        # An eval that uses no secrets needs no env file (ADR 0006).
        context.env_file_entries = []
        return passed(
            note=(
                f"There is no env file at {env_file}, so programs goodall launches get no secrets from it. "
                "That is fine if this eval uses no secrets."
            )
        )
    if isinstance(reading, EnvFileNotRead):
        return failed([env_file_unreadable_failure(reading)])

    problems: list[Failure] = []
    for line_problem in reading.problems:
        problems.append(env_file_line_failure(env_file, line_problem))
    for entry in reading.entries:
        if not is_well_formed_secret_reference(local_settings.secrets_manager, entry.value):
            problems.append(
                env_file_value_not_a_reference_failure(env_file, entry.name, entry.line_number, local_settings)
            )
    if problems:
        return failed(problems)
    context.env_file_entries = reading.entries
    return passed()


def env_file_unreadable_failure(not_read: EnvFileNotRead) -> Failure:
    """The failure for an env file that is there but cannot be read."""
    return Failure(
        kind="env_file_unreadable",
        what_happened=f"The env file at {not_read.file} could not be read: {not_read.reason}.",
        why="The env file says which secret each program goodall launches receives, so goodall must read it.",
        what_to_do_next=(
            f"Make {not_read.file} a text file that this macOS user account can read, then run goodall doctor again."
        ),
    )


def env_file_line_failure(env_file: Path, line_problem: EnvLineProblem) -> Failure:
    """The failure for a line of the env file that is not NAME=value."""
    return Failure(
        kind="env_file_line_unreadable",
        what_happened=f"Line {line_problem.line_number} of the env file at {env_file} {line_problem.problem}.",
        why=(
            "Every line of the env file must be a variable name, =, and a secret reference, so that every "
            "program that reads it agrees on what it says. Goodall does not show the line, in case it holds a "
            "secret."
        ),
        what_to_do_next=(
            f"Open {env_file} in a text editor and correct line {line_problem.line_number}, or delete it. If it "
            "holds a real key, change the key with whoever issued it, because it may have been exposed."
        ),
    )


def env_file_value_not_a_reference_failure(
    env_file: Path, name: str, line_number: int, local_settings: LocalSettings
) -> Failure:
    """The failure for a variable in the env file whose value is not a secret reference.

    It may be a raw key pasted by mistake, so the value is never shown, and
    the next step says to change the key if it was real: once a key has been
    in a file on disk, it may have been copied anywhere.
    """
    secrets_manager = local_settings.secrets_manager
    name = variable_as_shown(name)
    return Failure(
        kind="env_file_value_not_a_reference",
        what_happened=(
            f"The value of {name}, on line {line_number} of the env file at {env_file}, is not a secret reference "
            f"for {DISPLAY_NAMES[secrets_manager]}. It may be a secret pasted by mistake, so goodall does not show "
            "it."
        ),
        why=(
            "The env file must hold only secret references, never secrets themselves, so that a secret is never "
            "written to a file an agent could read."
        ),
        what_to_do_next=(
            f"On line {line_number}, paste the secret reference in place of the value of {name}. A "
            f"{DISPLAY_NAMES[secrets_manager]} reference has the form {REFERENCE_FORMS[secrets_manager]}. "
            f"{WHERE_TO_FIND_A_REFERENCE[secrets_manager]}. If the value was a real key, change the key with "
            "whoever issued it, because it may have been exposed."
        ),
    )


def check_env_file_private(context: CheckContext) -> CheckOutcome:
    """Only this macOS user account can read or change the env file.

    A warning, not a failure: the env file holds references, not secrets,
    so another account reading it learns no secret. But a reference names
    the vault and the item, which tells someone what to look for, and an
    account that can change the file could point a program at a different
    secret. On macOS a new file is usually readable by every account
    (permissions 644), and the folders above it often let them reach it.

    A missing env file passes: there is nothing to protect.
    """
    env_file = env_file_path(valid_local_settings(context))
    try:
        env_file_status = env_file.stat()
    except OSError:
        # No file, or one that cannot be reached, which the env file check
        # reports; saying it twice would not help.
        return passed()
    if env_file_status.st_mode & OTHER_ACCOUNTS_PERMISSION_BITS:
        return warned([env_file_readable_by_others_failure(env_file)])
    return passed()


def env_file_readable_by_others_failure(env_file: Path) -> Failure:
    """The warning for an env file that other accounts on the Mac can read or change."""
    return Failure(
        kind="env_file_readable_by_others",
        what_happened=(
            f"Other macOS user accounts on this Mac may be able to read or change the env file at {env_file}."
        ),
        why=(
            "The env file holds secret references, not secrets, but a reference names the vault and item a "
            "secret is kept in, and a changed reference would give a program a different secret. Only this "
            "account needs to read it."
        ),
        what_to_do_next=f"Run chmod 600 {shlex.quote(str(env_file))} in Terminal, then run goodall doctor again.",
    )


def check_shared_env_file_holds_no_secrets(context: CheckContext) -> CheckOutcome:
    """The shared env file has no variable whose name says it holds a secret."""
    local_settings = valid_local_settings(context)
    shared_env_file = shared_env_file_path(local_settings)
    reading = read_env_file(shared_env_file)
    if isinstance(reading, EnvFileNotRead) and reading.missing:
        context.shared_env_file_entries = []
        return warned([shared_env_file_missing_failure(local_settings, shared_env_file)])
    if isinstance(reading, EnvFileNotRead):
        return failed([shared_env_file_unreadable_failure(reading)])

    # Problems are certain; suspicions are guesses. Each variable is
    # reported once, for the most certain reason that applies to it.
    problems: list[Failure] = []
    suspicions: list[Failure] = []
    for line_problem in reading.problems:
        problems.append(shared_env_file_line_failure(shared_env_file, line_problem))
    for entry in reading.entries:
        if looks_like_a_secret_name(entry.name):
            problems.append(
                shared_env_file_secret_name_failure(shared_env_file, entry.name, entry.line_number, local_settings)
            )
        elif looks_like_a_secret_reference(entry.value):
            problems.append(
                shared_env_file_secret_reference_failure(shared_env_file, entry.name, entry.line_number, local_settings)
            )
        elif looks_like_a_random_token(entry.name):
            suspicions.append(shared_env_file_name_looks_secret_failure(shared_env_file, entry.line_number))
        elif looks_like_a_random_token(entry.value):
            suspicions.append(
                shared_env_file_value_looks_secret_failure(
                    shared_env_file, entry.name, entry.line_number, local_settings
                )
            )

    if problems:
        return failed(problems + suspicions)
    context.shared_env_file_entries = reading.entries
    if suspicions:
        return warned(suspicions)
    return passed()


def shared_env_file_missing_failure(local_settings: LocalSettings, shared_env_file: Path) -> Failure:
    """The warning for a record repo with no shared env file.

    A warning, not a failure: no secret is at risk, and goodall can still
    launch programs, only without the settings every contributor is meant
    to share, such as Claude Code's telemetry switches.
    """
    return Failure(
        kind="shared_env_file_missing",
        what_happened=f"There is no shared env file at {shared_env_file}.",
        why=(
            "The shared env file holds the settings every contributor's launched programs need, such as "
            "Claude Code's telemetry switches. Without it, those programs run without them, and records may "
            "be missing."
        ),
        what_to_do_next=(
            f"Run git pull in {local_settings.record_repo.path} to get the eval's latest record repo. If there "
            f"is still no {SHARED_ENV_FILE_NAME}, ask whoever runs the eval to add one to the record repo."
        ),
    )


def shared_env_file_unreadable_failure(not_read: EnvFileNotRead) -> Failure:
    """The failure for a shared env file that is there but cannot be read."""
    return Failure(
        kind="shared_env_file_unreadable",
        what_happened=f"The shared env file at {not_read.file} could not be read: {not_read.reason}.",
        why="The shared env file sets what every contributor's launched programs need, so goodall must read it.",
        what_to_do_next=(
            f"Make {not_read.file} a text file that this macOS user account can read, then run goodall doctor again."
        ),
    )


def shared_env_file_line_failure(shared_env_file: Path, line_problem: EnvLineProblem) -> Failure:
    """The failure for a line of the shared env file that is not NAME=value."""
    return Failure(
        kind="shared_env_file_line_unreadable",
        what_happened=(
            f"Line {line_problem.line_number} of the shared env file at {shared_env_file} {line_problem.problem}."
        ),
        why=(
            "Every line of the shared env file must be a variable name, =, and a value, so that every program "
            "that reads it agrees on what it says. Goodall does not show the line, in case it holds a secret."
        ),
        what_to_do_next=(
            f"Correct line {line_problem.line_number} of {shared_env_file}, or delete it, and commit the change "
            "to the record repo."
        ),
    )


def shared_env_file_secret_name_failure(
    shared_env_file: Path, name: str, line_number: int, local_settings: LocalSettings
) -> Failure:
    """The failure for a variable in the shared env file whose name ends in _KEY, _TOKEN or _SECRET.

    The shared env file is committed to the record repo, which may be
    published, so a secret there is a secret given away (ADR 0010).
    """
    env_file = env_file_path(local_settings)
    reference_form = REFERENCE_FORMS[local_settings.secrets_manager]
    name = variable_as_shown(name)
    return Failure(
        kind="shared_env_file_secret_name",
        what_happened=(
            f"The shared env file at {shared_env_file} sets {name}, on line {line_number}, whose name says it "
            "holds a secret."
        ),
        why=(
            "The shared env file is committed to the record repo, which every contributor can read and which "
            "may be published, so it must never hold a secret."
        ),
        what_to_do_next=(
            f"For {name}, move this to the env file as a reference: delete line {line_number} of "
            f"{shared_env_file}, and add {name} to {env_file} with its secret reference, of the form "
            f"{reference_form}. If the value was a real secret, change it with whoever issued it, because it "
            "may already have been committed."
        ),
    )


def shared_env_file_secret_reference_failure(
    shared_env_file: Path, name: str, line_number: int, local_settings: LocalSettings
) -> Failure:
    """The failure for a secret reference in the shared env file.

    Nothing resolves references in the shared env file, so the program
    would get the reference text instead of the secret; and the reference
    would publish the names of a contributor's vault and item.
    """
    env_file = env_file_path(local_settings)
    name = variable_as_shown(name)
    return Failure(
        kind="shared_env_file_secret_reference",
        what_happened=(
            f"The shared env file at {shared_env_file} sets {name}, on line {line_number}, to what looks like a "
            "secret reference."
        ),
        why=(
            "References are resolved only in the env file. In the shared env file a program would receive the "
            "reference text, not the secret, and the record repo would show which vault and item hold it."
        ),
        what_to_do_next=(
            f"For {name}, move this to the env file as a reference: delete line {line_number} of "
            f"{shared_env_file}, and add the same line to {env_file}."
        ),
    )


def shared_env_file_value_looks_secret_failure(
    shared_env_file: Path, name: str, line_number: int, local_settings: LocalSettings
) -> Failure:
    """The warning for a shared env file value shaped like a key or token (see `looks_like_a_random_token`)."""
    env_file = env_file_path(local_settings)
    reference_form = REFERENCE_FORMS[local_settings.secrets_manager]
    name = variable_as_shown(name)
    return Failure(
        kind="shared_env_file_value_looks_secret",
        what_happened=(
            f"The value of {name}, on line {line_number} of the shared env file at {shared_env_file}, contains "
            "a long run of letters and digits that looks like a key or token. Goodall does not show it, in case "
            "it is one."
        ),
        why=(
            "The shared env file is committed to the record repo, which every contributor can read and which "
            "may be published, so it must never hold a secret."
        ),
        what_to_do_next=(
            f"If {name} holds a secret, move this to the env file as a reference: delete line {line_number} of "
            f"{shared_env_file}, add {name} to {env_file} with its secret reference, of the form "
            f"{reference_form}, and change the secret with whoever issued it, because it may already have been "
            "committed. If it is not a secret, this warning can be ignored."
        ),
    )


def shared_env_file_name_looks_secret_failure(shared_env_file: Path, line_number: int) -> Failure:
    """The warning for a shared env file variable whose name is shaped like a key or token.

    A base64 token pasted on a line of its own reads as a name followed by
    its = padding, so the name may be the secret. It is never shown.
    """
    return Failure(
        kind="shared_env_file_name_looks_secret",
        what_happened=(
            f"Line {line_number} of the shared env file at {shared_env_file} sets a variable whose name is a long "
            "run of letters and digits that looks like a key or token. Goodall does not show it, in case it is one."
        ),
        why=(
            "The shared env file is committed to the record repo, which every contributor can read and which "
            "may be published, so it must never hold a secret."
        ),
        what_to_do_next=(
            f"If line {line_number} is a key or token pasted by mistake, delete it from {shared_env_file}, and "
            "change the secret with whoever issued it, because it may already have been committed. If it is a "
            "real variable name, this warning can be ignored."
        ),
    )


def check_no_variable_in_both_env_files(context: CheckContext) -> CheckOutcome:
    """No variable name is set in both the env file and the shared env file.

    If one were, which value a program received would depend on the order
    goodall applied the files in, and a contributor reading either file
    would be misled (ADR 0010). Names are compared exactly, because macOS
    treats Name and NAME as two variables.
    """
    local_settings = valid_local_settings(context)
    env_file_entries = context.env_file_entries
    shared_env_file_entries = context.shared_env_file_entries
    if env_file_entries is None or shared_env_file_entries is None:
        raise RuntimeError(
            f"{NO_VARIABLE_IN_BOTH_ENV_FILES} must list {ENV_FILE_HOLDS_ONLY_REFERENCES} and "
            f"{SHARED_ENV_FILE_HOLDS_NO_SECRETS} as prerequisites."
        )

    shared_entries_by_name: dict[str, EnvEntry] = {}
    for shared_entry in shared_env_file_entries:
        shared_entries_by_name[shared_entry.name] = shared_entry

    problems: list[Failure] = []
    for env_entry in env_file_entries:
        if env_entry.name in shared_entries_by_name:
            shared_entry = shared_entries_by_name[env_entry.name]
            problems.append(variable_in_both_env_files_failure(local_settings, env_entry, shared_entry))
    if problems:
        return failed(problems)
    return passed()


def variable_in_both_env_files_failure(
    local_settings: LocalSettings, env_entry: EnvEntry, shared_entry: EnvEntry
) -> Failure:
    """The failure for a variable name set in both env files.

    The next step cannot say which line to keep, because only the
    contributor knows whether the variable holds a secret; it gives the rule
    for deciding instead.
    """
    env_file = env_file_path(local_settings)
    shared_env_file = shared_env_file_path(local_settings)
    name = variable_as_shown(env_entry.name)
    return Failure(
        kind="variable_in_both_env_files",
        what_happened=(
            f"{name} is set on line {env_entry.line_number} of the env file at {env_file} and on line "
            f"{shared_entry.line_number} of the shared env file at {shared_env_file}."
        ),
        why=(
            "A program can receive only one value for a variable, so a name in both files leaves it unclear "
            "which one it gets."
        ),
        what_to_do_next=(
            f"Keep {name} in one file only: in the env file if it is a secret, which is the usual case, or in "
            f"the shared env file if every contributor's programs need the same value and it is not a secret. "
            "Delete the other line, then run goodall doctor again."
        ),
    )


def check_secrets_manager_installed(context: CheckContext) -> CheckOutcome:
    """The command of the secrets manager that local settings name is installed, when the env file refers to any secret.

    With no secret references the secrets manager is not needed at all
    (ADR 0006), so a Mac without it passes, with a note saying why.
    """
    local_settings = valid_local_settings(context)
    if not context.env_file_entries:
        return passed(note="This eval uses no secret references, so no secrets manager is needed.")
    return check_program_installed(context, SECRETS_MANAGER_PROGRAMS[local_settings.secrets_manager])


def check_secrets_manager_signed_in(context: CheckContext) -> CheckOutcome:
    """The secrets manager that local settings name is signed in, when the env file refers to any secret.

    With no secret references there is nothing to ask the secrets manager
    for, so it is not asked: an eval with no secrets needs no secrets
    manager (ADR 0006), and asking could only fail such an eval for nothing.

    A secrets manager that does not answer in time fails too, rather than
    counting as unknown: goodall cannot launch a program with its secrets
    through a secrets manager that does not answer, and failing here spares
    every reference check a timeout of its own.
    """
    local_settings = valid_local_settings(context)
    secrets_manager = local_settings.secrets_manager
    if not context.env_file_entries:
        return passed(
            note=(
                f"The env file refers to no secrets, so goodall did not ask {DISPLAY_NAMES[secrets_manager]} "
                "whether it is signed in."
            )
        )
    answer = check_signed_in(secrets_manager, context.runner)
    if answer.state == SignInState.SIGNED_IN:
        return passed()
    return failed([secrets_manager_not_signed_in_failure(local_settings, answer)])


def secrets_manager_not_signed_in_failure(local_settings: LocalSettings, answer: SignInAnswer) -> Failure:
    """The failure for a secrets manager that is signed out, or did not answer."""
    display_name = DISPLAY_NAMES[local_settings.secrets_manager]
    sign_in_step = SIGN_IN_STEPS[local_settings.secrets_manager]
    if answer.state == SignInState.NO_ANSWER:
        return Failure(
            kind="secrets_manager_did_not_answer",
            what_happened=(
                f"Goodall could not tell whether {display_name} is signed in, because it did not answer: "
                f"{answer.problem}. If {display_name} showed a request to approve access, it may have been "
                "waiting for you."
            ),
            why=f"Programs goodall launches get their secrets from {display_name}, so it must answer promptly.",
            what_to_do_next=f"{sign_in_step}. Then run goodall doctor again.",
        )
    return Failure(
        kind="secrets_manager_signed_out",
        what_happened=f"{display_name} is not signed in for this macOS user account: {answer.problem}.",
        why=(
            f"Programs goodall launches get their secrets from {display_name}, and goodall checks that each "
            f"secret reference points at a real item, so {display_name} must be signed in."
        ),
        what_to_do_next=f"{sign_in_step}. Then run goodall doctor again.",
    )


def check_referenced_items_exist(context: CheckContext) -> CheckOutcome:
    """The item each secret reference in the env file points at exists in the secrets manager.

    One check for every reference, rather than one check per reference, so
    the report stays the same length however many secrets an eval uses;
    each problem names its variable and line, so a researcher can tell which
    one it is. The reference itself is never shown: it names a vault and an
    item, which tell a reader where to look.

    A reference goodall could not check fails no one: it is reported as
    unverified, so "not checked" is never mistaken for "fine" or "broken".
    When some references are missing and others unverified, the check
    fails and lists both.
    """
    local_settings = valid_local_settings(context)
    entries = context.env_file_entries
    if entries is None:
        raise RuntimeError(f"{REFERENCED_ITEMS_EXIST} must list {ENV_FILE_HOLDS_ONLY_REFERENCES} as a prerequisite.")
    if not entries:
        return passed(note="The env file refers to no secrets, so there is no item to look for.")

    references = []
    for entry in entries:
        references.append(entry.value)
    answers = look_up_referenced_items(local_settings.secrets_manager, references, context.runner)

    problems: list[Failure] = []
    unverified: list[Failure] = []
    for entry, answer in zip(entries, answers, strict=True):
        if answer.state in [ItemState.MISSING, ItemState.VAULT_MISSING]:
            problems.append(referenced_item_missing_failure(local_settings, entry, answer))
        elif answer.state in [ItemState.AMBIGUOUS, ItemState.COULD_NOT_VERIFY]:
            unverified.append(referenced_item_not_verified_failure(local_settings, entry, answer))
    if problems:
        return failed(problems + unverified)
    if unverified:
        return could_not_verify(unverified)
    return passed()


def check_referenced_fields_exist(context: CheckContext) -> CheckOutcome:
    """The field each secret reference names exists in its item.

    A check of its own, rather than part of the item check, so the report
    says exactly what was checked: the items were, and passed; the fields
    were not. Today it can never pass when the env file has references,
    because 1Password, the one secrets manager, cannot say whether a field
    exists without handing over its value (goodall.secrets_manager). It
    says so as "unverified", never as a pass: a misspelt field name shows
    only when a program is launched with the secret.
    """
    local_settings = valid_local_settings(context)
    entries = context.env_file_entries
    if entries is None:
        raise RuntimeError(f"{REFERENCED_FIELDS_EXIST} must come after {ENV_FILE_HOLDS_ONLY_REFERENCES}.")
    if not entries:
        return passed(note="The env file refers to no secrets, so there is no field to look for.")
    return could_not_verify([referenced_fields_not_verified_failure(local_settings, entries)])


def referenced_fields_not_verified_failure(local_settings: LocalSettings, entries: list[EnvEntry]) -> Failure:
    """What to say about the fields goodall cannot check: which variables, why, and how to check by hand."""
    display_name = DISPLAY_NAMES[local_settings.secrets_manager]
    variables = []
    for entry in entries:
        variables.append(f"{variable_as_shown(entry.name)} (line {entry.line_number})")
    return Failure(
        kind="secret_reference_field_not_verified",
        what_happened=(
            f"Goodall checked that each item exists, but not the field each reference names, because "
            f"{WHY_FIELDS_CANNOT_BE_CHECKED[local_settings.secrets_manager]}. Not checked: "
            f"{', '.join(variables)}."
        ),
        why=(
            "No secret may enter goodall, so goodall reports what it could not check rather than "
            "read a secret to find out."
        ),
        what_to_do_next=(
            f"Nothing, if you copied each reference with Copy Secret Reference in {display_name} and have not "
            "renamed the field since. Otherwise, copy each one that way and paste it over its value in the env "
            "file."
        ),
    )


def referenced_item_not_verified_failure(local_settings: LocalSettings, entry: EnvEntry, answer: ItemAnswer) -> Failure:
    """What to say about a secret reference whose item goodall could not check.

    The next step lets a researcher check it by hand, since goodall could
    not; for a name two items share, it says how to make the reference
    unambiguous.
    """
    display_name = DISPLAY_NAMES[local_settings.secrets_manager]
    where_to_find = WHERE_TO_FIND_A_REFERENCE[local_settings.secrets_manager]
    name = variable_as_shown(entry.name)
    if answer.state == ItemState.AMBIGUOUS:
        what_to_do_next = (
            f"{where_to_find}, and paste it as the value of {name} on line {entry.line_number}; if the name "
            f"is shared, {display_name} puts the item's id in the reference instead of its name, which settles it. "
            "Or rename one of the items. Then run goodall doctor again."
        )
    else:
        what_to_do_next = (
            f"Run goodall doctor again. If it still cannot check, open {display_name} and confirm that the item "
            f"the reference for {name} names is there."
        )
    return Failure(
        kind="secret_reference_not_verified",
        what_happened=(
            f"Goodall could not check the item that {name} (line {entry.line_number}) of the env file refers "
            f"to, because {answer.reason}."
        ),
        why=(
            f"Goodall asks {display_name} only questions whose answer holds no secret; when that answer does not "
            "come, it says so rather than guess."
        ),
        what_to_do_next=what_to_do_next,
    )


def referenced_item_missing_failure(local_settings: LocalSettings, entry: EnvEntry, answer: ItemAnswer) -> Failure:
    """The failure for a secret reference whose item, or whose vault, the secrets manager does not have."""
    display_name = DISPLAY_NAMES[local_settings.secrets_manager]
    env_file = env_file_path(local_settings)
    name = variable_as_shown(entry.name)
    where = f"{name}, on line {entry.line_number} of the env file at {env_file}"
    if answer.state == ItemState.VAULT_MISSING:
        kind = "secret_reference_vault_missing"
        what_happened = (
            f"The secret reference for {where}, names a vault this {display_name} account cannot see. Goodall "
            "does not show the reference."
        )
    else:
        kind = "secret_reference_item_missing"
        what_happened = (
            f"The secret reference for {where}, names an item that is not in its vault in {display_name}. "
            "Goodall does not show the reference."
        )
    return Failure(
        kind=kind,
        what_happened=what_happened,
        why=(
            f"A program launched with {name} would get no secret, because {display_name} cannot find "
            "what the reference points at."
        ),
        what_to_do_next=(
            f"{WHERE_TO_FIND_A_REFERENCE[local_settings.secrets_manager]}, and paste it as the value of "
            f"{name} on line {entry.line_number}. If the item was deleted, archived or moved to a vault "
            "this account cannot see, restore it or ask whoever shares the vault for access. Then run goodall "
            "doctor again."
        ),
    )


SECRETS_CHECKS = [
    Check(
        id=ENV_FILE_HOLDS_ONLY_REFERENCES,
        group=GROUP,
        summary="The env file holds only secret references",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_env_file_holds_only_references,
    ),
    Check(
        id=SECRETS_MANAGER_INSTALLED,
        group=TOOLS_GROUP,
        summary="The secrets manager's command is installed",
        prerequisites=[ENV_FILE_HOLDS_ONLY_REFERENCES],
        run=check_secrets_manager_installed,
    ),
    Check(
        id=ENV_FILE_PRIVATE,
        group=GROUP,
        summary="Only this macOS user account can read the env file",
        prerequisites=[LOCAL_SETTINGS_VALID],
        run=check_env_file_private,
    ),
    Check(
        id=SHARED_ENV_FILE_HOLDS_NO_SECRETS,
        group=GROUP,
        summary="The shared env file holds no secrets",
        prerequisites=[RECORD_REPO_FOUND],
        run=check_shared_env_file_holds_no_secrets,
    ),
    Check(
        id=NO_VARIABLE_IN_BOTH_ENV_FILES,
        group=GROUP,
        summary="No variable is set in both the env file and the shared env file",
        prerequisites=[ENV_FILE_HOLDS_ONLY_REFERENCES, SHARED_ENV_FILE_HOLDS_NO_SECRETS],
        run=check_no_variable_in_both_env_files,
    ),
    Check(
        id=SECRETS_MANAGER_SIGNED_IN,
        group=GROUP,
        summary="The secrets manager is signed in",
        prerequisites=[SECRETS_MANAGER_INSTALLED, ENV_FILE_HOLDS_ONLY_REFERENCES],
        run=check_secrets_manager_signed_in,
    ),
    Check(
        id=REFERENCED_ITEMS_EXIST,
        group=GROUP,
        summary="Each secret reference points at an item that exists",
        prerequisites=[SECRETS_MANAGER_SIGNED_IN, ENV_FILE_HOLDS_ONLY_REFERENCES],
        run=check_referenced_items_exist,
    ),
    Check(
        id=REFERENCED_FIELDS_EXIST,
        group=GROUP,
        summary="Each secret reference names a field that exists",
        prerequisites=[REFERENCED_ITEMS_EXIST],
        run=check_referenced_fields_exist,
    ),
]
