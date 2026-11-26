"""The Claude family: where its agent sessions are kept, which belong to a work block, and how they are copied.

The Claude family is two source applications that write the same files:
the Claude desktop app's Code tab and Claude Code in a terminal. Each
record's `entrypoint` field says which one wrote it; goodall copies both
alike and reads that field nowhere. It is the first app family behind the
baseline (goodall.agent_session_copies, ADR 0019), which calls the three
functions gathered in APP_FAMILY at the bottom of this file. What the
record is known to lack is in docs/source-applications.md.

What is copied, in the family's own layout (ADR 0015), for each agent
session that has a record inside the work block:

    In Claude Code's config folder (~/.claude, or CLAUDE_CONFIG_DIR):
    projects/<project folder>/<session id>.jsonl
    projects/<project folder>/<session id>/...   everything, at any depth:
        custom-title.json, subagents/, tool-results/ (text, images, pdf-<id>/ pages), ...
    projects/<project folder>/MEMORY.md and memory/...   the project's memory
      are copied to
    <log directory>/transcripts/<project folder>/...   at the same paths

    history.jsonl                  the prompt history, across Claude sessions
    file-history/<session id>/...  a Claude session's file-edit backups
      (CLAUDE_CONFIG_FOLDER_ALLOW_LIST: nothing else in the config folder) are copied to
    <log directory>/claude-config/<config folder's path, dashed>/...   at the same paths

    and, beside them, earlier copies that were replaced (goodall.verbatim_copies):
    <log directory>/superseded-transcripts/<moment>/...
    <log directory>/superseded-claude-config/<moment>/...

Who else calls it: `goodall delete` (goodall.work_block_deletion) asks
find_copied_agent_sessions and copied_places which copies are one Claude
session's alone. `goodall hook` (goodall.hook) calls
remember_claude_config_folder, so this module is on the hook path: it
imports only the standard library (ADR 0005) and starts no program.

Where Claude Code's config folder is: `~/.claude` unless the environment
variable CLAUDE_CONFIG_DIR names another, with session files in `projects`
inside it. CLAUDE_CONFIG_DIR is Claude Code's own setting, not goodall's;
goodall honors it so it looks where Claude Code writes, and tests point it
(and HOME) at tmp_path so the real home folder, which holds a person's
private prompts, stays out of reach. The hooks inherit Claude Code's
environment, so they always know the right place, but a terminal may not
have the same CLAUDE_CONFIG_DIR. So each hook remembers its config folder
in the log directory, and goodall looks in every folder a hook remembered,
the default `~/.claude`, and the terminal's own: a person never needs to
know the variable exists.

Decisions a reader would otherwise reverse-engineer:

- A record's time is its top-level `timestamp`, which the family writes in
  UTC (`2026-11-16T09:35:00.123Z`). Records without one (titles, modes,
  file-history snapshots and other bookkeeping lines) say nothing about
  when the agent session was used and are passed over, as is a line that
  is not JSON. Nothing else is read from a record, and nothing read from a
  record is ever printed or stored.
- The records that count are those in the transcript and in its subagents'
  transcripts (`<session id>/subagents/`, at any depth). Other `.jsonl`
  files in the session's folder, such as a tool's saved output, are not
  the family's records and could hold a `timestamp` of anything.
- A file last changed before the work block began is not opened: a record
  from inside the work block was written inside it or later, so the file's
  modification time would say so. This keeps `goodall stop` from reading
  every session file in the account each time. Trap: a file copied from
  another Mac by a tool that kept an old modification time would be missed.
- Goodall copies everything the family keeps for an agent session, not
  only the transcripts: tool results, images, file-edit backups, the
  project's memory and the prompt history, and whatever a later version
  adds inside a session's folder. Storage is the cost of a complete raw
  record, and a file left out now cannot be copied after Claude Code
  deletes it. The project's memory and the prompt history are shared by
  several agent sessions; they are copied along with each one that belongs
  to the work block, and an unchanged file costs two stats.
- Goodall copies only from inside a Claude config folder it found itself,
  and there only from the projects folder and the places
  CLAUDE_CONFIG_FOLDER_ALLOW_LIST names: never settings, credentials or
  the other folders beside them. A `projects` that is a symbolic link is
  not followed, and is reported.
"""

import json
import os
import secrets
import stat
from datetime import datetime
from pathlib import Path

from goodall import records
from goodall.source_applications import AgentSession, AppFamily, FoundAgentSessions, is_inside_work_block
from goodall.verbatim_copies import (
    FILE_PERMISSIONS,
    SYNC_FOLDER_NAME,
    TEMPORARY_SUFFIX,
    Copying,
    Mirror,
    copy_places,
    describe_error,
    held_lock,
    list_files_at,
    list_folders,
    make_folders,
    status_or_none,
)

