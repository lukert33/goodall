"""Notifications: telling the person when a work block is started or stopped in the background.

Called by goodall.work_block_commands. A background start or stop is one
made by something other than the person's own command (ADR 0021): signing
in or out, a script or schedule, or goodall ending a work block by itself.
That is the one moment the person does not already know what goodall is
doing, so each shows exactly one notification. A start or stop the person
typed, or chose from the menu bar icon, shows none.

How goodall knows a start or stop is "background":

- Signing in and out, and scripts and schedules, say so. They run
  `goodall start`, `goodall stop` or `goodall pause` with `--background`
  and a reason (`BACKGROUND_STARTS`, `BACKGROUND_STOPS`,
  `BACKGROUND_PAUSES`). Nothing passes `sign-in` or `sign-out` yet: the
  LaunchAgent that will is a later issue.
- Goodall's own endings count without being told. Settling
  (goodall.work_block_commands.settle), which `goodall start`, `stop`,
  `pause` and `status` all do first, closes a work block left idle past the
  idle limit (an inferred end) and one whose marker it cannot read (an
  abnormal end). Each of those shows a notification even when the command
  was typed, because the person chose that command, not the closing, and
  when the command came from the menu bar icon nobody sees its output.
  Those are every place goodall ends a work block today; the automatic
  timeout (`goodall-fs9.30`) will be another.
- The menu bar icon's items run plain `goodall start --workstream NAME`
  and `goodall stop`, never `--background`: a click is the person's own
  command.

How a notification reaches the screen (compared 2026-11-27):

- It is shown by the menu bar icon's helper, so it appears under goodall's
  own name and macOS's permission for it belongs to the one bundle,
  `io.github.lukert33.goodall`, installed at `/Applications/goodall.app`
  (ADR 0024). Never by `osascript`, which shows it as Script Editor's.
- Goodall runs the helper's program directly, through the runner, as a
  second short-lived process of the same bundle:
  `.../goodall.app/Contents/MacOS/goodall-menu-bar-icon --notify TITLE BODY`.
  That process posts the notification and exits; it never draws an icon,
  so it works the same whether or not the icon is running. Its exit code
  says what happened, so a notification that was not shown becomes a
  warning with the reason.
- Rejected: `open -g /Applications/goodall.app --args --notify ...`. `open`
  hands `--args` only to an instance it launches; an instance already
  running gets nothing (open(1), and seen on this Mac). `open -n` does
  launch a second instance with the arguments, but `open` returns before
  the helper has tried, so goodall could never tell the person that the
  notification failed. It is the fallback if a directly run helper turns
  out not to be allowed to post.
- Rejected: a file in the folder the running icon watches. It reaches no
  one while the icon is not running, which is exactly when a sign-in start
  happens, and the helper would have to read, show and delete files,
  which is logic in the Swift.

What was and was not seen on a Mac (2026-11-27, a development account,
builds in a scratch folder, nothing installed): `open --args` to a running
instance delivered nothing; `open -n` started a second instance with the
arguments and exited 0 whatever the helper did; the helper run directly
read its own bundle identifier, drew no icon, and exited with the code
for "macOS has not asked yet", as an `open -n` launch of the same build
also reported. Not seen: a notification actually shown by a directly run
helper, because that needs the person to answer macOS's question first,
and a test must not ask it for the real bundle. Also open: whether an
ad-hoc signed rebuild keeps a permission given to the build before it, and
anything in `/Applications` or a standard account.

Everything the person reads is decided here: the title ("Recording
started" or "Recording stopped"), and a body giving the workstream and the
reason. No word of it is "eval" (ADR 0021). The notification asks nothing
and has no buttons. The first-time question macOS asks for permission is
macOS's own; the helper asks for it when it starts and goodall says setup
is usable (goodall.icon_state, `ask_notification_permission`), never here,
because a background moment is one the person may not be watching, and a
dismissed question counts as "no" for good.

Nothing here can stop a work block from starting or ending: by the time a
notification is asked for, the work block has started or ended, and one
that is not shown is a warning (`notification_not_shown_failure`). That
includes a Mac with no helper installed: the warning is shown even when a
typed command has already said in the terminal that goodall closed a work
block, so that "goodall never ends a work block without telling you" is
never quietly untrue.
"""

from dataclasses import dataclass

from goodall import menu_bar_icon, records
from goodall.runner import Command, CommandOutcome, Runner
from goodall.terminal import Failure

# The helper's program inside goodall.app, as Info.plist's CFBundleExecutable names it.
HELPER_PROGRAM = "Contents/MacOS/goodall-menu-bar-icon"

# The argument that tells the helper to show one notification and exit.
NOTIFY_ARGUMENT = "--notify"

# What the helper's exit code means in `--notify` mode (menu-bar-icon/main.swift).
SHOWN = 0
NOT_ALLOWED = 3
NOT_ASKED_YET = 4

# The helper waits a few seconds for macOS at most twice; this is the limit on the whole.
NOTIFY_TIMEOUT_SECONDS = 15.0

RECORDING_STARTED = "Recording started"
RECORDING_STOPPED = "Recording stopped"

# The reasons a command may give with --background, and the words the body
# gives for each. A start, a stop and a pause each have their own, because
# signing in starts and signing out stops.
SIGN_IN = "sign-in"
SIGN_OUT = "sign-out"
SCRIPT = "script"
BACKGROUND_STARTS = {SIGN_IN: "because you signed in", SCRIPT: "started by a script or schedule"}
BACKGROUND_STOPS = {SIGN_OUT: "because you signed out", SCRIPT: "stopped by a script or schedule"}
BACKGROUND_PAUSES = {SCRIPT: "paused by a script or schedule"}


