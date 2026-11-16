"""Goodall's own receiver for Claude Code's telemetry (ADR 0002).

Claude Code can export its telemetry (events, metrics and traces) over
OpenTelemetry's OTLP protocol. This module listens for that export over HTTP
with JSON bodies ("http/json") and keeps each request as a raw record: the
request body is appended, as one line, to a JSON Lines file named after the
signal, inside a directory the caller chooses (normally in the log
directory):

    POST /v1/logs     ->  logs.jsonl
    POST /v1/metrics  ->  metrics.jsonl
    POST /v1/traces   ->  traces.jsonl

Who calls it: code that starts a receiver for a stretch of work, and the
tests. `start_receiver` returns a `RunningReceiver` whose `stop` method shuts
it down. Running `python -m goodall.telemetry_receiver DIRECTORY` starts one
as its own process, which is how the tests kill a receiver mid-flight.

Design decisions a reader might otherwise reverse-engineer:

- Standard library only. This sits on the capture path, beside the hooks
  (ADR 0005), and must start even when goodall's optional extras are broken.
- Each line is written and forced to disk before Claude Code is told the
  request succeeded. An acknowledged request therefore survives the receiver
  being killed. The official Collector buffers for a second without syncing,
  which is one reason ADR 0002 chose this receiver instead.
- Files are opened for appending, never truncated, so restarting the
  receiver against the same directory keeps everything already captured.

Security. The receiver runs on a machine that holds credentials, and the
telemetry it receives carries the person's email address on every record
(and their prompts, if they switch that on). The worst problems in
comparable local tools came from a service that other programs or web pages
could reach. So the receiver:

- listens on 127.0.0.1 only, and refuses to start on any other address;
- answers only POST to the three paths above; anything else is refused;
- refuses a request whose Host header is not exactly 127.0.0.1:<its port>,
  which defeats DNS rebinding (a web page reaching 127.0.0.1 under its own
  domain name), and refuses any request carrying an Origin header, which
  browsers add and Claude Code's exporter does not;
- sends no CORS headers, so a browser never shows a web page the answer;
- requires a declared Content-Length no larger than MAXIMUM_BODY_BYTES, and
  refuses chunked bodies, so it never reads an unbounded amount;
- accepts only uncompressed application/json, which is what goodall tells
  Claude Code to send, so it never runs a decompressor on untrusted input;
- never prints, logs or echoes a body, and only parses it to check it is a
  JSON object;
- writes only to the three fixed file names, refuses to follow a symbolic
  link planted at one of them, and creates them readable by their owner only.
"""

import fcntl
import json
import os
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The only address the receiver listens on.
LOOPBACK_ADDRESS = "127.0.0.1"

# The three paths OTLP http exporters post to, and the file each one fills.
# Nothing in a request can choose a file name: only these exact paths match.
TELEMETRY_FILE_NAME_FOR_PATH = {
    "/v1/logs": "logs.jsonl",
    "/v1/metrics": "metrics.jsonl",
    "/v1/traces": "traces.jsonl",
}

# The largest request body accepted: 20 MiB, the same default limit the
# official Collector's HTTP receiver uses. Claude Code's batches observed in
# spike goodall-fs9.2 were a few kilobytes; full prompt and tool-detail
# content can make them much larger, so the limit is generous on purpose.
MAXIMUM_BODY_BYTES = 20 * 1024 * 1024

# How long one connection may sit idle while sending its request. Without a
# limit, a client that declares a body and never sends it holds a thread
# forever.
SOCKET_TIMEOUT_SECONDS = 10

# What an OTLP http/json exporter expects back on success: an empty
# Export*ServiceResponse, meaning "everything accepted, no partial failure".
SUCCESS_RESPONSE_BODY = b"{}"


@dataclass
class Refusal:
    """Why a request was turned away: the HTTP status and a plain message saying what to do instead."""

    status: int
    message: str


