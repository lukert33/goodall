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
//   1. Puts an icon in the menu bar: a pair of binoculars, goodall's mark, with one of
//      three looks for recording, paused and not recording. It shows no running time.
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

// How often to ask goodall again even if nothing seemed to change.
let slowPollSeconds = 60.0

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

// The gray first line of the menu while each command runs. Each says what that command is
// doing; none says "work", because in goodall that word means a work block.
let recordBusyLine = "Preparing to record…"
let pauseBusyLine = "Pausing…"
let stopBusyLine = "Stopping…"
let settingsBusyLine = "Opening settings…"

// One command a menu item runs: its arguments, and the line to show while it runs.
struct MenuCommand {
    let arguments: [String]
    let busyLine: String
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

    // Its menu. There is only ever this one: each redraw empties it and fills it again,
    // because a menu that is open goes on showing itself, and a new menu put in its place
    // would not appear until the person closed and reopened it.
    let menu = NSMenu()

    // What goodall last said. nil until the first answer, or when the answer was unreadable.
    var iconState: IconState?

    // The two gray lines shown after a failed command; empty when the last one worked.
    var failureLines: [String] = []

    // True from a failed command until the person next opens the menu: draws the "!".
    var failureNotSeenYet = false

    // While a menu command is running, the line saying what it is doing; nil when none is.
    // Every item is gray meanwhile, so a second click cannot start another command.
    var busyLine: String?

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
        menu.autoenablesItems = false  // each item is enabled or not exactly as set in fillMenu
        menu.delegate = self
        statusItem.menu = menu
        draw()
        askGoodallForState()

