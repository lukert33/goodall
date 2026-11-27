// Runs the menu bar icon's model without a screen, for tests/test_menu_bar_icon_model.py.
//
// It is compiled together with menu-bar-icon/MenuModel.swift by the tests, never by
// menu-bar-icon/build.sh, so it is not part of the helper anyone runs (ADR 0025).
//
// It reads one JSON document on standard input:
//
//     {"goodall_is_known": true, "log_path_for_people": "~/...", "events": [...]}
//
// where each event is one of
//
//     {"event": "state_answered", "output": "<what goodall icon-state --json printed>"}
//     {"event": "item_chosen", "titles": ["Record work block", "research"]}
//     {"event": "command_finished", "exit_code": 1}
//     {"event": "menu_opened"}
//
// It hands each event to the model exactly as main.swift does, and prints a JSON list with
// one entry per event: the menu and icon as they would be drawn right after it, and
// whether main.swift would ask macOS for notification permission at that moment. Choosing
// an item that is missing or gray is a mistake in the test, so the driver stops with exit
// code 2 and says which.

import Foundation

func stop(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8) ?? Data())
    exit(2)
}

// The JSON form of a row. A missing value is written as null, so every key is always there.
func describe(_ row: MenuRow) -> [String: Any] {
    var submenu: Any = NSNull()
    if let submenuRows = row.submenu {
        var described: [[String: Any]] = []
        for submenuRow in submenuRows {
            described.append(describe(submenuRow))
        }
        submenu = described
    }
    return [
        "title": row.title,
        "enabled": row.enabled,
        "separator": row.isSeparator,
        "arguments": row.command?.arguments ?? NSNull(),
        "submenu": submenu,
    ]
}

// Find the row the titles lead to, outermost first, in these rows.
func findRow(titles: [String], in rows: [MenuRow]) -> MenuRow? {
    guard let first = titles.first else { return nil }
    for row in rows where row.title == first && !row.isSeparator {
        if titles.count == 1 {
            return row
        }
        guard row.enabled else { stop("\(first) is gray, so its submenu cannot be opened.") }
        return findRow(titles: Array(titles.dropFirst()), in: row.submenu ?? [])
    }
    return nil
}

let inputData = FileHandle.standardInput.readDataToEndOfFile()
guard let input = (try? JSONSerialization.jsonObject(with: inputData)) as? [String: Any],
    let goodallIsKnown = input["goodall_is_known"] as? Bool,
    let logPathForPeople = input["log_path_for_people"] as? String,
    let events = input["events"] as? [[String: Any]]
else {
    stop("The input is not the document this driver reads; its comment at the top describes it.")
}

var model = MenuModel(goodallIsKnown: goodallIsKnown, logPathForPeople: logPathForPeople)
var renderings: [[String: Any]] = []

for event in events {
    // What Quit goodall did, when this event chose it.
    var quit: Any = NSNull()
    // Whether this event made the model say to ask for notification permission.
    var askNotificationPermission = false

    switch event["event"] as? String {
    case "state_answered":
        askNotificationPermission = model.stateAnswered(output: event["output"] as? String ?? "")
    case "item_chosen":
        let titles = event["titles"] as? [String] ?? []
        guard let row = findRow(titles: titles, in: model.rows) else {
            stop("There is no item \(titles) in the menu.")
        }
        guard row.enabled else { stop("\(titles) is gray, so it cannot be chosen.") }
        if row.isQuit {
            switch model.quitAction() {
            case .leaveNow:
                quit = "leave_now"
            case .stopThenLeave(let command):
                quit = "stop_then_leave"
                model.commandStarted(command)
            }
        } else if let command = row.command {
            model.commandStarted(command)
        } else {
            stop("\(titles) runs nothing.")
        }
    case "command_finished":
        model.commandFinished(exitCode: Int32(event["exit_code"] as? Int ?? -1))
    case "menu_opened":
        model.menuOpened()
    default:
        stop("Unknown event: \(event)")
    }

    var rows: [[String: Any]] = []
    for row in model.rows {
        rows.append(describe(row))
    }
    renderings.append([
        "look": model.look.rawValue,
        "failure_mark": model.showsFailureMark,
        "tool_tip": model.toolTip,
        "rows": rows,
        "running_arguments": model.runningCommand?.arguments ?? NSNull(),
        "quit": quit,
        "ask_notification_permission": askNotificationPermission,
    ])
}

let output = try JSONSerialization.data(withJSONObject: renderings, options: [.sortedKeys])
FileHandle.standardOutput.write(output)
