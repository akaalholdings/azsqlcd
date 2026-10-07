"""The lexer on hostile text. The lint rules read these tokens and the engine runs the text, so the
lexer must split the text as the engine does, or refuse it."""

import pytest

from azsqlcd.lex import (
    CURRENCY,
    LexError,
    directives,
    module_header,
    significant,
    split_batches,
    tokenize,
)

BOM = "﻿"


def toks(src: str) -> list[tuple[str, str]]:
    return [(t.kind, t.text) for t in significant(tokenize(src))]


def refused(src: str) -> LexError:
    with pytest.raises(LexError) as e:
        tokenize(src)
    return e.value


# ------------------------------------------------------------------ numbers
@pytest.mark.parametrize(
    "number",
    [
        "0",
        "42",
        "1e5",
        "1E5",
        "1.e5",
        "1.5",
        ".5",
        "5.",
        "1e-5",
        "1.5E+10",
        "0x",
        "0x1F",
        "0X1f",
        "0xDEADBEEF",
    ],
)
def test_a_number_is_one_token(number):
    assert toks(f"SELECT {number} FROM [t]") == [
        ("word", "SELECT"),
        ("number", number),
        ("word", "FROM"),
        ("bident", "[t]"),
    ]
    assert toks(f"({number})") == [("op", "("), ("number", number), ("op", ")")]


@pytest.mark.parametrize(
    "src",
    [
        "SELECT 1eDELETE FROM [dbo].[T]",  # the engine reads the float 1e and the keyword DELETE
        "SELECT 1eDROP TABLE [dbo].[T]",
        "SELECT 1e",
        "SELECT 1e+",
        "SELECT 1a",  # the engine reads 1 AS a
        "SELECT 1FROM [t]",
        "SELECT 1.5e",
        "SELECT 1.x",
        "SELECT .5x",
        "SELECT 0xZZ",
        "SELECT 0x1g",
        "SELECT 1_000",
        "SELECT 1#x",
        "SELECT 1é",
    ],
)
def test_number_directly_followed_by_a_word_is_a_lex_error(src):
    # were it two tokens, a keyword after the number would be part of a name for the lint rules
    assert "write a space" in str(refused(src))


@pytest.mark.parametrize("src", ["²", "①", "1²", "¹", "٣", "SELECT ½", "x²"])
def test_a_digit_that_is_not_ascii_is_refused_and_never_an_assertion(src):
    # superscripts and circled digits are digits for str.isdigit and not for the number rule
    refused(src)


def test_a_go_count_in_other_digits_is_not_a_count():
    with pytest.raises(LexError):
        split_batches("SELECT 1\nGO ١٢\nSELECT 2\n")


# ------------------------------------------------------------------ line ends, BOM
@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"])
def test_every_line_end_counts_one_line_and_ends_a_line_comment(nl):
    src = f"SELECT 1 -- DELETE{nl}/* a{nl}b */ 'c{nl}d'{nl}[x]"
    found = significant(tokenize(src))
    assert [(t.text, t.line) for t in found] == [
        ("SELECT", 1),
        ("1", 1),
        (f"'c{nl}d'", 3),
        ("[x]", 5),
    ]
    assert "".join(t.text for t in tokenize(src)) == src


@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"])
def test_go_splits_with_every_line_end(nl):
    src = f"SELECT 1{nl}GO{nl}{nl}SELECT 2{nl}go{nl}SELECT 3"
    assert [(b.text, b.repeat, b.first_line) for b in split_batches(src)] == [
        ("SELECT 1", 1, 1),
        ("SELECT 2", 1, 4),
        ("SELECT 3", 1, 6),
    ]


def test_a_leading_bom_is_skipped_and_a_bom_inside_the_text_is_refused():
    assert toks(BOM + "SELECT 1") == [("word", "SELECT"), ("number", "1")]
    assert [b.text for b in split_batches(BOM + "GO\nSELECT 1")] == ["SELECT 1"]
    assert [d.name for d in directives(BOM + "-- azsqlcd:data\nSELECT 1")] == ["data"]
    refused("SELECT" + BOM + " 1")
    refused("DEL" + BOM + "ETE")


