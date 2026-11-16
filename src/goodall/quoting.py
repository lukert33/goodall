"""Deciding whether text someone wrote in a settings or env file may be shown back to them.

Goodall's output can end up in a terminal history or a transcript an agent
reads, and anything a person wrote in a file may be a secret pasted into the
wrong place (ADR 0006). So a failure never repeats a value from local
settings, the eval config, the env file or the shared env file. It may
repeat a name (a setting's key, a variable's name) only when `can_show_name`
says so, and otherwise points at the place instead: "a key goodall does not
know in the 1st [[contributors]] block", "the variable on line 6".

Trap: a name is not safe just because the file parsed. `sk-ant-api03-...`
is a valid bare TOML key, and a base64 token is a valid variable name whose
= padding reads as the = after it. That is why every name passes through
`can_show_name` before it is put in a message.

Called by goodall.local_settings, goodall.eval_config, goodall.env_files
and goodall.secrets_checks. The first three are on the hook path, so this
module imports only the standard library (ADR 0005);
tests/test_hook_path_imports.py proves it.
"""

import re

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


def can_show_name(name: str) -> bool:
    """True when `name` is short, shaped like a name, and does not look like a key or token.

    Only such a name may appear in a message. Anything else is referred to
    by where it is, never by what it says.
    """
    if len(name) > LONGEST_NAME_SHOWN:
        return False
    if NAME_SHAPE.fullmatch(name) is None:
        return False
    return not looks_like_a_random_token(name)
