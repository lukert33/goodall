# Telemetry is one request per line, with a receipt log beside it

Goodall's telemetry receiver keeps each request it receives as one line of a JSON Lines file, and writes a receipt for that request to a separate receipt log in the same directory. A receipt holds a sequence number that goodall assigns on arrival, the time of arrival, which file the request went to, the length and SHA-256 of the body exactly as it was received, and whether any line break in the body was replaced by a space to keep it on one line. We kept one request per line because it is the shape the OpenTelemetry Collector's file exporter writes, so readers already accept it (ADR 0002), and because a researcher can search and count such a file with ordinary tools. On its own that format has three weaknesses: a lost line leaves no trace, a request the sender retried looks like new data, and a body that arrived with line breaks is stored slightly changed with nothing to say so. OTLP over HTTP carries no request id, so any id has to be goodall's own. We rejected putting that id inside the data, by wrapping each line in an envelope, because the raw record would then contain goodall's additions and would no longer be the Collector's shape (ADR 0003). We rejected one file per request, which would be exactly verbatim, because it produces thousands of small files a day that are awkward to analyze. The receipt log gets the id and the proof without touching the raw record.

The data line is written and forced to disk first, then its receipt, and only then is the sender told the request succeeded.

## Consequences

"Raw records stay verbatim" has a stated meaning for telemetry: a stored line is byte for byte what was sent unless its receipt says a line break was replaced, and the receipt's hash is of the original bytes either way. A replaced body can be detected but not restored; Claude Code sends compact JSON, so this has not yet happened in practice.

A gap in the sequence numbers, or a data line with no receipt, shows that something was lost or that the receiver stopped mid-write. Two receipts with the same hash show a retried request. Readers of telemetry must use the receipt log to drop such duplicates.

The receipt log is written by goodall but is not derived data: the arrival time and the original hash cannot be regenerated, so it is kept and published with the raw record it describes. Telemetry written by a Collector that someone runs themselves has no receipt log, and readers must accept its absence.