# ------------------------------------------------------------------ comments
@pytest.mark.parametrize(
    ("src", "rest"),
    [
        ("/* a /* b */ DELETE */ x", ["x"]),
        ("/*/**/*/ x", ["x"]),
        ("/*/ DELETE */ x", ["x"]),
        ("/***/ x", ["x"]),
        ("/* -- */ x", ["x"]),
        ("-- /* \nx", ["x"]),
        ("a--b\nc", ["a", "c"]),
        ("a/**/b", ["a", "b"]),
        ("- -1", ["-", "-", "1"]),
        ("'--' x", ["'--'", "x"]),
        ("'/*' x", ["'/*'", "x"]),
        ("[--] x", ["[--]", "x"]),
        ('"/*" x', ['"/*"', "x"]),
    ],
)
def test_comments_nest_and_do_not_start_inside_strings_or_names(src, rest):
    assert [t.text for t in significant(tokenize(src))] == rest


@pytest.mark.parametrize(
    "src", ["/*", "/* /* */", "/* a */ /* b", "'", "N'a", "'a''", "[", "[a]]", '"', '"a""']
)
def test_text_that_does_not_end_is_refused(src):
    assert "unterminated" in str(refused(src))


@pytest.mark.parametrize(
    "char",
    ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x1a", "\x1b", "\x7f", "\u0085", "\u009f", " ", " "],
)
def test_a_control_character_in_a_comment_is_refused(char):
    # were it a line end for the engine, the text after it would run and the lint rules would not see it
    assert refused(f"SELECT 1 -- x{char}DELETE FROM [dbo].[T]").line == 1
    assert refused(f"SELECT 1\n/* x\n{char} */ DELETE FROM [dbo].[T]").line == 3


def test_tab_in_a_comment_and_line_ends_in_a_block_comment_stay():
    assert toks("-- a\tb\n/* a\tb\r\nc\rd\ne */ x") == [("word", "x")]


@pytest.mark.parametrize("src", ["'a\x00b'", "N'a\x00b'", "[a\x00b]", '"a\x00b"', "a \x00 b", "-- \x00"])
def test_nul_is_refused_everywhere(src):
    assert "NUL" in str(refused(src))


def test_nul_is_reported_with_its_line():
    assert refused("SELECT 1;\r\nSELECT 2;\rSELECT '\x00'").line == 3


@pytest.mark.parametrize("char", ["\x01", "\x1a", "\x7f", "\u0085", " ", "​", "　", " ", "`"])
def test_a_character_that_is_no_token_is_refused_outside_strings(char):
    assert "unexpected character" in str(refused(f"SELECT{char}1"))
    # inside a literal or a quoted name the character is data, and both readers end the token at the quote
    assert len(toks(f"SELECT '{char}', [{char}]")) == 4


def test_no_break_space_is_white_space():
    assert toks("SELECT 1") == [("word", "SELECT"), ("number", "1")]


# ------------------------------------------------------------------ strings and names
@pytest.mark.parametrize(
    ("src", "kind", "value"),
    [
        ("'a''b'", "string", "a'b"),
        ("''''", "string", "'"),
        ("''", "string", ""),
        ("N'a''b'", "nstring", "a'b"),
        ("n''", "nstring", ""),
        ("'a\nGO\nb'", "string", "a\nGO\nb"),
        ("'-- azsqlcd:data'", "string", "-- azsqlcd:data"),
        ("[a]]b]", "bident", "a]b"),
        ("[]]]", "bident", "]"),
        ("[a]]]", "bident", "a]"),
        ("[]]]]]", "bident", "]]"),
        ("[a b.c]", "bident", "a b.c"),
        ("[a'b\"c]", "bident", "a'b\"c"),
        ('"a""b"', "qident", 'a"b'),
        ('"a]b"', "qident", "a]b"),
        ("[DELETE]", "bident", "DELETE"),
    ],
)
def test_doubled_quotes_decode_to_one_and_the_token_ends_at_the_single_quote(src, kind, value):
    (tok,) = significant(tokenize(src))
    assert (tok.kind, tok.text, tok.value) == (kind, src, value)
    # the token after it is read as code again
    assert toks(src + " DELETE")[-1] == ("word", "DELETE")


