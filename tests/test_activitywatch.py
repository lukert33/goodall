"""Tests for goodall.activitywatch: what the person did outside any agent.

Each test hands a hand-written ActivityWatch export from
tests/fixtures/activitywatch/ to `parse_activitywatch_export`, the module's
entry point. The fixtures are synthetic, shaped after ActivityWatch's own
source (see docs/activitywatch-rules.md); nothing here was checked against a
running ActivityWatch. Window titles, URLs and file paths in real exports
are private, so the fixtures use invented ones.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from goodall.activitywatch import (
    RULES_VERSION,
    ActiveAtKeyboard,
    ActivityWatchExportError,
    ApplicationFocus,
    AwayFromKeyboard,
    BrowserPageFocus,
    EditorFileFocus,
    UnfamiliarRecord,
    parse_activitywatch_export,
)

FIXTURES = Path(__file__).parent / "fixtures" / "activitywatch"


def test_each_watcher_gives_its_own_kind_with_a_duration_in_time_order() -> None:
    result = parse_activitywatch_export(FIXTURES / "all_buckets_rust_server.json")

    window_bucket = "aw-watcher-window_example-host"
    afk_bucket = "aw-watcher-afk_example-host"
    assert result.actions == [
        ApplicationFocus(
            bucket_id=window_bucket,
            hostname="example-host",
            event_index=1,
            event_id=2,
            timestamp="2026-04-01T09:01:00Z",
            duration_seconds=180.5,
            app="Example Editor",
            title="example.py - example-project",
            url=None,
        ),
        EditorFileFocus(
            bucket_id="aw-watcher-vscode_example-host",
            hostname="example-host",
            event_index=0,
            event_id=31,
            timestamp="2026-04-01T09:01:00Z",
            duration_seconds=170.0,
            file="/work/example-project/example.py",
            project="/work/example-project",
            language="python",
            branch="main",
        ),
        ApplicationFocus(
            bucket_id=window_bucket,
            hostname="example-host",
            event_index=0,
            event_id=3,
            timestamp="2026-04-01T09:04:00Z",
            duration_seconds=60.0,
            app="Example Browser",
            title="Example private page title",
            url="https://example.com/private-path",
        ),
        BrowserPageFocus(
            bucket_id="aw-watcher-web-firefox_example-host",
            hostname="example-host",
            event_index=0,
            event_id=21,
            timestamp="2026-04-01T09:04:00Z",
            duration_seconds=45.25,
            url="https://example.com/private-path",
            title="Example private page title",
            audible=False,
            incognito=False,
            tab_count=7,
        ),
        AwayFromKeyboard(
            bucket_id=afk_bucket,
            hostname="example-host",
            event_index=1,
            event_id=11,
            timestamp="2026-04-01T09:05:00Z",
            duration_seconds=900.0,
        ),
        ActiveAtKeyboard(
            bucket_id=afk_bucket,
            hostname="example-host",
            event_index=0,
            event_id=12,
            timestamp="2026-04-01T09:20:00Z",
            duration_seconds=600.0,
        ),
    ]
    kinds = []
    for action in result.actions:
        assert action.source == "activitywatch_export"
        kinds.append(action.kind)
    assert kinds == [
        "application_focus",
        "editor_file_focus",
        "application_focus",
        "browser_page_focus",
        "away_from_keyboard",
        "active_at_keyboard",
    ]
    assert result.rules_version == RULES_VERSION == "1"


def test_what_the_parser_does_not_recognise_is_reported_without_its_values() -> None:
    result = parse_activitywatch_export(FIXTURES / "all_buckets_rust_server.json")

    assert result.unfamiliar_records == [
        UnfamiliarRecord("aw-watcher-window_example-host", 2, "currentwindow event has no app"),
        UnfamiliarRecord("aw-watcher-afk_example-host", 2, "afkstatus event has an unknown status"),
        UnfamiliarRecord("aw-watcher-example-future_example-host", None, "unknown bucket type 'example.future.type'"),
    ]


def test_an_export_from_the_python_server_is_read_the_same_way() -> None:
    # The Python server writes `+00:00` times and no event ids; one bucket's
    # export has the same wrapper as an export of all buckets.
    result = parse_activitywatch_export(FIXTURES / "one_bucket_python_server.json")

    assert result.actions == [
        ApplicationFocus(
            bucket_id="aw-watcher-window_example-host",
            hostname="example-host",
            event_index=1,
            event_id=None,
            timestamp="2026-04-02T10:00:00+00:00",
            duration_seconds=30.25,
            app="Example Editor",
            title="excluded",
            url=None,
        ),
        ApplicationFocus(
            bucket_id="aw-watcher-window_example-host",
            hostname="example-host",
            event_index=0,
            event_id=None,
            timestamp="2026-04-02T10:00:30.250000+00:00",
            duration_seconds=12.5,
            app="Example Terminal",
            title="example-shell",
            url=None,
        ),
    ]
    assert result.unfamiliar_records == [
        UnfamiliarRecord("aw-watcher-window_example-host", 2, "event has no timestamp"),
    ]


def test_buckets_given_as_a_list_are_read_too(tmp_path: Path) -> None:
    # ActivityWatch's own JSON schema describes `buckets` as a list, though
    # both servers write an object keyed by bucket id.
    export_path = tmp_path / "export.json"
    export_path.write_text(
        '{"buckets": [{"id": "aw-watcher-afk_example-host", "type": "afkstatus", "hostname": "example-host",'
        ' "events": [{"timestamp": "2026-04-03T09:00:00Z", "duration": 30.0, "data": {"status": "afk"}}]}]}'
    )

    result = parse_activitywatch_export(export_path)

    assert [action.kind for action in result.actions] == ["away_from_keyboard"]


def test_private_text_stays_out_of_how_an_action_prints() -> None:
    # Titles, URLs and file paths are kept verbatim for the pre-redaction
    # record, but must never reach a log or an error message by accident.
    result = parse_activitywatch_export(FIXTURES / "all_buckets_rust_server.json")

    printed = repr(result)
    assert "private" not in printed
    assert "example-project" not in printed
    assert "Example Editor" in printed


def test_a_file_that_is_not_an_export_says_what_to_do_and_quotes_nothing(tmp_path: Path) -> None:
    export_path = tmp_path / "not-an-export.json"
    export_path.write_text('{"something": "Example SECRET page title"}')

    with pytest.raises(ActivityWatchExportError) as raised:
        parse_activitywatch_export(export_path)

    message = str(raised.value)
    assert "buckets" in message
    assert "export" in message.lower()
    assert "SECRET" not in message


def test_a_file_that_is_not_json_says_so(tmp_path: Path) -> None:
    export_path = tmp_path / "broken.json"
    export_path.write_text("Example SECRET page title {")

    with pytest.raises(ActivityWatchExportError) as raised:
        parse_activitywatch_export(export_path)

    assert "SECRET" not in str(raised.value)


def test_the_module_loads_nothing_outside_the_standard_library() -> None:
    probe = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import goodall.activitywatch\n"
        "outside = set()\n"
        "for name in set(sys.modules) - before:\n"
        "    top = name.split('.')[0]\n"
        "    if top not in sys.stdlib_module_names and top != 'goodall':\n"
        "        outside.add(top)\n"
        "print(sorted(outside))\n"
    )
    completed = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True)

    assert completed.stdout.strip() == "[]"
