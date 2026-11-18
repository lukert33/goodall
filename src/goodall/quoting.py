"""Deciding whether text someone wrote in a settings or env file may be shown back to them.

Goodall's output can end up in a terminal history or a transcript an agent
reads, and anything a person wrote in a file may be a secret pasted into the
wrong place (ADR 0006). So a failure never repeats a value from local
settings, the eval config, the env file or the shared env file. It may
repeat a name (a setting's key, a variable's name) only when `can_show_name`
says so, and otherwise points at the place instead: "a key goodall does not
know in the 1st [[contributors]] block", "the variable on line 6".

Two ways of telling that text may be a key, kept apart because they are
not equally sure:

- `looks_like_a_random_token` is a guess from shape alone: a long run of
  letters and digits. UUIDs and git commit ids have the same shape, so
  where it raises a warning, the warning says it may be wrong.
- `key_format_in` recognises the published formats of well-known issuers'
  keys (`KEY_FORMATS`), such as Anthropic's `sk-ant-`. A match is close to
  certain, so a caller may fail on it, and it names the issuer, so the
  next step can say who replaces the key. It never says anything about the
  key itself.

Trap: a name is not safe just because the file parsed. `sk-ant-api03-...`
is a valid bare TOML key, and a base64 token is a valid variable name whose
= padding reads as the = after it. That is why every name passes through
`can_show_name` before it is put in a message. For the same reason, the
TOML reader's own error messages, some of which repeat a key, pass through
`toml_error_as_shown`.

Called by goodall.local_settings, goodall.eval_config, goodall.env_files
and goodall.secrets_checks. The first three are on the hook path, so this
module imports only the standard library (ADR 0005);
tests/test_hook_path_imports.py proves it.
"""

import re
from dataclasses import dataclass

# A run of characters that API keys and tokens are made of: letters, digits,
# and the -, _ and + that their encodings use. See `looks_like_a_random_token`.
TOKEN_CHARACTERS_RUN = re.compile(r"[A-Za-z0-9_\-+]+")

# How long a run of token characters must be to look like a token. Real API
# keys are 20 characters or more (AWS access key ids are exactly 20); words
# and settings rarely are.
SHORTEST_TOKEN_LENGTH = 20

# What a name a person typed looks like: letters, digits, underscores and
# hyphens, starting with a letter or underscore. This covers setting names,
# eval config keys and environment variable names.
NAME_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")

# The longest name goodall shows. Long enough for the longest variable names
# real tools use (OpenTelemetry's run to about 50 characters), short enough
# that a whole pasted key is never shown.
LONGEST_NAME_SHOWN = 64


def looks_like_a_random_token(text: str) -> bool:
    """True when `text` contains something shaped like an API key or token.

    That is: a run of at least SHORTEST_TOKEN_LENGTH letters, digits, -, _
    and +, holding both letters and digits. It catches keys such as
    sk-ant-..., ghp_..., AKIA... and a bearer token inside a header value,
    and passes ordinary settings such as "otlp", "http/json" and URLs.

    It is a guess: a long identifier such as a UUID, a git commit id or a
    long variable name with a digit in it matches too. Where a match only
    stops goodall showing a name, a false match costs nothing; where it
    raises a warning, the warning says it may be wrong.
    """
    for run in TOKEN_CHARACTERS_RUN.findall(text):
        if len(run) < SHORTEST_TOKEN_LENGTH:
            continue
        has_letter = any(character.isalpha() for character in run)
        has_digit = any(character.isdigit() for character in run)
        if has_letter and has_digit:
            return True
    return False


@dataclass(frozen=True)
class KeyFormat:
    """The published format of one issuer's keys.

    - `described` names the kind of key in a sentence, such as "an Anthropic
      API key".
    - `issuer` is who replaces a key that may have been exposed, written to
      follow "with", such as "Anthropic"; "whoever issued it" for a format
      many issuers use.
    - `pattern` finds the key anywhere in a longer text. Each requires a
      plausible length after the prefix, so that a short word that happens
      to start the same way (`ops_team`, `sk-learn`) is not taken for a key,
      and requires that no letter or digit comes straight before the
      prefix, so that a prefix inside a longer word does not count.
    """

    described: str
    issuer: str
    pattern: re.Pattern[str]


# No letter or digit straight before a key's prefix.
KEY_START = r"(?<![A-Za-z0-9])"