# The folder in the log directory that mirrors Claude Code's projects folder.
TRANSCRIPTS_FOLDER_NAME = "transcripts"

# The folder in the log directory where an earlier copy is kept when the
# source no longer starts with it.
SUPERSEDED_FOLDER_NAME = "superseded-transcripts"

# The folder in the log directory that mirrors, for each Claude config
# folder, the places in the allow-list below, and its own place for earlier
# copies. Each Claude config folder gets a folder of its own inside, named
# after its real path the way Claude Code names a project folder
# (`/Users/ada/.claude` becomes `-Users-ada--claude`), because the prompt
# history of two config folders would otherwise land on the same path.
CLAUDE_CONFIG_COPIES_FOLDER_NAME = "claude-config"
SUPERSEDED_CLAUDE_CONFIG_FOLDER_NAME = "superseded-claude-config"

# The allow-list: the only places goodall copies from a Claude config folder
# outside its `projects` folder, as paths inside it. A folder is copied
# whole; SESSION_ID_PLACEHOLDER stands for one Claude session's id.
#
# - `history.jsonl`: the prompts typed into Claude Code, across every Claude
#   session of the account, one line per prompt.
# - `file-history/<session id>/`: the copies Claude Code keeps of each file
#   a Claude session edits, before and after, so its edits can be undone.
#
# Nothing else in the folder is ever read. Settings, credentials
# (`.credentials.json`), `session-env/`, `shell-snapshots/`, `telemetry/`,
# `sessions/` and the rest hold secrets or say nothing about the work, and
# goodall stays out of them. Add to this list only by a decision.
SESSION_ID_PLACEHOLDER = "<session id>"
CLAUDE_CONFIG_FOLDER_ALLOW_LIST = ["history.jsonl", f"file-history/{SESSION_ID_PLACEHOLDER}"]

# What Claude Code keeps for a whole project, beside its Claude sessions in
# the project folder: its memory (notes Claude Code reads into every Claude
# session in that project), copied whole into the same place in
# `transcripts/`. Scout's importer reads only the `*.jsonl` files directly in
# a project folder, so neither disturbs it.
PROJECT_MEMORY_PATHS = ["MEMORY.md", "memory"]

# The Claude config folders hooks have run in, as JSON:
# {"claude_config_folders": ["/Users/ada/.claude", ...]}, oldest first. Its
# own lock, in `locks/`, keeps two hooks from losing each other's folder;
# the name cannot clash with a Claude session's, which is a UUID, nor with
# the other locks, whose names end in a hash.
REMEMBERED_FOLDERS_FILE_NAME = "claude-config-folders.json"
REMEMBERED_FOLDERS_LOCK_NAME = "claude-config-folders"

# The most folders remembered. An eval account has one or two; the limit
# only keeps a misbehaving setup from growing the file without bound. When
# it is reached the folder remembered longest ago is forgotten, and the
# next hook that runs in it remembers it again.
REMEMBERED_FOLDERS_LIMIT = 20


def claude_projects_folder() -> Path:
    """Where Claude Code keeps its transcripts: `projects` in its config folder (claude_config_folder)."""
    return claude_config_folder() / "projects"


def claude_config_folder() -> Path:
    """Claude Code's own folder: the one CLAUDE_CONFIG_DIR names, or `~/.claude`.

    Read from the environment each time, so a test can change it. An empty
    value counts as unset, as goodall treats its own GOODALL_CONFIG_DIR.
    """
    claude_config_directory = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if claude_config_directory == "":
        return default_claude_config_folder()
    return Path(claude_config_directory).expanduser()


def default_claude_config_folder() -> Path:
    """Claude Code's own folder when CLAUDE_CONFIG_DIR is not set: `~/.claude`."""
    return Path.home() / ".claude"


