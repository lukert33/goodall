# One `goodall` command with subcommands

Goodall ships a single command, `goodall`, with subcommands. This reverses an earlier design of separate `jane-*` programs plus a `jane` stub that listed them, chosen when there were five commands; the planned surface is now more than ten, hooks need one stable entry point, and one name on a stranger's `PATH` is easier to keep from colliding. The command is named after the package because `goodall` was unclaimed on PyPI, Homebrew, npm and crates.io when checked, while `jane` is taken on PyPI and npm.
