"""The exit codes every goodall command returns.

Agents and scripts branch on these numbers, so they are a stable contract
(see "Command-line conventions" in AGENTS.md). Return one of these names
rather than a bare number, so a reader can see which case was meant.
"""

# The command did what was asked.
SUCCESS = 0

# The command was understood but could not finish: a check failed, a file was
# missing, or the feature is not built yet. The message says what to do next.
ERROR = 1

# The command line itself was wrong: an unknown command, a missing argument,
# or no command at all. Nothing was attempted.
USAGE_ERROR = 2

# The session gate is closed: no eval session is running, so the prompt must
# not go through. Used by the Claude Code hook path to block a prompt.
SESSION_GATE_CLOSED = 3
