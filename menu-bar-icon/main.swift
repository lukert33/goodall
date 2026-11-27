// The menu bar icon: goodall's icon at the top right of the Mac's screen (ADR 0024).
//
// The helper is the only Swift in goodall, because an icon in the menu bar cannot be made
// with Python's standard library. It is written to be read top to bottom by someone who
// does not know Swift, and it is kept small by one rule:
//
//     THE HELPER DECIDES AS LITTLE AS IT CAN, AND THIS FILE DECIDES NOTHING.
//
// It asks goodall what to show by running `goodall icon-state --json`, and draws that.
// Each menu item runs exactly one goodall command. Which items can be chosen, the words
// of the status line, the list of workstreams and which of them is the main one all come
// from goodall (src/goodall/icon_state.py). If you want to change what the icon says or
// when an item is offered, change the Python, not the Swift.
//
// The little the helper does decide (the line shown while a command runs, when a failure
// is shown, what Quit goodall does first) is in MenuModel.swift, which has no AppKit and
// is tested by tests/test_menu_bar_icon_model.py (ADR 0025). This file tells the model what
// happened, draws what it says, and runs goodall.
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

    // What to show (MenuModel.swift). This file only tells it what happened and draws it.
    var model = MenuModel(goodallIsKnown: goodallCommand != nil, logPathForPeople: logPathForPeople())

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

    func askGoodallForState() {
        goodallQueue.async {
            let result = runGoodall(askStateArguments, keepErrors: false)
            DispatchQueue.main.async {
                self.model.stateAnswered(output: result.output)
                self.draw()
                self.watch(folder: self.model.iconState?.watchFolder)
            }
        }
    }

    // Run one menu command, then ask for the state again. `afterSuccess` is extra work for
    // the one item that needs it ("Quit goodall").
    func runMenuCommand(_ command: MenuCommand, afterSuccess: (() -> Void)? = nil) {
        model.commandStarted(command)
        draw()
        goodallQueue.async {
            let result = runGoodall(command.arguments, keepErrors: true)
            if result.exitCode != 0 {
                writeToLog(title: command.itemTitle, arguments: command.arguments, result: result)
            }
            DispatchQueue.main.async {
                self.model.commandFinished(exitCode: result.exitCode)
                if result.exitCode == 0 {
                    afterSuccess?()
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
        button.image = binocularsImage(look: model.look, description: model.lookDescription)
        button.imagePosition = .imageLeading
        button.title = model.showsFailureMark ? " !" : ""
        button.toolTip = model.toolTip
    }

    // Draws goodall's mark: a pair of binoculars seen from behind, the same drawing as the
    // onboarding page's masthead. It is drawn here, with lines and circles, so that no
    // picture file has to be shipped. The numbers are positions on a grid 46 wide and 34
    // tall, with (0, 0) at the top left; the grid is then shrunk to the menu bar's height.
    func binocularsImage(look: IconLook, description: String) -> NSImage {
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
            let color: NSColor = look == .recording ? .systemOrange : .black
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
                if look == .recording {
                    NSBezierPath(ovalIn: NSRect(x: lens.x - 5, y: lens.y - 5, width: 10, height: 10)).fill()
                } else if look == .paused {
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
        image.isTemplate = look != .recording
        image.accessibilityDescription = description
        return image
    }

    // Empty the menu and fill it again with the model's rows.
    func fillMenu() {
        menu.removeAllItems()
        for row in model.rows {
            menu.addItem(menuItem(for: row))
        }
    }

    // The menu item that draws one row. A row that runs a command carries that command
    // along on its item ("representedObject"), and `commandChosen` reads it back when the
    // item is clicked.
    func menuItem(for row: MenuRow) -> NSMenuItem {
        if row.isSeparator {
            return NSMenuItem.separator()
        }
        let item = NSMenuItem(title: row.title, action: nil, keyEquivalent: "")
        item.isEnabled = row.enabled
        if row.isQuit {
            item.action = #selector(quitChosen(_:))
            item.keyEquivalent = "q"
            item.target = self
        } else if let command = row.command {
            item.action = #selector(commandChosen(_:))
            item.target = self
            item.representedObject = command
        } else if let submenuRows = row.submenu {
            let submenu = NSMenu()
            submenu.autoenablesItems = false
            for submenuRow in submenuRows {
                submenu.addItem(menuItem(for: submenuRow))
            }
            item.submenu = submenu
        }
        return item
    }

    // -----------------------------------------------------------------------------------
    // What happens when a menu item is chosen
    // -----------------------------------------------------------------------------------

    @objc func commandChosen(_ sender: NSMenuItem) {
        guard let command = sender.representedObject as? MenuCommand else { return }
        runMenuCommand(command)
    }

    // "Quit goodall". The model says whether to stop recording first. If stopping fails the
    // icon stays, because goodall would still be recording with nothing on screen to say so.
    //
    // Leaving is done with exit status 0, which tells the LaunchAgent that this was meant
    // and the icon should not be started again until the next sign-in.
    @objc func quitChosen(_ sender: NSMenuItem) {
        switch model.quitAction() {
        case .leaveNow:
            exit(0)
        case .stopThenLeave(let command):
            runMenuCommand(command, afterSuccess: { exit(0) })
        }
    }

    // macOS calls this as the menu opens.
    func menuWillOpen(_ menu: NSMenu) {
        model.menuOpened()
        drawIcon()
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
