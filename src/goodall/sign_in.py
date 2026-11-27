"""`goodall sign-in`: what the first-run script does at every sign-in to every account (ADR 0022).

Called by `cli.main`. The first-run script, which the administrator install
staged owned by root, is three lines that run this command
(goodall.system_places), so every decision is here, in Python, where it can
be tested and read. launchd runs that script at every sign-in to every
account on the Mac, through the LaunchAgent in `/Library/LaunchAgents`.

What it does, in order:

1. In any account but the eval account it says so and stops, having read
   one answer and changed nothing: no file, no folder, no program run. The
   eval account is the one the staged answers file names in `[eval_account]
   name`, compared with the account's short name as the system gives it.
   ADR 0022 also allowed a marker file in the new home folder for this;
   the staged answers file already names the account and only root can
   change it, so no marker is needed.
2. In the eval account, the first time: first sign-in setup. It reads the
   staged answers file, refusing it unless root owns it and no other
   account can change it, and sets the account up from it by the
   unattended path of ADR 0012, `goodall onboard --answers`, which shows
   each change before making it. The Claude Code hooks run the goodall
   installed for every account unless the answers say otherwise. Once that
   succeeds, it notes that setup is done (`FIRST_SIGN_IN_DONE_FILE_NAME`,
   beside local settings), so a later sign-in repeats no setup step. If
   setup fails, nothing is noted, and the next sign-in tries again; running
   it again repeats nothing already done.
3. In the eval account, every time: start a work block on the main
   workstream as a background start (`goodall start --background
   sign-in`), which shows a notification. When one is already active,
   nothing starts and no notification is shown. Nothing starts when local
   settings say `start_at_sign_in = false` (goodall.local_settings), which
   is how a person turns it off, or in mixed account mode, where nothing
   starts unless the person starts it (ADR 0021).

Where the person sees what it is doing: launchd shows a program's output to
nobody, so in the eval account everything this command prints also goes to
`~/Library/Logs/goodall/sign-in.log`, readable by that account alone. After
the first sign-in's setup, or when it fails, goodall opens that log in the
person's text editor (`open -t`), so they read each step it took and any
next step. Later sign-ins add to the log without opening it; the
notification says recording started. Whether that window really appears
after macOS's own first sign-in screens is one of the things the hand test
checks (docs/administrator-install.md). In any other account nothing is
written, so the log is never created there.

Signing out: the stop is `goodall stop --background sign-out`, which is
built and shows its notification. What would run it is not built: ADR 0022
and `goodall-fs9.27` leave unknown whether macOS lets anything run
dependably at sign-out until the hand test (`goodall-ayy.11`, step 5).
Until then the three-hour idle rule ends a work block nobody stopped.

A known gap: first sign-in setup makes the record repo's first commit, so
git needs a name and email in the eval account (Part 2 of
docs/setup-by-hand.md), which a new account does not have. Until the
answers file can carry them, the first sign-in fails with git's next step,
and the next sign-in tries again.
"""

import dataclasses
import io
import os
from pathlib import Path
from typing import TextIO, cast

from goodall import administrator_install, exit_codes, notifications, system_places
from goodall.answers_file import read_answers_file
from goodall.local_settings import (
    MIXED_ACCOUNT_MODE,
    RelativeConfigDirectoryError,
    find_local_settings_location,
    read_local_settings,
    relative_config_directory_failure,
)
from goodall.onboard import onboard_from_answers
from goodall.runner import Command, Runner
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import Clock, describe_moment, run_start

# The note, beside local settings, that first sign-in setup has finished.
FIRST_SIGN_IN_DONE_FILE_NAME = "first-sign-in-done"

# Where the eval account's sign-ins are logged, inside its home folder,
# beside the menu bar icon's log.
SIGN_IN_LOG_IN_HOME = Path("Library") / "Logs" / "goodall" / "sign-in.log"

# The log's folder and file, readable by the eval account alone.
PRIVATE_FOLDER = 0o700
PRIVATE_FILE = 0o600

