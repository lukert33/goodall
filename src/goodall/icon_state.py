"""`goodall icon-state`: what the menu bar icon draws, read from disk and nothing else.

Called by `cli.main` when the menu bar icon's helper (the Swift program in
`menu-bar-icon/`, ADR 0024) runs `goodall icon-state --json`. The helper
holds no logic: it runs this command when the folder named in `watch_folder`
changes and about once a minute, and draws what the document says. A person
can run it too, without `--json`, to see the words the icon would show.

Why this is a command of its own and not `goodall status`:

- `goodall status` settles what earlier work blocks left behind: it can
  close a forgotten work block, commit to the record repo and copy agent
  sessions (goodall.status). The icon asks every minute without anyone
  having asked it to, so its command must change nothing. This one reads
  local settings, the eval config and two small files in the log directory.
  It never writes a file, never starts a program (it is not even handed
  the runner), and never settles anything.
- `goodall status` answers a contributor or an agent ("may I proceed?"),
  with an exit code and a document to match. A `--read-only` flag would
  give one command two promises, two exit code rules and two documents.
  The icon is a different caller with a different question, as Claude Code
  is for `goodall hook`, so it gets its own command, named for its caller.

The document, which is a stable contract with the helper
(docs/menu-bar-icon.md describes each key):

    schema_version      1
    state               "recording", "paused" or "not_recording"
    status_line         the first line of the menu, in words for a person
    account_mode        "dedicated" or "mixed"; null when setup is not usable
    work_block          {id, workstream, started_at} while recording, else null
    paused_work_block   {id, workstream, paused_at} while paused, else null
    workstreams         [{name, main}], the contributor's main workstream first
    enabled             {record_work_block, pause, stop_recording}: which menu items make sense now
    setup               {usable, reason}: reason is null when usable, else a sentence for a person
    watch_folder        the folder whose changes mean "ask again", or null

Decisions a reader would otherwise reverse-engineer:

- The words and the choice of which menu items are enabled are made here,
  so the helper never decides anything and the Swift stays small.
- Nothing in `status_line` or `setup.reason` says "eval" (ADR 0021): the
  helper shows them as they are. That is why `setup.reason` is one of two
  fixed sentences and not the text of goodall's own failures, which name
  the eval account and eval.toml; it points at `goodall status`, which
  shows those in full.
- A work block that is overdue (no heartbeat for longer than
  work_blocks.IDLE_LIMIT) is shown as not recording, which is what the
  prompt gate already treats it as. Its marker is left alone: the next
  `goodall start`, `stop` or `status` closes it.
- A pause is shown only when no work block's marker is in place, because a
  pause left behind by a running work block is one settling forgets.
- `watch_folder` is the folder that holds `active-work-block.json` and
  `pause.json`. Before the first work block that folder does not exist,
  and this command may not create it, so it names the log directory,
  where the folder will appear. The helper watches whichever it is told.
- The exit code is 0 when setup is usable and 1 when it is not. The
  document is printed either way, and the helper reads the document.
"""

from datetime import datetime

from goodall import exit_codes, work_blocks
from goodall.eval_config import EvalForThisAccount
from goodall.terminal import Failure, Terminal
from goodall.work_block_commands import Clock, find_eval_for_work_blocks

# The version of the document's shape, written into it as `schema_version`.
ICON_STATE_SCHEMA_VERSION = 1

RECORDING = "recording"
PAUSED = "paused"
NOT_RECORDING = "not_recording"

NOT_RECORDING_LINE = "Not recording"

# The two reasons setup can be unusable, in words the menu can show. Neither
# says "eval"; `goodall status` gives the full account.
NOT_SET_UP_REASON = (
    "goodall is not set up in this macOS user account yet. Run goodall status in Terminal to see what is needed."
)
CANNOT_RECORD_REASON = "goodall cannot record right now. Run goodall status in Terminal to see why."


def run_icon_state(terminal: Terminal, clock: Clock, as_json: bool) -> int:
    """Print what the menu bar icon should show, and return the exit code. Changes nothing."""
    eval_reading = find_eval_for_work_blocks()
    if isinstance(eval_reading, list):
        document = unusable_document(eval_reading)
        exit_code = exit_codes.ERROR
    else:
        document = icon_state_document(eval_reading, clock())
        exit_code = exit_codes.SUCCESS
    if as_json:
        terminal.write_json(document)
    else:
        terminal.show_paragraph(str(document["status_line"]))
        reason = setup_reason(document)
        if reason is not None:
            terminal.show_paragraph(reason)
    return exit_code