def refusal_for_host(host_values: list[str], port: int) -> Refusal | None:
    """Refuse unless there is exactly one Host header naming 127.0.0.1 and this receiver's port.

    Trap: "localhost" is refused too. It is harmless in itself, but goodall
    points Claude Code at 127.0.0.1, so anything else is a sign the request
    did not come from where we expect.
    """
    expected_host = f"{LOOPBACK_ADDRESS}:{port}"
    if host_values != [expected_host]:
        return Refusal(403, f"Refused: this receiver only answers requests addressed to {expected_host}.")
    return None


def refusal_for_content_type(content_type_values: list[str]) -> Refusal | None:
    """Refuse anything but one Content-Type of application/json (parameters such as charset are allowed)."""
    if len(content_type_values) == 1:
        media_type = content_type_values[0].split(";")[0].strip().lower()
        if media_type == "application/json":
            return None
    return Refusal(
        415,
        "Refused: only application/json is accepted. Set OTEL_EXPORTER_OTLP_PROTOCOL=http/json; "
        "http/protobuf and grpc are not supported.",
    )


def refusal_for_declared_length(content_length_values: list[str]) -> Refusal | None:
    """Refuse a body whose length is missing, malformed or over MAXIMUM_BODY_BYTES.

    Trap: Python's int() accepts "+19", " 19" and "1_9", so the value is
    checked to be plain ASCII digits before it is converted.
    """
    if not content_length_values:
        return Refusal(411, "Refused: send the body with a Content-Length header, not chunked.")
    if len(content_length_values) > 1:
        return Refusal(400, "Refused: send exactly one Content-Length header.")
    declared_length = content_length_values[0]
    if not (declared_length.isascii() and declared_length.isdigit()):
        return Refusal(400, "Refused: Content-Length must be a whole number of bytes.")
    if int(declared_length) > MAXIMUM_BODY_BYTES:
        return Refusal(413, f"Refused: bodies larger than {MAXIMUM_BODY_BYTES} bytes are not accepted.")
    return None


def refusal_for_body(body: bytes) -> Refusal | None:
    """Refuse a body that is not a single JSON object in UTF-8.

    The body is parsed only to check it; the parsed value is thrown away and
    the stored line is made from the original bytes. The parser is strict:
    it rejects a byte-order mark, raw control characters inside strings,
    and NaN or Infinity, none of which are JSON. Very deep nesting makes the
    parser raise RecursionError rather than ValueError, so both are caught.
    """
    refusal = Refusal(400, "Refused: the body must be one OTLP JSON object, encoded as UTF-8.")
    try:
        body_text = body.decode("utf-8")
        parsed_body = json.loads(body_text, parse_constant=reject_non_json_constant)
    except (ValueError, RecursionError):
        return refusal
    if not isinstance(parsed_body, dict):
        return refusal
    return None


def reject_non_json_constant(constant_name: str) -> None:
    """Make the JSON parser fail on NaN, Infinity and -Infinity, which Python accepts by default."""
    raise ValueError(f"{constant_name} is not valid JSON")


