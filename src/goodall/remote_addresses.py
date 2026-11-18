"""Reading where a git remote's address points: GitHub, somewhere else, or nowhere goodall can tell.

`goodall doctor`'s record repo checks (goodall.remote_checks) ask git for
the address the record repo pushes to, and hand it to
`describe_remote_address`. The answer decides whether goodall asks gh about
the record repo at all: goodall can check sign-in and visibility only on
GitHub, through GitHub's own gh (ADR 0011: goodall makes no network calls
of its own).

Decisions a reader would otherwise reverse-engineer:

- Only github.com counts as GitHub (with its `www.` and `ssh.` names).
  A GitHub Enterprise server has a name of its own that goodall cannot
  recognise, so it counts as "elsewhere", and the record repo's visibility
  there is left for a person to check.
- An address may carry a user name and a token (`https://user:token@...`).
  Only the host and the `owner/name` pair are kept; nothing else of the
  address is ever returned, so no caller can print a token by accident.
- Both of git's ways of writing an address are read: as a URL
  (`https://`, `ssh://`) and in the short form `[user@]host:owner/name`,
  which git recognises by a colon before any slash. Anything else is a
  folder on this machine.

This module imports only the standard library and nothing from goodall.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

# The names github.com answers to in a git address.
GITHUB_HOSTS = ["github.com", "www.github.com", "ssh.github.com"]

# What GitHub allows in an owner's or a repository's name.
GITHUB_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.\-]+")


class RemoteHost(StrEnum):
    """Where the record repo is shared, as far as goodall can tell."""

    # The record repo has no remote: its records are only on this Mac.
    NONE = "none"
    # It has a remote, but goodall cannot tell which one records go to, or
    # which repository on GitHub it is.
    UNCLEAR = "unclear"
    # A repository on github.com, named in `github_repository`.
    GITHUB = "github"
    # Somewhere other than github.com: another host, or a folder.
    ELSEWHERE = "elsewhere"


@dataclass(frozen=True)
class RecordRepoRemote:
    """Where the record repo is shared: the kind of host and, on GitHub, the repository as `owner/name`."""

    host: RemoteHost
    github_repository: str | None = None


def describe_remote_address(address: str) -> RecordRepoRemote:
    """Say whether `address` points at a repository on GitHub, and which one."""
    host, path = split_address(address)
    if host is None or host.lower() not in GITHUB_HOSTS:
        return RecordRepoRemote(host=RemoteHost.ELSEWHERE)
    repository = github_repository_in(path)
    if repository is None:
        return RecordRepoRemote(host=RemoteHost.UNCLEAR)
    return RecordRepoRemote(host=RemoteHost.GITHUB, github_repository=repository)


def split_address(address: str) -> tuple[str | None, str]:
    """The host and the path of a git address; the host is None for a folder on this machine.

    Trap: a user name and token before the host (`user:token@host`) must be
    dropped before looking for a port, because the token sits after a colon
    too.
    """
    if "://" in address:
        scheme, rest = address.split("://", 1)
        if scheme.lower() == "file":
            return None, ""
        authority, _, path = rest.partition("/")
        host_and_port = authority.rpartition("@")[2]
        host = host_and_port.partition(":")[0]
        return host, path
    colon_position = address.find(":")
    slash_position = address.find("/")
    if colon_position > 0 and (slash_position == -1 or colon_position < slash_position):
        host = address[:colon_position].rpartition("@")[2]
        return host, address[colon_position + 1 :]
    return None, ""


def github_repository_in(path: str) -> str | None:
    """`owner/name` from the path part of a GitHub address, or None when it names no single repository."""
    path = path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2:
        return None
    for part in parts:
        if GITHUB_NAME_PATTERN.fullmatch(part) is None:
            return None
    return f"{parts[0]}/{parts[1]}"
