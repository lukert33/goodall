"""Tests for goodall.toml_writer: goodall's own small TOML writer.

What is written must read back, through Python's own TOML reader, as exactly
the values that went in, and appending a block must leave every byte of the
file before it as it was.
"""

import tomllib

import pytest

from goodall.toml_writer import (
    TomlDocument,
    append_marked_block,
    render_toml,
    replace_top_level_line,
    toml_value,
)


def test_text_is_quoted_and_escaped_so_it_reads_back_exactly() -> None:
    awkward_text = 'A "quoted" word, a back\\slash, a tab\tand a\nnew line, and é.'
    rendered = "value = " + toml_value(awkward_text) + "\n"
    assert tomllib.loads(rendered)["value"] == awkward_text


def test_control_characters_are_written_as_escapes() -> None:
    rendered = "value = " + toml_value("bell\x07") + "\n"
    assert "\x07" not in rendered
    assert tomllib.loads(rendered)["value"] == "bell\x07"


def test_whole_numbers_true_and_false_and_lists_of_text_are_written_bare() -> None:
    assert toml_value(5610) == "5610"
    assert toml_value(True) == "true"
    assert toml_value(False) == "false"
    assert toml_value(["research", "go-to-market"]) == '["research", "go-to-market"]'


def test_a_value_of_a_kind_goodall_does_not_write_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        toml_value(1.5)  # type: ignore[arg-type]


def test_a_document_reads_back_as_the_values_that_went_in() -> None:
    document = TomlDocument(
        top_level={"schema_version": 1, "name": "goodall-pilot", "workstreams": ["engineering", "research"]},
        arrays_of_tables={
            "contributors": [{"id": "ada-lovelace", "display_name": "Ada Lovelace"}],
            "scope": [{"repository": "github.com/your-org/goodall-pilot-app", "paths": ["**"]}],
        },
        comment_lines=["Written by goodall onboard."],
    )

    rendered = render_toml(document)

    assert rendered.startswith("# Written by goodall onboard.\n")
    assert tomllib.loads(rendered) == {
        "schema_version": 1,
        "name": "goodall-pilot",
        "workstreams": ["engineering", "research"],
        "contributors": [{"id": "ada-lovelace", "display_name": "Ada Lovelace"}],
        "scope": [{"repository": "github.com/your-org/goodall-pilot-app", "paths": ["**"]}],
    }


def test_appending_a_block_keeps_every_byte_before_it() -> None:
    existing = '# my note\nschema_version = 1\ncontributor = "ada-lovelace"'

    appended = append_marked_block(existing, "Added by goodall onboard on 2026-11-27.", ["activitywatch_port = 5610"])

    assert appended.startswith(existing)
    assert appended.endswith("# Added by goodall onboard on 2026-11-27.\nactivitywatch_port = 5610\n")
    assert tomllib.loads(appended)["activitywatch_port"] == 5610


def test_replacing_a_top_level_line_keeps_the_others_and_their_comments() -> None:
    existing = '# my note\ncontributor = "ada-lovelace"  # me\nmain_workstream = "research"\n'

    replaced = replace_top_level_line(existing, "main_workstream", 'main_workstream = "engineering"')

    assert replaced == '# my note\ncontributor = "ada-lovelace"  # me\nmain_workstream = "engineering"\n'


def test_a_line_that_cannot_be_found_once_is_not_replaced() -> None:
    assert replace_top_level_line("a = 1\n[table]\nb = 2\n", "b", "b = 3") is None
    assert replace_top_level_line("a = 1\n", "b", "b = 3") is None
