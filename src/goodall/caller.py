"""Who started this command: a question goodall can answer only from the environment.

Called by goodall.entries today, and by any later command that must be run
by a person rather than an agent. It imports nothing from goodall, so any
module can use it cheaply, including code on the hook path.

Claude Code sets the environment variable `CLAUDECODE=1` on the commands it
runs. Goodall cannot prove who wrote the words of a command, and a person can
unset the variable, so this is an honest marker, not a lock: it stops an agent
doing this by accident or by habit, and leaves no doubt that goodall asked.
"""

from collections.abc import Mapping

CLAUDE_CODE_VARIABLE = "CLAUDECODE"


def was_started_by_claude_code(environment: Mapping[str, str]) -> bool:
    """True when `environment` carries Claude Code's mark, `CLAUDECODE=1`.

    Only the exact value Claude Code sets counts. Any other value, including
    "0", "true" or empty, is taken as not marked: that is how a person tells
    their own shell the command is theirs, and guessing at other spellings
    would refuse people for no reason.
    """
    return environment.get(CLAUDE_CODE_VARIABLE) == "1"
