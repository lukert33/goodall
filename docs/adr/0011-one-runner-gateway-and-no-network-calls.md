# One runner gateway, and no network calls of goodall's own

Everything goodall runs goes through one general runner: a caller passes a command and gets back a result it can read, and expected failures (program not found, timed out, exited non-zero) are returned, never raised. Each command says what happens to its output: captured, discarded, or the terminal handed over to the program. Programs that outlive one goodall command, such as the telemetry receiver during a work block, are represented by a record that can be saved and used by a later command to stop them. Goodall makes no network calls itself, because checking a GitHub token over HTTPS would put the token's value inside goodall, which ADR 0010 forbids; `gh` does that check. Giving every external tool its own purpose-built interface was considered and rejected as three edits for every new diagnostic check, with one exception: the secrets manager gets its own module above the runner, because a second secrets manager is expected (ADR 0006) and it is the one place a value could leak.

## Consequences

- Short commands default to a ten-second timeout, overridable per call. Handing over the terminal has no timeout: it is a person's working session.
- Retrying is the caller's decision, since a retry after a failed push involves git policy.
- "Discard" is the output mode for any command that might print a secret.
- In tests the fake runner scripts everything except `git`, which runs for real in a temporary repository; a command that is neither scripted nor allowed through fails the test.
- Tests hand the runner and the UI module to the program's entry point, so an outside-in test drives `goodall` with a fake runner and reads back what was printed.
