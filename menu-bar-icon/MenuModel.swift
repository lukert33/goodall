// The menu bar icon's model: what the icon and its menu show, decided in one place (ADR 0025).
//
// The helper is two files. This one decides; main.swift draws. Everything here is plain
// Swift with no AppKit (Apple's library for windows and menus), so it can run without a
// screen: tests/menu_bar_icon_driver/main.swift feeds it events and prints the menu it
// would draw, and tests/test_menu_bar_icon_model.py checks that output. main.swift does no
// more than pass the same events in, draw `rows` and `look`, and run goodall.
//
// "Decides" is still narrow (ADR 0024). goodall decides what can be done and says so in
// `goodall icon-state --json`: the status line, which items are enabled, the workstreams
// and which is the main one. This file turns that answer, and what the person has just
// done, into rows of a menu: which line comes first while a command runs, when a failure
// is shown and when its mark clears, what Quit goodall has to do first, and when to ask
// macOS for permission to show notifications.
//
// The model changes only through four events, each a function below:
//   stateAnswered     goodall answered `icon-state --json`; says whether to ask for permission
//   commandStarted    the person chose an item that runs a goodall command
//   commandFinished   that command ended, with this exit code
//   menuOpened        the person opened the menu
//
// Most commands hold the menu: every item is gray until the command ends, so a second
// click cannot start a second command. Settings… does not. `goodall settings` opens the
// setup page and then waits, up to an hour, for the person to press Save settings; a menu
// held all that time would leave no way to stop recording. So Settings… waits for the
// person on its own (`holdsTheMenu` false), the rest of the menu stays usable, and only
// Settings… itself is gray until its command ends, so a second click opens no second page.
//
// A few Swift words, for reading on:
//   let / var        a name whose value cannot / can change
//   String?          a String that may be missing ("nil"); `if let x = maybe` unwraps it
//   struct           a record of named values
//   mutating func    a function that changes the struct it belongs to
//   enum             a value that is exactly one of a few named cases

import Foundation

// ---------------------------------------------------------------------------------------
// Every goodall command the helper runs. Nothing else in the helper names one.
// tests/test_menu_bar_icon_helper.py reads the "goodall-command:" notes and checks each
// is a command goodall has.
// ---------------------------------------------------------------------------------------

let askStateArguments = ["icon-state", "--json"]  // goodall-command: icon-state
let pauseArguments = ["pause"]  // goodall-command: pause
let stopArguments = ["stop"]  // goodall-command: stop
let settingsArguments = ["settings"]  // goodall-command: settings

func recordArguments(workstream: String) -> [String] {
    return ["start", "--workstream", workstream]  // goodall-command: start
}

// The gray first line of the menu while each command runs. Each says what that command is
// doing; none says "work", because in goodall that word means a work block.
let recordBusyLine = "Preparing to record…"
let pauseBusyLine = "Pausing…"
let stopBusyLine = "Stopping…"
let settingsBusyLine = "Opening settings…"

// The gray line under the status line while the settings page waits for the person.
let settingsWaitingLine = "Settings… is open in your browser."

// The titles of the items that are not one per workstream.
let recordTitle = "Record work block"
let pauseTitle = "Pause"
let stopTitle = "Stop recording"
let settingsTitle = "Settings…"
let quitTitle = "Quit goodall"

// ---------------------------------------------------------------------------------------
// The document `goodall icon-state --json` prints (docs/menu-bar-icon.md describes it).
// Only the keys the helper draws are listed. `Decodable` lets Swift fill these in from
// JSON; a key written `status_line` in the JSON arrives here as `statusLine`.
// ---------------------------------------------------------------------------------------

struct IconState: Decodable {
    let state: String  // "recording", "paused" or "not_recording"
    let statusLine: String
    let workBlock: WorkBlock?
    let workstreams: [Workstream]
    let enabled: EnabledItems
    let setup: Setup
    let watchFolder: String?
    let askNotificationPermission: Bool
}

struct WorkBlock: Decodable {
    let startedAt: String  // for example 2026-11-16T09:30:00Z
}

// goodall also says which workstream is the main one; the menu does not show it.
struct Workstream: Decodable {
    let name: String
}

struct EnabledItems: Decodable {
    let recordWorkBlock: Bool
    let pause: Bool
    let stopRecording: Bool
}

struct Setup: Decodable {
    let usable: Bool
    let reason: String?
}

// Read what `goodall icon-state --json` printed. goodall prints the document whether its
// exit code is 0 or 1, so only the document is looked at. Anything unreadable becomes nil.
func readIconState(_ output: String) -> IconState? {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try? decoder.decode(IconState.self, from: output.data(using: .utf8) ?? Data())
}