def remember_claude_config_folder(log_directory: Path, config_folder: Path) -> None:
    """Add `config_folder` to the Claude config folders goodall looks in, kept in `transcript-sync/`.

    A hook calls this each time it runs, so the file is first read without
    the lock, and when the folder is already there, as it nearly always is,
    nothing is written: one small read per hook. Otherwise, under the lock,
    the file is read again (another hook may just have added a folder), the
    folder is added at the end, the oldest are dropped past
    REMEMBERED_FOLDERS_LIMIT, and the whole file is replaced by renaming a
    temporary file over it, so a reader never sees half of it.
    """
    folder_text = str(config_folder)
    if folder_text in read_remembered_claude_config_folders(log_directory):
        return
    with held_lock(log_directory, REMEMBERED_FOLDERS_LOCK_NAME, "remembering a Claude config folder"):
        remembered = read_remembered_claude_config_folders(log_directory)
        if folder_text in remembered:
            return
        remembered.append(folder_text)
        remembered = remembered[-REMEMBERED_FOLDERS_LIMIT:]
        sync_folder = make_folders(log_directory, [SYNC_FOLDER_NAME])
        remembered_file = sync_folder / REMEMBERED_FOLDERS_FILE_NAME
        temporary_file = sync_folder / f".{REMEMBERED_FOLDERS_FILE_NAME}.{secrets.token_hex(4)}{TEMPORARY_SUFFIX}"
        try:
            descriptor = os.open(temporary_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSIONS)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                json.dump({"claude_config_folders": remembered}, temporary, indent=2)
                temporary.write("\n")
            os.replace(temporary_file, remembered_file)
        finally:
            temporary_file.unlink(missing_ok=True)


def read_remembered_claude_config_folders(log_directory: Path) -> list[str]:
    """The Claude config folders hooks have remembered, oldest first; none when the file is missing.

    A file that is not the expected JSON (it was edited by hand, say) counts
    as empty, so the next hook replaces it rather than every hook failing.
    It is only goodall's bookkeeping, not a record. Raises OSError when the
    file is there but cannot be read.
    """
    remembered_file = log_directory / SYNC_FOLDER_NAME / REMEMBERED_FOLDERS_FILE_NAME
    try:
        remembered_text = remembered_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        remembered = json.loads(remembered_text)
    except ValueError:
        return []
    if not isinstance(remembered, dict):
        return []
    folders = remembered.get("claude_config_folders")
    if not isinstance(folders, list):
        return []
    folder_texts = []
    for folder in folders:
        if isinstance(folder, str):
            folder_texts.append(folder)
    return folder_texts


def claude_config_folders_to_search(own_config_folder: Path, log_directory: Path) -> tuple[list[Path], list[str]]:
    """Every Claude config folder goodall looks in, each once, by its real path, with problems found on the way.

    The folders are this process's own (from its environment), the default
    `~/.claude`, and every folder hooks have remembered. Two names for one
    folder (through a symbolic link, say) count once, so no agent session
    is found twice. A folder that does not exist is passed over without a word: most
    accounts never have more than one.
    """
    problems = []
    candidates = [own_config_folder, default_claude_config_folder()]
    try:
        for folder_text in read_remembered_claude_config_folders(log_directory):
            candidates.append(Path(folder_text))
    except OSError as error:
        problems.append(f"The list of Claude config folders hooks used, in the log directory: {describe_error(error)}.")
    real_folders: list[Path] = []
    for candidate in candidates:
        try:
            real_folder = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        except OSError as error:
            problems.append(f"The Claude config folder {candidate}: {describe_error(error)}.")
            continue
        if real_folder not in real_folders:
            real_folders.append(real_folder)
    return real_folders, problems


def list_claude_sessions(project_folder: Path) -> list[str]:
    """The Claude session ids in one project folder: from `<id>.jsonl` files and `<id>/` folders, sorted."""
    claude_sessions = set()
    with os.scandir(project_folder) as entries:
        for entry in entries:
            name = entry.name
            if entry.is_file(follow_symlinks=False) and name.endswith(".jsonl"):
                name = name.removesuffix(".jsonl")
            elif not entry.is_dir(follow_symlinks=False):
                continue
            if records.CLAUDE_SESSION_ID_PATTERN.fullmatch(name):
                claude_sessions.add(name)
    return sorted(claude_sessions)


def transcripts_mirror(projects_folder: Path) -> Mirror:
    """A Claude projects folder, copied into `transcripts/`."""
    return Mirror(projects_folder, (TRANSCRIPTS_FOLDER_NAME,), SUPERSEDED_FOLDER_NAME)


def claude_config_mirror(config_folder: Path) -> Mirror:
    """The allow-list's places in a Claude config folder, copied into `claude-config/<name for that folder>/`."""
    return Mirror(
        config_folder,
        (CLAUDE_CONFIG_COPIES_FOLDER_NAME, mirror_folder_name(config_folder)),
        SUPERSEDED_CLAUDE_CONFIG_FOLDER_NAME,
    )


def mirror_folder_name(config_folder: Path) -> str:
    """A folder name for a Claude config folder: its path with every character but ASCII letters and digits a dash.

    This is how Claude Code names a project folder after the project's path,
    so `/Users/ada/.claude` becomes `-Users-ada--claude`. Two paths that
    differ only in such characters would share a name; for the one or two
    Claude config folders an account has, that does not happen.
    """
    name = ""
    for character in str(config_folder):
        if character.isascii() and character.isalnum():
            name += character
        else:
            name += "-"
    return name


