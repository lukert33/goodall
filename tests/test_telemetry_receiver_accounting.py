"""Tests that every request sent to the telemetry receiver is accounted for.

Why this file exists: in CI on Linux a test sent 40 simultaneous requests and
one line was missing from the file. The log was cut off, the test had not
recorded what each request did, and the cause was never seen. Raising the
listen backlog made the next run pass, but nobody knows whether that was the
cause. This is a recorder, so "a line went missing and we do not know why" is
not acceptable. These tests make the question answerable: every request sent
ends in exactly one of three outcomes, and each outcome has a rule about the
file.

The invariant (one of these three for every request sent):

- Succeeded (HTTP 200): the file holds exactly one whole line for it.
- Refused (any other HTTP status): the file holds no line for it.
- Errored (the client saw no answer: a timeout, a reset, a refused
  connection): the file holds zero or one line for it. The receiver may have
  stored the body before the connection broke, and the sender, which retries,
  can then store it twice. That is at-least-once delivery; it is the sender's
  job to retry, and a reader may see a duplicate but never a loss.

And whatever the outcomes: every line in the file is whole, parseable, and
matches a request that was sent.

The original loss is not reproduced on macOS (see the pull request). What
these tests show is that a missing line can only have been a request that
errored on the client side, or a test that did not count its errors. The
`stress` variants run the same accounting much harder:

    uv run pytest -m stress

A plain `uv run pytest` skips them. Every payload is synthetic and the
receiver listens on 127.0.0.1 on a port the operating system chooses.
"""

import http.client
import json
import socket
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from goodall import telemetry_receipts, telemetry_receiver
from goodall.telemetry_receiver import RunningReceiver

# The size of the original failing test's requests. Large enough that a request
# takes several reads to arrive, so writes from different requests overlap.
PADDING_BYTES = 50000


def body_for(marker: str, padding_bytes: int = PADDING_BYTES) -> bytes:
    """A synthetic OTLP log export whose "marker" says which request it was."""
    return json.dumps({"resourceLogs": [], "marker": marker, "padding": "x" * padding_bytes}).encode()


@dataclass
class BurstResult:
    """What happened to each request of a burst, keyed by its marker."""

    # marker -> HTTP status, for requests that were answered.
    statuses: dict[str, int] = field(default_factory=dict)
    # marker -> the kind of error, for requests that were not.
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> list[str]:
        return [marker for marker, status in self.statuses.items() if status == 200]

    @property
    def refused(self) -> list[str]:
        return [marker for marker, status in self.statuses.items() if status != 200]

    @property
    def errored(self) -> list[str]:
        return list(self.errors)

    def summary(self) -> str:
        """A one-line account that a failing assertion can show, so a failure is never a mystery."""
        error_kinds = Counter(self.errors.values())
        status_counts = Counter(self.statuses.values())
        return f"statuses={dict(status_counts)} errors={dict(error_kinds)}"


