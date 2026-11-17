"""Unit tests for goodall.folders, which asks the disk about the folders local settings name.

`goodall status` and `goodall doctor` drive it from outside; local settings
already refuse a path no folder can have, so this pins down that the folder
check itself never raises either.
"""

from pathlib import Path

from goodall.folders import describe_folder_problem


def test_a_path_with_a_null_character_is_a_problem_not_an_exception() -> None:
    problem = describe_folder_problem(Path("/Users/SYNTHETICsynthetic\x00/records"), needs_writing=True)

    assert problem is not None
    assert "SYNTHETICsynthetic" not in problem
