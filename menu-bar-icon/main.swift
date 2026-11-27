// The menu bar icon: goodall's icon at the top right of the Mac's screen (ADR 0024).
//
// This is the only Swift in goodall, because an icon in the menu bar cannot be made with
// Python's standard library. It is written to be read top to bottom by someone who does
// not know Swift, and it is kept small by one rule:
//
//     THIS PROGRAM DECIDES NOTHING.
//
// It asks goodall what to show by running `goodall icon-state --json`, and draws that.
// Each menu item runs exactly one goodall command. Which items can be chosen, the words
// of the status line, the list of workstreams and which of them is the main one all come
// from goodall (src/goodall/icon_state.py). If you want to change what the icon says or
// when an item is offered, change the Python, not this file.
//
// What it does, in order:
//
//   1. Puts an icon in the menu bar, with one of three looks: recording, paused, not
//      recording. While recording it also shows how long, as hours:minutes.
//   2. Asks goodall for the state when it starts, whenever the folder goodall names in
//      `watch_folder` changes (so a `goodall start` typed in a terminal shows within a
//      second), after every menu command, and once a minute in case anything was missed.
//   3. Builds the menu from the answer. It never asks a question: there is no window, no
//      dialog and no text field anywhere in this file.
//
// How it finds goodall: whoever starts this program puts the full path of the `goodall`
// program in the environment variable GOODALL_COMMAND. `goodall start` does that when it
// brings the icon back, and the LaunchAgent that starts the icon at sign-in will too. The
// program never searches for goodall, because it is started with almost no PATH and a
// search would be a decision made here.
//
// When a command fails: nothing pops up. The menu gains two gray lines saying which item
// did not work and where the details are, the icon shows a "!" until the menu is next
// opened, and everything goodall printed is added to the log file. goodall's own messages
// are not shown in the menu because they use words the menu must not (ADR 0021).
//
// A few Swift words, for reading on:
//   let / var        a name whose value cannot / can change
//   String?          a String that may be missing ("nil"); `if let x = maybe` unwraps it
//   func             a function;  `@objc func` is one a menu item is allowed to call
//   { ... in ... }   a block of code handed to something else to run later
//   [weak self]      "do not keep this object alive just for this block"

import AppKit

// ---------------------------------------------------------------------------------------
// Settings read once, from the environment this program was started with
// ---------------------------------------------------------------------------------------

let environment = ProcessInfo.processInfo.environment

// The full path of the goodall program, or nil when nobody said.
let goodallCommand: String? = environment["GOODALL_COMMAND"]

// Where failed commands are written down. The variable exists so a test run can keep its
// log out of the real home folder.
let logPath: String =
    environment["GOODALL_MENU_BAR_LOG"] ?? (NSHomeDirectory() + "/Library/Logs/goodall/menu-bar-icon.log")

// How often to ask goodall again even if nothing seemed to change, and how often to redraw
// the hours:minutes beside the icon (which needs no goodall, only the clock).
let slowPollSeconds = 60.0
let clockTickSeconds = 15.0

// After the watched folder changes, wait this long before asking, so that the several
// changes one goodall command makes lead to one question, not five.
let settleSeconds = 0.3

// ---------------------------------------------------------------------------------------
// Every goodall command this program runs. Nothing else in this file names one.
// tests/test_menu_bar_icon.py reads the "goodall-command:" notes and checks each exists.
// ---------------------------------------------------------------------------------------

let askStateArguments = ["icon-state", "--json"]  // goodall-command: icon-state
let pauseArguments = ["pause"]  // goodall-command: pause
let stopArguments = ["stop"]  // goodall-command: stop
let settingsArguments = ["settings"]  // goodall-command: settings

func recordArguments(workstream: String) -> [String] {
    return ["start", "--workstream", workstream]  // goodall-command: start
}

// ---------------------------------------------------------------------------------------
// The document `goodall icon-state --json` prints (docs/menu-bar-icon.md describes it).
// Only the keys this program draws are listed. `Decodable` lets Swift fill these in from
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
}

struct WorkBlock: Decodable {
    let startedAt: String  // for example 2026-11-16T09:30:00Z
}

