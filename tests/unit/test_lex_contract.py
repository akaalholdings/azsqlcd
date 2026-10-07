"""The lexer API that every other module builds on. These tests pin the contract."""

import pytest

from azsqlcd.lex import LexError, directives, module_header, significant, split_batches, tokenize


def test_token_text_reassembles_the_source_byte_for_byte():
    src = "CREATE TABLE [a].[b] (\r\n  [c] int NOT NULL -- note\r\n);\n/* x /* y */ z */ SELECT N'it''s'"
    assert "".join(t.text for t in tokenize(src)) == src


def test_go_inside_string_comment_and_brackets_is_not_a_separator():
    src = "SELECT 'a\nGO\nb';\n/*\nGO\n*/\nSELECT [GO];\nGO\nSELECT 2;\n"
    batches = split_batches(src)
    assert [b.text.splitlines()[0] for b in batches] == ["SELECT 'a", "SELECT 2;"]


def test_go_count_is_reported_so_the_caller_can_refuse_it():
    assert [b.repeat for b in split_batches("SELECT 1\nGO 5\nSELECT 2\ngo\n")] == [5, 1]


def test_batch_first_line_points_into_the_file():
    batches = split_batches("-- a\nSELECT 1\nGO\n\nSELECT 2\n")
    assert [(b.first_line, b.text) for b in batches] == [(1, "-- a\nSELECT 1"), (5, "SELECT 2")]


def test_module_header_reads_kind_and_name_without_parsing_the_body():
    h = module_header("/* c */ create or alter proc [sales].[usp ]]x] @a int AS SELECT 'CREATE VIEW v'")
    assert (h.verb, h.kind, h.schema, h.name) == ("CREATE OR ALTER", "PROCEDURE", "sales", "usp ]x")


def test_module_header_spans_allow_a_token_level_rewrite():
    src = "ALTER VIEW dbo.v AS SELECT 1 AS x"
    h = module_header(src)
    assert src[h.verb_span[0] : h.verb_span[1]] == "ALTER"
    assert src[h.name_span[0] : h.name_span[1]] == "dbo.v"


def test_three_part_module_name_is_rejected():
    with pytest.raises(LexError):
        module_header("CREATE VIEW db.s.v AS SELECT 1")


def test_unterminated_string_fails_loud_with_the_line():
    with pytest.raises(LexError) as e:
        tokenize("SELECT 1;\nSELECT 'abc")
    assert e.value.line == 2


def test_directive_must_start_in_column_zero_and_outside_strings():
    src = "-- azsqlcd:mode nontx expected-minutes: 40\n  -- azsqlcd:allow X\nSELECT '\n-- azsqlcd:data\n';\n"
    assert [(d.name, d.args, d.line) for d in directives(src)] == [("mode", "nontx expected-minutes: 40", 1)]


def test_significant_drops_only_trivia():
    assert [t.text for t in significant(tokenize("a -- c\n /* d */ b"))] == ["a", "b"]