def setup_reason(document: dict[str, object]) -> str | None:
    """The sentence saying why setup is not usable, out of a document; None when it is usable."""
    setup = document["setup"]
    if isinstance(setup, dict) and isinstance(setup.get("reason"), str):
        return str(setup["reason"])
    return None


def icon_state_document(eval_for_this_account: EvalForThisAccount, now: datetime) -> dict[str, object]:
    """The document when goodall can be used in this account. Times are UTC, as in records."""
    settings = eval_for_this_account.local_settings
    log_directory = settings.log_directory.path

    marker = work_blocks.read_marker(log_directory)
    active = None
    if isinstance(marker, work_blocks.RunningWorkBlock) and not marker.is_overdue(now):
        active = marker
    pause = None
    if not isinstance(marker, work_blocks.RunningWorkBlock):
        pause = work_blocks.read_pause(log_directory)

    state = NOT_RECORDING
    status_line = NOT_RECORDING_LINE
    work_block = None
    paused_work_block = None
    if active is not None:
        state = RECORDING
        started_at = active.record.started_at
        status_line = f"Recording {active.record.workstream} since {describe_since(started_at, now)}"
        work_block = {
            "id": active.id,
            "workstream": active.record.workstream,
            "started_at": work_blocks.format_timestamp(started_at),
        }
    elif pause is not None:
        state = PAUSED
        status_line = f"Paused on {pause.workstream} since {describe_since(pause.paused_at, now)}"
        paused_work_block = {
            "id": pause.work_block_id,
            "workstream": pause.workstream,
            "paused_at": work_blocks.format_timestamp(pause.paused_at),
        }

    # The main workstream first, then the others in the eval config's order.
    workstreams = [{"name": settings.main_workstream, "main": True}]
    for workstream in eval_for_this_account.eval_config.workstreams:
        if workstream != settings.main_workstream:
            workstreams.append({"name": workstream, "main": False})

    # Before the first work block the work-blocks folder is not there yet.
    watch_folder = work_blocks.WorkBlockFiles(log_directory).folder
    if not watch_folder.is_dir():
        watch_folder = log_directory

    return {
        "schema_version": ICON_STATE_SCHEMA_VERSION,
        "state": state,
        "status_line": status_line,
        "account_mode": settings.account_mode,
        "work_block": work_block,
        "paused_work_block": paused_work_block,
        "workstreams": workstreams,
        "enabled": {
            # `goodall start` while recording changes nothing, so it is not offered.
            "record_work_block": state != RECORDING,
            "pause": state == RECORDING,
            # While paused, `goodall stop` forgets the pause.
            "stop_recording": state != NOT_RECORDING,
        },
        "setup": {"usable": True, "reason": None},
        "watch_folder": str(watch_folder),
    }


def unusable_document(failures: list[Failure]) -> dict[str, object]:
    """The document when goodall cannot be used in this account: the same keys, and nothing to offer.

    No work block can be active while goodall cannot find its eval or write
    to its log directory (ADR 0008), so the state is "not recording".
    There is no folder to watch; the helper asks again on its slow poll.
    """
    reason = CANNOT_RECORD_REASON
    for failure in failures:
        if failure.kind == "local_settings_missing":
            reason = NOT_SET_UP_REASON
    return {
        "schema_version": ICON_STATE_SCHEMA_VERSION,
        "state": NOT_RECORDING,
        "status_line": NOT_RECORDING_LINE,
        "account_mode": None,
        "work_block": None,
        "paused_work_block": None,
        "workstreams": [],
        "enabled": {"record_work_block": False, "pause": False, "stop_recording": False},
        "setup": {"usable": False, "reason": reason},
        "watch_folder": None,
    }


def describe_since(moment: datetime, now: datetime) -> str:
    """A moment in this Mac's local time, short enough for a menu: `09:30`, or `Mon 16 Nov, 09:30` on another day."""
    local_moment = moment.astimezone()
    if local_moment.date() == now.astimezone().date():
        return local_moment.strftime("%H:%M")
    return local_moment.strftime("%a %d %b, %H:%M")