def test_a_bracket_name_ends_at_the_first_single_bracket():
    assert toks("[a]]] ]") == [("bident", "[a]]]"), ("op", "]")]
    assert toks("[a]b]") == [("bident", "[a]"), ("word", "b"), ("op", "]")]
    # an even run of ] is all doubled brackets, so the name has no end
    assert "unterminated" in str(refused("[a]]]]"))


def test_n_is_a_string_prefix_only_directly_before_the_quote():
    assert toks("N 'a'") == [("word", "N"), ("string", "'a'")]
    assert toks("xN'a'") == [("word", "xN"), ("string", "'a'")]
    assert toks("'a' 'b'") == [("string", "'a'"), ("string", "'b'")]


@pytest.mark.parametrize("src", ["[a\nb]", '"a\nb"', "[a\rb]"])
def test_a_quoted_name_does_not_cross_a_line_end(src):
    refused(src)


# ------------------------------------------------------------------ words
@pytest.mark.parametrize(
    "word", ["é", "naïve", "日本語", "Größe", "Straße", "x٠", "ｶﾅ", "_a", "#t", "##t", "a#b", "a$b", "a@b"]
)
def test_a_unicode_word_is_one_word_token(word):
    assert toks(f"SELECT {word} FROM t")[1] == ("word", word)


@pytest.mark.parametrize(
    "word",
    [
        "ＤＥＬＥＴＥ",  # fullwidth: DELETE after width mapping
        "ſELECT",  # long s: 'ſ'.upper() is 'S'
        "ıF",  # dotless i: upper is 'I'
        "KEY",  # Kelvin sign: lower is 'k'
        "İF",  # I with dot: lower starts with 'i'
        "ﬁx",  # ligature
        "x²",  # superscript two: not a letter
        "½",  # one half
        "Ⅷ",  # Roman numeral
    ],
)
def test_an_unquoted_word_that_a_mapping_turns_into_ascii_is_refused(word):
    # the lint rules compare keywords with str.upper; the mapping of the engine is not known
    assert "brackets" in str(refused(f"UPDATE {word} SET [a] = 1"))
    assert toks(f"[{word}]") == [("bident", f"[{word}]")]


def test_a_combining_mark_or_a_hidden_character_cannot_split_or_join_a_word():
    for char in ("́", "‍", "­", "​"):
        refused(f"DEL{char}ETE FROM [t]")


def test_variables_and_dollar_names():
    assert toks("@a @@ROWCOUNT $action $(v)") == [
        ("var", "@a"),
        ("var", "@@ROWCOUNT"),
        ("op", "$"),
        ("word", "action"),
        ("op", "$"),
        ("op", "("),
        ("word", "v"),
        ("op", ")"),
    ]


def test_dots_and_dotted_names():
    assert [t for _, t in toks("x.y . z..w")] == ["x", ".", "y", ".", "z", ".", ".", "w"]
    assert [t for _, t in toks('a.[b]."c"')] == ["a", ".", "[b]", ".", '"c"']


# ------------------------------------------------------------------ line continuation
@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"])
def test_a_backslash_before_a_line_end_outside_a_string_is_refused(nl):
    # T-SQL removes the pair and joins the lines; DEL\<line end>ETE could be DELETE for the engine
    assert "line continuation" in str(refused(f"DEL\\{nl}ETE FROM [dbo].[T]"))
    assert "line continuation" in str(refused(f"SELECT 0x12\\{nl}34"))
    assert toks(f"'a\\{nl}b' -- c\\{nl}x") == [("string", f"'a\\{nl}b'"), ("word", "x")]
    assert toks("a \\ b") == [("word", "a"), ("op", "\\"), ("word", "b")]


