# Capture with Claude Code hooks and goodall's own telemetry receiver

What matters about capture is the outcome, not the mechanism: the logs must retain enough data, in enough structure, to support at least conversion to OpenTelemetry format and to Inspect's event format. Any capture mechanism that meets that bar is acceptable, and one that loses or flattens the data is not.

Today AI work is captured by copying each source application's own session files when a work block ends (ADR 0019; until 2026-11-26 Claude Code hooks copied transcripts as a session ran, and hooks now give only the prompt gate and heartbeats), and by a small standard-library receiver inside goodall that writes Claude Code's OpenTelemetry export verbatim to JSON Lines files. We first chose the official OpenTelemetry Collector for the telemetry half, then reversed after testing it: its file exporter truncates its output on restart by default, cannot combine appending with rotation, buffers writes for a second with no way to force them to disk, and re-encodes the data, and the Collector ships as a 171 MB binary that macOS does not treat as notarized. A logging proxy in front of the API was rejected earlier as a large dependency on a machine that holds credentials. The Collector remains an optional alternative for people who already run one, so readers of telemetry files accept both encodings.

## Consequences

Claude Code drops telemetry silently when nothing is listening, so goodall must check its receiver is up; transcripts are copied regardless, when the work block ends, and are the record of last resort.