// ---------------------------------------------------------------------------------------
// What the model hands to main.swift to draw
// ---------------------------------------------------------------------------------------

// One goodall command a menu item runs.
struct MenuCommand {
    let arguments: [String]
    // The menu's first line while it runs.
    let busyLine: String
    // The item a failure line names: "Record work block" for a workstream, since the
    // workstreams sit inside it.
    let itemTitle: String
    // False for a command that waits for the person (Settings…): the menu goes on working
    // while it runs, and main.swift runs it apart from the commands that hold the menu.
    var holdsTheMenu = true
}

// One row of the menu. A row is a gray line, a separator, an item that runs a command, an
// item that opens a submenu, or Quit goodall.
struct MenuRow {
    let title: String
    let enabled: Bool
    var isSeparator = false
    var command: MenuCommand? = nil
    var submenu: [MenuRow]? = nil
    var isQuit = false
}

// The three looks of the binoculars.
enum IconLook: String {
    case notRecording = "not_recording"  // an outline with empty lenses
    case recording  // orange, with a dot in each lens
    case paused  // an outline with a bar in each lens
}

// What choosing Quit goodall has to do.
enum QuitAction {
    case leaveNow
    // Stop recording first, and leave only if that worked: otherwise goodall would go on
    // recording with nothing on screen to say so.
    case stopThenLeave(MenuCommand)
}

// ---------------------------------------------------------------------------------------
// The model
// ---------------------------------------------------------------------------------------

struct MenuModel {
    // Whether the helper was told where goodall is (GOODALL_COMMAND).
    let goodallIsKnown: Bool

    // The log file's path as a person would write it, for the failure line.
    let logPathForPeople: String

    // What goodall last said. nil until the first answer, or when the answer was unreadable.
    var iconState: IconState? = nil

    // The menu command running now. It is set when the command is chosen and cleared only
    // when goodall next answers after the command has finished: cleared any earlier, the
    // menu would show, for a moment, the state from before the command.
    var runningCommand: MenuCommand? = nil
    var runningCommandHasFinished = false

    // The command that waits for the person (Settings…) while it runs; nil otherwise. It
    // does not hold the menu, and is cleared as soon as it finishes.
    var waitingCommand: MenuCommand? = nil

    // The two gray lines shown after a failed command; empty when the last one worked.
    var failureLines: [String] = []

    // True from a failed command until the person next opens the menu: draws the "!".
    var failureNotSeenYet = false

    // How many commands have failed in this run, and how many had when each running command
    // started. A command that works clears only failures from before it started: with two
    // running at once, Pause working must not hide that Settings… failed meanwhile.
    var failureCount = 0
    var failureCountWhenRunningStarted = 0
    var failureCountWhenWaitingStarted = 0

    // Whether this run of the helper has asked macOS for permission to show notifications.
    var notificationPermissionAsked = false

    init(goodallIsKnown: Bool, logPathForPeople: String) {
        self.goodallIsKnown = goodallIsKnown
        self.logPathForPeople = logPathForPeople
    }

    // -----------------------------------------------------------------------------------
    // The four events
    // -----------------------------------------------------------------------------------

    // Returns true when main.swift should ask macOS, now, for permission to show
    // notifications. goodall says when that may be (`ask_notification_permission`, true
    // once goodall is set up); the model asks only the first time it hears so in this run.
    // macOS itself shows its question only once ever, and a question dismissed counts as
    // "no", which is why goodall waits until setup has told the person it is coming.
    mutating func stateAnswered(output: String) -> Bool {
        iconState = readIconState(output)
        if runningCommandHasFinished {
            runningCommand = nil
            runningCommandHasFinished = false
        }
        if iconState?.askNotificationPermission == true && !notificationPermissionAsked {
            notificationPermissionAsked = true
            return true
        }
        return false
    }

    mutating func commandStarted(_ command: MenuCommand) {
        if !command.holdsTheMenu {
            waitingCommand = command
            failureCountWhenWaitingStarted = failureCount
            return
        }
        runningCommand = command
        runningCommandHasFinished = false
        failureCountWhenRunningStarted = failureCount
    }

    // `command` is the one that ended. Two can be running: Settings…, waiting for the
    // person, and one that holds the menu.
    mutating func commandFinished(_ command: MenuCommand, exitCode: Int32) {
        let failureCountWhenStarted: Int
        if !command.holdsTheMenu {
            guard waitingCommand != nil else { return }
            waitingCommand = nil
            failureCountWhenStarted = failureCountWhenWaitingStarted
        } else {
            guard runningCommand != nil else { return }
            runningCommandHasFinished = true
            failureCountWhenStarted = failureCountWhenRunningStarted
        }
        if exitCode != 0 {
            failureCount += 1
            failureLines = ["\"\(command.itemTitle)\" did not work.", "Details are in \(logPathForPeople)"]
            failureNotSeenYet = true
        } else if failureCount == failureCountWhenStarted {
            failureLines = []
            failureNotSeenYet = false
        }
    }

