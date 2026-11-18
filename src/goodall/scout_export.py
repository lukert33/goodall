"""`goodall export scout`: build an Inspect Scout database of this eval's records.

Called by goodall.cli. It finds the eval through local settings, checks where
the database is to go, gathers goodall's own additions
(goodall.scout_additions), and has goodall.scout_database run Scout's
importer and write the database. It then tells the person where the database
is and how to open it, or what to do next.

Design decisions a reader would otherwise reverse-engineer:

- The database is derived data (ADR 0003): every export builds it afresh
  from the raw records and the record repo. It is built in a new folder
  beside the destination and swapped in only when complete, so a failed
  export leaves the last good database in place. Scout never overwrites a
  transcript it already holds, so adding to an old database is never right.
- Replacing a folder means deleting it, so goodall replaces only a folder it
  made itself, which it knows by the `goodall-export.json` file it leaves in
  it. Any other folder that is not empty is refused.
- The default destination is `<log directory>/exports/scout`: beside the
  raw records it is made from, private to this Mac, and never inside
  `transcripts/` or `superseded-transcripts/`, where an importer would take
  it for raw records (ADR 0015). Those two are refused as destinations.
- inspect-ai and inspect-scout are the optional `scout` extra. This module
  imports goodall.scout_database, which needs them, only inside
  `run_export_scout`, so `goodall` loads and every other command runs
  without the extra, and a missing extra is a failure with the command that
  installs it (AGENTS.md invariant 8).
- Telemetry is not exported yet: Scout's claude_code importer reads
  transcripts only, and goodall's telemetry files have no mapping to Inspect
  events decided.
"""

import importlib
import json
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from goodall import exit_codes
from goodall.eval_config import read_eval_config
from goodall.folders import describe_folder_problem, record_repo_missing_failure, where_goodall_looked
from goodall.local_settings import LocalSettings, read_local_settings
from goodall.scout_additions import gather_additions, rules_versions
from goodall.terminal import Failure, Terminal
from goodall.transcripts import SUPERSEDED_FOLDER_NAME, TRANSCRIPTS_FOLDER_NAME

# Where the database goes unless --output says otherwise, inside the log directory.
DEFAULT_EXPORT_FOLDER = Path("exports") / "scout"

# The file goodall leaves in every database it builds, so it knows which
# folders it may replace. It also says what built the database.
EXPORT_MARKER_FILE_NAME = "goodall-export.json"

# The suffix of the folder a new database is built in, beside the destination.
BUILDING_SUFFIX = ".goodall-building"

# Commands that install the optional extra: from the copy of goodall it was
# installed from, as a command, or into a development copy.
INSTALL_EXTRA_COMMANDS = ("uv tool install --reinstall '.[scout]'", "uv sync --extra scout")


@dataclass
class ExportResult:
    """What one run of `goodall export scout` did, for both text and JSON output."""

    database: Path | None = None
    transcripts: dict[str, int] = field(default_factory=dict)
    records: dict[str, int] = field(default_factory=dict)
    events_added: dict[str, int] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)
    warnings: list[Failure] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)


def run_export_scout(terminal: Terminal, output: Path | None, as_json: bool) -> int:
    """Run `goodall export scout`, show the result, and return the exit code."""
    result = export_scout(output)
    show_result(terminal, result, as_json)
    if result.failures:
        return exit_codes.ERROR
    return exit_codes.SUCCESS


