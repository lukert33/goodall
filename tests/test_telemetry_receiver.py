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
import socket
from collections.abc import Iterator
from dataclasses import dataclass
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


@dataclass
class RawResponse:
    """What the receiver answered to one raw request."""

    status: int
    headers: dict[str, str]
    body: bytes


def send_raw_request(
    port: int, method: str, path: str, headers: list[tuple[str, str]], body: bytes = b""
) -> RawResponse:
    """Send a request with exactly the headers given, and nothing added by http.client.

    `skip_host` and `skip_accept_encoding` stop http.client adding its own
    Host and Accept-Encoding headers, so a test controls every header the
    receiver sees, including leaving out ones a real client always sends.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for header_name, header_value in headers:
            connection.putheader(header_name, header_value)
        connection.endheaders()
        if body:
            connection.send(body)
        response = connection.getresponse()
        return RawResponse(status=response.status, headers=dict(response.getheaders()), body=response.read())
    finally:
        connection.close()


def well_formed_headers(port: int, body: bytes) -> list[tuple[str, str]]:
    """The headers of a request the receiver accepts; refusal tests change one of them."""
    return [
        ("Host", f"127.0.0.1:{port}"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]


def replace_header(headers: list[tuple[str, str]], header_name: str, header_value: str | None) -> list[tuple[str, str]]:
    """Return the headers with one header replaced, or removed when the value is None."""
    changed_headers = []
    for existing_name, existing_value in headers:
        if existing_name != header_name:
            changed_headers.append((existing_name, existing_value))
    if header_value is not None:
        changed_headers.append((header_name, header_value))
    return changed_headers


def assert_nothing_stored(telemetry_directory: Path) -> None:
    """A refused request must leave no telemetry file behind."""
    assert sorted(entry.name for entry in telemetry_directory.iterdir()) == []


SMALL_BODY = b'{"resourceLogs":[]}'


def test_well_formed_raw_request_is_accepted(receiver: RunningReceiver, tmp_path: Path) -> None:
    # Guards the refusal tests below: their baseline headers really are accepted.
    headers = well_formed_headers(receiver.port, SMALL_BODY)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 200
    assert stored_lines(tmp_path / "logs.jsonl") == [SMALL_BODY]


def test_content_type_with_charset_is_accepted(receiver: RunningReceiver) -> None:
    headers = replace_header(
        well_formed_headers(receiver.port, SMALL_BODY), "Content-Type", "application/json; charset=utf-8"
    )

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 200


@pytest.mark.parametrize(
    "path",
    ["/", "/v1/profiles", "/v1/logs/", "/v1/logs/../../escape", "/v1/logs?file=elsewhere", "http://127.0.0.1/v1/logs"],
)
def test_any_other_path_is_refused(receiver: RunningReceiver, tmp_path: Path, path: str) -> None:
    headers = well_formed_headers(receiver.port, SMALL_BODY)

    response = send_raw_request(receiver.port, "POST", path, headers, SMALL_BODY)

    assert response.status == 404
    assert_nothing_stored(tmp_path)


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_any_method_but_post_is_refused(receiver: RunningReceiver, tmp_path: Path, method: str) -> None:
    headers = well_formed_headers(receiver.port, SMALL_BODY)

    response = send_raw_request(receiver.port, method, "/v1/logs", headers, SMALL_BODY)

    assert response.status == 405
    assert response.headers.get("Allow") == "POST"
    # No CORS headers, so a browser never lets a web page read or send anything.
    for header_name in response.headers:
        assert not header_name.lower().startswith("access-control-")
    assert_nothing_stored(tmp_path)


@pytest.mark.parametrize(
    "host_header",
    ["attacker.example", "attacker.example:{port}", "localhost:{port}", "127.0.0.1", "127.0.0.1:1", "0.0.0.0:{port}"],
)
def test_host_header_other_than_own_loopback_address_is_refused(
    receiver: RunningReceiver, tmp_path: Path, host_header: str
) -> None:
    # A web page using DNS rebinding reaches 127.0.0.1 under its own domain
    # name, which shows up here as the Host header.
    headers = replace_header(
        well_formed_headers(receiver.port, SMALL_BODY), "Host", host_header.format(port=receiver.port)
    )

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 403
    assert_nothing_stored(tmp_path)


def test_missing_host_header_is_refused(receiver: RunningReceiver, tmp_path: Path) -> None:
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Host", None)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 403
    assert_nothing_stored(tmp_path)


def test_duplicate_host_header_is_refused(receiver: RunningReceiver, tmp_path: Path) -> None:
    headers = well_formed_headers(receiver.port, SMALL_BODY) + [("Host", "attacker.example")]

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 403
    assert_nothing_stored(tmp_path)


def test_request_from_a_web_page_is_refused(receiver: RunningReceiver, tmp_path: Path) -> None:
    # Browsers add Origin to cross-site POSTs; Claude Code's exporter does not send one.
    headers = well_formed_headers(receiver.port, SMALL_BODY) + [("Origin", "https://attacker.example")]

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 403
    assert_nothing_stored(tmp_path)


@pytest.mark.parametrize("content_type", ["application/x-protobuf", "text/plain", "application/jsonx", None])
def test_content_type_other_than_json_is_refused(
    receiver: RunningReceiver, tmp_path: Path, content_type: str | None
) -> None:
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Content-Type", content_type)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 415
    assert b"http/json" in response.body
    assert_nothing_stored(tmp_path)


@pytest.mark.parametrize("content_encoding", ["gzip", "deflate", "identity"])
def test_any_content_encoding_is_refused(receiver: RunningReceiver, tmp_path: Path, content_encoding: str) -> None:
    headers = well_formed_headers(receiver.port, SMALL_BODY) + [("Content-Encoding", content_encoding)]

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, SMALL_BODY)

    assert response.status == 415
    assert b"uncompressed" in response.body
    assert_nothing_stored(tmp_path)


def test_missing_content_length_is_refused(receiver: RunningReceiver, tmp_path: Path) -> None:
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Content-Length", None)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers)

    assert response.status == 411
    assert_nothing_stored(tmp_path)


def test_chunked_body_is_refused(receiver: RunningReceiver, tmp_path: Path) -> None:
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Content-Length", None)
    headers.append(("Transfer-Encoding", "chunked"))
    chunked_body = format(len(SMALL_BODY), "x").encode() + b"\r\n" + SMALL_BODY + b"\r\n0\r\n\r\n"

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, chunked_body)

    assert response.status == 411
    assert_nothing_stored(tmp_path)


@pytest.mark.parametrize("content_length", ["-1", "+19", "1_9", "nineteen", "19, 19"])
def test_malformed_content_length_is_refused(receiver: RunningReceiver, tmp_path: Path, content_length: str) -> None:
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Content-Length", content_length)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers)

    assert response.status == 400
    assert_nothing_stored(tmp_path)


def test_body_over_the_size_limit_is_refused_before_it_is_read(receiver: RunningReceiver, tmp_path: Path) -> None:
    too_large = telemetry_receiver.MAXIMUM_BODY_BYTES + 1
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Content-Length", str(too_large))

    # No body is sent: the refusal must come from the declared length alone.
    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers)

    assert response.status == 413
    assert_nothing_stored(tmp_path)


def test_body_shorter_than_its_declared_length_is_not_stored(receiver: RunningReceiver, tmp_path: Path) -> None:
    declared_length = str(len(SMALL_BODY) + 5)
    headers = replace_header(well_formed_headers(receiver.port, SMALL_BODY), "Content-Length", declared_length)
    connection = http.client.HTTPConnection("127.0.0.1", receiver.port, timeout=10)
    try:
        connection.putrequest("POST", "/v1/logs", skip_host=True, skip_accept_encoding=True)
        for header_name, header_value in headers:
            connection.putheader(header_name, header_value)
        connection.endheaders()
        connection.send(SMALL_BODY)
        # Closing our sending half of the connection ends the body early.
        assert connection.sock is not None
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        assert response.status == 400
    finally:
        connection.close()

    assert_nothing_stored(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"resourceLogs":',
        b"[]",
        b'"text"',
        b'{"value":NaN}',
        b'{"text":"\xff"}',
        b'\xef\xbb\xbf{"resourceLogs":[]}',
        b'{"text":"raw\ttab"}',
        # Deep enough to make the parser raise RecursionError, not ValueError.
        b'{"a":' * 50000 + b"1" + b"}" * 50000,
    ],
    ids=["not json", "truncated", "array", "string", "NaN", "not utf-8", "byte-order mark", "raw tab", "deep nesting"],
)
def test_body_that_is_not_a_json_object_is_refused(receiver: RunningReceiver, tmp_path: Path, body: bytes) -> None:
    headers = well_formed_headers(receiver.port, body)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, body)

    assert response.status == 400
    assert_nothing_stored(tmp_path)


def test_refusal_does_not_echo_the_body(receiver: RunningReceiver) -> None:
    body = b'{"user.email":"person@example.com"'
    headers = well_formed_headers(receiver.port, body)

    response = send_raw_request(receiver.port, "POST", "/v1/logs", headers, body)

    assert response.status == 400
    assert b"person@example.com" not in response.body


@pytest.mark.parametrize("bind_address", ["0.0.0.0", "localhost", "::1", "192.168.1.10", ""])
def test_receiver_refuses_to_start_on_any_address_but_loopback(tmp_path: Path, bind_address: str) -> None:
    with pytest.raises(ValueError, match="only listens on 127.0.0.1"):
        telemetry_receiver.start_receiver(tmp_path, bind_address=bind_address)


def test_receiver_refuses_to_start_without_its_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        telemetry_receiver.start_receiver(tmp_path / "missing")


def test_telemetry_files_are_readable_by_their_owner_only(receiver: RunningReceiver, tmp_path: Path) -> None:
    post_like_claude_code(receiver.port, "/v1/logs", SMALL_BODY)

    assert (tmp_path / "logs.jsonl").stat().st_mode & 0o777 == 0o600


def test_symbolic_link_at_a_telemetry_file_is_not_followed(receiver: RunningReceiver, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"untouched\n")
    (tmp_path / "logs.jsonl").symlink_to(elsewhere)

    status, _ = post_like_claude_code(receiver.port, "/v1/logs", SMALL_BODY)

    # 503 tells the exporter to retry later rather than drop the batch.
    assert status == 503
    assert elsewhere.read_bytes() == b"untouched\n"


def test_receiver_prints_nothing_about_requests(receiver: RunningReceiver, capfd: pytest.CaptureFixture[str]) -> None:
    body = b'{"resourceLogs":[],"user.email":"person@example.com"}'

    post_like_claude_code(receiver.port, "/v1/logs", body)
    send_raw_request(receiver.port, "GET", "/v1/logs", well_formed_headers(receiver.port, b""))

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
