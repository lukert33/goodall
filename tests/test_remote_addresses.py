"""Tests for reading which GitHub repository, if any, a git remote's address names."""

import pytest

from goodall.remote_addresses import RemoteHost, describe_remote_address


@pytest.mark.parametrize(
    "address",
    [
        "https://github.com/your-org/goodall-pilot-records.git",
        "https://github.com/your-org/goodall-pilot-records",
        "https://github.com/your-org/goodall-pilot-records/",
        "https://www.github.com/your-org/goodall-pilot-records.git",
        "git@github.com:your-org/goodall-pilot-records.git",
        "github.com:your-org/goodall-pilot-records",
        "ssh://git@github.com/your-org/goodall-pilot-records.git",
        "ssh://git@ssh.github.com:443/your-org/goodall-pilot-records.git",
        "https://x-access-token:ghp_SYNTHETIC@github.com/your-org/goodall-pilot-records.git",
        "HTTPS://GitHub.com/your-org/goodall-pilot-records.git",
    ],
)
def test_every_way_of_writing_a_github_address_names_the_same_repository(address: str) -> None:
    remote = describe_remote_address(address)

    assert remote.host == RemoteHost.GITHUB
    assert remote.github_repository == "your-org/goodall-pilot-records"


@pytest.mark.parametrize(
    "address",
    [
        "git@gitlab.com:your-org/records.git",
        "https://example.org/records.git",
        "/Users/ada/shared/records.git",
        "file:///Users/ada/shared/records.git",
        "../records",
        "https://github.com.example.org/your-org/records.git",
        "https://notgithub.com/your-org/records.git",
    ],
)
def test_an_address_anywhere_but_github_is_elsewhere(address: str) -> None:
    remote = describe_remote_address(address)

    assert remote.host == RemoteHost.ELSEWHERE
    assert remote.github_repository is None


@pytest.mark.parametrize(
    "address",
    [
        "https://github.com/your-org",
        "https://github.com/your-org/records/tree/main",
        "git@github.com:records.git",
        "https://github.com/your-org/rec ords.git",
        "https://github.com/",
    ],
)
def test_a_github_address_that_names_no_single_repository_is_unclear(address: str) -> None:
    remote = describe_remote_address(address)

    assert remote.host == RemoteHost.UNCLEAR
    assert remote.github_repository is None