# ------------------------------------------------------------------ GO
GO_MATRIX = [
    ("GO on its own line", "SELECT 1\nGO\nSELECT 2\nGO\n", [1, 1]),
    ("lower case", "SELECT 1\ngo\nSELECT 2\n", [1, 1]),
    ("mixed case, indented, trailing comment", "SELECT 1\n   Go  -- done\nSELECT 2\n", [1, 1]),
    ("count", "SELECT 1\nGO 5\nSELECT 2\n", [5, 1]),
    ("count and comment", "SELECT 1\r\nGO 2 -- c\r\nSELECT 2", [2, 1]),
    ("count zero", "SELECT 1\nGO 0\nSELECT 2", [0, 1]),
    ("CRLF", "SELECT 1\r\nGO\r\nSELECT 2\r\n", [1, 1]),
    ("CR only", "SELECT 1\rGO\rSELECT 2\r", [1, 1]),
    ("BOM", BOM + "SELECT 1\nGO\nSELECT 2\n", [1, 1]),
    ("tab before and after", "SELECT 1\n\tGO\t\nSELECT 2", [1, 1]),
    ("at the end, no line end", "SELECT 1\nGO", [1]),
    ("first line", "GO\nSELECT 1", [1]),
    ("two in a row", "SELECT 1\nGO\nGO\nSELECT 2", [1, 1]),
    ("only GO", "GO\nGO\n", []),
    ("in a string", "SELECT 'a\nGO\nb'\nGO\nSELECT 2\n", [1, 1]),
    ("in a block comment", "/* x\nGO\n*/ SELECT 1\nGO\nSELECT 2\n", [1, 1]),
    ("in a nested comment", "/* x /* y\nGO\n*/\nGO\n*/ SELECT 1\nGO\nSELECT 2\n", [1, 1]),
    ("[GO] on its own line", "SELECT\n[GO]\nFROM t\nGO\nSELECT 2\n", [1, 1]),
    ('"GO" on its own line', 'SELECT\n"GO"\nFROM t\nGO\nSELECT 2\n', [1, 1]),
    ("an alias, not first on its line", "SELECT 1 AS GO\nGO\n", [1]),
    ("GOTO and GOLD", "GOTO done\nGOLD:\nGO\nSELECT 2", [1, 1]),
    (
        "semicolons in a body",
        "CREATE PROC dbo.p AS BEGIN SELECT 1; SELECT 2; END\nGO\nSELECT 1;\nGO\n",
        [1, 1],
    ),
    # not a separator: the line holds more than GO, a count and a line comment
    ("GO;", "SELECT 1\nGO;\nSELECT 2", [1]),
    ("GO and a block comment", "SELECT 1\nGO /* x */\nSELECT 2", [1]),
    ("a block comment and GO", "SELECT 1\n/* c */ GO\nSELECT 2", [1]),
    ("GO after the end of a comment", "SELECT 1 /*\n*/ GO\nSELECT 2", [1]),
    ("two counts", "SELECT 1\nGO 1 2\nSELECT 2", [1]),
    ("a count that is no whole number", "SELECT 1\nGO 1.5\nSELECT 2", [1]),
    ("a hex count", "SELECT 1\nGO 0x10\nSELECT 2", [1]),
    ("GO and a statement", "SELECT 1\nGO SELECT 2", [1]),
]


@pytest.mark.parametrize(("script", "repeats"), [c[1:] for c in GO_MATRIX], ids=[c[0] for c in GO_MATRIX])
def test_go_matrix(script, repeats):
    found = split_batches(script)
    assert [b.repeat for b in found] == repeats
    # no text is lost and none is made: every statement token of the script is in one batch, in
    # order, apart from the GO lines
    kept = [t.text for b in found for t in significant(tokenize(b.text))]
    every = [t.text for t in significant(tokenize(script))]
    assert len(every) - len(kept) in range(0, 2 * len(repeats) + 3)
    assert [t for t in kept if t.upper() != "GO" and not t.isdigit()] == [
        t for t in every if t.upper() != "GO" and not t.isdigit()
    ]


