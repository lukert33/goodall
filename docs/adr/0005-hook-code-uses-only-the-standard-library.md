# Code run by hooks uses only the standard library

Any goodall code that a Claude Code hook runs imports nothing outside the Python standard library. The prompt gate runs synchronously before every prompt and the copy hooks run after every tool call, so import time is paid constantly, and a hook that fails because a third-party package is missing or broken would stop capture or block the person's work. This is why the hook path does not use the packages the rest of goodall may adopt, and why analysis dependencies such as Inspect are kept out of the capture path entirely.
