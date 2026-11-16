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
it down.

Design decisions a reader might otherwise reverse-engineer:

- Standard library only. This sits on the capture path, beside the hooks
  (ADR 0005), and must start even when goodall's optional extras are broken.
- Each line is written and forced to disk before Claude Code is told the
  request succeeded. An acknowledged request therefore survives the receiver
  being killed. The official Collector buffers for a second without syncing,
  which is one reason ADR 0002 chose this receiver instead.
- Files are opened for appending, never truncated, so restarting the
  receiver against the same directory keeps everything already captured.
"""

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

# What an OTLP http/json exporter expects back on success: an empty
# Export*ServiceResponse, meaning "everything accepted, no partial failure".
SUCCESS_RESPONSE_BODY = b"{}"


def append_line(telemetry_file: Path, line: bytes) -> None:
    """Append one line to a telemetry file and force it to disk before returning."""
    file_descriptor = os.open(telemetry_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(file_descriptor, line + b"\n")
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


class TelemetryRequestHandler(BaseHTTPRequestHandler):
    """Handles one HTTP request from Claude Code's OTLP exporter."""

    # Set by the server class built in `start_receiver`.
    telemetry_directory: Path

    def do_POST(self) -> None:
        telemetry_file_name = TELEMETRY_FILE_NAME_FOR_PATH[self.path]
        content_length = int(self.headers["Content-Length"])
        body = self.rfile.read(content_length)
        append_line(self.telemetry_directory / telemetry_file_name, body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(SUCCESS_RESPONSE_BODY)))
        self.end_headers()
        self.wfile.write(SUCCESS_RESPONSE_BODY)


@dataclass
class RunningReceiver:
    """A receiver that is listening, and the means to stop it."""

    telemetry_directory: Path
    port: int
    server: ThreadingHTTPServer = field(repr=False)
    serving_thread: threading.Thread = field(repr=False)

    def stop(self) -> None:
        """Stop listening and wait for the serving thread to finish."""
        self.server.shutdown()
        self.server.server_close()
        self.serving_thread.join()


def start_receiver(telemetry_directory: Path, port: int = 0) -> RunningReceiver:
    """Start a receiver in a background thread and return once it is listening."""

    class Handler(TelemetryRequestHandler):
        pass

    Handler.telemetry_directory = telemetry_directory
    server = ThreadingHTTPServer((LOOPBACK_ADDRESS, port), Handler)
    serving_thread = threading.Thread(target=server.serve_forever, name="telemetry-receiver")
    serving_thread.start()
    return RunningReceiver(
        telemetry_directory=telemetry_directory,
        port=server.server_address[1],
        server=server,
        serving_thread=serving_thread,
    )