    // The person is now looking at the failure lines, so the "!" has done its job.
    mutating func menuOpened() {
        failureNotSeenYet = false
    }

    // -----------------------------------------------------------------------------------
    // What to draw
    // -----------------------------------------------------------------------------------

    var look: IconLook {
        if iconState?.state == "recording" {
            return .recording
        }
        if iconState?.state == "paused" {
            return .paused
        }
        return .notRecording
    }

    // Read aloud by VoiceOver, and the tool tip until goodall has answered.
    var lookDescription: String {
        switch look {
        case .recording: return "goodall: recording"
        case .paused: return "goodall: paused"
        case .notRecording: return "goodall: not recording"
        }
    }

    var toolTip: String {
        return iconState?.statusLine ?? lookDescription
    }

    // The "!" beside the binoculars.
    var showsFailureMark: Bool {
        return failureNotSeenYet
    }

    var rows: [MenuRow] {
        let canRunCommands = goodallIsKnown && runningCommand == nil

        // The status line, then any lines explaining a problem. All gray: they do nothing.
        var grayLines: [String] = []
        if !goodallIsKnown {
            grayLines.append("This icon was not told where goodall is.")
            grayLines.append("Choose Quit goodall, then run goodall start in Terminal.")
        } else if let runningCommand = runningCommand {
            grayLines.append(runningCommand.busyLine)
        } else if let iconState = iconState {
            grayLines.append(iconState.statusLine)
            if let reason = iconState.setup.reason {
                grayLines.append(reason)
            }
        } else {
            grayLines.append("goodall did not answer.")
            grayLines.append("Run goodall status in Terminal to see why.")
        }
        if goodallIsKnown && runningCommand == nil && waitingCommand != nil {
            grayLines.append(settingsWaitingLine)
        }

        var rows: [MenuRow] = []
        for line in grayLines + failureLines {
            rows.append(MenuRow(title: line, enabled: false))
        }
        rows.append(MenuRow(title: "", enabled: false, isSeparator: true))

        // "Record work block", with one item per workstream. goodall lists the main one first;
        // it is not marked, because a mark invites people to change which one is main, and
        // that matters little to them.
        var workstreamRows: [MenuRow] = []
        for workstream in iconState?.workstreams ?? [] {
            let title = workstream.name
            let command = MenuCommand(
                arguments: recordArguments(workstream: workstream.name), busyLine: recordBusyLine,
                itemTitle: recordTitle)
            workstreamRows.append(MenuRow(title: title, enabled: canRunCommands, command: command))
        }
        let recordEnabled =
            canRunCommands && (iconState?.enabled.recordWorkBlock ?? false) && !workstreamRows.isEmpty
        rows.append(MenuRow(title: recordTitle, enabled: recordEnabled, submenu: workstreamRows))

        let pause = MenuCommand(arguments: pauseArguments, busyLine: pauseBusyLine, itemTitle: pauseTitle)
        rows.append(
            MenuRow(title: pauseTitle, enabled: canRunCommands && (iconState?.enabled.pause ?? false), command: pause))

        let stop = MenuCommand(arguments: stopArguments, busyLine: stopBusyLine, itemTitle: stopTitle)
        rows.append(
            MenuRow(
                title: stopTitle, enabled: canRunCommands && (iconState?.enabled.stopRecording ?? false), command: stop))

        rows.append(MenuRow(title: "", enabled: false, isSeparator: true))

        let settings = MenuCommand(
            arguments: settingsArguments, busyLine: settingsBusyLine, itemTitle: settingsTitle, holdsTheMenu: false)
        rows.append(MenuRow(title: settingsTitle, enabled: canRunCommands && waitingCommand == nil, command: settings))

        rows.append(MenuRow(title: quitTitle, enabled: runningCommand == nil, isQuit: true))
        return rows
    }

    // Quit goodall stops any recording cleanly, exactly as Stop recording would. A paused
    // work block is stopped too: goodall enables Stop recording while paused.
    func quitAction() -> QuitAction {
        if goodallIsKnown && (iconState?.enabled.stopRecording ?? false) {
            return .stopThenLeave(MenuCommand(arguments: stopArguments, busyLine: stopBusyLine, itemTitle: quitTitle))
        }
        return .leaveNow
    }
}