# `open` hands the file to the text editor and returns at once.
OPEN_COMMAND = "/usr/bin/open"
OPEN_TIMEOUT_SECONDS = 5.0


def run_sign_in(terminal: Terminal, runner: Runner, clock: Clock) -> int:
    """`goodall sign-in`, as cli.main calls it: decide whether this is the eval account, then act."""
    staged_file = system_places.staged_answers_file()
    if not os.path.lexists(staged_file):
        terminal.show_paragraph(
            "No first sign-in setup is staged on this Mac, so goodall sign-in has nothing to do. Nothing was changed."
        )
        return exit_codes.SUCCESS
    eval_account_name = administrator_install.eval_account_name_in(staged_file)
    if eval_account_name != system_places.current_account_name():
        terminal.show_paragraph(
            "This macOS user account is not the eval account the administrator install set up, so goodall "
            "sign-in has nothing to do here. Nothing was changed."
        )
        return exit_codes.SUCCESS

    log_file = Path.home() / SIGN_IN_LOG_IN_HOME
    with open_log(log_file) as log:
        both = Terminal(
            cast(TextIO, BothStreams(terminal.standard_output, log)),
            cast(TextIO, BothStreams(terminal.standard_error, log)),
            standard_input=terminal.standard_input,
        )
        return sign_in_to_eval_account(both, runner, clock, eval_account_name, log_file)


def sign_in_to_eval_account(terminal: Terminal, runner: Runner, clock: Clock, account_name: str, log_file: Path) -> int:
    """In the eval account: first sign-in setup if it has not finished yet, then start recording."""
    terminal.show_paragraph(f"Signed in to the eval account {account_name} at {describe_moment(clock())}.")
    try:
        done_file = find_local_settings_location().config_directory / FIRST_SIGN_IN_DONE_FILE_NAME
    except RelativeConfigDirectoryError:
        terminal.show_failure(relative_config_directory_failure())
        return exit_codes.ERROR

    first_sign_in = not done_file.exists()
    if first_sign_in:
        setup_exit_code = first_sign_in_setup(terminal, runner, clock, done_file)
        if setup_exit_code != exit_codes.SUCCESS:
            show_log_to_person(terminal, runner, log_file)
            return setup_exit_code
    else:
        terminal.show_paragraph("First sign-in setup finished at an earlier sign-in, so no setup step runs again.")

    exit_code = start_recording(terminal, runner, clock)
    if first_sign_in:
        show_log_to_person(terminal, runner, log_file)
    return exit_code


def first_sign_in_setup(terminal: Terminal, runner: Runner, clock: Clock, done_file: Path) -> int:
    """Set this account up from the staged answers file, and note that it is done once it is."""
    staged_file = system_places.staged_answers_file()
    terminal.show_paragraph(
        f"First sign-in: goodall sets up this account from the answers file staged at {staged_file}. Each change "
        "is listed below before it is made."
    )
    protection_problem = administrator_install.describe_protection(staged_file)
    if protection_problem is not None:
        terminal.show_failure(staged_answers_unprotected_failure(staged_file, protection_problem))
        return exit_codes.ERROR
    reading = read_answers_file(staged_file)
    if isinstance(reading, list):
        terminal.show_failures(reading)
        terminal.show_paragraph(setup_not_finished_sentence())
        return exit_codes.ERROR
    answers = reading
    if answers.goodall_program is None:
        # launchd's PATH has no goodall on it, and the hooks must run the
        # goodall this account cannot change (goodall.system_places).
        answers = dataclasses.replace(answers, goodall_program=str(system_places.installed_goodall()))
    exit_code = onboard_from_answers(terminal, runner, clock, answers)
    if exit_code != exit_codes.SUCCESS:
        terminal.show_paragraph(setup_not_finished_sentence())
        return exit_code
    done_file.write_text(f"First sign-in setup finished at {clock().isoformat()}.\n", encoding="utf-8")
    terminal.show_paragraph(
        "First sign-in setup is done. From now on, each time you sign in to this account goodall starts "
        "recording and shows a notification saying so."
    )
    return exit_codes.SUCCESS