def test_the_deploy_script_of_the_bakeoff_splits_into_its_six_batches():
    lines = [
        "-- deploy script",
        "CREATE TABLE dbo.A (x int);",
        "GO",
        "CREATE TABLE dbo.B (x nvarchar(20) DEFAULT N'a",
        "GO",
        "b');",
        "go",
        "/* comment",
        "GO",
        "   /* nested",
        "GO",
        "   */",
        "GO",
        "*/",
        "CREATE TABLE dbo.C (",
        "[GO]",
        "int NULL,",
        '"GO"',
        "int NULL, GOLD int NULL);",
        "  Go  -- trailing comment",
        "INSERT INTO dbo.A (x) VALUES (1);",
        "GO 5",
        "-- GO",
        "SELECT 1 AS GO; GOTO done; done:",
        "PRINT 'x';",
        "\tGO",
        "PRINT N'end'",
        "GO",
    ]
    found = split_batches(BOM + "\r\n".join(lines))
    firsts = [" ".join(t.text for t in significant(tokenize(b.text))[:3]) for b in found]
    assert [(first, b.repeat, b.first_line) for first, b in zip(firsts, found, strict=True)] == [
        ("CREATE TABLE dbo", 1, 1),
        ("CREATE TABLE dbo", 1, 4),
        ("CREATE TABLE dbo", 1, 8),
        ("INSERT INTO dbo", 5, 21),
        ("SELECT 1 AS", 1, 23),
        ("PRINT N'end'", 1, 27),
    ]


def test_a_batch_keeps_its_text_between_the_go_lines_byte_for_byte():
    src = "  SELECT 1 -- a\r\n\r\nGO\r\n\r\n\tSELECT 2\r\n-- tail\r\n"
    assert [(b.text, b.first_line) for b in split_batches(src)] == [
        ("  SELECT 1 -- a", 1),
        ("\tSELECT 2\r\n-- tail", 5),
    ]


# ------------------------------------------------------------------ module header
@pytest.mark.parametrize(
    ("src", "kind", "schema", "name"),
    [
        (
            "ALTER FUNCTION [my.schema].[fn]]x] (@a int) RETURNS int AS BEGIN RETURN @a END",
            "FUNCTION",
            "my.schema",
            "fn]x",
        ),
        ('CREATE VIEW "sales"."v w" AS SELECT 1 AS x', "VIEW", "sales", "v w"),
        (
            "create or alter trigger trg_x on dbo.t after delete as begin set nocount on end",
            "TRIGGER",
            None,
            "trg_x",
        ),
        ("CREATE /* c */ OR -- x\n ALTER PROCEDURE dbo . p @a int AS SELECT @a", "PROCEDURE", "dbo", "p"),
        ("CREATE PROCEDURE [procedure].[function] AS SELECT 1", "PROCEDURE", "procedure", "function"),
        (
            "-- CREATE FUNCTION dbo.decoy\n/* CREATE VIEW dbo.decoy2 AS */\n"
            "CREATE OR ALTER VIEW dbo.real_one AS SELECT N'CREATE PROCEDURE dbo.decoy3' AS s",
            "VIEW",
            "dbo",
            "real_one",
        ),
        (BOM + "CREATE VIEW dbo.v AS SELECT 1", "VIEW", "dbo", "v"),
        ("CREATE PROC [dbo].[p]@a int AS SELECT 1", "PROCEDURE", "dbo", "p"),
    ],
)
def test_module_header_on_hostile_headers(src, kind, schema, name):
    header = module_header(src)
    assert (header.kind, header.schema, header.name) == (kind, schema, name)