def append_line(telemetry_file: Path, line: bytes) -> None:
    """Append one line to a telemetry file and force it to disk before returning.

    O_APPEND makes the operating system put every write at the current end
    of the file, so nothing is ever overwritten. O_NOFOLLOW refuses to write
    through a symbolic link planted at the file name. On macOS, os.fsync
    only hands data to the drive; F_FULLFSYNC asks the drive to store it, so
    it is used where it exists.
    """
    file_descriptor = os.open(
        telemetry_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        remaining_bytes = memoryview(line + b"\n")
        while remaining_bytes:
            written_count = os.write(file_descriptor, remaining_bytes)
            remaining_bytes = remaining_bytes[written_count:]
        force_to_disk(file_descriptor)
    finally:
        os.close(file_descriptor)


def force_to_disk(file_descriptor: int) -> None:
    """Ask the operating system, and on macOS the drive, to store everything written so far."""
    # F_FULLFSYNC exists only on macOS; Linux (where CI runs) has no such command.
    full_sync_command = getattr(fcntl, "F_FULLFSYNC", None)
    if full_sync_command is not None:
        try:
            fcntl.fcntl(file_descriptor, full_sync_command)
            return
        except OSError:
            # Some file systems (network shares) do not support it; fall back.
            pass
    os.fsync(file_descriptor)


class TelemetryRequestHandler(BaseHTTPRequestHandler):
    """Handles one HTTP request from Claude Code's OTLP exporter.

    `http.server` calls `do_POST`, `do_GET` and so on by method name. Every
    method goes through `handle_request`, so the Host and Origin checks run
    first whatever the method.
    """

    # Set on the per-receiver subclass built in `start_receiver`.
    telemetry_directory: Path
    write_lock: threading.Lock
    own_port: int

    # Applied by http.server to the connection's socket.
    timeout = SOCKET_TIMEOUT_SECONDS
    server_version = "goodall-telemetry-receiver"
    sys_version = ""

    def do_POST(self) -> None:
        self.handle_request()

    def do_GET(self) -> None:
        self.handle_request()

    def do_HEAD(self) -> None:
        self.handle_request()

    def do_PUT(self) -> None:
        self.handle_request()

    def do_DELETE(self) -> None:
        self.handle_request()

    def do_PATCH(self) -> None:
        self.handle_request()

    def do_OPTIONS(self) -> None:
        self.handle_request()

    def handle_request(self) -> None:
        """Check the request, store its body, and answer; or refuse it."""
        refusal = refusal_for_host(self.headers.get_all("Host", []), self.own_port)
        if refusal is None and self.headers.get_all("Origin"):
            refusal = Refusal(403, "Refused: requests from web pages are not accepted.")
        if refusal is None and self.path not in TELEMETRY_FILE_NAME_FOR_PATH:
            refusal = Refusal(404, "Refused: only /v1/logs, /v1/metrics and /v1/traces exist.")
        if refusal is None and self.command != "POST":
            refusal = Refusal(405, "Refused: only POST is accepted.")
        if refusal is None:
            refusal = refusal_for_content_type(self.headers.get_all("Content-Type", []))
        if refusal is None and self.headers.get_all("Content-Encoding"):
            refusal = Refusal(
                415,
                "Refused: send the body uncompressed (set OTEL_EXPORTER_OTLP_COMPRESSION=none); "
                "goodall configures Claude Code to send uncompressed http/json.",
            )
        if refusal is None and self.headers.get_all("Transfer-Encoding"):
            refusal = Refusal(411, "Refused: send the body with a Content-Length header, not chunked.")
        if refusal is None:
            refusal = refusal_for_declared_length(self.headers.get_all("Content-Length", []))
        if refusal is not None:
            self.refuse(refusal)
            return

        declared_length = int(self.headers["Content-Length"])
        body = self.rfile.read(declared_length)
        if len(body) != declared_length:
            self.refuse(Refusal(400, "Refused: the body ended before its declared Content-Length."), body_was_read=True)
            return
        refusal = refusal_for_body(body)
        if refusal is not None:
            self.refuse(refusal, body_was_read=True)
            return

        telemetry_file = self.telemetry_directory / TELEMETRY_FILE_NAME_FOR_PATH[self.path]
        try:
            with self.write_lock:
                append_line(telemetry_file, body)
        except OSError:
            # 503 is a status OTLP exporters retry, so a full disk or a
            # moved directory does not silently lose the batch.
            self.refuse(
                Refusal(503, "Refused: the receiver could not store the request; try again."), body_was_read=True
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(SUCCESS_RESPONSE_BODY)))
        self.end_headers()
        self.wfile.write(SUCCESS_RESPONSE_BODY)

    def refuse(self, refusal: Refusal, body_was_read: bool = False) -> None:
        """Answer with a plain-text refusal and close the connection.

        Trap: if a refused request's body is left unread, closing the socket
        makes the operating system reset the connection, and the client may
        never see the refusal. So a body of acceptable declared size that has
        not been read yet is read and thrown away first; nothing is done with
        it. Reading it a second time would wait for bytes that never come.
        """
        if not body_was_read:
            self.discard_declared_body()
        message_bytes = (refusal.message + "\n").encode("utf-8")
        self.send_response(refusal.status)
        if refusal.status == 405:
            self.send_header("Allow", "POST")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(message_bytes)

    def discard_declared_body(self) -> None:
        """Read and drop a body whose declared length is valid and within the limit."""
        content_length_values = self.headers.get_all("Content-Length", [])
        if refusal_for_declared_length(content_length_values) is not None:
            return
        if self.headers.get_all("Transfer-Encoding"):
            return
        remaining_length = int(content_length_values[0])
        while remaining_length > 0:
            chunk = self.rfile.read(min(remaining_length, 65536))
            if not chunk:
                return
            remaining_length -= len(chunk)

    def log_message(self, format: str, *args: object) -> None:
        """Print nothing. http.server would otherwise write every request line to stderr."""