# The key formats goodall recognises, checked in this order, so a more
# specific prefix (sk-ant-) is tried before a general one (sk-). A format
# belongs here only when its prefix is published or widely documented and
# rare outside keys. Add one with a line naming its issuer and a test in
# tests/test_quoting.py for each direction: a key it finds, and a near
# miss it does not.
KEY_FORMATS = [
    # Anthropic API keys and admin keys: sk-ant-api03-..., sk-ant-admin01-...
    KeyFormat("an Anthropic API key", "Anthropic", re.compile(KEY_START + r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    # OpenAI project, service account and admin keys.
    KeyFormat("an OpenAI API key", "OpenAI", re.compile(KEY_START + r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}")),
    # OpenAI's older keys: sk- and a long run of letters and digits, no dashes.
    KeyFormat("an OpenAI API key", "OpenAI", re.compile(KEY_START + r"sk-[A-Za-z0-9]{32,}")),
    # GitHub personal, OAuth, user-to-server, server-to-server and refresh tokens.
    KeyFormat("a GitHub token", "GitHub", re.compile(KEY_START + r"gh[pousr]_[A-Za-z0-9]{30,}")),
    # GitHub fine-grained personal access tokens.
    KeyFormat("a GitHub token", "GitHub", re.compile(KEY_START + r"github_pat_[A-Za-z0-9_]{22,}")),
    # AWS access key ids, long-term (AKIA) and temporary (ASIA): exactly 16
    # capital letters or digits after the prefix.
    KeyFormat("an AWS access key id", "AWS", re.compile(KEY_START + r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    # Slack bot, user and app tokens.
    KeyFormat("a Slack token", "Slack", re.compile(KEY_START + r"xox[bpa]-[A-Za-z0-9\-]{20,}")),
    # Google API keys: AIza and exactly 35 more characters.
    KeyFormat("a Google API key", "Google", re.compile(KEY_START + r"AIza[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])")),
    # 1Password service account tokens, which run to hundreds of characters.
    KeyFormat("a 1Password service account token", "1Password", re.compile(KEY_START + r"ops_[A-Za-z0-9+/=_\-]{50,}")),
    # The first line of a PEM private key, of any type (RSA, EC, OPENSSH, ...).
    KeyFormat("a private key", "whoever issued it", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    # JSON Web Tokens: three base64url parts joined by dots, the first a
    # JSON object, so it starts eyJ.
    KeyFormat(
        "a JSON Web Token",
        "whoever issued it",
        re.compile(KEY_START + r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
]


def key_format_in(text: str) -> KeyFormat | None:
    """The first of KEY_FORMATS found anywhere in `text`, or None.

    Says which format, never where or what: the caller reports the issuer
    and the place in the file, and nothing of the key.
    """
    for key_format in KEY_FORMATS:
        if key_format.pattern.search(text) is not None:
            return key_format
    return None


# The messages of Python's TOML reader (tomllib) that repeat a key from the
# file, by how each starts, and what goodall says instead. Checked against
# Python 3.12's tomllib; `toml_error_as_shown` also hides any message that
# holds something shaped like a token, in case a later Python words one
# differently.
TOML_ERRORS_THAT_NAME_A_KEY = {
    "Cannot declare": "A table or key is declared twice",
    "Cannot mutate immutable namespace": "A key or table is changed after it was already set",
    "Cannot redefine namespace": "A table is defined again",
    "Duplicate inline table key": "A key appears twice in one inline table",
}

# What goodall says in place of a TOML reader's message it will not repeat.
TOML_ERROR_NOT_SHOWN = "There is a mistake here that goodall does not describe, in case it would repeat a secret"


def toml_error_as_shown(error_message: str) -> str:
    """A TOML reader's error message with any key from the file taken out, keeping where the mistake is.

    tomllib ends each message with the place, such as
    "(at line 2, column 15)", which is what a person editing by hand needs,
    so the place is always kept.
    """
    description, separator, place = error_message.rpartition(" (at ")
    if separator == "":
        description = error_message
    for message_start, replacement in TOML_ERRORS_THAT_NAME_A_KEY.items():
        if description.startswith(message_start):
            description = replacement
    if looks_like_a_random_token(description):
        description = TOML_ERROR_NOT_SHOWN
    if separator == "":
        return description
    return f"{description} (at {place}"


def can_show_name(name: str) -> bool:
    """True when `name` is short, shaped like a name, and does not look like a key or token.

    Only such a name may appear in a message. Anything else is referred to
    by where it is, never by what it says. Both key rules apply: a key in a
    recognised format can have no digit in it, which the random-token rule
    needs.
    """
    if len(name) > LONGEST_NAME_SHOWN:
        return False
    if NAME_SHAPE.fullmatch(name) is None:
        return False
    if key_format_in(name) is not None:
        return False
    return not looks_like_a_random_token(name)