struct Workstream: Decodable {
    let name: String
    let main: Bool
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

// ---------------------------------------------------------------------------------------
// Running goodall
// ---------------------------------------------------------------------------------------

struct CommandResult {
    let exitCode: Int32
    let output: String
}

// Run goodall with these arguments, wait for it to finish, and return what it printed.
//
// What it prints goes to a temporary file, not a pipe. `goodall start` leaves a program
// running in the background, and a pipe is only "finished" once every program holding it
// has let go; a file can be read as soon as goodall itself exits.
//
// `keepErrors` says whether goodall's error messages are wanted in the output too. They
// are for a menu command (they go in the log); they are not when asking for the state,
// where the output has to be exactly one JSON document.
func runGoodall(_ arguments: [String], keepErrors: Bool) -> CommandResult {
    guard let goodallPath = goodallCommand else {
        return CommandResult(exitCode: -1, output: "GOODALL_COMMAND is not set.")
    }
    let outputURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("goodall-menu-bar-\(UUID().uuidString).txt")
    FileManager.default.createFile(atPath: outputURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
    defer { try? FileManager.default.removeItem(at: outputURL) }  // "defer" runs when the function ends
    guard let outputFile = try? FileHandle(forWritingTo: outputURL) else {
        return CommandResult(exitCode: -1, output: "A temporary file could not be made.")
    }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: goodallPath)
    process.arguments = arguments
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = outputFile
    process.standardError = keepErrors ? outputFile : FileHandle.nullDevice
    do {
        try process.run()
    } catch {
        return CommandResult(exitCode: -1, output: "\(goodallPath) could not be started: \(error.localizedDescription)")
    }
    process.waitUntilExit()
    try? outputFile.close()

    let output = (try? String(contentsOf: outputURL, encoding: .utf8)) ?? ""
    return CommandResult(exitCode: process.terminationStatus, output: output)
}

// Add one failed command to the log file, with everything goodall printed.
func writeToLog(title: String, arguments: [String], result: CommandResult) {
    let when = ISO8601DateFormatter().string(from: Date())
    let entry =
        "\(when)  \"\(title)\" did not work.\n"
        + "Command: goodall \(arguments.joined(separator: " "))\n"
        + "Exit code: \(result.exitCode)\n"
        + result.output
        + "\n\n"
    let folder = (logPath as NSString).deletingLastPathComponent
    try? FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
    if !FileManager.default.fileExists(atPath: logPath) {
        // Readable by this macOS user account only: the messages name folders and people.
        FileManager.default.createFile(atPath: logPath, contents: nil, attributes: [.posixPermissions: 0o600])
    }
    if let logFile = FileHandle(forWritingAtPath: logPath) {
        logFile.seekToEndOfFile()
        logFile.write(entry.data(using: .utf8) ?? Data())
        logFile.closeFile()
    }
}

// The log file's path as a person would write it, with ~ for the home folder.
func logPathForPeople() -> String {
    return (logPath as NSString).abbreviatingWithTildeInPath
}

// ---------------------------------------------------------------------------------------
// The icon and its menu
// ---------------------------------------------------------------------------------------

final class MenuBarIcon: NSObject, NSApplicationDelegate, NSMenuDelegate {
    // The icon itself. macOS calls an icon in the menu bar a "status item".
    var statusItem: NSStatusItem!

    // What goodall last said. nil until the first answer, or when the answer was unreadable.
    var iconState: IconState?

    // The two gray lines shown after a failed command; empty when the last one worked.
    var failureLines: [String] = []

    // True from a failed command until the person next opens the menu: draws the "!".
    var failureNotSeenYet = false

    // True while a menu command is running, so a second click cannot start another.
    var commandIsRunning = false

    // goodall commands run one at a time, in the order asked, away from the main thread, so
    // the menu bar never freezes while goodall works. Drawing always happens on the main thread.
    let goodallQueue = DispatchQueue(label: "goodall commands")

    // The folder being watched, the watch on it, and the question waiting to be asked.
    var watchedFolder: String?
    var folderWatch: DispatchSourceFileSystemObject?
    var waitingQuestion: DispatchWorkItem?

    // -----------------------------------------------------------------------------------
    // Starting up
    // -----------------------------------------------------------------------------------

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // Digits of equal width, so the icon does not shift sideways as the time changes.
        let fontSize = NSFont.menuBarFont(ofSize: 0).pointSize
        statusItem.button?.font = NSFont.monospacedDigitSystemFont(ofSize: fontSize, weight: .regular)
        draw()
        askGoodallForState()

        // Timers run in ".common" mode so they keep going while a menu is open.
        let slowPoll = Timer(timeInterval: slowPollSeconds, repeats: true) { [weak self] _ in
            self?.askGoodallForState()
        }
        RunLoop.main.add(slowPoll, forMode: .common)
        let clockTick = Timer(timeInterval: clockTickSeconds, repeats: true) { [weak self] _ in
            self?.drawIcon()
        }
        RunLoop.main.add(clockTick, forMode: .common)
    }

    // -----------------------------------------------------------------------------------
    // Asking goodall
    // -----------------------------------------------------------------------------------