@pytest.mark.parametrize(
    "src",
    [
        "CREATE",
        "CREATE OR",
        "CREATE OR ALTER",
        "CREATE OR REPLACE VIEW v AS SELECT 1",
        "ALTER OR ALTER VIEW v AS SELECT 1",
        "CREATE PROCEDURE",
        "CREATE VIEW .v AS SELECT 1",
        "CREATE VIEW dbo.v. AS SELECT 1",
        "CREATE VIEW @v AS SELECT 1",
        "CREATE VIEW a.b.c AS SELECT 1",
        "CREATE VIEW [].[v] AS SELECT 1",
        "CREATE VIEW [dbo].[] AS SELECT 1",
        'CREATE VIEW "" AS SELECT 1',
        "CREATE TABLE t (a int)",
        "SELECT 1",
        "-- CREATE VIEW v AS SELECT 1",
        "",
    ],
)
def test_module_header_refuses_what_is_not_a_header(src):
    with pytest.raises(LexError):
        module_header(src)


# ------------------------------------------------------------------ directives
def test_directives_are_line_comments_in_column_zero_only():
    src = (
        "-- azsqlcd:allow X [a] reason: y\n"
        " -- azsqlcd:data\n"
        "/*\n-- azsqlcd:raw\n*/\n"
        "'\n-- azsqlcd:x\n'\n"
        "--  azsqlcd:no\n"
        "SELECT 1 -- azsqlcd:data\n"
        "-- azsqlcd:last"
    )
    assert [(d.name, d.args, d.line) for d in directives(src)] == [
        ("allow", "X [a] reason: y", 1),
        ("last", "", 11),
    ]


def test_directives_with_cr_line_ends_and_a_bom():
    src = BOM + "-- azsqlcd:migration a\r-- azsqlcd:mode tx\r\n-- azsqlcd:allow A  b"
    assert [(d.name, d.args, d.line) for d in directives(src)] == [
        ("migration", "a", 1),
        ("mode", "tx", 2),
        ("allow", "A  b", 3),
    ]


def test_the_first_line_of_each_batch_counts_every_kind_of_line_break_once():
    """N5-08: the line comes from one index of the line breaks; it is the line that _line_at counts
    (LF, CRLF and a lone CR are one line break each), also for a batch after empty lines."""
    from azsqlcd import lex

    src = (
        "SELECT 0;\r\nGO\r\n\r\n\rSELECT 1;\nGO\n\n-- c\rSELECT 2;\rGO\r\n\n\r\nSELECT 3;\r\n"
        "go 1 -- x\nSELECT 4;"
    )
    batches = split_batches(src)
    assert [b.text.split(";")[0][-1] for b in batches] == ["0", "1", "2", "3", "4"]
    assert [b.first_line for b in batches] == [lex._line_at(src, src.index(b.text)) for b in batches]
    assert [b.first_line for b in batches] == [1, 5, 8, 13, 15]
    bom = split_batches(lex.BOM + "\nSELECT 1;\nGO\nSELECT 2;")
    assert [b.first_line for b in bom] == [2, 4]


# ------------------------------------------------------------------ review 2: currency symbols (LFP-08)
@pytest.mark.parametrize("symbol", ["$", "£", "€", "¥", "₩", "＄"])
def test_a_currency_symbol_is_a_token_of_its_own_before_the_number(symbol):
    """T-SQL reads the symbol as the start of a money literal; it was 'unexpected character'."""
    assert toks(f"SELECT {symbol}5.25;") == [
        ("word", "SELECT"),
        ("op", symbol),
        ("number", "5.25"),
        ("op", ";"),
    ]
    assert symbol in CURRENCY


def test_a_currency_symbol_never_joins_a_word():
    # a keyword after the symbol stays a keyword for the lint rules
    assert toks("£DELETE") == [("op", "£"), ("word", "DELETE")]
    assert toks("€1 DROP") == [("op", "€"), ("number", "1"), ("word", "DROP")]


def test_a_symbol_that_is_no_currency_symbol_of_the_engine_is_still_refused():
    assert "unexpected character" in str(refused("SELECT ¢5"))  # the cent sign
