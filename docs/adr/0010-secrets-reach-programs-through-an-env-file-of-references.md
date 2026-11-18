# Secrets reach programs through an env file of references

Goodall keeps secret references in an env file, `.env`, beside local settings in the eval account's home, and every value in it must be a well-formed reference for the one secrets manager that local settings name. When a program needs a secret, the secrets manager launches that program with the value already in its environment, so the value never passes through goodall. Non-secret environment variables that launched programs need, such as an agent tool's telemetry switches, go in a shared env file, `.env.shared`, in the record repo, because they are the same for every contributor and are safe to version. Holding the references inside local settings would have meant one file fewer, but `.env` is the form people already recognize, and a file that holds only name-to-reference lines makes its one job obvious and makes the raw-secret check trivial: every line must pass.

## Consequences

- The env file is never in a repository, so it cannot be committed by accident; the shared env file is always in one.
- A variable whose name ends in `_KEY`, `_TOKEN` or `_SECRET` is refused in the shared env file and must be a reference in the env file. The same name in both files is an error.
- Goodall holds references only for secrets goodall itself uses (a model API key per workstream, later an admin key for usage reports). Secrets belonging to the work an eval records stay with that work.
- Goodall may check that a reference is well formed, and `goodall doctor` may ask the secrets manager whether the item exists using an operation that returns no value; where a manager cannot answer that safely, doctor reports that it could not verify.
- Any mechanism in which a secret value would pass through goodall, such as a helper program that prints a key for an agent tool to read, needs its own decision.
- There is no local file for non-secret variables true on only one machine. Goodall works such values out from local settings and sets them itself.