    func askGoodallForState() {
        goodallQueue.async {
            let result = runGoodall(askStateArguments, keepErrors: false)
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            // goodall prints the document whether its exit code is 0 or 1, so only the
            // document is looked at. Anything unreadable becomes nil.
            let answer = try? decoder.decode(IconState.self, from: result.output.data(using: .utf8) ?? Data())
            DispatchQueue.main.async {
                self.iconState = answer
                self.draw()
                self.watch(folder: answer?.watchFolder)
            }
        }
    }

    // Run one menu command, then ask for the state again. `afterSuccess` is extra work for
    // the one item that needs it ("Quit goodall").
    func runMenuCommand(title: String, arguments: [String], afterSuccess: (() -> Void)? = nil) {
        commandIsRunning = true
        draw()
        goodallQueue.async {
            let result = runGoodall(arguments, keepErrors: true)
            if result.exitCode != 0 {
                writeToLog(title: title, arguments: arguments, result: result)
            }
            DispatchQueue.main.async {
                self.commandIsRunning = false
                if result.exitCode == 0 {
                    self.failureLines = []
                    self.failureNotSeenYet = false
                    afterSuccess?()
                } else {
                    self.failureLines = ["\"\(title)\" did not work.", "Details are in \(logPathForPeople())"]
                    self.failureNotSeenYet = true
                }
                self.draw()
                self.askGoodallForState()
            }
        }
    }

    // -----------------------------------------------------------------------------------
    // Watching the folder goodall names
    // -----------------------------------------------------------------------------------