@dataclass(frozen=True)
class Notification:
    """One notification: its title, and the body under it."""

    title: str
    body: str

    @property
    def what_it_said(self) -> str:
        """`recording started` or `recording stopped`, for the warning when it could not be shown."""
        return self.title[0].lower() + self.title[1:]


def background_start(workstream: str, reason: str) -> Notification:
    """The notification for a work block started by signing in or by a script."""
    return Notification(RECORDING_STARTED, f"On {workstream}, {BACKGROUND_STARTS[reason]}.")


def background_stop(workstream: str, reason: str) -> Notification:
    """The notification for a work block stopped by signing out or by a script."""
    return Notification(RECORDING_STOPPED, f"On {workstream}, {BACKGROUND_STOPS[reason]}.")


def background_pause(workstream: str, reason: str) -> Notification:
    """The notification for a work block paused by a script. A pause ends the work block, so it is a stop."""
    return Notification(RECORDING_STOPPED, f"On {workstream}, {BACKGROUND_PAUSES[reason]}.")


def closed_when_idle(workstream: str, idle_limit: str, last_activity: str) -> Notification:
    """The notification for a work block goodall closed at its last heartbeat (an inferred end).

    `idle_limit` and `last_activity` come already worded, as the terminal
    says them, so the two never disagree.
    """
    return Notification(
        RECORDING_STOPPED,
        f"On {workstream}. Nothing happened for more than {idle_limit}, so goodall stopped recording at the "
        f"last activity, {last_activity}.",
    )


def closed_when_unreadable(workstream: str) -> Notification:
    """The notification for a work block goodall closed because it could not read its marker (an abnormal end)."""
    opening = f"On {workstream}."
    if workstream == records.UNKNOWN_WORKSTREAM:
        opening = "Workstream unknown."
    return Notification(
        RECORDING_STOPPED,
        f"{opening} goodall could not read what it was recording, so it stopped. Run goodall status in Terminal "
        "for details.",
    )


def show(runner: Runner, notification: Notification) -> Failure | None:
    """Ask the menu bar icon's helper to show `notification`; a warning when it was not shown, else None."""
    helper = menu_bar_icon.installed_helper()
    if helper is None:
        return notification_not_shown_failure(notification, "the menu bar icon is not installed on this Mac")
    result = runner.run(
        Command(
            [str(helper / HELPER_PROGRAM), NOTIFY_ARGUMENT, notification.title, notification.body],
            timeout_seconds=NOTIFY_TIMEOUT_SECONDS,
        )
    )
    if result.succeeded:
        return None
    if result.outcome == CommandOutcome.COMPLETED and result.exit_code == NOT_ALLOWED:
        return notifications_not_allowed_failure(notification)
    if result.outcome == CommandOutcome.COMPLETED and result.exit_code == NOT_ASKED_YET:
        return notifications_not_asked_yet_failure(notification)
    problem = result.problem
    assert problem is not None
    return notification_not_shown_failure(notification, problem)


def notification_not_shown_failure(notification: Notification, problem: str) -> Failure:
    """The warning for a notification that could not be shown. The work block is as goodall said regardless."""
    return Failure(
        kind="notification_not_shown",
        what_happened=f"goodall could not show a notification that {notification.what_it_said}: {problem}.",
        why=(
            "goodall shows one whenever recording starts or stops without your own command, so you always know. "
            "The work block itself is as this command says."
        ),
        what_to_do_next=(
            "Run goodall status to see whether goodall is recording. If the menu bar icon is not installed, ask "
            "whoever set up goodall on this Mac to install it."
        ),
        commands_to_try=("goodall status",),
    )


def notifications_not_asked_yet_failure(notification: Notification) -> Failure:
    """The warning for a notification macOS would not show because it has not asked the person yet.

    macOS asks when the menu bar icon first starts once goodall is set up
    (goodall.icon_state). Until then goodall is not listed in System
    Settings, so the next step is to bring the icon back, not to go there.
    """
    return Failure(
        kind="notification_not_shown",
        what_happened=(
            f"goodall could not show a notification that {notification.what_it_said}: macOS has not yet asked "
            "whether goodall may show notifications."
        ),
        why=(
            "macOS asks once, when the menu bar icon first starts after goodall is set up. Until you answer, you "
            "are not told when recording starts or stops without your own command. The work block itself is as "
            "this command says."
        ),
        what_to_do_next=(
            "Run goodall start, which brings the menu bar icon back, and choose Allow when macOS asks. Run goodall "
            "status to see whether goodall is recording."
        ),
        commands_to_try=("goodall status",),
    )


def notifications_not_allowed_failure(notification: Notification) -> Failure:
    """The warning for a notification macOS would not show, because goodall is not allowed to show any."""
    return Failure(
        kind="notification_not_shown",
        what_happened=(
            f"goodall could not show a notification that {notification.what_it_said}: macOS is not letting goodall "
            "show notifications."
        ),
        why=(
            "macOS asks once whether goodall may show notifications, and a question that was dismissed or left "
            "unanswered counts as no. Without them you are not told when recording starts or stops without your "
            "own command. The work block itself is as this command says."
        ),
        what_to_do_next=(
            "Open System Settings > Notifications > goodall and turn on Allow notifications. Run goodall status to "
            "see whether goodall is recording."
        ),
        commands_to_try=("goodall status",),
    )
