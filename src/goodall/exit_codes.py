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

# The prompt gate is closed: no work block is active, so the prompt must not
# go through to the agent. For goodall's own commands that report on the
# gate. Trap: `goodall hook` does not return it; see the next code.
PROMPT_GATE_CLOSED = 3

# Claude Code's own code for "refuse this prompt". Only `goodall hook
# UserPromptSubmit` returns it (goodall.hook), because Claude Code refuses a
# prompt only when a hook exits 2 and treats any other code as a hook error
# to show while letting the prompt through. It is the same number as
# USAGE_ERROR because Claude Code chose it, not goodall.
CLAUDE_CODE_REFUSES_PROMPT = 2
