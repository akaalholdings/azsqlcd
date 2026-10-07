"""The DDL parser: the fixture tables for the whole parser, and object files.

Fixtures are in tests/fixtures/ddl. The leading '-- key: value' comment lines are the header.
  ok/*.sql   Optional '-- path: <object file path>'. With it the text is an object file
             (parse_object_file); without it the text is one migration statement
             (parse_statement). <name>.json holds the expected result, every field as written.
  bad/*.sql  '-- expect: <CODE>', '-- says: <phrase of the message>', '-- line: <line of the
             error>' and the optional '-- path:'. One rejected construct per file.
After an intended change of behaviour, record the results again and read the diff:
    AZSQLCD_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/unit/test_parse.py -q
"""

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from azsqlcd.model import Check, Index, Model, PrimaryKey, Table, TableType, Temporal
from azsqlcd.parse import ParseError, parse_object_file, parse_statement

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ddl"
OK = sorted((FIXTURES / "ok").glob("*.sql"))
BAD = sorted((FIXTURES / "bad").glob("*.sql"))


def header(text: str, key: str) -> str | None:
    for line in text.splitlines():
        if not line.startswith("-- "):
            return None
        if line.startswith(f"-- {key}: "):
            return line.split(": ", 1)[1].strip()
    return None


def parse_fixture(text: str) -> Any:
    path = header(text, "path")
    return parse_object_file(text, path) if path else parse_statement(text)


# Fields of the wider table coverage (NOT FOR REPLICATION, ROWGUIDCOL, SPARSE, heap compression,
# columnstore). A golden holds one only when it is not the default, as the canonical form does: the
# golden of an object that does not use the feature stays what it was.
LATER_FIELDS = frozenset({"not_for_replication", "rowguidcol", "sparse", "compression", "columnstore"})


def as_written(value: Any) -> Any:
    """Every field of a parse result, names and expression tokens as written (no folding).
    A field of LATER_FIELDS is left out while it holds its default."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            f.name: as_written(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if not (f.name in LATER_FIELDS and getattr(value, f.name) == f.default)
        }
        return {"class": type(value).__name__, **fields}
    if isinstance(value, list | tuple):
        return [as_written(v) for v in value]
    return value


def pretty(value: Any, indent: int = 0) -> str:
    """JSON with one line for each value that holds no nested object (a type, a key column, a token list)."""
    pad = " " * indent
    nested = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    if not any(
        isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, dict | list) for x in v))
        for v in nested
    ):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        items = [f"{pad}  {json.dumps(k)}: {pretty(v, indent + 2)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    return "[\n" + ",\n".join(f"{pad}  {pretty(v, indent + 2)}" for v in value) + f"\n{pad}]"


def table(sql: str, path: str = "schema/tables/dbo.T.sql") -> Table:
    (found,) = parse_object_file(sql, path)
    assert isinstance(found, Table)
    return found


def rejected(sql: str, path: str = "schema/tables/dbo.T.sql") -> ParseError:
    with pytest.raises(ParseError) as caught:
        parse_object_file(sql, path)
    return caught.value


# ------------------------------------------------------------------ fixture tables
def test_the_fixture_tables_are_not_empty():
    assert len(OK) >= 45
    assert len(BAD) >= 35


@pytest.mark.parametrize("path", OK, ids=lambda p: p.stem)
def test_accepted_ddl_gives_the_recorded_result(path: Path):
    result = as_written(parse_fixture(path.read_text(encoding="utf-8")))
    golden = path.with_suffix(".json")
    if os.environ.get("AZSQLCD_UPDATE_GOLDEN"):
        golden.write_text(pretty(result) + "\n", encoding="utf-8", newline="\n")
    assert result == json.loads(golden.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", BAD, ids=lambda p: p.stem)
def test_rejected_ddl_fails_with_the_recorded_code_at_the_recorded_line(path: Path):
    text = path.read_text(encoding="utf-8")
    code, says, line = header(text, "expect"), header(text, "says"), header(text, "line")
    assert code and says and line, "a bad fixture needs the expect, says and line headers"
    with pytest.raises(ParseError) as caught:
        parse_fixture(text)
    error = caught.value
    assert (error.code, error.line) == (code, int(line))
    assert says in error.message


def test_every_refused_code_has_a_fixture():
    codes = {header(p.read_text(encoding="utf-8"), "expect") for p in BAD}
    assert codes == {"SYNTAX", "UNSUPPORTED", "NF001", "NF002", "NF003", "NF004", "NF005", "NF006"}


@pytest.mark.parametrize(
    "path", [p for p in OK if header(p.read_text(encoding="utf-8"), "path")], ids=lambda p: p.stem
)
def test_a_parsed_object_survives_a_canonical_json_round_trip(path: Path):
    model = Model(parse_fixture(path.read_text(encoding="utf-8")))
    again = Model.from_canonical_json(model.to_canonical_json())
    assert again == model
    assert again.to_canonical_json() == model.to_canonical_json()
    assert model.diff_paths(again) == []


# ------------------------------------------------------------------ equality of what is written
CANONICAL = """\
CREATE TABLE [sales].[Order] (
    [OrderId] int IDENTITY(1,1) NOT NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT ((0)),
    [Note] nvarchar(max) NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId] ASC),
    CONSTRAINT [CK_Order_Status] CHECK ([Status] IN (0, 1, 2) AND [Note] <> N'It''s')
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status] DESC)
    INCLUDE ([Note]) WHERE [Status] > 0;