def send_burst(port: int, markers: list[str], client_timeout: float, padding_bytes: int = PADDING_BYTES) -> BurstResult:
    """Send one request per marker, all released at the same moment, from threads.

    Every thread records its own outcome in the result, because an exception
    inside a thread would otherwise only be printed, and the request would show
    up later as an unexplained missing line.
    """
    result = BurstResult()
    start_together = threading.Barrier(len(markers))

    def send_one(marker: str) -> None:
        body = body_for(marker, padding_bytes)
        start_together.wait()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=client_timeout)
            try:
                connection.request("POST", "/v1/logs", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                response.read()
                result.statuses[marker] = response.status
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as request_error:
            result.errors[marker] = type(request_error).__name__

    threads = []
    for marker in markers:
        thread = threading.Thread(target=send_one, args=(marker,))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    unfinished = len(markers) - len(result.statuses) - len(result.errors)
    assert unfinished == 0, f"{unfinished} requests neither finished nor failed within 60 seconds"
    return result


def markers_in_file(telemetry_file: Path) -> Counter[str]:
    """Count the lines carrying each marker, insisting every line is whole and parseable."""
    counts: Counter[str] = Counter()
    if not telemetry_file.exists():
        return counts
    content = telemetry_file.read_bytes()
    assert content == b"" or content.endswith(b"\n"), "the last line is incomplete"
    if content == b"":
        return counts
    for line_number, line in enumerate(content[:-1].split(b"\n"), start=1):
        try:
            parsed_line = json.loads(line)
        except ValueError:
            pytest.fail(f"line {line_number} is torn or merged: it is not valid JSON ({line[:80]!r}...)")
        assert parsed_line["padding"] == "x" * len(parsed_line["padding"]), f"line {line_number} has mixed content"
        counts[parsed_line["marker"]] += 1
    return counts


def assert_every_request_accounted_for(
    sent_markers: list[str], result: BurstResult, telemetry_file: Path, expected_padding_bytes: int = PADDING_BYTES
) -> None:
    """Check the invariant in this module's docstring, and say plainly what broke."""
    account = f"sent={len(sent_markers)} {result.summary()}"
    assert len(result.succeeded) + len(result.refused) + len(result.errored) == len(sent_markers), account

    line_counts = markers_in_file(telemetry_file)
    for marker in result.succeeded:
        assert line_counts[marker] == 1, f"{marker} was answered 200 but has {line_counts[marker]} lines. {account}"
    for marker in result.refused:
        assert line_counts[marker] == 0, f"{marker} was refused but has a line. {account}"
    for marker in result.errored:
        assert line_counts[marker] <= 1, f"{marker} errored but has {line_counts[marker]} lines. {account}"
    for marker in line_counts:
        assert marker in sent_markers, f"a line carries marker {marker}, which was never sent"
    # Each whole line carries exactly the padding that was sent, so none is cut short.
    for line in telemetry_file.read_bytes().split(b"\n")[:-1] if telemetry_file.exists() else []:
        assert len(json.loads(line)["padding"]) == expected_padding_bytes
    # Every stored line has its receipt, whatever the client saw: the receiver
    # finishes both writes even for a client that has given up (ADR 0013).
    receipt_check = telemetry_receipts.check_receipts(telemetry_file.parent)
    assert receipt_check.problems() == [], account
    assert receipt_check.receipt_count == sum(line_counts.values()), account


def run_rounds(tmp_path: Path, rounds: int, clients_per_round: int, client_timeout: float) -> list[BurstResult]:
    """Run bursts against fresh receivers, checking the invariant after each one."""
    results = []
    for round_number in range(rounds):
        round_directory = tmp_path / f"round-{round_number}"
        round_directory.mkdir(parents=True)
        running_receiver = telemetry_receiver.start_receiver(round_directory)
        try:
            markers = [f"round{round_number}-client{client_number}" for client_number in range(clients_per_round)]
            result = send_burst(running_receiver.port, markers, client_timeout)
        finally:
            running_receiver.stop()
        assert_every_request_accounted_for(markers, result, round_directory / "logs.jsonl")
        results.append(result)
    return results


def test_burst_within_the_backlog_is_answered_and_stored_in_full(tmp_path: Path) -> None:
    # The size of the burst that failed in CI. Nothing in it may error: a
    # burst this small is the receiver's to take, not the sender's to retry.
    results = run_rounds(tmp_path, rounds=5, clients_per_round=40, client_timeout=10)

    for result in results:
        assert len(result.succeeded) == 40, result.summary()


def test_burst_far_beyond_the_backlog_loses_nothing_it_answered(tmp_path: Path) -> None:
    # Too many connections at once for any backlog, and a short client timeout
    # so some clients give up. Those clients see an error; the accounting must
    # still hold exactly. This is the shape of the original incident: a request
    # missing from the file whose client had been left to fail unnoticed.
    results = run_rounds(tmp_path, rounds=2, clients_per_round=150, client_timeout=0.5)

    answered = sum(len(result.statuses) for result in results)
    assert answered > 0, "no request was answered at all, so the accounting checked nothing"


def test_clients_that_hang_up_before_the_answer_leave_whole_lines_only(tmp_path: Path) -> None:
    # A sender that gives up (a short timeout) or crashes after sending still
    # may have its body stored; what is stored must be whole, never cut short.
    running_receiver = telemetry_receiver.start_receiver(tmp_path)
    try:
        sent_markers = []
        for client_number in range(30):
            marker = f"hangup{client_number}"
            sent_markers.append(marker)
            body = body_for(marker)
            request_head = (
                f"POST /v1/logs HTTP/1.1\r\nHost: 127.0.0.1:{running_receiver.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode()
            with socket.create_connection(("127.0.0.1", running_receiver.port), timeout=5) as client_socket:
                client_socket.sendall(request_head + body)
            # The socket closes here without the answer being read.
    finally:
        # stop() waits for requests in progress, so every body that was fully
        # sent has been stored or refused by the time it returns.
        running_receiver.stop()

    line_counts = markers_in_file(tmp_path / "logs.jsonl")
    for marker in sent_markers:
        assert line_counts[marker] <= 1
    for marker in line_counts:
        assert marker in sent_markers


def test_server_not_yet_accepting_still_takes_a_burst_of_connections() -> None:
    # The backlog, tested directly rather than by hoping a race goes wrong.
    # With nobody calling accept, the operating system queues connections up to
    # the backlog and (on macOS, observed) leaves later ones unanswered, so
    # their connect times out. Forty is the burst that failed in CI.
    server = telemetry_receiver.LoopbackTelemetryServer(
        (telemetry_receiver.LOOPBACK_ADDRESS, 0), telemetry_receiver.TelemetryRequestHandler
    )
    client_sockets = []
    unanswered = 0
    try:
        for _ in range(40):
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_sockets.append(client_socket)
            client_socket.settimeout(0.3)
            try:
                client_socket.connect((telemetry_receiver.LOOPBACK_ADDRESS, server.server_port))
            except OSError:
                unanswered += 1
    finally:
        for client_socket in client_sockets:
            client_socket.close()
        server.server_close()

    assert unanswered == 0, f"{unanswered} of 40 simultaneous connections were not queued by the operating system"


# What a client in its own process runs: one request, then print the outcome.
# Separate processes avoid every thread sharing Python's one interpreter lock,
# which can make a thread-only test too polite to expose a race.
CLIENT_PROCESS_SCRIPT = """
import http.client, json, sys
port, marker, padding = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
body = json.dumps({"resourceLogs": [], "marker": marker, "padding": "x" * padding}).encode()
try:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=float(sys.argv[4]))
    connection.request("POST", "/v1/logs", body=body, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    response.read()
    print(response.status)
except (OSError, http.client.HTTPException) as request_error:
    print(type(request_error).__name__)
"""


def send_burst_from_processes(port: int, markers: list[str], client_timeout: float) -> BurstResult:
    """Send one request per marker from its own Python process, started together as closely as possible."""
    processes = []
    for marker in markers:
        processes.append(
            subprocess.Popen(
                [sys.executable, "-I", "-c", CLIENT_PROCESS_SCRIPT, str(port), marker, str(PADDING_BYTES)]
                + [str(client_timeout)],
                stdout=subprocess.PIPE,
                text=True,
            )
        )
    result = BurstResult()
    for marker, process in zip(markers, processes, strict=True):
        output, _ = process.communicate(timeout=120)
        outcome = output.strip()
        if outcome.isdigit():
            result.statuses[marker] = int(outcome)
        else:
            result.errors[marker] = outcome or "NoOutput"
    return result


@pytest.mark.stress
def test_stress_threads_and_processes_account_for_every_request(tmp_path: Path) -> None:
    # Many rounds of large bursts from threads, then from separate processes.
    # Run on demand with: uv run pytest -m stress
    thread_results = run_rounds(tmp_path / "threads", rounds=20, clients_per_round=60, client_timeout=10)
    for result in thread_results:
        assert len(result.succeeded) == 60, result.summary()

    process_directory = tmp_path / "processes"
    for round_number in range(5):
        round_directory = process_directory / f"round-{round_number}"
        round_directory.mkdir(parents=True)
        running_receiver: RunningReceiver = telemetry_receiver.start_receiver(round_directory)
        try:
            markers = [f"proc{round_number}-client{client_number}" for client_number in range(40)]
            result = send_burst_from_processes(running_receiver.port, markers, client_timeout=10)
        finally:
            running_receiver.stop()
        assert_every_request_accounted_for(markers, result, round_directory / "logs.jsonl")


@pytest.mark.stress
def test_stress_overload_never_loses_an_answered_request(tmp_path: Path) -> None:
    # Overload on purpose: far more connections than the backlog, with short
    # timeouts so many clients error. The accounting must hold in every round.
    run_rounds(tmp_path, rounds=10, clients_per_round=300, client_timeout=0.5)
