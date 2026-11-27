"""Unit tests for goodall.notifications: the words of each notification, and how the helper is asked.

What each command shows, and when, is tested from the outside in
tests/test_background_notifications.py.
"""

import plistlib
from pathlib import Path

import pytest

from goodall import cli, menu_bar_icon, notifications
from goodall.fake_runner import FakeRunner

REPOSITORY_ROOT = Path(__file__).parent.parent


def every_notification() -> list[notifications.Notification]:
    """One notification of every kind goodall shows, for checks over all of them."""
    every = []
    for reason in notifications.BACKGROUND_STARTS:
        every.append(notifications.background_start("research", reason))
    for reason in notifications.BACKGROUND_STOPS:
        every.append(notifications.background_stop("research", reason))
    for reason in notifications.BACKGROUND_PAUSES:
        every.append(notifications.background_pause("research", reason))
    every.append(notifications.closed_when_idle("research", "3 h", "Mon 16 Nov 2026, 17:02 GMT"))
    every.append(notifications.closed_when_unreadable("research"))
    every.append(notifications.closed_when_unreadable("unknown"))
    return every


def test_no_notification_says_eval() -> None:
    # The menu and the notifications never say "eval" (ADR 0021).
    for notification in every_notification():
        assert "eval" not in notification.title.lower()
        assert "eval" not in notification.body.lower()


def test_every_notification_says_recording_started_or_stopped_and_names_the_workstream() -> None:
    for notification in every_notification():
        assert notification.title in ["Recording started", "Recording stopped"]
        assert "research" in notification.body or notification.body.startswith("Workstream unknown.")


def test_the_helper_program_goodall_runs_is_the_one_info_plist_names() -> None:
    with (REPOSITORY_ROOT / "menu-bar-icon" / "Info.plist").open("rb") as plist_file:
        info = plistlib.load(plist_file)

    assert notifications.HELPER_PROGRAM == f"Contents/MacOS/{info['CFBundleExecutable']}"


@pytest.mark.parametrize(
    ("command_name", "reasons"),
    [
        ("start", notifications.BACKGROUND_STARTS),
        ("stop", notifications.BACKGROUND_STOPS),
        ("pause", notifications.BACKGROUND_PAUSES),
    ],
)
def test_each_command_accepts_exactly_its_own_background_reasons(command_name: str, reasons: dict[str, str]) -> None:
    parser = cli.build_parser()
    for reason in reasons:
        arguments = parser.parse_args([command_name, "--background", reason])
        assert arguments.background == reason
    assert parser.parse_args([command_name]).background is None


def test_show_runs_the_installed_helper_with_the_title_and_body_as_two_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", tmp_path)
    (tmp_path / "goodall.app").mkdir()
    program = tmp_path / "goodall.app" / "Contents" / "MacOS" / "goodall-menu-bar-icon"
    runner = FakeRunner(real_programs=[])
    notification = notifications.background_start("research", "script")
    runner.answer([str(program), "--notify", notification.title, notification.body])

    warning = notifications.show(runner, notification)

    assert warning is None
    [command] = runner.calls
    assert command.timeout_seconds == notifications.NOTIFY_TIMEOUT_SECONDS


def test_show_with_no_helper_installed_runs_nothing_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", tmp_path)
    runner = FakeRunner(real_programs=[])

    warning = notifications.show(runner, notifications.background_stop("research", "script"))

    assert runner.calls == []
    assert warning is not None
    assert warning.kind == "notification_not_shown"
    assert "recording stopped" in warning.what_happened


def test_a_helper_that_is_not_allowed_to_show_notifications_gets_its_own_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", tmp_path)
    (tmp_path / "goodall.app").mkdir()
    runner = FakeRunner(real_programs=[])
    notification = notifications.background_start("research", "sign-in")
    program = str(tmp_path / "goodall.app" / notifications.HELPER_PROGRAM)
    runner.answer([program, "--notify", notification.title, notification.body], exit_code=notifications.NOT_ALLOWED)

    warning = notifications.show(runner, notification)

    assert warning is not None
    assert "System Settings > Notifications > goodall" in warning.what_to_do_next


def test_a_helper_macos_has_not_asked_about_yet_says_how_to_get_the_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before the icon has run once after setup, goodall is not listed in
    # System Settings at all, so pointing there would not help.
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", tmp_path)
    (tmp_path / "goodall.app").mkdir()
    runner = FakeRunner(real_programs=[])
    notification = notifications.background_start("research", "sign-in")
    program = str(tmp_path / "goodall.app" / notifications.HELPER_PROGRAM)
    runner.answer([program, "--notify", notification.title, notification.body], exit_code=notifications.NOT_ASKED_YET)

    warning = notifications.show(runner, notification)

    assert warning is not None
    assert "macOS has not yet asked whether goodall may show notifications" in warning.what_happened
    assert "System Settings" not in warning.what_to_do_next
    assert "Allow" in warning.what_to_do_next


def test_a_helper_that_cannot_be_started_is_a_warning_naming_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(menu_bar_icon, "APPLICATIONS_FOLDER", tmp_path)
    (tmp_path / "goodall.app").mkdir()
    runner = FakeRunner(real_programs=[])
    notification = notifications.background_start("research", "sign-in")
    program = str(tmp_path / "goodall.app" / notifications.HELPER_PROGRAM)
    runner.answer_not_found([program, "--notify", notification.title, notification.body])

    warning = notifications.show(runner, notification)

    assert warning is not None
    assert "was not found" in warning.what_happened