        // The timer runs in ".common" mode so it keeps going while a menu is open.
        let slowPoll = Timer(timeInterval: slowPollSeconds, repeats: true) { [weak self] _ in
            self?.askGoodallForState()
        }
        RunLoop.main.add(slowPoll, forMode: .common)
    }

    // -----------------------------------------------------------------------------------
    // Asking goodall
    // -----------------------------------------------------------------------------------

    // `afterCommand` is true when a menu command has just finished. Its busy line is cleared
    // only here, in the same redraw as goodall's fresh answer: cleared any earlier, the menu
    // would flash the state from before the command.
    func askGoodallForState(afterCommand: Bool = false) {
        goodallQueue.async {
            let result = runGoodall(askStateArguments, keepErrors: false)
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            // goodall prints the document whether its exit code is 0 or 1, so only the
            // document is looked at. Anything unreadable becomes nil.
            let answer = try? decoder.decode(IconState.self, from: result.output.data(using: .utf8) ?? Data())
            DispatchQueue.main.async {
                self.iconState = answer
                if afterCommand {
                    self.busyLine = nil
                }
                self.draw()
                self.watch(folder: answer?.watchFolder)
            }
        }
    }

    // Run one menu command, then ask for the state again. `afterSuccess` is extra work for
    // the one item that needs it ("Quit goodall").
    func runMenuCommand(title: String, command: MenuCommand, afterSuccess: (() -> Void)? = nil) {
        busyLine = command.busyLine
        draw()
        goodallQueue.async {
            let result = runGoodall(command.arguments, keepErrors: true)
            if result.exitCode != 0 {
                writeToLog(title: title, arguments: command.arguments, result: result)
            }
            DispatchQueue.main.async {
                if result.exitCode == 0 {
                    self.failureLines = []
                    self.failureNotSeenYet = false
                    afterSuccess?()
                } else {
                    self.failureLines = ["\"\(title)\" did not work.", "Details are in \(logPathForPeople())"]
                    self.failureNotSeenYet = true
                }
                // Only the icon now, for the "!". The menu keeps its busy line until goodall
                // says what the command changed.
                self.drawIcon()
                self.askGoodallForState(afterCommand: true)
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
        fillMenu()
    }

    // The three looks, all the same binoculars:
    //   not recording   an outline, with empty lenses
    //   paused          an outline, with an upright bar in each lens: together, a pause mark
    //   recording       orange, with a filled dot in each lens, like two recording lights
    // The icon shows no running time. A clock that reads 0:00 for the first minute looks
    // broken, and the icon's job is only to say whether goodall is recording.
    func drawIcon() {
        guard let button = statusItem.button else { return }
        let state = iconState?.state ?? "not_recording"
        var description = "goodall: not recording"
        if state == "recording" {
            description = "goodall: recording"
        } else if state == "paused" {
            description = "goodall: paused"
        }
        button.image = binocularsImage(state: state, description: description)
        button.imagePosition = .imageLeading
        button.title = failureNotSeenYet ? " !" : ""
        button.toolTip = iconState?.statusLine ?? description
    }

    // Draws goodall's mark: a pair of binoculars seen from behind, the same drawing as the
    // onboarding page's masthead. It is drawn here, with lines and circles, so that no
    // picture file has to be shipped. The numbers are positions on a grid 46 wide and 34
    // tall, with (0, 0) at the top left; the grid is then shrunk to the menu bar's height.
    func binocularsImage(state: String, description: String) -> NSImage {
        let gridWidth: CGFloat = 46
        let gridHeight: CGFloat = 34
        let height: CGFloat = 16  // points; the menu bar is 22 or more tall
        let scale = height / gridHeight
        let size = NSSize(width: gridWidth * scale, height: height)
        let leftLens = NSPoint(x: 11, y: 23)
        let rightLens = NSPoint(x: 35, y: 23)

        let image = NSImage(size: size, flipped: true) { _ in
            let shrink = NSAffineTransform()
            shrink.scale(by: scale)
            shrink.concat()
            // Black here is only a stand-in when the image is a "template" (see below).
            let color: NSColor = state == "recording" ? .systemOrange : .black
            color.setStroke()
            color.setFill()

            let outline = NSBezierPath()
            outline.lineWidth = 2.6
            outline.lineCapStyle = .round
            outline.lineJoinStyle = .round
            // The two big lenses.
            for lens in [leftLens, rightLens] {
                outline.appendOval(in: NSRect(x: lens.x - 9, y: lens.y - 9, width: 18, height: 18))
            }
            // The two eyepieces, above the lenses.
            outline.move(to: NSPoint(x: 8, y: 14.5))
            outline.line(to: NSPoint(x: 11, y: 4))
            outline.line(to: NSPoint(x: 17, y: 4))
            outline.line(to: NSPoint(x: 19, y: 13))
            outline.move(to: NSPoint(x: 38, y: 14.5))
            outline.line(to: NSPoint(x: 35, y: 4))
            outline.line(to: NSPoint(x: 29, y: 4))
            outline.line(to: NSPoint(x: 27, y: 13))
            // The bridge that joins the two halves.
            outline.move(to: NSPoint(x: 19, y: 13))
            outline.line(to: NSPoint(x: 27, y: 13))
            outline.move(to: NSPoint(x: 20, y: 21))
            outline.line(to: NSPoint(x: 26, y: 21))
            outline.stroke()

            // What is inside the lenses says which of the three looks this is.
            for lens in [leftLens, rightLens] {
                if state == "recording" {
                    NSBezierPath(ovalIn: NSRect(x: lens.x - 5, y: lens.y - 5, width: 10, height: 10)).fill()
                } else if state == "paused" {
                    let bar = NSBezierPath()
                    bar.lineWidth = 3.4
                    bar.lineCapStyle = .round
                    bar.move(to: NSPoint(x: lens.x, y: lens.y - 4.5))
                    bar.line(to: NSPoint(x: lens.x, y: lens.y + 4.5))
                    bar.stroke()
                }
            }
            return true
        }
        // A "template" image is one macOS recolors black or white to suit the menu bar.
        // The recording look stays orange: macOS's own bright, citrus orange, which the
        // owner chose over red.
        image.isTemplate = state != "recording"
        image.accessibilityDescription = description
        return image
    }

    // Empty the menu and fill it again from what goodall last said.
    func fillMenu() {
        menu.removeAllItems()

        // Whether goodall can be run at all, and whether its last answer could be read.
        let goodallIsKnown = goodallCommand != nil
        let canRunCommands = goodallIsKnown && busyLine == nil

        // The status line, then any lines explaining a problem. All gray: they do nothing.
        var grayLines: [String] = []
        if !goodallIsKnown {
            grayLines.append("This icon was not told where goodall is.")
            grayLines.append("Choose Quit goodall, then run goodall start in Terminal.")
        } else if let busyLine = busyLine {
            grayLines.append(busyLine)
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
            let item = commandItem(
                title: title,
                command: MenuCommand(arguments: recordArguments(workstream: workstream.name), busyLine: recordBusyLine))
            item.isEnabled = canRunCommands
            workstreamMenu.addItem(item)
        }
        record.submenu = workstreamMenu
        record.isEnabled =
            canRunCommands && (iconState?.enabled.recordWorkBlock ?? false) && workstreamMenu.items.count > 0
        menu.addItem(record)

        let pause = commandItem(title: "Pause", command: MenuCommand(arguments: pauseArguments, busyLine: pauseBusyLine))
        pause.isEnabled = canRunCommands && (iconState?.enabled.pause ?? false)
        menu.addItem(pause)

        let stop = commandItem(
            title: "Stop recording", command: MenuCommand(arguments: stopArguments, busyLine: stopBusyLine))
        stop.isEnabled = canRunCommands && (iconState?.enabled.stopRecording ?? false)
        menu.addItem(stop)

        menu.addItem(NSMenuItem.separator())

        let settings = commandItem(
            title: "Settings…", command: MenuCommand(arguments: settingsArguments, busyLine: settingsBusyLine))
        settings.isEnabled = canRunCommands
        menu.addItem(settings)

        let quit = NSMenuItem(title: "Quit goodall", action: #selector(quitChosen(_:)), keyEquivalent: "q")
        quit.target = self
        quit.isEnabled = busyLine == nil
        menu.addItem(quit)
    }

    // A menu item that runs one goodall command. The command rides along on the item
    // ("representedObject") and `commandChosen` reads it back when it is clicked.
    func commandItem(title: String, command: MenuCommand) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: #selector(commandChosen(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = command
        return item
    }

    // -----------------------------------------------------------------------------------
    // What happens when a menu item is chosen
    // -----------------------------------------------------------------------------------

    @objc func commandChosen(_ sender: NSMenuItem) {
        guard let command = sender.representedObject as? MenuCommand else { return }
        // A workstream sits inside "Record work block"; a failure should name that item.
        let title = sender.parent?.title ?? sender.title
        runMenuCommand(title: title, command: command)
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
            runMenuCommand(
                title: sender.title, command: MenuCommand(arguments: stopArguments, busyLine: stopBusyLine),
                afterSuccess: { exit(0) })
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
