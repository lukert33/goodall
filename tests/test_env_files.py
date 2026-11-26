"""Unit tests for goodall.env_files, the one reader of the env file and the shared env file.

`goodall doctor`'s tests (tests/test_doctor.py) drive the reader from
outside; these pin down the line format itself, which the code that launches
programs will rely on too.
"""

from pathlib import Path

from goodall.env_files import EnvEntry, EnvFileNotRead, EnvFileReading, read_env_file, read_env_text


def test_quoted_and_unquoted_values_are_read_without_their_quotes() -> None:
    reading = read_env_text(
        "DOUBLE=\"op://Eval/item/field\"\nSINGLE='op://Eval/item with spaces/field'\nPLAIN=op://Eval/item/field\n"
    )

    assert reading == EnvFileReading(
        entries=[
            EnvEntry(name="DOUBLE", value="op://Eval/item/field", line_number=1),
            EnvEntry(name="SINGLE", value="op://Eval/item with spaces/field", line_number=2),
            EnvEntry(name="PLAIN", value="op://Eval/item/field", line_number=3),
        ],
        problems=[],
    )


def test_comments_blank_lines_and_surrounding_whitespace_are_accepted() -> None:
    reading = read_env_text("# a comment\n\n   \n  # an indented comment\n  NAME=1  \r\nOTHER=2")

    assert reading.problems == []
    assert reading.entries == [
        EnvEntry(name="NAME", value="1", line_number=5),
        EnvEntry(name="OTHER", value="2", line_number=6),
    ]


def test_an_empty_value_is_a_problem_because_a_pasted_base64_token_reads_that_way() -> None:
    # A base64 token ending in one = of padding parses as NAME=, so an empty
    # value is far more likely a pasted secret than a variable meant to be empty.
    reading = read_env_text('EMPTY=\nQUOTED_EMPTY=""\nSYNTHETICsyntheticSYNTHETIC0123456789x9=\n')

    assert reading.entries == []
    assert [problem.line_number for problem in reading.problems] == [1, 2, 3]
    for problem in reading.problems:
        assert "SYNTHETICsynthetic" not in problem.problem


def test_the_empty_value_problem_is_marked_so_each_file_can_give_its_own_advice() -> None:
    reading = read_env_text("EMPTY=\nNOT_EMPTY_BUT_BAD\n")

    assert [problem.empty_value for problem in reading.problems] == [True, False]
    assert "placeholder" not in reading.problems[0].problem


def problem_lines(text: str) -> list[int]:
    """The line numbers of every problem the reader finds in `text`."""
    line_numbers = []
    for problem in read_env_text(text).problems:
        line_numbers.append(problem.line_number)
    return line_numbers


def test_a_line_that_is_not_name_equals_value_is_a_problem_with_its_line_number() -> None:
    assert problem_lines("GOOD=1\nsk-ant-api03-a-pasted-key-on-its-own\nALSO_GOOD=2\n") == [2]


def test_a_name_that_is_not_letters_digits_and_underscores_is_a_problem() -> None:
    assert problem_lines("2STARTS_WITH_A_DIGIT=1\nHAS-A-HYPHEN=1\nSPACE BEFORE=1\nNAME =1\n=1\n") == [1, 2, 3, 4, 5]


def test_export_in_front_of_a_name_is_refused_with_its_own_explanation() -> None:
    reading = read_env_text("export NAME=1\n")

    assert reading.entries == []
    assert len(reading.problems) == 1
    assert reading.problems[0].line_number == 1
    assert "export" in reading.problems[0].problem


def test_badly_quoted_values_are_problems() -> None:
    text = 'UNCLOSED="op://a/b/c\nMISMATCHED="op://a/b/c\'\nTRAILING="op://a/b/c" # note\nINSIDE=a"b\n'

    assert problem_lines(text) == [1, 2, 3, 4]


def test_an_unquoted_value_with_a_space_is_a_problem_because_the_end_of_the_value_is_unclear() -> None:
    # Some readers would take "value" and a comment, others the whole line.
    assert problem_lines("NAME=value # a comment\nOTHER=op://Eval/an item/field\n") == [1, 2]


def test_a_name_given_twice_is_a_problem_naming_both_lines() -> None:
    reading = read_env_text("NAME=1\nOTHER=2\nNAME=3\n")

    assert [problem.line_number for problem in reading.problems] == [3]
    assert "line 1" in reading.problems[0].problem
    # The first one is kept, so a later check does not report the name as missing.
    assert [entry.name for entry in reading.entries] == ["NAME", "OTHER"]


def test_a_name_given_twice_is_not_shown_when_it_looks_like_a_token() -> None:
    token_name = "SYNTHETICsyntheticSYNTHETIC0123456789x9"
    reading = read_env_text(f"{token_name}==\n{token_name}==\n")

    assert [problem.line_number for problem in reading.problems] == [2]
    assert "SYNTHETICsynthetic" not in reading.problems[0].problem
    assert "line 1" in reading.problems[0].problem


def test_no_problem_ever_repeats_what_was_written_on_the_line() -> None:
    # Anything on a malformed line could be a key pasted by mistake.
    pasted_value = "sk-ant-api03-RECOGNIZABLEFAKEVALUE"
    text = (
        f"{pasted_value}\n"
        f"export NAME={pasted_value}\n"
        f'QUOTED="{pasted_value}\n'
        f"{pasted_value}=1\n"
        f"SPACED={pasted_value} more\n"
    )

    reading = read_env_text(text)

    assert len(reading.problems) == 5
    for problem in reading.problems:
        assert "RECOGNIZABLEFAKEVALUE" not in problem.problem


def test_a_missing_file_is_reported_as_missing_not_raised(tmp_path: Path) -> None:
    reading = read_env_file(tmp_path / ".env")

    assert isinstance(reading, EnvFileNotRead)
    assert reading.missing


def test_a_file_that_is_not_text_is_reported_not_raised(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"NAME=\xff\xfe\n")

    reading = read_env_file(env_file)

    assert isinstance(reading, EnvFileNotRead)
    assert not reading.missing
    assert "UTF-8" in reading.reason


def test_a_folder_in_place_of_the_file_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()

    reading = read_env_file(tmp_path / ".env")

    assert isinstance(reading, EnvFileNotRead)
    assert not reading.missing


def test_a_readable_file_is_read(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NAME=1\n")

    assert read_env_file(env_file) == EnvFileReading(entries=[EnvEntry(name="NAME", value="1", line_number=1)])
