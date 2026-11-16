# Capture with Claude Code hooks and goodall's own telemetry receiver

AI work is captured by Claude Code hooks that copy transcripts as a session runs, and by a small standard-library receiver inside goodall that writes Claude Code's OpenTelemetry export verbatim to JSON Lines files. We first chose the official OpenTelemetry Collector for the telemetry half, then reversed after testing it: its file exporter truncates its output on restart by default, cannot combine appending with rotation, buffers writes for a second with no way to force them to disk, and re-encodes the data, and the Collector ships as a 171 MB binary that macOS does not treat as notarised. A logging proxy in front of the API was rejected earlier as a large dependency on a machine that holds credentials. The Collector remains an optional alternative for people who already run one, so readers of telemetry files accept both encodings.

## Consequences

Claude Code drops telemetry silently when nothing is listening, so goodall must check its receiver is up; transcripts are captured regardless and are the record of last resort.