    // Watch `folder` and ask goodall again when something in it is added, removed or
    // renamed. The folder is watched, not a file in it, because goodall replaces its files
    // whole (write a new one, rename it into place), and a watch on a file would go on
    // watching the old one.
    func watch(folder: String?) {
        if folder == watchedFolder && folderWatch != nil {
            return
        }
        folderWatch?.cancel()
        folderWatch = nil
        watchedFolder = folder
        guard let folder = folder else { return }

        let descriptor = open(folder, O_EVTONLY)  // "open only to be told about changes"
        if descriptor < 0 {
            // Not there (any more). The slow poll will try again.
            watchedFolder = nil
            return
        }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor, eventMask: [.write, .delete, .rename], queue: .main)
        source.setEventHandler { [weak self] in
            guard let self = self else { return }
            if !source.data.isDisjoint(with: [.delete, .rename]) {
                // The folder itself went away or moved: forget this watch, so the next
                // answer from goodall starts a fresh one.
                self.folderWatch?.cancel()
                self.folderWatch = nil
                self.watchedFolder = nil
            }
            self.askSoon()
        }
        source.setCancelHandler { close(descriptor) }
        source.resume()
        folderWatch = source
    }

    // Ask goodall for the state once things have been quiet for a moment.
    func askSoon() {
        waitingQuestion?.cancel()
        let question = DispatchWorkItem { [weak self] in self?.askGoodallForState() }
        waitingQuestion = question
        DispatchQueue.main.asyncAfter(deadline: .now() + settleSeconds, execute: question)
    }

    // -----------------------------------------------------------------------------------
    // Drawing
    // -----------------------------------------------------------------------------------

    func draw() {
        drawIcon()
        statusItem.menu = buildMenu()
    }

    // The three looks. The pictures are SF Symbols, which come with macOS.
    func drawIcon() {
        guard let button = statusItem.button else { return }
        let state = iconState?.state ?? "not_recording"
        var symbolName = "circle"
        var description = "goodall: not recording"
        var text = ""
        if state == "recording" {
            symbolName = "record.circle.fill"
            description = "goodall: recording"
            text = elapsedText()
        } else if state == "paused" {
            symbolName = "pause.circle"
            description = "goodall: paused"
        }
        if failureNotSeenYet {
            text += " !"
        }

        var image = NSImage(systemSymbolName: symbolName, accessibilityDescription: description)
        if state == "recording" {
            // Red, the color every recording light has. The other two looks are "templates":
            // macOS draws them black or white to suit the menu bar.
            image = image?.withSymbolConfiguration(NSImage.SymbolConfiguration(hierarchicalColor: .systemRed))
            image?.isTemplate = false
        } else {
            image?.isTemplate = true
        }
        button.image = image
        button.imagePosition = .imageLeading
        button.title = text.isEmpty ? "" : " " + text
        button.toolTip = iconState?.statusLine ?? description
    }

    // How long the active work block has been recording, as hours:minutes, such as 0:42.
    // Worked out from the start time goodall gave and this Mac's clock, so it can tick
    // without running goodall every few seconds.
    func elapsedText() -> String {
        guard let startedAt = iconState?.workBlock?.startedAt,
            let start = ISO8601DateFormatter().date(from: startedAt)
        else { return "" }
        let minutes = max(0, Int(Date().timeIntervalSince(start)) / 60)
        return String(format: "%d:%02d", minutes / 60, minutes % 60)
    }

    func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false  // each item is enabled or not exactly as set below
        menu.delegate = self

        // Whether goodall can be run at all, and whether its last answer could be read.
        let goodallIsKnown = goodallCommand != nil
        let canRunCommands = goodallIsKnown && !commandIsRunning

        // The status line, then any lines explaining a problem. All gray: they do nothing.
        var grayLines: [String] = []
        if !goodallIsKnown {
            grayLines.append("This icon was not told where goodall is.")
            grayLines.append("Choose Quit goodall, then run goodall start in Terminal.")
        } else if commandIsRunning {
            grayLines.append("Working…")
        } else if let iconState = iconState {
            grayLines.append(iconState.statusLine)
            if let reason = iconState.setup.reason {
                grayLines.append(reason)
            }
        } else {
            grayLines.append("goodall did not answer.")
            grayLines.append("Run goodall status in Terminal to see why.")
        }
        for line in grayLines + failureLines {
            let item = NSMenuItem(title: line, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }
        menu.addItem(NSMenuItem.separator())

        // "Record work block", with one item per workstream. goodall lists the main one first.
        let record = NSMenuItem(title: "Record work block", action: nil, keyEquivalent: "")
        let workstreamMenu = NSMenu()
        workstreamMenu.autoenablesItems = false
        for workstream in iconState?.workstreams ?? [] {
            let title = workstream.main ? "\(workstream.name) (main)" : workstream.name
            let item = commandItem(title: title, arguments: recordArguments(workstream: workstream.name))
            item.isEnabled = canRunCommands
            workstreamMenu.addItem(item)
        }
        record.submenu = workstreamMenu
        record.isEnabled =
            canRunCommands && (iconState?.enabled.recordWorkBlock ?? false) && workstreamMenu.items.count > 0
        menu.addItem(record)

        let pause = commandItem(title: "Pause", arguments: pauseArguments)
        pause.isEnabled = canRunCommands && (iconState?.enabled.pause ?? false)
        menu.addItem(pause)

        let stop = commandItem(title: "Stop recording", arguments: stopArguments)
        stop.isEnabled = canRunCommands && (iconState?.enabled.stopRecording ?? false)
        menu.addItem(stop)

        menu.addItem(NSMenuItem.separator())

        let settings = commandItem(title: "Settings…", arguments: settingsArguments)
        settings.isEnabled = canRunCommands
        menu.addItem(settings)

        let quit = NSMenuItem(title: "Quit goodall", action: #selector(quitChosen(_:)), keyEquivalent: "q")
        quit.target = self
        quit.isEnabled = !commandIsRunning
        menu.addItem(quit)
        return menu
    }

    // A menu item that runs one goodall command. The arguments ride along on the item
    // ("representedObject") and `commandChosen` reads them back when it is clicked.
    func commandItem(title: String, arguments: [String]) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: #selector(commandChosen(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = arguments
        return item
    }

    // -----------------------------------------------------------------------------------
    // What happens when a menu item is chosen
    // -----------------------------------------------------------------------------------

    @objc func commandChosen(_ sender: NSMenuItem) {
        guard let arguments = sender.representedObject as? [String] else { return }
        // A workstream sits inside "Record work block"; a failure should name that item.
        let title = sender.parent?.title ?? sender.title
        runMenuCommand(title: title, arguments: arguments)
    }

    // "Quit goodall": stop any recording cleanly, exactly as "Stop recording" would, then
    // leave. If stopping fails the icon stays, because goodall would still be recording
    // with nothing on screen to say so.
    //
    // Leaving is done with exit status 0, which tells the LaunchAgent that this was meant
    // and the icon should not be started again until the next sign-in.
    @objc func quitChosen(_ sender: NSMenuItem) {
        let somethingToStop = goodallCommand != nil && (iconState?.enabled.stopRecording ?? false)
        if somethingToStop {
            runMenuCommand(title: sender.title, arguments: stopArguments, afterSuccess: { exit(0) })
        } else {
            exit(0)
        }
    }

    // macOS calls this as the menu opens. The person is now looking at the failure lines,
    // so the "!" beside the icon has done its job.
    func menuWillOpen(_ menu: NSMenu) {
        if failureNotSeenYet {
            failureNotSeenYet = false
            drawIcon()
        }
    }
}

// ---------------------------------------------------------------------------------------
// The program starts here.
// ---------------------------------------------------------------------------------------

let application = NSApplication.shared
let menuBarIcon = MenuBarIcon()
application.delegate = menuBarIcon
// ".accessory" means: no icon in the Dock and no window, only the menu bar.
application.setActivationPolicy(.accessory)
application.run()