"""
SAME_IN_ANOTHER_HAND = """\
-- the same table, written by somebody else
create table SALES."order"
(
    orderid  INT  identity ( 1 , 1 )  not null ,
    STATUS   TinyInt not null constraint df_order_status default ( ( 0 ) ) ,
    note     NVARCHAR ( MAX ) null ,
    constraint ck_order_status check ( status in ( 0,1,2 ) /* open */ and [NOTE] <> N'It''s' ) ,
    constraint pk_order primary key clustered ( orderid )
) ;
go
create nonclustered index ix_order_status on sales.[ORDER] ( status desc ) include ( note ) where status > 0 ;
"""


def test_the_same_table_written_with_different_case_and_whitespace_gives_an_equal_model():
    one = table(CANONICAL, "schema/tables/sales.Order.sql")
    two = table(SAME_IN_ANOTHER_HAND, "schema/tables/SALES.order.sql")
    assert one == two
    assert Model([one]) == Model([two])
    assert Model([one]).to_canonical_json() == Model([two]).to_canonical_json()
    assert Model([one]).diff_paths(Model([two])) == []


def test_names_are_kept_as_written_even_though_they_compare_folded():
    two = table(SAME_IN_ANOTHER_HAND, "schema/tables/SALES.order.sql")
    assert (two.schema, two.name) == ("SALES", "order")
    assert [c.name for c in two.columns] == ["orderid", "STATUS", "note"]
    assert two.key == "TABLE:[SALES].[order]"


def test_a_string_literal_that_differs_in_case_makes_a_different_model():
    other = table(CANONICAL.replace("N'It''s'", "N'IT''S'"), "schema/tables/sales.Order.sql")
    assert other != table(CANONICAL, "schema/tables/sales.Order.sql")


def test_column_level_and_table_level_constraints_give_the_same_model():
    column_level = table(
        "CREATE TABLE [dbo].[T] ("
        " [Id] int NOT NULL CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED,"
        " [P] int NULL CONSTRAINT [FK_T_P] REFERENCES [dbo].[P] ([Id]) CONSTRAINT [CK_T_P] CHECK ([P] > 0),"
        " [U] int NOT NULL CONSTRAINT [UQ_T_U] UNIQUE NONCLUSTERED INDEX [IX_T_U] NONCLUSTERED)"
    )
    table_level = table(
        "CREATE TABLE [dbo].[T] ([Id] int NOT NULL, [P] int NULL, [U] int NOT NULL,"
        " INDEX [IX_T_U] NONCLUSTERED ([U]),"
        " CONSTRAINT [UQ_T_U] UNIQUE NONCLUSTERED ([U]),"
        " CONSTRAINT [CK_T_P] CHECK ([P] > 0),"
        " CONSTRAINT [FK_T_P] FOREIGN KEY ([P]) REFERENCES [dbo].[P] ([Id]) ON DELETE NO ACTION,"
        " CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([Id] ASC))"
    )
    assert column_level == table_level


def test_an_index_in_its_own_batch_equals_the_same_index_written_inline():
    inline = table("CREATE TABLE [dbo].[T] ([Id] int NOT NULL, INDEX [IX_T] NONCLUSTERED ([Id] DESC))")
    batch = table(
        "CREATE TABLE [dbo].[T] ([Id] int NOT NULL)\nGO\n"
        "CREATE NONCLUSTERED INDEX [IX_T] ON [dbo].[T] ([Id] DESC)"
    )
    assert inline == batch
    assert [i.name for i in batch.indexes] == ["IX_T"]


def test_decimal_and_numeric_stay_distinct():
    found = table("CREATE TABLE [dbo].[T] ([d] decimal(18, 4) NOT NULL, [n] numeric(18, 4) NOT NULL)")
    d, n = (c.type for c in found.columns)
    assert d is not None and n is not None
    assert (d.name, n.name) == ("decimal", "numeric")
    assert (d.precision, d.scale) == (n.precision, n.scale) == (18, 4)
    assert d != n
    as_decimal = table("CREATE TABLE [dbo].[T] ([d] decimal(18, 4) NOT NULL, [n] decimal(18, 4) NOT NULL)")
    assert Model([found]).diff_paths(Model([as_decimal])) == [("TABLE:[dbo].[T]", "columns[n].type.name")]


def test_declared_length_precision_and_scale_are_kept_as_declared_including_max():
    found = table(
        "CREATE TABLE [dbo].[T] ([a] nvarchar(50) NULL, [b] varbinary(MAX) NULL, [c] datetime2(3) NULL,"
        " [d] decimal(19, 4) NULL, [e] [dbo].[Phone] NULL)"
    )
    a, b, c, d, e = (col.type for col in found.columns)
    assert a is not None and b is not None and c is not None and d is not None and e is not None
    assert (a.length, b.length, c.scale, d.precision, d.scale) == (50, "max", 3, 19, 4)
    assert (e.schema, e.name, e.length) == ("dbo", "Phone", None)
    assert a.schema is None


# ------------------------------------------------------------------ expressions are kept whole
def test_an_expression_that_holds_a_string_with_paren_and_comma_is_kept_whole():
    found = table(
        "CREATE TABLE [dbo].[T] ([a] int NULL,"
        " [b] AS (CASE WHEN [a] = 1 THEN '),(' ELSE N'x, y)' END) PERSISTED NOT NULL,"
        " [c] varchar(9) NOT NULL CONSTRAINT [DF_T_c] DEFAULT ('a,b)'),"
        " CONSTRAINT [CK_T] CHECK ([c] <> ')' AND [c] <> ','))"
    )
    b, c = found.columns[1], found.columns[2]
    assert b.computed is not None and c.default is not None
    assert b.computed.expression.tokens == (
        *("(", "CASE", "WHEN", "[a]", "=", "1", "THEN", "'),('", "ELSE", "N'x, y)'", "END", ")"),
    )
    assert (b.computed.persisted, b.nullable, b.type) == (True, False, None)
    assert c.default.expression.tokens == ("(", "'a,b)'", ")")
    check = found.constraint("CK_T")
    assert isinstance(check, Check)
    assert check.expression.tokens == ("(", "[c]", "<>", "')'", "AND", "[c]", "<>", "','", ")")
    assert [col.name for col in found.columns] == ["a", "b", "c"]


def test_comments_inside_an_expression_are_dropped_and_do_not_change_equality():
    plain = table("CREATE TABLE [dbo].[T] ([a] int NULL, CONSTRAINT [CK] CHECK ([a] > 0))")
    noisy = table("CREATE TABLE [dbo].[T] ([a] int NULL, CONSTRAINT [CK] CHECK ([a] /* ) , */ > -- (\n 0))")
    assert plain == noisy
    check = noisy.constraint("CK")
    assert isinstance(check, Check) and check.expression.tokens == ("(", "[a]", ">", "0", ")")


@pytest.mark.parametrize(
    ("written", "tokens"),
    [
        ("DEFAULT 0 NOT NULL", ("0",)),
        ("DEFAULT (0) NOT NULL", ("(", "0", ")")),
        ("DEFAULT -1 NOT NULL", ("-", "1")),
        ("DEFAULT NULL NULL", ("NULL",)),
        ("DEFAULT GETDATE() NOT NULL", ("GETDATE", "(", ")")),
        ("DEFAULT NEXT VALUE FOR [dbo].[S] NOT NULL", ("NEXT", "VALUE", "FOR", "[dbo]", ".", "[S]")),
        ("DEFAULT (1) + (2) NOT NULL", ("(", "1", ")", "+", "(", "2", ")")),
        ("DEFAULT N'a' + N'b' NULL", ("N'a'", "+", "N'b'")),
        ("DEFAULT 'x' COLLATE Latin1_General_BIN2 NOT NULL", ("'x'", "COLLATE", "Latin1_General_BIN2")),
        (
            "DEFAULT [dbo].[fn_Default](1, 'a') NULL",
            ("[dbo]", ".", "[fn_Default]", "(", "1", ",", "'a'", ")"),
        ),
    ],
)
def test_an_unparenthesised_default_ends_where_the_next_column_option_starts(
    written: str, tokens: tuple[str, ...]
):
    found = table(f"CREATE TABLE [dbo].[T] ([a] sql_variant CONSTRAINT [DF_T_a] {written})")
    default = found.columns[0].default
    assert default is not None
    assert default.expression.tokens == tokens
    assert found.columns[0].nullable is (not written.endswith("NOT NULL"))


@pytest.mark.parametrize(
    "written",
    [
        "DEFAULT 0 AUTO NOT NULL",  # an unknown word must not become part of the expression
        "DEFAULT NEWID() ROWGUIDCOL NOT NULL",  # nor a refused feature
        "DEFAULT 1 + NOT NULL",  # an operator needs a second operand
        "DEFAULT CASE WHEN 1 = 1 THEN 0 END NOT NULL",  # anything complex needs parentheses
        "DEFAULT NOT NULL",
    ],
)
def test_an_unparenthesised_default_never_swallows_what_follows_it(written: str):
    error = rejected(f"CREATE TABLE [dbo].[T] ([a] int CONSTRAINT [DF_T_a] {written})")
    assert error.code in ("SYNTAX", "UNSUPPORTED")


def test_an_index_filter_runs_to_the_options_and_keeps_its_parentheses():
    found = table(
        "CREATE TABLE [dbo].[T] ([a] int NULL, [b] int NULL)\nGO\n"
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WHERE ([a] IS NOT NULL AND [b] IN (1, 2))"
        " WITH (FILLFACTOR = 80) ON [PRIMARY];"
    )
    index = found.index("ix")
    assert isinstance(index, Index) and index.filter is not None
    assert "".join(index.filter.tokens) == "([a]ISNOTNULLAND[b]IN(1,2))"
    assert index.options == (("FILLFACTOR", "80"),)


# ------------------------------------------------------------------ lexical traps
def test_bracket_names_are_decoded_and_keyword_names_need_their_brackets():
    found = table(
        'CREATE TABLE [my.schema].[Order Details]]v2] ([Weird]]Name] int NULL, "double""quoted" int NULL,'
        " [select] int NULL, Period int NOT NULL)",
        "schema/tables/my.schema.Order Details]v2.sql",
    )
    assert (found.schema, found.name) == ("my.schema", "Order Details]v2")
    assert [c.name for c in found.columns] == ["Weird]Name", 'double"quoted', "select", "Period"]
    assert found.key == "TABLE:[my.schema].[Order Details]]v2]"
    assert rejected("CREATE TABLE [dbo].[T] (select int NULL)").code == "SYNTAX"


def test_keywords_and_built_in_types_are_ascii_so_a_look_alike_word_is_a_name():
    # "\u017felect".upper() is "SELECT" and "\u017fmallint".casefold() is "smallint"; neither is the keyword
    # In brackets such a word is a name. Unquoted, the lexer refuses it: the engine could read a keyword.
    found = table("CREATE TABLE [dbo].[T] ([\u017felect] int NULL, [\u0131ndex] int NULL)")
    assert [c.name for c in found.columns] == ["\u017felect", "\u0131ndex"]
    for text in (
        "CREATE TABLE [dbo].[T] (\u017felect int NULL)",
        "CREATE TABLE [dbo].[T] (\u0131ndex int NULL)",
        "CREATE TABLE [dbo].[T] ([a] \u017fmallint NULL)",
    ):
        error = rejected(text)
        assert (error.code, "in an unquoted word" in error.message) == ("SYNTAX", True)
    error = rejected("CREATE TABLE [dbo].[T] ([a] [\u017fmallint] NULL)")
    assert error.code in ("SYNTAX", "UNSUPPORTED") and "\u017fmallint" in error.message


def test_go_inside_a_string_or_a_comment_does_not_split_an_object_file():
    found = table(
        "/* a comment with\nGO\ninside */\n"
        "CREATE TABLE [dbo].[T] ([a] nvarchar(40) NOT NULL"
        " CONSTRAINT [DF_T_a] DEFAULT (N'one\nGO\nthree; -- x'))\n"
        "GO\n"
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WHERE [a] <> N'\nGO\n'\n"
    )
    default = found.columns[0].default
    assert default is not None and default.expression.tokens[1] == "N'one\nGO\nthree; -- x'"
    assert len(found.indexes) == 1


def test_line_endings_and_a_byte_order_mark_do_not_change_the_model():
    plain = table(CANONICAL, "schema/tables/sales.Order.sql")
    windows = table("﻿" + CANONICAL.replace("\n", "\r\n"), "schema/tables/sales.Order.sql")
    assert plain == windows


# ------------------------------------------------------------------ closed lists
@pytest.mark.parametrize(
    ("sql", "found"),
    [
        ("CREATE TABLE [dbo].[T] ([a] int NULL NOCOMPRESS)", "'NOCOMPRESS'"),
        ("CREATE TABLE [dbo].[T] ([a] int NULL) WITH (REMOTE_DATA_ARCHIVE = ON)", "'REMOTE_DATA_ARCHIVE'"),
        ("CREATE TABLE [dbo].[T] ([a] int NULL) LOCK_ESCALATION AUTO", "'LOCK_ESCALATION'"),
        (
            "CREATE TABLE [dbo].[T] ([a] int NULL, CONSTRAINT [U] UNIQUE CLUSTERED ([a]) WITH (BUCKETS = 4))",
            "'BUCKETS'",
        ),
        (
            "CREATE TABLE dbo.T ([a] int NULL, INDEX [IX] CLUSTERED ([a]) WITH (PAD_INDEX = ON, ZIP = ON))",
            "'ZIP'",
        ),
        (
            "CREATE TABLE [dbo].[T] ([a] int NULL, INDEX [IX] CLUSTERED ([a]) WITH (DATA_COMPRESSION = LZ4))",
            "'LZ4'",
        ),
        (
            "CREATE TABLE [dbo].[T] ([a] int NULL, INDEX [IX] NONCLUSTERED ([a]) WITH (PAD_INDEX = MAYBE))",
            "'MAYBE'",
        ),
        ("CREATE TABLE [dbo].[T] ([a] int NULL, INDEX [IX] NONCLUSTERED ([a] NULLS FIRST))", "'NULLS'"),
    ],
)
def test_an_option_the_parser_does_not_know_is_an_error_not_ignored(sql: str, found: str):
    error = rejected(sql)
    assert error.code == "SYNTAX"
    assert f"found {found}" in error.message


def test_an_option_written_twice_is_an_error():
    options = "WITH (FILLFACTOR = 90, PAD_INDEX = ON, FILLFACTOR = 80)"
    sql = f"CREATE TABLE [dbo].[T] ([a] int NULL, INDEX [IX] CLUSTERED ([a]) {options})"
    assert rejected(sql).message == "option FILLFACTOR is written twice"


def test_index_options_come_back_sorted_with_upper_case_values():
    found = table(
        "CREATE TABLE [dbo].[T] ([a] int NULL,"
        " INDEX [IX] NONCLUSTERED ([a]) WITH (pad_index = on, FillFactor = 090, data_compression = page))"
    )
    assert found.indexes[0].options == (
        ("DATA_COMPRESSION", "PAGE"),
        ("FILLFACTOR", "90"),
        ("PAD_INDEX", "ON"),
    )


def test_the_primary_filegroup_is_read_and_is_not_part_of_the_model():
    plain = table("CREATE TABLE [dbo].[T] ([a] int NOT NULL, CONSTRAINT [PK] PRIMARY KEY CLUSTERED ([a]))")
    stored = table(
        "CREATE TABLE [dbo].[T] ([a] int NOT NULL, CONSTRAINT [PK] PRIMARY KEY CLUSTERED ([a]) ON [PRIMARY])"
        ' ON "default" TEXTIMAGE_ON [PRIMARY]'
    )
    assert plain == stored


# ------------------------------------------------------------------ table types
def test_constraints_of_a_table_type_have_no_name_and_need_none():
    (found,) = parse_object_file(
        "CREATE TYPE [dbo].[IdList] AS TABLE ([Id] int NOT NULL DEFAULT (0), PRIMARY KEY CLUSTERED ([Id]))",
        "schema/types/dbo.IdList.sql",
    )
    assert isinstance(found, TableType)
    (key,) = found.constraints
    assert isinstance(key, PrimaryKey) and key.name is None
    assert found.columns[0].default is not None and found.columns[0].default.name is None
    assert found.key == "TYPE:[dbo].[IdList]"


def test_a_table_type_cannot_hold_a_foreign_key():
    error = rejected(
        "CREATE TYPE [dbo].[L] AS TABLE ([Id] int NOT NULL REFERENCES [dbo].[P] ([Id]))",
        "schema/types/dbo.L.sql",
    )
    assert (error.code, "FOREIGN KEY" in error.message) == ("SYNTAX", True)


# ------------------------------------------------------------------ file rules and errors
def test_a_path_outside_the_table_class_directories_is_a_caller_error():
    with pytest.raises(ValueError, match="not a table-class object file"):
        parse_object_file("CREATE OR ALTER VIEW [dbo].[v] AS SELECT 1 AS x", "schema/views/dbo.v.sql")
    with pytest.raises(ValueError):
        parse_object_file("CREATE TABLE [dbo].[T] ([a] int NULL)", "tables/dbo.T.sql")


def test_the_path_must_be_the_one_that_the_object_name_gives():
    error = rejected("CREATE TABLE [dbo].[Customer] ([a] int NULL)", "schema/tables/dbo.customer.sql")
    assert error.code == "SYNTAX"
    assert "TABLE:[dbo].[Customer] belongs in the file schema/tables/dbo.Customer.sql" in error.message


def test_a_name_that_cannot_be_a_file_name_is_rejected():
    error = rejected("CREATE TABLE [dbo].[a/b] ([a] int NULL)", "schema/tables/dbo.a.sql")
    assert error.code == "SYNTAX"


def test_an_error_names_the_token_found_and_what_was_expected():
    error = rejected("CREATE TABLE [dbo].[T] ([a] int NOT NULL,\n    [b] int NULL\n    [c] int NULL)")
    assert error.message == "expected a column option, ',' or ')', found '[c]'"
    assert (error.code, error.line, error.column) == ("SYNTAX", 3, 5)
    assert str(error) == "SYNTAX at 3:5: expected a column option, ',' or ')', found '[c]'"


def test_an_error_in_a_later_batch_has_the_line_of_the_file():
    sql = (
        "-- header\n"
        "CREATE TABLE [dbo].[T] (\n"
        "    [a] int NOT NULL\n"
        ");\n"
        "GO\n"
        "\n"
        "CREATE NONCLUSTERED INDEX [IX_1] ON [dbo].[T] ([a]);\n"
        "GO\n"
        "CREATE NONCLUSTERED INDEX [IX_2] ON [dbo].[T]\n"
        "    ([a]) WITH (FILLFACTOR = 90, SHINY = ON);\n"
    )
    for text in (sql, "﻿" + sql.replace("\n", "\r\n")):
        error = rejected(text)
        assert (error.line, error.column) == (10, 34)
        assert "found 'SHINY'" in error.message


def test_an_unterminated_string_is_a_syntax_error_at_the_line_where_it_starts():
    error = rejected("CREATE TABLE [dbo].[T] (\n [a] varchar(9) NULL CONSTRAINT [D] DEFAULT ('abc)\n);\n")
    assert (error.code, error.line, error.column) == ("SYNTAX", 2, 0)
    assert "unterminated string" in error.message


def test_the_text_of_a_string_literal_is_never_repeated_in_an_error():
    error = rejected("CREATE TABLE [dbo].[T] ([a] int NULL 'hunter2')")
    assert "hunter2" not in str(error)
    assert "found a string literal" in error.message


def test_the_end_of_the_text_is_reported_at_the_last_line():
    error = rejected("CREATE TABLE [dbo].[T] (\n    [a] int NULL\n\n")
    assert (error.line, "found end of text" in error.message) == (2, True)


def test_a_duplicate_name_inside_one_table_is_rejected():
    for sql in (
        "CREATE TABLE [dbo].[T] ([a] int NULL, [A] int NULL)",
        "CREATE TABLE [dbo].[T] ([a] int NULL CONSTRAINT [X] DEFAULT (0), CONSTRAINT [x] CHECK ([a] > 0))",
        "CREATE TABLE dbo.T ([a] int NULL, CONSTRAINT [X] UNIQUE CLUSTERED ([a]), INDEX [x] CLUSTERED ([a]))",
    ):
        error = rejected(sql)
        assert (error.code, "has two" in error.message) == ("SYNTAX", True)


# ------------------------------------------------------------------ system-versioned temporal tables
TEMPORAL = (
    "CREATE TABLE [dbo].[T] ([Id] int NOT NULL, "
    "[A] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL, "
    "[B] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL, "
    "CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([Id]), PERIOD FOR SYSTEM_TIME ([A], [B])) "
    "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[T_History]{}))"
)
ROW_START = "datetime2(7) GENERATED ALWAYS AS ROW START"


def test_a_temporal_table_file_gives_the_period_the_history_table_name_and_no_history_object():
    found = parse_object_file(TEMPORAL.format(""), "schema/tables/dbo.T.sql")
    assert [obj.key for obj in found] == ["TABLE:[dbo].[T]"]  # the history table is not an object
    assert table(TEMPORAL.format("")).temporal == Temporal("A", "B", "dbo", "T_History")
    assert [(c.name, c.generated, c.hidden) for c in table(TEMPORAL.format("")).columns] == [
        ("Id", None, False),
        ("A", "ROW_START", False),
        ("B", "ROW_END", False),
    ]


@pytest.mark.parametrize(
    ("written", "retention"),
    [
        ("INFINITE", None),
        ("1 DAY", (1, "DAYS")),
        ("7 DAYS", (7, "DAYS")),
        ("1 WEEK", (1, "WEEKS")),
        ("2 weeks", (2, "WEEKS")),
        ("1 MONTH", (1, "MONTHS")),
        ("6 MONTHS", (6, "MONTHS")),
        ("1 YEAR", (1, "YEARS")),
        ("10 YEARS", (10, "YEARS")),
    ],
)
def test_a_singular_retention_unit_is_read_as_the_plural_and_infinite_as_no_retention(written, retention):
    found = table(TEMPORAL.format(f", HISTORY_RETENTION_PERIOD = {written}")).temporal
    assert found is not None and found.retention == retention


def test_the_period_may_stand_anywhere_in_the_body_and_a_column_may_be_called_period():
    moved = (
        TEMPORAL.format("")
        .replace(", PERIOD FOR SYSTEM_TIME ([A], [B])", "")
        .replace(
            "([Id] int NOT NULL,", "(PERIOD FOR SYSTEM_TIME ([A], [B]), [Id] int NOT NULL, Period int NULL,"
        )
    )
    found = table(moved)
    assert found.temporal == Temporal("A", "B", "dbo", "T_History")
    assert [c.name for c in found.columns] == ["Id", "Period", "A", "B"]


def changed(old: str, new: str) -> str:
    text = TEMPORAL.format("")
    assert old in text
    return text.replace(old, new)


@pytest.mark.parametrize(
    ("sql", "code", "says"),
    [
        (TEMPORAL.format(", HISTORY_RETENTION_PERIOD = 0 DAYS"), "SYNTAX", "INFINITE or at least 1"),
        (TEMPORAL.format(", HISTORY_RETENTION_PERIOD = 3 FORTNIGHTS"), "SYNTAX", "DAY, DAYS"),
        (TEMPORAL.format(", HISTORY_TABLE = [dbo].[Other]"), "SYNTAX", "HISTORY_TABLE is written twice"),
        (TEMPORAL.format(", LEDGER = ON"), "SYNTAX", "HISTORY_TABLE or HISTORY_RETENTION_PERIOD"),
        (TEMPORAL.format("") + " WITH (SYSTEM_VERSIONING = ON)", "SYNTAX", "the end of the statement"),
        (changed("([A], [B])", "([A], [B], [Id])"), "SYNTAX", "names two columns"),
        (changed("([A], [B])", "([B], [A])"), "SYNTAX", "must be GENERATED ALWAYS AS ROW_START"),
        (changed(ROW_START, "datetime GENERATED ALWAYS AS ROW START"), "SYNTAX", "the data type datetime2"),
        (changed(ROW_START, ROW_START + " GENERATED ALWAYS AS ROW END"), "SYNTAX", "written twice"),
        (changed("ROW START", "ROW MIDDLE"), "SYNTAX", "START or END"),
        (changed("ON (HISTORY_TABLE = [dbo].[T_History])", "OFF"), "SYNTAX", "leave the WITH clause out"),
        (
            changed("[Id] int NOT NULL", "[Id] int HIDDEN NOT NULL"),
            "UNSUPPORTED",
            "HIDDEN on a column that is not",
        ),
        (changed("[dbo].[T_History]", "[db].[dbo].[T_History]"), "UNSUPPORTED", "three-part names"),
        (changed("[dbo].[T_History]", "T_History"), "NF006", "two parts"),
        (changed(" (HISTORY_TABLE = [dbo].[T_History])", ""), "NF006", "history table by name"),
        (
            changed("HISTORY_TABLE = [dbo].[T_History]", "HISTORY_RETENTION_PERIOD = 1 YEAR"),
            "NF006",
            "history table by name",
        ),
    ],
)
def test_a_temporal_table_file_outside_the_closed_grammar_is_refused_with_its_code(sql, code, says):
    error = rejected(sql)
    assert (error.code, says in error.message) == (code, True), error.message


def test_a_table_type_cannot_hold_a_period_or_a_generated_column():
    for body, says in (
        ("([A] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL)", "GENERATED ALWAYS) in a table type"),
        ("([A] int NOT NULL, PERIOD FOR SYSTEM_TIME ([A], [A]))", "PERIOD FOR SYSTEM_TIME) in a table type"),
    ):
        error = rejected(f"CREATE TYPE [dbo].[L] AS TABLE {body}", "schema/types/dbo.L.sql")
        assert error.code == "UNSUPPORTED" and says in error.message
