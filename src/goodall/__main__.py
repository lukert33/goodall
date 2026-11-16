"""Lets `python -m goodall` behave exactly like the `goodall` command.

This is useful before the package is installed, for example with
`PYTHONPATH=src python -m goodall --help`.
"""

import sys

from goodall.cli import main

sys.exit(main())
