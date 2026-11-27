"""`goodall settings`: the setup page again, with this account's current answers filled in.

Called by `cli.main`. A person types it, or chooses "Settings…" in the menu
bar icon, which runs it with no terminal in sight (ADR 0024); whatever it
prints then goes to the icon's log file, so the page itself says what a
person needs (docs/menu-bar-icon.md). The waiting, the answers file and the
plan-and-apply are the same as `goodall onboard`'s, in goodall.setup_page and
goodall.onboard; this module only checks that there is something to change
and opens the page in its settings form.

What a settings run may change is the rule in docs/setup-page.md,
"Settings": the same things onboarding with the eval already on this Mac may
change, and nothing in the eval config except new names at the end of its
workstreams line. It never starts a new eval and never creates one: the page
leaves the "new eval" choice out.

Not set up yet is exit 1 with the next step, `goodall onboard`: settings are
answers about a setup that exists. The same goes for an eval config goodall
cannot read, because adding a workstream to it would be a guess.
"""

import dataclasses
from collections.abc import Callable
from datetime import datetime

from goodall import exit_codes
from goodall.eval_config import read_eval_config
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.runner import Runner
from goodall.setup_page import ask_on_setup_page, starting_answers_from_this_account
from goodall.terminal import Terminal

SETTINGS_PAGE_TITLE = "Goodall settings"


def run_settings(terminal: Terminal, runner: Runner, clock: Callable[[], datetime], plan_only: bool) -> int:
    """Open the settings page and apply what changed; return the exit code."""
    settings = read_local_settings()
    if not isinstance(settings, LocalSettings):
        terminal.show_failures(settings)
        return exit_codes.ERROR
    eval_config = read_eval_config(settings.record_repo.path, settings.settings_file)
    if isinstance(eval_config, list):
        terminal.show_failures(eval_config)
        return exit_codes.ERROR
    starting = dataclasses.replace(starting_answers_from_this_account(), settings_run=True)
    return ask_on_setup_page(
        terminal,
        runner,
        clock,
        starting,
        plan_only=plan_only,
        page_title=SETTINGS_PAGE_TITLE,
        command_name="settings",
    )
