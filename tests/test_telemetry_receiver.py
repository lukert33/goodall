"""Tests for goodall.telemetry_receiver, driven the way Claude Code drives it.

Every test starts a real receiver on a free port chosen by the operating
system (port 0), sends it real HTTP requests with the standard library's
`http.client`, and reads back the telemetry files it wrote in `tmp_path`.
The `receiver` fixture stops the receiver after each test, even a failing one.

`http.client` is used rather than `urllib` because several tests need to send
headers a well-behaved client never would (a wrong Host, no Content-Length).
"""

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from goodall import telemetry_receiver
from goodall.telemetry_receiver import RunningReceiver

# A trimmed copy of a real Claude Code log export, in the shape spike
# goodall-fs9.2 observed: one {"resourceLogs": [...]} object per request,
# compact, with no line breaks.
CLAUDE_CODE_LOGS_BODY = (
    b'{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"claude-code"}}]},'
    b'"scopeLogs":[{"scope":{"name":"com.anthropic.claude_code.events"},"logRecords":[{"timeUnixNano":"1763251200000000000",'
    b'"body":{"stringValue":"claude_code.user_prompt"},"attributes":[{"key":"session.id",'
    b'"value":{"stringValue":"0b7c1c1e-0000-4000-8000-000000000000"}},{"key":"prompt_length","value":{"intValue":12}}]}]}]}]}'
)


@pytest.fixture
def receiver(tmp_path: Path) -> Iterator[RunningReceiver]:
    """Start a receiver writing into tmp_path, and always stop it afterwards."""
    running_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        yield running_receiver
    finally:
        running_receiver.stop()


def post_like_claude_code(port: int, path: str, body: bytes) -> tuple[int, bytes]:
    """Send one OTLP http/json export the way Claude Code's exporter does, and return status and response body."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "User-Agent": "OTel-OTLP-Exporter-JavaScript/0.200.0"},
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def stored_lines(telemetry_file: Path) -> list[bytes]:
    """Read a telemetry file back as its lines, without their line endings."""
    return telemetry_file.read_bytes().splitlines()


def test_claude_code_log_export_lands_verbatim_as_one_line(receiver: RunningReceiver, tmp_path: Path) -> None:
    status, response_body = post_like_claude_code(receiver.port, "/v1/logs", CLAUDE_CODE_LOGS_BODY)

    assert status == 200
    assert json.loads(response_body) == {}
    assert (tmp_path / "logs.jsonl").read_bytes() == CLAUDE_CODE_LOGS_BODY + b"\n"


@pytest.mark.parametrize(
    ("path", "telemetry_file_name", "body"),
    [
        ("/v1/logs", "logs.jsonl", b'{"resourceLogs":[]}'),
        ("/v1/metrics", "metrics.jsonl", b'{"resourceMetrics":[]}'),
        ("/v1/traces", "traces.jsonl", b'{"resourceSpans":[]}'),
    ],
)
def test_each_signal_is_stored_in_its_own_file(
    receiver: RunningReceiver, tmp_path: Path, path: str, telemetry_file_name: str, body: bytes
) -> None:
    status, _ = post_like_claude_code(receiver.port, path, body)

    assert status == 200
    assert stored_lines(tmp_path / telemetry_file_name) == [body]
    other_files = sorted(entry.name for entry in tmp_path.iterdir() if entry.name != telemetry_file_name)
    assert other_files == []


def test_two_requests_become_two_lines_in_the_order_received(receiver: RunningReceiver, tmp_path: Path) -> None:
    first_body = b'{"resourceMetrics":[],"order":1}'
    second_body = b'{"resourceMetrics":[],"order":2}'

    post_like_claude_code(receiver.port, "/v1/metrics", first_body)
    post_like_claude_code(receiver.port, "/v1/metrics", second_body)

    assert stored_lines(tmp_path / "metrics.jsonl") == [first_body, second_body]


def test_restart_appends_to_existing_files_instead_of_truncating(tmp_path: Path) -> None:
    before_restart = b'{"resourceLogs":[],"run":"before"}'
    after_restart = b'{"resourceLogs":[],"run":"after"}'

    first_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        post_like_claude_code(first_receiver.port, "/v1/logs", before_restart)
    finally:
        first_receiver.stop()

    second_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        post_like_claude_code(second_receiver.port, "/v1/logs", after_restart)
    finally:
        second_receiver.stop()

    assert stored_lines(tmp_path / "logs.jsonl") == [before_restart, after_restart]