def setup_not_finished_sentence() -> str:
    return (
        "First sign-in setup did not finish. Do what the message above says; goodall tries again the next time "
        "you sign in, and repeats nothing that is already done."
    )


def start_recording(terminal: Terminal, runner: Runner, clock: Clock) -> int:
    """Start a work block on the main workstream as a background start, unless that is turned off."""
    settings = read_local_settings()
    if isinstance(settings, list):
        terminal.show_failures(settings)
        return exit_codes.ERROR
    if not settings.start_at_sign_in:
        terminal.show_paragraph(
            f"Starting a work block at sign-in is turned off: {settings.settings_file} says start_at_sign_in = "
            "false. Nothing was started. Run goodall start to record."
        )
        return exit_codes.SUCCESS
    if settings.account_mode == MIXED_ACCOUNT_MODE:
        terminal.show_paragraph(
            "This account is in mixed account mode, where nothing starts unless you start it. Nothing was started."
        )
        return exit_codes.SUCCESS
    return run_start(terminal, runner, clock, workstream=settings.main_workstream, background=notifications.SIGN_IN)


def show_log_to_person(terminal: Terminal, runner: Runner, log_file: Path) -> None:
    """Open the sign-in log in the person's text editor, so they read what first sign-in setup did."""
    result = runner.run(Command([OPEN_COMMAND, "-t", str(log_file)], timeout_seconds=OPEN_TIMEOUT_SECONDS))
    if result.problem is not None:
        terminal.show_warnings([log_not_opened_failure(log_file, result.problem)])


class open_log:
    """The eval account's sign-in log, opened to add to, made readable by that account alone.

    A context manager so the file is closed however the sign-in ends. A
    log that cannot be opened is replaced by a stream that keeps nothing,
    because the log only copies what is printed; it must never stop the
    sign-in.
    """

    def __init__(self, log_file: Path) -> None:
        self.log_file = log_file
        self.stream: TextIO = io.StringIO()

    def __enter__(self) -> TextIO:
        try:
            self.log_file.parent.mkdir(mode=PRIVATE_FOLDER, parents=True, exist_ok=True)
            descriptor = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, PRIVATE_FILE)
            self.stream = os.fdopen(descriptor, "a", encoding="utf-8")
        except OSError:
            pass
        return self.stream

    def __exit__(self, *exception: object) -> None:
        self.stream.close()


class BothStreams(io.TextIOBase):
    """A stream that writes everything to two others: what goodall prints, and the sign-in log.

    It reports that it is not a terminal, so the text is wrapped at the
    fixed width and has no color codes, which would be noise in a log.
    """

    def __init__(self, first: TextIO, second: TextIO) -> None:
        super().__init__()
        self.first = first
        self.second = second

    def write(self, text: str) -> int:
        self.first.write(text)
        self.second.write(text)
        return len(text)

    def flush(self) -> None:
        self.first.flush()
        self.second.flush()

    def isatty(self) -> bool:
        return False


def staged_answers_unprotected_failure(staged_file: Path, problem: str) -> Failure:
    return Failure(
        kind="staged_answers_unprotected",
        what_happened=(
            f"The answers file staged for this account at {staged_file} was not used: {problem}. Nothing was set up."
        ),
        why=(
            "First sign-in setup takes its answers from that file, so only root may be able to change it. Otherwise "
            "another account could have changed what this one is set up to record."
        ),
        what_to_do_next=(
            "Ask whoever administers this Mac to run the administrator install again from an administrator account "
            "(docs/administrator-install.md), then sign out and in again."
        ),
    )


def log_not_opened_failure(log_file: Path, problem: str) -> Failure:
    return Failure(
        kind="sign_in_log_not_opened",
        what_happened=f"goodall could not open its sign-in log for you to read: {problem}.",
        why="The log says what first sign-in setup did, step by step, and what to do if anything did not work.",
        what_to_do_next=f"Open {log_file} in TextEdit to read it.",
    )