class LoopbackTelemetryServer(ThreadingHTTPServer):
    """The HTTP server, with the threading choices spelled out.

    Non-daemon request threads, with block_on_close, mean `server_close`
    waits for a request that is mid-write to finish, so stopping never cuts
    a line short. SO_REUSEPORT stays off so no other program can listen on
    the same port alongside the receiver.
    """

    daemon_threads = False
    block_on_close = True
    allow_reuse_port = False


@dataclass
class RunningReceiver:
    """A receiver that is listening, and the means to stop it."""

    telemetry_directory: Path
    port: int
    server: LoopbackTelemetryServer = field(repr=False)
    serving_thread: threading.Thread = field(repr=False)

    @property
    def endpoint_url(self) -> str:
        """The base URL to give Claude Code as OTEL_EXPORTER_OTLP_ENDPOINT."""
        return f"http://{LOOPBACK_ADDRESS}:{self.port}"

    def stop(self) -> None:
        """Stop accepting requests, wait for any in progress to finish, and release the port."""
        self.server.shutdown()
        self.server.server_close()
        self.serving_thread.join()


def start_receiver(telemetry_directory: Path, port: int = 0, bind_address: str = LOOPBACK_ADDRESS) -> RunningReceiver:
    """Start a receiver in a background thread and return once it is listening.

    `port` 0 asks the operating system for any free port; read the chosen
    one from the result. `bind_address` exists only so that asking for any
    address other than 127.0.0.1 fails loudly instead of being ignored.
    The directory must already exist: creating it, and choosing where it is,
    belongs to the caller.
    """
    if bind_address != LOOPBACK_ADDRESS:
        raise ValueError(
            f"The telemetry receiver only listens on {LOOPBACK_ADDRESS}, not {bind_address!r}; "
            "anything else would expose telemetry to other machines."
        )
    if not telemetry_directory.is_dir():
        raise FileNotFoundError(
            f"Telemetry directory {telemetry_directory} does not exist. Create it in the log directory first."
        )

    # A subclass per receiver, so two receivers in one process (as in the
    # tests) never share a directory or a lock.
    class Handler(TelemetryRequestHandler):
        pass

    Handler.telemetry_directory = telemetry_directory
    Handler.write_lock = threading.Lock()
    server = LoopbackTelemetryServer((LOOPBACK_ADDRESS, port), Handler)
    # Known only once the socket is bound, when port 0 has become a real port.
    Handler.own_port = server.server_port
    serving_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="telemetry-receiver"
    )
    serving_thread.start()
    return RunningReceiver(
        telemetry_directory=telemetry_directory,
        port=server.server_port,
        server=server,
        serving_thread=serving_thread,
    )