def find_agent_sessions(log_directory: Path) -> FoundAgentSessions:
    """Every Claude session in this macOS user account, whatever folder it ran in, with problems found on the way.

    Looks in the projects folder of every Claude config folder from
    claude_config_folders_to_search. Each agent session's `folder` is its
    project folder, `<config folder>/projects/<project folder>`, which is
    how the two functions below find the rest. Never raises for anything on
    the disk: what cannot be read becomes one of the problems, and the rest
    is still found.
    """
    agent_sessions = []
    config_folders, problems = claude_config_folders_to_search(claude_config_folder(), log_directory)
    for config_folder in config_folders:
        projects_folder = config_folder / "projects"
        try:
            projects_status = status_or_none(projects_folder)
            if projects_status is not None and stat.S_ISLNK(projects_status.st_mode):
                problems.append(
                    f"The Claude projects folder {projects_folder} is a symbolic link, which goodall does not follow."
                )
                continue
            project_folder_names = list_folders(projects_folder)
        except OSError as error:
            problems.append(f"The Claude projects folder {projects_folder}: {describe_error(error)}.")
            continue
        for project_folder_name in project_folder_names:
            project_folder = projects_folder / project_folder_name
            try:
                claude_sessions = list_claude_sessions(project_folder)
            except OSError as error:
                problems.append(f"The Claude project folder {project_folder_name}: {describe_error(error)}.")
                continue
            for claude_session in claude_sessions:
                agent_sessions.append(AgentSession(id=claude_session, folder=project_folder))
    return FoundAgentSessions(agent_sessions, problems)


def has_record_inside(agent_session: AgentSession, started_at: datetime, ended_at: datetime) -> bool:
    """True when the Claude session has a record inside the work block.

    The records are those of its transcript and of its subagents'
    transcripts. Stops at the first such record. Raises OSError when a transcript is
    there but cannot be read, so the baseline reports it rather than
    quietly leaving the agent session out.
    """
    record_files = [Path(f"{agent_session.id}.jsonl")]
    subagent_files, _ = list_files_at(agent_session.folder, Path(agent_session.id) / "subagents")
    for subagent_file in subagent_files:
        if subagent_file.suffix == ".jsonl":
            record_files.append(subagent_file)
    for record_file in record_files:
        if file_has_record_inside(agent_session.folder / record_file, started_at, ended_at):
            return True
    return False


