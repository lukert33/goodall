# One eval per macOS user account, found through local settings

A macOS user account holds exactly one eval, and goodall finds that eval through local settings: one file at a fixed place in the account's home folder that says where the record repo and the log directory are and which contributor this account is. The eval config is then read from the record repo. Searching upward from the working directory was rejected because commands and hooks run inside whatever repository the work is in, which is rarely the record repo; an environment variable was rejected as the main mechanism because it is not reliably present in every terminal or in the processes an agent tool starts. One variable may override the location, so that tests and goodall's own development never touch a real home folder. Running two evals in one macOS user account is unsupported: the account is the boundary of the record, and one eval per account means goodall never has to guess which eval a command belongs to.

## Consequences

When the local settings are missing or unreadable, the prompt gate refuses the prompt and says how to fix it, because in an eval account unrecorded work is the worse failure. When the log directory cannot be reached, goodall reports that no work block is active, so "cannot write records" and "not recording" are the same observable state.