def export_scout(output: Path | None) -> ExportResult:
    """Build the database and say what happened. Never raises for a problem a person can fix."""
    result = ExportResult()
    settings_reading = read_local_settings()
    if isinstance(settings_reading, list):
        result.failures.extend(settings_reading)
        return result
    settings = settings_reading

    record_repo_problem = describe_folder_problem(settings.record_repo.path, needs_writing=False)
    if record_repo_problem is not None:
        result.failures.append(record_repo_missing_failure(settings, record_repo_problem))
        return result
    log_directory_problem = describe_folder_problem(settings.log_directory.path, needs_writing=False)
    if log_directory_problem is not None:
        result.failures.append(log_directory_missing_failure(settings, log_directory_problem))
        return result
    eval_config_reading = read_eval_config(settings.record_repo.path, settings.settings_file)
    if isinstance(eval_config_reading, list):
        result.failures.extend(eval_config_reading)
        return result

    log_directory = settings.log_directory.path
    database = destination_for(output, log_directory)
    destination_failure = check_destination(database, log_directory)
    if destination_failure is not None:
        result.failures.append(destination_failure)
        return result

    try:
        scout_database: Any = importlib.import_module("goodall.scout_database")
    except ImportError:
        result.failures.append(scout_extra_missing_failure())
        return result

    additions = gather_additions(settings.record_repo.path, log_directory, eval_config_reading.name)
    result.warnings.extend(additions.warnings)
    building = database.parent / (database.name + BUILDING_SUFFIX)
    try:
        if building.exists():
            shutil.rmtree(building)
        summary = scout_database.build_database(log_directory / TRANSCRIPTS_FOLDER_NAME, building, additions)
        versions = scout_database.package_versions()
        write_marker(building, additions.rules_versions, versions)
        if database.exists():
            shutil.rmtree(database)
        building.rename(database)
    except OSError as folder_error:
        shutil.rmtree(building, ignore_errors=True)
        result.failures.append(database_not_written_failure(database, folder_error))
        return result
    except Exception as import_error:
        # Scout's importer can fail on a transcript in a shape it does not
        # know. The error's own message may quote the transcript, so only
        # its type is shown.
        shutil.rmtree(building, ignore_errors=True)
        result.failures.append(scout_import_failed_failure(type(import_error).__name__))
        return result

    result.database = database
    result.transcripts = {
        "claude_code": summary.claude_code_transcripts,
        "work_blocks": summary.work_block_transcripts,
        "outside_work_blocks": summary.outside_work_blocks_transcripts,
    }
    result.records = additions.record_counts
    result.events_added = summary.events_added
    result.versions = versions
    return result


def destination_for(output: Path | None, log_directory: Path) -> Path:
    """The folder the database goes in: --output as given, or the default in the log directory."""
    if output is None:
        return log_directory / DEFAULT_EXPORT_FOLDER
    return Path(output).expanduser().absolute()


def check_destination(database: Path, log_directory: Path) -> Failure | None:
    """Refuse a destination inside the raw transcripts, or a folder goodall did not make."""
    resolved = database.resolve()
    for raw_folder_name in [TRANSCRIPTS_FOLDER_NAME, SUPERSEDED_FOLDER_NAME]:
        raw_folder = (log_directory / raw_folder_name).resolve()
        if resolved == raw_folder or raw_folder in resolved.parents:
            return export_folder_inside_raw_records_failure(database, raw_folder_name)
    if not database.exists():
        return None
    if not database.is_dir():
        return export_folder_not_goodalls_failure(database, "is a file")
    if (database / EXPORT_MARKER_FILE_NAME).is_file():
        return None
    if any(database.iterdir()):
        return export_folder_not_goodalls_failure(database, "already holds files goodall did not put there")
    return None


def write_marker(database: Path, versions_of_rules: dict[str, str], versions_of_packages: dict[str, str]) -> None:
    """Leave the file that says goodall built this database, and with which rules and packages."""
    marker = {
        "built_by": "goodall export scout",
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rules_versions": versions_of_rules,
        "versions": versions_of_packages,
        "note": "Derived data: goodall export scout deletes and rebuilds this folder each time.",
    }
    (database / EXPORT_MARKER_FILE_NAME).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def scout_extra_missing_failure() -> Failure:
    """The failure for an install of goodall without the optional scout extra."""
    return Failure(
        kind="scout_extra_missing",
        what_happened=(
            "goodall export scout needs inspect-ai and inspect-scout, which are not installed. Nothing was exported."
        ),
        why=(
            "They are an optional part of goodall, the scout extra, because they bring about 95 packages that "
            "recording an eval does not need."
        ),
        what_to_do_next=(
            "Install goodall again with its scout extra: in the copy of goodall you installed from, run the first "
            "command below; in a copy you develop goodall in, run the second. Then run goodall export scout again."
        ),
        commands_to_try=INSTALL_EXTRA_COMMANDS,
    )


def log_directory_missing_failure(settings: LocalSettings, problem: str) -> Failure:
    """The failure for a log directory the export cannot read."""
    return Failure(
        kind="log_directory_unreachable",
        what_happened=(
            f"The log directory {settings.log_directory.as_written} {problem}, so there is nothing to export."
            + where_goodall_looked(settings.log_directory)
        ),
        why="The export reads the Claude Code transcripts goodall copied into the log directory.",
        what_to_do_next=(
            "If the log directory is on an external drive, connect it. Otherwise correct log_directory in "
            f"{settings.settings_file}. Then run goodall export scout again."
        ),
    )