def file_has_record_inside(transcript: Path, started_at: datetime, ended_at: datetime) -> bool:
    """True when one transcript file has a record whose time is inside the work block.

    A file that is missing, is not a regular file, or was last changed
    before the work block began is not opened (see the module docstring).
    The file is read as bytes, a line at a time, because a transcript may
    hold bytes that are not valid text.
    """
    transcript_status = status_or_none(transcript)
    if transcript_status is None or not stat.S_ISREG(transcript_status.st_mode):
        return False
    if transcript_status.st_mtime < started_at.timestamp():
        return False
    # O_NOFOLLOW refuses a symbolic link put in the file's place since the
    # check above; O_NONBLOCK keeps a named pipe put there from waiting.
    descriptor = os.open(transcript, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as lines:
        for line in lines:
            moment = record_time(line)
            if moment is not None and is_inside_work_block(moment, started_at, ended_at):
                return True
    return False


def record_time(line: bytes) -> datetime | None:
    """The time one transcript record carries, from its top-level `timestamp`; None when it carries none.

    None, too, for a line that is not a JSON object, or whose `timestamp`
    is not a time with a zone: goodall guesses no zone for a record.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def copy_agent_session(agent_session: AgentSession, log_directory: Path, now: datetime) -> Copying:
    """Copy everything the Claude family keeps for one Claude session into the log directory, at the same paths.

    From the projects folder, into `transcripts/`: the transcript, the
    whole of the session's folder, and its project's memory. From the rest
    of the config folder, into `claude-config/`: what the allow-list names,
    which is this session's file-edit backups and the prompt history. This
    is the only function that reads from a Claude config folder outside
    its projects folder, and it reads nothing the allow-list does not name.
    Raises OSError when the log directory cannot be written.
    """
    projects_folder = agent_session.folder.parent
    config_folder = projects_folder.parent
    project_folder_name = Path(agent_session.folder.name)
    places_in_projects = [project_folder_name / f"{agent_session.id}.jsonl", project_folder_name / agent_session.id]
    for memory_path in PROJECT_MEMORY_PATHS:
        places_in_projects.append(project_folder_name / memory_path)
    places_in_config_folder = []
    for allowed in CLAUDE_CONFIG_FOLDER_ALLOW_LIST:
        places_in_config_folder.append(Path(allowed.replace(SESSION_ID_PLACEHOLDER, agent_session.id)))
    copying = copy_places(transcripts_mirror(projects_folder), log_directory, places_in_projects, now)
    copying.add(copy_places(claude_config_mirror(config_folder), log_directory, places_in_config_folder, now))
    return copying


# ---------------------------------------------------------------------------
# The copies, for `goodall delete` (goodall.work_block_deletion)


def find_copied_agent_sessions(log_directory: Path) -> FoundAgentSessions:
    """Every Claude session with a copy in `transcripts/`, with its folder there, and problems found on the way.

    The copies keep the projects folder's layout and each file's
    modification time, so has_record_inside reads a copy as it reads the
    original. Earlier copies kept aside are not listed: a Claude session's
    current copy is always there beside them.
    """
    agent_sessions = []
    problems = []
    transcripts_folder = log_directory / TRANSCRIPTS_FOLDER_NAME
    try:
        project_folder_names = list_folders(transcripts_folder)
    except OSError as error:
        return FoundAgentSessions([], [f"The copied transcripts, {transcripts_folder}: {describe_error(error)}."])
    for project_folder_name in project_folder_names:
        project_folder = transcripts_folder / project_folder_name
        try:
            claude_sessions = list_claude_sessions(project_folder)
        except OSError as error:
            problems.append(f"The copied project folder {project_folder_name}: {describe_error(error)}.")
            continue
        for claude_session in claude_sessions:
            agent_sessions.append(AgentSession(id=claude_session, folder=project_folder))
    return FoundAgentSessions(agent_sessions, problems)


def copied_places(log_directory: Path, claude_session: str) -> list[Path]:
    """Every file and folder in the log directory that holds a copy of this one Claude session, and nothing shared.

    That is its transcript and its folder in each project folder of
    `transcripts/`, its file-edit backups in each config folder's copies in
    `claude-config/`, and the same places inside every `<moment>/` of the
    two superseded folders. The prompt history and a project's memory are
    shared by every Claude session, so they are never among them
    (SHARED_COPIES_LEFT). An id that is not a Claude session id has none,
    so it can never name a path outside these folders. Raises OSError when
    a folder cannot be listed.
    """
    if not records.CLAUDE_SESSION_ID_PATTERN.fullmatch(claude_session):
        return []
    project_folders = folders_inside(log_directory / TRANSCRIPTS_FOLDER_NAME)
    config_copies_folders = folders_inside(log_directory / CLAUDE_CONFIG_COPIES_FOLDER_NAME)
    for moment_folder in folders_inside(log_directory / SUPERSEDED_FOLDER_NAME):
        project_folders.extend(folders_inside(moment_folder))
    for moment_folder in folders_inside(log_directory / SUPERSEDED_CLAUDE_CONFIG_FOLDER_NAME):
        config_copies_folders.extend(folders_inside(moment_folder))
    candidates = []
    for project_folder in project_folders:
        candidates.append(project_folder / f"{claude_session}.jsonl")
        candidates.append(project_folder / claude_session)
    for config_copies_folder in config_copies_folders:
        candidates.append(config_copies_folder / "file-history" / claude_session)
    places = []
    for candidate in candidates:
        if status_or_none(candidate) is not None:
            places.append(candidate)
    return places


def folders_inside(folder: Path) -> list[Path]:
    """The real folders directly inside `folder`, as full paths; none when it is missing."""
    found = []
    for folder_name in list_folders(folder):
        found.append(folder / folder_name)
    return found


# The copies every Claude session shares, which `goodall delete` leaves in
# place, named for a person.
SHARED_COPIES_LEFT = (
    "the copies of Claude Code's prompt history (history.jsonl, in claude-config/ in the log directory) and of "
    "each project's memory (MEMORY.md and memory/, in transcripts/)"
)

# What the baseline needs to know about the Claude family (goodall.agent_session_copies).
APP_FAMILY = AppFamily(
    name="Claude",
    copies_folder_names=(TRANSCRIPTS_FOLDER_NAME, CLAUDE_CONFIG_COPIES_FOLDER_NAME),
    find_agent_sessions=find_agent_sessions,
    has_record_inside=has_record_inside,
    copy_agent_session=copy_agent_session,
    find_copied_agent_sessions=find_copied_agent_sessions,
    copied_places=copied_places,
    shared_copies_left=SHARED_COPIES_LEFT,
)