def export_folder_inside_raw_records_failure(database: Path, raw_folder_name: str) -> Failure:
    """The failure for a destination inside the folders of raw transcripts."""
    return Failure(
        kind="export_folder_inside_raw_records",
        what_happened=(
            f"goodall did not export to {database}, because it is inside {raw_folder_name}/ in the log directory."
        ),
        why=(
            "That folder holds raw records exactly as they were copied, and an importer reading it would take "
            "anything else there for a Claude session."
        ),
        what_to_do_next="Choose a folder outside it with --output, or leave --output out to use the default.",
    )


def export_folder_not_goodalls_failure(database: Path, problem: str) -> Failure:
    """The failure for a destination goodall would have to delete something it did not make to use."""
    return Failure(
        kind="export_folder_not_goodalls",
        what_happened=f"goodall did not export to {database}, because it {problem}. Nothing was changed.",
        why=(
            "Each export replaces the whole database folder, and goodall replaces only a folder it made itself, "
            f"which it knows by the {EXPORT_MARKER_FILE_NAME} file it leaves inside."
        ),
        what_to_do_next="Choose a new or empty folder with --output, then run goodall export scout again.",
    )


def database_not_written_failure(database: Path, folder_error: OSError) -> Failure:
    """The failure for a database folder that could not be written."""
    reason = folder_error.strerror or type(folder_error).__name__
    return Failure(
        kind="database_not_written",
        what_happened=(
            f"goodall could not write the database to {database}: {reason}. The last export, if any, is unchanged."
        ),
        why="The database is built in a folder beside the destination and then moved into place.",
        what_to_do_next=(
            f"Make sure this macOS user account can write to {database.parent}, or choose another folder with "
            "--output. Then run goodall export scout again."
        ),
    )


def scout_import_failed_failure(error_type: str) -> Failure:
    """The failure for an error inside Scout's importer or database."""
    return Failure(
        kind="scout_import_failed",
        what_happened=(
            f"Inspect Scout stopped with an error ({error_type}) while building the database. Nothing was exported."
        ),
        why=(
            "Scout's importer reads Claude Code's transcript format, which changes between Claude Code versions, "
            "so a newer transcript can hold something it does not know yet."
        ),
        what_to_do_next=(
            "Check for a newer goodall, which may pin a newer inspect-scout, and run goodall export scout again. "
            "If it still fails, report it to whoever looks after goodall, with the error's name above."
        ),
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def show_result(terminal: Terminal, result: ExportResult, as_json: bool) -> None:
    """Show `result` as text, or as one JSON document whose keys are always the same."""
    if as_json:
        warning_documents = []
        for warning in result.warnings:
            warning_documents.append(warning.as_json())
        failure_documents = []
        for failure in result.failures:
            failure_documents.append(failure.as_json())
        database = None
        if result.database is not None:
            database = str(result.database)
        terminal.write_json(
            {
                "exported": result.database is not None,
                "database": database,
                "transcripts": result.transcripts,
                "records": result.records,
                "events_added": result.events_added,
                "rules_versions": rules_versions(),
                "versions": result.versions,
                "warnings": warning_documents,
                "failures": failure_documents,
            }
        )
        return
    if result.database is None:
        terminal.show_failures(result.failures)
        return
    terminal.show_paragraph(f"Exported this eval to an Inspect Scout database in {result.database}.")
    terminal.show_facts(
        [
            ("Claude Code transcripts", str(result.transcripts["claude_code"])),
            ("Work blocks", str(result.records["work_blocks"])),
            ("Journal entries", str(result.records["journal_entries"])),
            ("Interventions", str(result.records["interventions"])),
            ("Events goodall added", describe_events(result.events_added)),
        ]
    )
    terminal.show_paragraph(
        "The database is derived from the raw records and the record repo; running goodall export scout again "
        "rebuilds it. To look through it in Scout's viewer:"
    )
    terminal.show_commands((f"scout view -T {shlex.quote(str(result.database))}",))
    terminal.show_warnings(result.warnings)


def describe_events(events_added: dict[str, int]) -> str:
    """Say how many events of each type goodall added, in words."""
    return (
        f"{events_added['approval']} approval, {events_added['info']} info, {events_added['input']} input, "
        f"{events_added['interrupt']} interrupt"
    )
