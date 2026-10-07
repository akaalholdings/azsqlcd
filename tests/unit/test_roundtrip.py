"""The round trip parse -> emit -> parse over the fixture tables, and the NF000 token check (A14).

Fixtures are those of test_parse.py: tests/fixtures/ddl/ok/*.sql. A file with a '-- path:' header
is an object file; every other file is one migration statement. The two *_canonical_form.sql
files are also the recorded text of the emitter: it must write them byte for byte.
"""

import typing
from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from azsqlcd import model as masking
from azsqlcd.emit import (
    NORMAL_FORM_EQUIVALENTS,
    emit_object_file,
    emit_operation,
    token_differences,
    token_roundtrip_differences,
)
from azsqlcd.lex import TRIVIA, LexError, significant, tokenize
from azsqlcd.model import (
    AddColumn,
    AddConstraint,
    AliasType,
    AlterColumn,
    AlterColumnProperty,
    AlterSequence,
    Check,
    Column,
    Computed,
    CreateIndex,
    CreateSchema,
    CreateSequence,
    CreateSynonym,
    CreateTable,
    CreateType,
    DefaultConstraint,
    DropColumn,
    DropConstraint,
    DropIndex,
    DropSchema,
    DropSequence,
    DropSynonym,
    DropTable,
    DropType,
    Expression,
    ForeignKey,
    Identity,
    Index,
    KeyColumn,
    Operation,
    PrimaryKey,
    RebuildTable,
    Rename,
    Schema,
    Sequence,
    SetSystemVersioning,
    Synonym,
    Table,
    TableType,
    TypeRef,
    Unique,
)
from azsqlcd.parse import ParseError, parse_object_file, parse_statement

OK = Path(__file__).resolve().parents[1] / "fixtures" / "ddl" / "ok"


def path_of(text: str) -> str | None:
    first = text.splitlines()[0]
    return first.split(": ", 1)[1].strip() if first.startswith("-- path: ") else None


def read(stem: str) -> tuple[str, str]:
    """(text, object file path) of an object file fixture."""
    text = (OK / f"{stem}.sql").read_text(encoding="utf-8")
    path = path_of(text)
    assert path is not None, f"{stem} is not an object file fixture"
    return text, path


TEXTS = {p.stem: p.read_text(encoding="utf-8") for p in sorted(OK.glob("*.sql"))}
OBJECT_FILES = [stem for stem, text in TEXTS.items() if path_of(text)]
STATEMENTS = [stem for stem, text in TEXTS.items() if not path_of(text)]
CANONICAL_FILES = [stem for stem in OBJECT_FILES if stem.endswith("_canonical_form")]


def spelled(value: Any) -> list[str]:
    """Every name, keyword value and expression token of a result, exactly as the model holds it.

    Model equality ignores case. This list does not, so it shows a name or a token that came
    back in another spelling. A built-in type name is the exception: its one spelling is lower case.
    """
    if isinstance(value, TypeRef) and value.schema is None:
        value = replace(value, name=value.name.lower())
    if is_dataclass(value) and not isinstance(value, type):
        return [text for f in fields(value) for text in spelled(getattr(value, f.name))]
    if isinstance(value, list | tuple):
        return [text for item in value for text in spelled(item)]
    return [value] if isinstance(value, str) else []


def sql(text: str) -> Expression:
    return Expression.from_sql(text)


# ------------------------------------------------------------------ every fixture, through the emitter
def test_the_fixture_tables_hold_object_files_statements_and_recorded_canonical_text():
    assert len(OBJECT_FILES) >= 35 and len(STATEMENTS) >= 35
    assert CANONICAL_FILES == [
        "table_canonical_form",
        "table_sparse_canonical_form",
        "table_wide_canonical_form",
        "type_table_canonical_form",
    ]


@pytest.mark.parametrize("stem", OBJECT_FILES)
def test_an_object_file_is_the_same_object_after_emit_and_parse(stem: str):
    text, path = read(stem)
    (obj,) = parse_object_file(text, path)
    (again,) = parse_object_file(emit_object_file(obj), path)
    assert again == obj
    assert sorted(spelled(again)) == sorted(spelled(obj))


@pytest.mark.parametrize("stem", STATEMENTS)
def test_a_statement_is_the_same_operation_after_emit_and_parse(stem: str):
    operation = parse_statement(TEXTS[stem])
    again = parse_statement(emit_operation(operation))
    assert again == operation
    assert sorted(spelled(again)) == sorted(spelled(operation))


@pytest.mark.parametrize("stem", OBJECT_FILES)
def test_the_canonical_text_of_an_object_is_always_the_same_text(stem: str):
    text, path = read(stem)
    (obj,) = parse_object_file(text, path)
    canonical = emit_object_file(obj)
    assert emit_object_file(obj) == canonical
    # a second trip changes nothing: the text is the canonical text of what it says
    assert emit_object_file(parse_object_file(canonical, path)[0]) == canonical


@pytest.mark.parametrize("stem", STATEMENTS)
def test_the_statement_of_an_operation_is_always_the_same_text(stem: str):
    operation = parse_statement(TEXTS[stem])
    statement = emit_operation(operation)
    assert emit_operation(operation) == statement
    assert emit_operation(parse_statement(statement)) == statement


@pytest.mark.parametrize("stem", OBJECT_FILES)
def test_the_canonical_text_is_in_the_normal_form_and_passes_nf000(stem: str):
    text, path = read(stem)
    canonical = emit_object_file(parse_object_file(text, path)[0])
    assert token_roundtrip_differences(canonical, path) == []


@pytest.mark.parametrize("stem", CANONICAL_FILES)
def test_the_emitter_writes_the_recorded_canonical_text_byte_for_byte(stem: str):
    text, path = read(stem)
    header, recorded = text.split("\n", 1)
    assert header == f"-- path: {path}"
    assert emit_object_file(parse_object_file(text, path)[0]) == recorded


# ------------------------------------------------------------------ every operation kind
INT = TypeRef("int")
EVERYTHING = Table(
    "sales",
    "Shipment",
    (
        Column("ShipmentId", TypeRef("bigint"), False, identity=Identity(-5, 5)),
        Column("OrderId", INT, False),
        Column(
            "Carrier",
            TypeRef("nvarchar", length=40),
            True,
            default=DefaultConstraint("DF_Shipment_Carrier", sql("N'post' + N''")),
            collation="Latin1_General_100_CI_AS",
        ),
        Column("Phone", TypeRef("PhoneNumber", "dbo"), True),
        Column("Twice", None, False, computed=Computed(sql("([OrderId] * 2)"), persisted=True)),
    ),
    (
        PrimaryKey("PK_Shipment", True, (KeyColumn("ShipmentId", True),), (("PAD_INDEX", "ON"),)),
        Unique("UQ_Shipment", False, (KeyColumn("OrderId"), KeyColumn("Carrier", True))),
        Check("CK_Shipment", sql("([OrderId] > 0 AND [Carrier] <> N'')")),
        ForeignKey(
            "FK_Shipment_Order", ("OrderId",), "sales", "Order", ("OrderId",), "SET NULL", "SET DEFAULT"
        ),
    ),
    (
        Index(
            "IX_Shipment",
            True,
            False,
            (KeyColumn("Carrier"), KeyColumn("OrderId", True)),
            ("Phone", "Twice"),
            sql("[Carrier] IS NOT NULL"),
            (("DATA_COMPRESSION", "NONE"), ("FILLFACTOR", "70")),
        ),
    ),
)
ID_LIST = TableType(
    "dbo",
    "IdList",
    (Column("Id", INT, False, default=DefaultConstraint(None, sql("(0)"))),),
    (PrimaryKey(None, True, (KeyColumn("Id"),)), Check(None, sql("([Id] >= 0)"))),
    (Index("IX_Id", False, False, (KeyColumn("Id", True),)),),
)
LOW_PRIORITY = ("WAIT_AT_LOW_PRIORITY", "MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = BLOCKERS")
# every feature of the wider table coverage in one heap and one clustered columnstore table
WIDE = Table(
    "sales",
    "Wide",
    (
        Column("Id", INT, False, identity=Identity(5, -2, True)),
        Column("Row Guid", TypeRef("uniqueidentifier"), False, rowguidcol=True),
        Column("Notes", TypeRef("nvarchar", length=40), True, collation="Latin1_General_BIN2", sparse=True),
        Column("Card", TypeRef("char", length=16), True, masked="default()", sparse=True),
    ),
    (
        PrimaryKey("PK_Wide", False, (KeyColumn("Id"),), (("XML_COMPRESSION", "ON"),)),
        Unique("UQ_Wide", False, (KeyColumn("Row Guid"),), (("XML_COMPRESSION", "OFF"),)),
        Check("CK_Wide", sql("([Id] > 0)"), True),
        ForeignKey("FK_Wide", ("Id",), "sales", "Other", ("Id",), "CASCADE", "SET NULL", True),
    ),
    (
        Index(
            "NCCI_Wide",
            False,
            False,
            (KeyColumn("Id"),),
            ("Id", "Row Guid"),
            sql("[Id] > 100"),
            (("COMPRESSION_DELAY", "10"), ("DATA_COMPRESSION", "COLUMNSTORE_ARCHIVE")),
            True,
        ),
    ),
    compression="PAGE",
)
COLUMNSTORE = Table(
    "dw",
    "Fact",
    (Column("d", INT, False), Column("s", INT, False)),
    indexes=(Index("CCI", False, True, (KeyColumn("s"), KeyColumn("d")), columnstore=True),),
)
OPERATIONS: list[Operation] = [
    CreateSchema(Schema("reporting")),
    CreateSchema(Schema("reporting", "dbo")),
    DropSchema("reporting"),
    CreateType(AliasType("dbo", "Postcode", TypeRef("varchar", length=8), True)),
    CreateType(ID_LIST),
    DropType("dbo", "IdList"),
    CreateSequence(
        Sequence("sales", "No", TypeRef("decimal", precision=18, scale=0), -1, -1, -99, -1, True, False)
    ),
    CreateSequence(Sequence("sales", "No", TypeRef("bigint"), 1, 1, 1, 99, False, True)),
    CreateSequence(Sequence("sales", "No", TypeRef("bigint"), 1, 1, 1, 99, False, True, 10)),
    AlterSequence("sales", "No", restart=True),
    AlterSequence("sales", "No", restart=True, restart_with=-7),
    AlterSequence("sales", "No", increment=2, minvalue=-9, maxvalue=9, cycle=False, cached=True),
    AlterSequence("sales", "No", cycle=True, cached=True, cache_size=3),
    AlterSequence("sales", "No", cached=False),
    DropSequence("sales", "No"),
    CreateSynonym(Synonym("dbo", "Cust", "sales", "Customer")),
    DropSynonym("dbo", "Cust"),
    CreateTable(EVERYTHING),
    DropTable("sales", "Shipment"),
    *(AddColumn("sales", "Shipment", column) for column in EVERYTHING.columns),
    AddColumn("sales", "Shipment", EVERYTHING.columns[2], with_values=True),
    AlterColumn("sales", "Shipment", "Carrier", TypeRef("nvarchar", length="max"), False),
    AlterColumn("sales", "Shipment", "Carrier", TypeRef("char", length=2), True, "Latin1_General_BIN2"),
    AlterColumn(
        "sales", "Shipment", "Phone", TypeRef("PhoneNumber", "dbo"), True, None, (("ONLINE", "OFF"),)
    ),
    AlterColumn("sales", "Shipment", "OrderId", TypeRef("bigint"), False, None, (("ONLINE", "ON"),)),
    DropColumn("sales", "Shipment", "Carrier"),
    *(AddConstraint("sales", "Shipment", constraint) for constraint in EVERYTHING.constraints),
    AddConstraint(
        "sales", "Shipment", EVERYTHING.constraints[0], exec_options=(("MAXDOP", "1"), ("ONLINE", "ON"))
    ),
    AddConstraint("sales", "Shipment", EVERYTHING.constraints[2], with_check=False),
    AddConstraint("sales", "Shipment", EVERYTHING.constraints[3], with_check=True),
    AddConstraint(
        "sales", "Shipment", DefaultConstraint("DF_Shipment_Order", sql("-1")), for_column="OrderId"
    ),
    DropConstraint("sales", "Shipment", "CK_Shipment"),
    CreateIndex("sales", "Shipment", EVERYTHING.indexes[0]),
    CreateIndex(
        "sales",
        "Shipment",
        EVERYTHING.indexes[0],
        (
            ("MAX_DURATION", "30"),
            ("MAXDOP", "8"),
            ("ONLINE", "ON"),
            ("RESUMABLE", "ON"),
            ("SORT_IN_TEMPDB", "OFF"),
            LOW_PRIORITY,
        ),
    ),
    CreateIndex(
        "sales", "Shipment", Index("CIX", False, True, (KeyColumn("OrderId"),)), (("ONLINE", "OFF"),)
    ),
    DropIndex("sales", "Shipment", "IX_Shipment"),
    Rename("column", ("sales", "Shipment", "Carrier"), "Carrier Name"),
    Rename("index", ("my.schema", "Order Details]v2", "IX 1"), "It's [new]"),
    Rename("object", ("sales", "Shipment"), "Shipments"),
    SetSystemVersioning("sales", "Shipment", False),
    SetSystemVersioning("sales", "Shipment", True, "history", "Shipment Archive"),
    SetSystemVersioning("sales", "Shipment", True, "sales", "Shipment_History", (18, "MONTHS")),
    masking.MaskColumn("sales", "Shipment", "Carrier", "default()"),
    masking.MaskColumn("my.schema", "Order Details]v2", "Card No", 'partial(0, "it\'\'s ""x"", (y)", 4)'),
    masking.MaskColumn("sales", "Shipment", "Weight", "random(1.50, 12.00)"),
    masking.UnmaskColumn("sales", "Shipment", "Carrier"),
    *(
        AlterColumnProperty("sales", "Shipment", "Row Guid", add, word)
        for word in ("ROWGUIDCOL", "SPARSE", "NOT FOR REPLICATION")
        for add in (True, False)
    ),
    RebuildTable("sales", "Shipment", "NONE"),
    RebuildTable("sales", "Shipment", "PAGE", (("MAXDOP", "4"), ("ONLINE", "ON"), LOW_PRIORITY)),
    RebuildTable("sales", "Shipment", "ROW", (("SORT_IN_TEMPDB", "ON"),)),
    CreateTable(WIDE),
    CreateTable(COLUMNSTORE),
    *(AddColumn("sales", "Wide", column) for column in WIDE.columns),
    *(AddConstraint("sales", "Wide", constraint) for constraint in WIDE.constraints),
    CreateIndex("sales", "Wide", WIDE.indexes[0], (("MAXDOP", "2"), ("ONLINE", "ON"))),
    CreateIndex("dw", "Fact", COLUMNSTORE.indexes[0], (("ONLINE", "OFF"),)),
]


def test_the_operations_and_the_statement_fixtures_each_hold_every_operation_kind():
    kinds = set(typing.get_args(Operation))
    assert len(kinds) == 24
    assert {type(operation) for operation in OPERATIONS} == kinds
    assert {type(parse_statement(TEXTS[stem])) for stem in STATEMENTS} == kinds


@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda op: type(op).__name__)
def test_an_operation_of_every_kind_is_the_same_operation_after_emit_and_parse(operation: Operation):
    again = parse_statement(emit_operation(operation))
    assert again == operation
    assert sorted(spelled(again)) == sorted(spelled(operation))


@pytest.mark.parametrize("kind", ["table", "constraint"])
def test_a_rename_of_a_table_or_a_constraint_reads_back_as_a_rename_of_an_object(kind):
    # sp_rename has one word for both, N'OBJECT'; the statement text cannot say which it is
    again = parse_statement(emit_operation(Rename(kind, ("sales", "Old"), "New")))
    assert again == Rename("object", ("sales", "Old"), "New")


# ------------------------------------------------------------------ NF000: which fixtures are canonical
# Object file fixtures that the parser accepts and NF000 does not, with the reason.
NOT_CANONICAL = {
    "table_checks": "CHECK constraints written on a column",
    "table_column_properties_any_order": "column options in another order, a key on a column, NONE",
    "table_column_level_keys": "PRIMARY KEY and UNIQUE written on a column",
    "table_composite_keys_desc": "ASC, ON [PRIMARY], TEXTIMAGE_ON [PRIMARY]",
    "table_defaults_unparenthesised": "DEFAULT written before NULL or NOT NULL",
    "table_foreign_keys": "keys written on a column, ON DELETE NO ACTION",
    "table_heap_with_clustered_index": "ON [PRIMARY]",
    "table_inline_columnstore_index": "a columnstore index written inside CREATE TABLE, MINUTES",
    "table_inline_indexes": "indexes written inside CREATE TABLE, a key written on a column",
    "table_lower_case_bare_names": "IDENTITY without (seed, increment), a key written on a column",
    "table_masked_default_and_email": "the masking function as an N literal",
    "table_strings_with_go_and_comment_markers": "a CHECK constraint written on a column",
    "table_temporal_period_column_defaults": "ON [PRIMARY], HISTORY_RETENTION_PERIOD = INFINITE",
    "table_with_index_batches": "ASC",
    "type_table": "a CHECK constraint written on a column",
}


@pytest.mark.parametrize("stem", OBJECT_FILES)
def test_nf000_passes_exactly_the_fixtures_that_say_what_the_model_holds(stem: str):
    differences = token_roundtrip_differences(*read(stem))
    if stem in NOT_CANONICAL:
        assert differences, f"{stem} holds: {NOT_CANONICAL[stem]}"
        assert all(d.startswith("line ") for d in differences)
    else:
        assert differences == []


def test_the_list_of_fixtures_that_are_not_canonical_names_fixtures():
    assert set(NOT_CANONICAL) <= set(OBJECT_FILES)


# Statement fixtures whose text is not the text that the emitter writes, with the reason. A model
# batch of a migration need not be canonical. The proof replays the operation and the engine runs
# the text, so every token that the parser reads and the operation does not hold is listed here.
STATEMENTS_NOT_CANONICAL = {
    "stmt_create_columnstore_clustered": "ON [PRIMARY]",
    "stmt_create_columnstore_nonclustered": "MINUTES after COMPRESSION_DELAY",
    "stmt_create_index_online_resumable": "ASC, ON [PRIMARY]",
    "stmt_create_table": "a key and a foreign key written on a column",
    "stmt_create_type_table": "a key written on a column",
    "stmt_rename_object": "EXECUTE for EXEC, names without brackets inside the strings",
    "stmt_set_system_versioning_on_consistency_check": "DATA_CONSISTENCY_CHECK = OFF, 1 WEEK for 1 WEEKS",
}


@pytest.mark.parametrize("stem", STATEMENTS)
def test_the_tokens_of_a_statement_fixture_are_those_of_its_operation_or_the_fixture_is_listed(stem: str):
    differences = token_differences(TEXTS[stem], emit_operation(parse_statement(TEXTS[stem])))
    if stem in STATEMENTS_NOT_CANONICAL:
        assert differences, f"{stem} holds: {STATEMENTS_NOT_CANONICAL[stem]}"
    else:
        assert differences == []


def test_the_list_of_statements_that_are_not_canonical_names_fixtures():
    assert set(STATEMENTS_NOT_CANONICAL) <= set(STATEMENTS)


# Words that change what the engine does. Put between two tokens of a statement, each is refused
# or changes the operation. DROPPED lists the only ones that the parser reads and does not keep:
# each says what the engine does without it.
MEANINGFUL = (
    "NOT FOR REPLICATION",
    "SPARSE",
    "PERSISTED",
    "DESC",
    "ROWGUIDCOL",
    "WITH NOCHECK",
    "WITH CHECK",
    "ON DELETE CASCADE",
    "ON UPDATE CASCADE",
    "ON DELETE SET NULL",
    "UNIQUE",
    "CLUSTERED",
    "NONCLUSTERED",
    "NULL",
    "NOT NULL",
    "IDENTITY",
    "MASKED",
    "COLLATE Latin1_General_BIN2",
    "WITH VALUES",
    "IF EXISTS",
    "IF NOT EXISTS",
    "WITH (ONLINE = ON)",
    "DEFAULT (0)",
    "WITH (FILLFACTOR = 50)",
    "WITH (IGNORE_DUP_KEY = ON)",
    "WHERE [a] > 0",
    "INCLUDE ([x])",
    "ON [FG2]",
    "TEXTIMAGE_ON [FG2]",
    "CYCLE",
    "NO CACHE",
    "RESTART",
    ", [x]",
    "NOT",
)
DROPPED = ("ASC", "ON [PRIMARY]", "TEXTIMAGE_ON [PRIMARY]", "ON DELETE NO ACTION", "ON UPDATE NO ACTION")


def insertions(text: str, words: tuple[str, ...]) -> list[str]:
    """The text with each of the words put after each significant token, and before the first."""
    ends = [0]
    position = 0
    for tok in tokenize(text):
        position += len(tok.text)
        if tok.kind not in TRIVIA:
            ends.append(position)
    return [f"{text[:end]} {word} {text[end:]}" for end in ends for word in words]


@pytest.mark.parametrize("stem", STATEMENTS)
def test_a_word_that_changes_what_the_engine_does_is_refused_or_changes_the_operation(stem: str):
    operation = parse_statement(TEXTS[stem])
    canonical = emit_operation(operation)
    for mutated in insertions(canonical, MEANINGFUL):
        try:
            assert parse_statement(mutated) != operation, mutated
        except (ParseError, LexError):
            pass


def test_the_words_that_the_parser_reads_and_does_not_keep_are_the_listed_ones():
    kept = set()
    for stem in STATEMENTS:
        operation = parse_statement(TEXTS[stem])
        for word in DROPPED:
            for mutated in insertions(emit_operation(operation), (word,)):
                try:
                    if parse_statement(mutated) == operation:
                        kept.add(word)
                except (ParseError, LexError):
                    pass
    assert kept == set(DROPPED)  # each is read somewhere; none is on the list for nothing


# ------------------------------------------------------------------ NF000: the one table of equivalents
PATH = "schema/tables/sales.Order.sql"
CANONICAL = """\
CREATE TABLE [sales].[Order] (
    [OrderId] int IDENTITY(1, 1) NOT NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT ((0)),
    [Note] nvarchar(max) NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]) WITH (DATA_COMPRESSION = PAGE, FILLFACTOR = 90),
    CONSTRAINT [CK_Order_Status] CHECK ([Status] IN (0, 1, 2)),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Customer] ([CustomerId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Note] ON [sales].[Order] ([Status] DESC)
    INCLUDE ([Note])
    WHERE [Status] > 0;
GO
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);
"""
PK_LINE = (
    "    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]) "
    "WITH (DATA_COMPRESSION = PAGE, FILLFACTOR = 90),\n"
)
CK_LINE = "    CONSTRAINT [CK_Order_Status] CHECK ([Status] IN (0, 1, 2)),\n"
FK_LINE = (
    "    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([OrderId]) "
    "REFERENCES [sales].[Customer] ([CustomerId])\n"
)
ID_LINE = "    [OrderId] int IDENTITY(1, 1) NOT NULL,\n"
STATUS_LINE = "    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT ((0)),\n"
NOTE_LINE = "    [Note] nvarchar(max) NULL,\n"
LAST_BATCH = "GO\nCREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);\n"


def rewritten(*changes: tuple[str, str]) -> str:
    """CANONICAL with each (old, new) applied once. A change that finds nothing is a broken test."""
    text = CANONICAL
    for old, new in changes:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def tokens_of(text: str) -> list[str]:
    return [tok.text for tok in significant(tokenize(text))]


def test_the_base_text_of_these_tests_is_canonical_byte_for_byte():
    assert emit_object_file(parse_object_file(CANONICAL, PATH)[0]) == CANONICAL
    assert ID_LINE + STATUS_LINE + NOTE_LINE + PK_LINE + CK_LINE + FK_LINE in CANONICAL


# name in NORMAL_FORM_EQUIVALENTS -> other spellings of CANONICAL that this entry allows
ACCEPTED = {
    "SPELLING": [
        rewritten(
            ("CREATE TABLE [sales].[Order] (", 'create table sales."Order" ('),
            ("[OrderId] int IDENTITY(1, 1) NOT NULL", "orderid INT identity(1, 1) not null"),
            ("tinyint", "[tinyint]"),
            ("nvarchar(max)", "[NVARCHAR](MAX)"),
            ("PRIMARY KEY CLUSTERED ([OrderId])", "Primary Key Clustered (ORDERID)"),
            ("[Status] IN (0, 1, 2)", '"status" in (0, 1, 2)'),
            ("WHERE [Status] > 0", "where STATUS > 0"),
        )
    ],
    "TERMINATOR": [CANONICAL.replace(";", "")],
    "OPTION_ORDER": [
        rewritten(("DATA_COMPRESSION = PAGE, FILLFACTOR = 90", "FILLFACTOR = 90, DATA_COMPRESSION = PAGE"))
    ],
    "ELEMENT_ORDER": [
        # another order of the constraints
        rewritten(
            (
                PK_LINE + CK_LINE + FK_LINE,
                FK_LINE.rstrip("\n") + ",\n" + CK_LINE + PK_LINE.rstrip(",\n") + "\n",
            )
        ),
        # a constraint between two columns
        rewritten((NOTE_LINE + PK_LINE, PK_LINE + NOTE_LINE)),
    ],
    "BATCH_ORDER": [
        rewritten(
            (LAST_BATCH, ""),
            (");\nGO\n", ");\n" + LAST_BATCH + "GO\n"),
        )
    ],
}


def test_every_equivalent_of_the_table_has_an_example_and_the_table_holds_nothing_else():
    assert list(NORMAL_FORM_EQUIVALENTS) == [
        "SPELLING",
        "TERMINATOR",
        "OPTION_ORDER",
        "ELEMENT_ORDER",
        "BATCH_ORDER",
    ]
    assert set(ACCEPTED) == set(NORMAL_FORM_EQUIVALENTS)
    assert all(text and callable(step) for text, step in NORMAL_FORM_EQUIVALENTS.values())


@pytest.mark.parametrize(
    ("name", "text"),
    [(name, text) for name, texts in ACCEPTED.items() for text in texts],
    ids=lambda v: v if v in ACCEPTED else "",
)
def test_a_listed_equivalent_passes_nf000(name: str, text: str):
    assert tokens_of(text) != tokens_of(CANONICAL)  # the example is another token sequence
    assert parse_object_file(text, PATH) == parse_object_file(CANONICAL, PATH)
    assert token_roundtrip_differences(text, PATH) == []


def test_white_space_comments_line_ends_a_byte_order_mark_and_a_last_go_are_not_tokens():
    body = "\ufeff-- header\r\n" + CANONICAL.replace("\n", "\r\n").replace(" (", "  /* note */ (")
    assert tokens_of(body) == tokens_of(CANONICAL)
    assert token_roundtrip_differences(body + "GO\r\n", PATH) == []


# What the parser reads as the same model and NF000 still reports, because the parser does not
# keep the token or fills one in: the spelling -> (the file, what a difference must say).
NOT_KEPT = {
    "ASC": (rewritten(("CLUSTERED ([OrderId])", "CLUSTERED ([OrderId] ASC)")), "the file has ASC;"),
    "ON [PRIMARY] on a key": (
        rewritten(("FILLFACTOR = 90),", "FILLFACTOR = 90) ON [PRIMARY],")),
        "the file has ON [PRIMARY];",
    ),
    "ON [PRIMARY] on the table": (
        rewritten(("\n);\nGO", "\n) ON [PRIMARY];\nGO")),
        "the file has ON [PRIMARY];",
    ),
    'ON "default" on the table': (
        rewritten(("\n);\nGO", '\n) ON "default";\nGO')),
        'the file has ON "default";',
    ),
    "TEXTIMAGE_ON": (
        rewritten(("\n);\nGO", "\n) TEXTIMAGE_ON [PRIMARY];\nGO")),
        "the file has TEXTIMAGE_ON [PRIMARY];",
    ),
    "ON [PRIMARY] on an index": (
        rewritten(("([Status]);", "([Status]) ON [PRIMARY];")),
        "the file has ON [PRIMARY];",
    ),
    "ON DELETE NO ACTION": (
        rewritten(("([CustomerId])", "([CustomerId]) ON DELETE NO ACTION")),
        "the file has ON DELETE NO ACTION;",
    ),
    "ON UPDATE NO ACTION": (
        rewritten(("([CustomerId])", "([CustomerId]) ON UPDATE NO ACTION")),
        "the file has ON UPDATE NO ACTION;",
    ),
    "IDENTITY without seed and increment": (
        rewritten(("IDENTITY(1, 1)", "IDENTITY")),
        "the canonical form has ( 1 , 1 );",
    ),
}
# The same model again, with a clause at another place than the canonical one (a constraint on
# its column, an index inside CREATE TABLE, another order inside a column): NF000 reports that too.
MOVED = {
    "PRIMARY KEY on a column": rewritten(
        (PK_LINE, ""), (ID_LINE, ID_LINE.rstrip(",\n") + " " + PK_LINE.strip() + "\n")
    ),
    "a key on a column, without its column list": rewritten(
        (PK_LINE, ""),
        (ID_LINE, ID_LINE.rstrip(",\n") + " " + PK_LINE.strip().replace(" ([OrderId])", "") + "\n"),
    ),
    "a foreign key on a column, without FOREIGN KEY and its column": rewritten(
        (CK_LINE + FK_LINE, CK_LINE.rstrip(",\n") + "\n"),
        (
            ID_LINE,
            ID_LINE.rstrip(",\n") + " " + FK_LINE.strip().replace(" FOREIGN KEY ([OrderId])", "") + ",\n",
        ),
    ),
    "CHECK on a column": rewritten(
        (CK_LINE, ""), (STATUS_LINE, STATUS_LINE.rstrip(",\n") + " " + CK_LINE.strip() + "\n")
    ),
    "INDEX inside CREATE TABLE": rewritten(
        (LAST_BATCH, ""),
        (FK_LINE, FK_LINE.rstrip("\n") + ",\n    INDEX [IX_Order_Status] NONCLUSTERED ([Status])\n"),
    ),
    "DEFAULT before NOT NULL": rewritten(
        (STATUS_LINE, "    [Status] tinyint CONSTRAINT [DF_Order_Status] DEFAULT ((0)) NOT NULL,\n")
    ),
    "IDENTITY after NOT NULL": rewritten((ID_LINE, "    [OrderId] int NOT NULL IDENTITY(1, 1),\n")),
}


@pytest.mark.parametrize(("text", "says"), NOT_KEPT.values(), ids=NOT_KEPT.keys())
def test_a_token_that_the_parser_reads_and_does_not_keep_fails_nf000(text: str, says: str):
    assert parse_object_file(text, PATH) == parse_object_file(CANONICAL, PATH)  # the parser sees no change
    differences = token_roundtrip_differences(text, PATH)
    assert any(says in d for d in differences), differences


@pytest.mark.parametrize("text", MOVED.values(), ids=MOVED.keys())
def test_a_clause_at_another_place_than_the_canonical_one_fails_nf000(text: str):
    assert parse_object_file(text, PATH) == parse_object_file(CANONICAL, PATH)
    assert token_roundtrip_differences(text, PATH) != []


def test_a_name_has_the_letters_of_the_file_also_where_the_model_sees_no_difference():
    # the model compares names case-folded, so NF000 is the check that sees other letters
    (order,) = parse_object_file(CANONICAL, PATH)
    shouted = with_column(order, "Note", name="NOTE")
    assert shouted == order
    assert token_differences(CANONICAL, emit_object_file(shouted)) == [
        "line 4: the canonical form has [NOTE]; the file has other letters"
    ]


def test_two_elements_that_differ_in_letters_only_are_no_difference_in_any_order():
    path = "schema/types/dbo.L.sql"
    text = (
        "CREATE TYPE [dbo].[L] AS TABLE (\n"
        "    [a] int NOT NULL,\n"
        "    check ([a] > 0),\n"
        "    CHECK ([A] > 0),\n"
        "    Check ([a] > 0)\n"
        ");\n"
    )
    assert token_roundtrip_differences(text, path) == []
    lines = text.splitlines(keepends=True)
    for order in ([2, 4, 3], [3, 2, 4], [4, 3, 2]):
        moved = "".join(lines[:2] + [lines[n].rstrip(",\n") + ",\n" for n in order] + lines[5:])
        moved = moved.replace(",\n);", "\n);")
        assert parse_object_file(moved, path) == parse_object_file(text, path)
        assert token_roundtrip_differences(moved, path) == []


def test_the_clauses_of_a_sequence_have_one_order():
    path = "schema/sequences/dbo.S.sql"
    head, tail = "CREATE SEQUENCE [dbo].[S] AS int ", " MINVALUE 1 MAXVALUE 9 NO CYCLE NO CACHE;\n"
    canonical = head + "START WITH 1 INCREMENT BY 2" + tail
    moved = head + "INCREMENT BY 2 START WITH 1" + tail
    assert parse_object_file(moved, path) == parse_object_file(canonical, path)
    assert token_roundtrip_differences(canonical, path) == []
    assert token_roundtrip_differences(moved, path) != []


def test_included_columns_are_written_in_the_order_of_the_file():
    # The model holds them as a set. The emitter keeps their order, so each file is its own
    # canonical text; a parser that changed their order would be a difference.
    one = rewritten(("INCLUDE ([Note])", "INCLUDE ([Note], [OrderId])"))
    other = rewritten(("INCLUDE ([Note])", "INCLUDE ([OrderId], [Note])"))
    assert parse_object_file(one, PATH) == parse_object_file(other, PATH)
    assert token_roundtrip_differences(one, PATH) == [] == token_roundtrip_differences(other, PATH)
    assert token_differences(one, other) != []


# ------------------------------------------------------------------ NF000: a token that the parser loses
def with_column(obj: Any, name: str, /, **changes: Any) -> Any:
    assert any(c.name == name for c in obj.columns), name
    return replace(obj, columns=tuple(replace(c, **changes) if c.name == name else c for c in obj.columns))


def with_constraint(obj: Any, name: str, /, **changes: Any) -> Any:
    assert any(c.name == name for c in obj.constraints), name
    found = tuple(replace(c, **changes) if c.name == name else c for c in obj.constraints)
    return replace(obj, constraints=found)


def with_index(obj: Any, name: str, /, **changes: Any) -> Any:
    assert any(i.name == name for i in obj.indexes), name
    return replace(obj, indexes=tuple(replace(i, **changes) if i.name == name else i for i in obj.indexes))


def without(obj: Any, field: str, name: str) -> Any:
    assert any(item.name == name for item in getattr(obj, field)), name
    return replace(obj, **{field: tuple(item for item in getattr(obj, field) if item.name != name)})


def of_kind(obj: Any, cls: type, **changes: Any) -> Any:
    assert any(isinstance(c, cls) for c in obj.constraints), cls
    found = tuple(replace(c, **changes) if isinstance(c, cls) else c for c in obj.constraints)
    return replace(obj, constraints=found)


T = "table_canonical_form"
TT = "type_table_canonical_form"
UX = "UX_InvoiceLine_Open"
# what a defective parser would lose or fill in -> (fixture, the model it would give, a token run
# that a difference must show). The fixture itself passes NF000, so the change is the only cause.
LOST: dict[str, tuple[str, Callable[[Any], Any], str]] = {
    "ON DELETE CASCADE": (
        T,
        lambda t: with_constraint(t, "FK_InvoiceLine_Invoice", on_delete="NO ACTION"),
        "the file has ON DELETE CASCADE; the canonical form does not",
    ),
    "ON UPDATE CASCADE": (
        T,
        lambda t: with_constraint(t, "FK_InvoiceLine_Tenant", on_update="NO ACTION"),
        "the file has ON UPDATE CASCADE; the canonical form does not",
    ),
    "another action": (
        T,
        lambda t: with_constraint(t, "FK_InvoiceLine_Invoice", on_delete="SET NULL"),
        "the file has CASCADE; the canonical form has SET NULL",
    ),
    "DESC of a key column": (
        T,
        lambda t: with_constraint(
            t, "UQ_InvoiceLine_Sku", columns=(KeyColumn("InvoiceId"), KeyColumn("Sku"))
        ),
        "the file has DESC;",
    ),
    "DESC of an index column": (
        T,
        lambda t: with_index(
            t, "CIX_InvoiceLine", columns=(KeyColumn("InvoiceId"), KeyColumn("InvoiceLineId"))
        ),
        "the file has DESC;",
    ),
    "a key column": (
        T,
        lambda t: with_constraint(t, "UQ_InvoiceLine_Sku", columns=(KeyColumn("InvoiceId"),)),
        "[Sku] DESC",
    ),
    "one INCLUDE column": (T, lambda t: with_index(t, UX, included=("Quantity",)), "[UnitPrice]"),
    "the INCLUDE clause": (
        T,
        lambda t: with_index(t, UX, included=()),
        "INCLUDE ( [Quantity] , [UnitPrice] )",
    ),
    "the filter": (T, lambda t: with_index(t, UX, filter=None), "the file has WHERE [Note] IS NULL;"),
    "a token of the filter": (
        T,
        lambda t: with_index(t, UX, filter=sql("[Note] IS NOT NULL")),
        "the canonical form has NOT;",
    ),
    "UNIQUE of an index": (T, lambda t: with_index(t, UX, unique=False), "the file has UNIQUE;"),
    "an index option": (
        T,
        lambda t: with_index(t, UX, options=(("IGNORE_DUP_KEY", "OFF"),)),
        "FILLFACTOR = 80",
    ),
    "the value of an option": (
        T,
        lambda t: with_index(t, UX, options=(("FILLFACTOR", "80"), ("IGNORE_DUP_KEY", "ON"))),
        "the file has OFF; the canonical form has ON",
    ),
    "the options of a key": (
        T,
        lambda t: with_constraint(t, "PK_InvoiceLine", options=()),
        "WITH ( DATA_COMPRESSION = PAGE , FILLFACTOR = 90 )",
    ),
    "NONCLUSTERED": (
        T,
        lambda t: with_constraint(t, "PK_InvoiceLine", clustered=True),
        "the file has NONCLUSTERED; the canonical form has CLUSTERED",
    ),
    "PERSISTED": (
        T,
        lambda t: with_column(
            t, "LineTotal", computed=Computed(sql("([Quantity] * [UnitPrice])")), nullable=None
        ),
        "the file has PERSISTED NOT NULL;",
    ),
    "NOT NULL of a computed column": (
        T,
        lambda t: with_column(t, "LineTotal", nullable=None),
        "the file has NOT NULL;",
    ),
    "a token of a computed expression": (
        T,
        lambda t: with_column(
            t, "IsLarge", computed=Computed(sql("(CASE WHEN [Quantity] > 100 THEN 1 END)"))
        ),
        "the file has ELSE 0;",
    ),
    "IDENTITY": (
        T,
        lambda t: with_column(t, "InvoiceLineId", identity=None),
        "the file has IDENTITY ( 1000 , 10 );",
    ),
    "the identity seed": (
        T,
        lambda t: with_column(t, "InvoiceLineId", identity=Identity(1, 10)),
        "the file has 1000; the canonical form has 1",
    ),
    "COLLATE": (
        T,
        lambda t: with_column(t, "Sku", collation=None),
        "the file has COLLATE Latin1_General_100_BIN2;",
    ),
    "a DEFAULT": (
        T,
        lambda t: with_column(t, "Quantity", default=None),
        "the file has CONSTRAINT [DF_InvoiceLine_Quantity] DEFAULT ( ( 1 ) );",
    ),
    "the parentheses of a DEFAULT": (
        T,
        lambda t: with_column(
            t, "Quantity", default=DefaultConstraint("DF_InvoiceLine_Quantity", sql("(1)"))
        ),
        "the canonical form does not",
    ),
    "NOT of NOT NULL": (T, lambda t: with_column(t, "InvoiceId", nullable=True), "the file has NOT;"),
    "a length": (
        T,
        lambda t: with_column(t, "Sku", type=TypeRef("varchar", length=30)),
        "the file has 20; the canonical form has 30",
    ),
    "max": (
        T,
        lambda t: with_column(t, "Note", type=TypeRef("nvarchar", length=4000)),
        "the file has max; the canonical form has 4000",
    ),
    "a scale": (
        T,
        lambda t: with_column(t, "UnitPrice", type=TypeRef("decimal", precision=19, scale=2)),
        "the file has 4; the canonical form has 2",
    ),
    "the type name": (
        T,
        lambda t: with_column(t, "UnitPrice", type=TypeRef("numeric", precision=19, scale=4)),
        "the file has decimal; the canonical form has numeric",
    ),
    "a whole constraint": (
        T,
        lambda t: without(t, "constraints", "CK_InvoiceLine_Quantity"),
        "CONSTRAINT [CK_InvoiceLine_Quantity] CHECK",
    ),
    "a whole index batch": (
        T,
        lambda t: without(t, "indexes", "CIX_InvoiceLine"),
        "INDEX [CIX_InvoiceLine] ON [sales] . [InvoiceLine]",
    ),
    "a whole column": (T, lambda t: without(t, "columns", "Note"), "[Note] nvarchar ( max )"),
    "the order of two columns": (
        T,
        lambda t: replace(t, columns=(t.columns[1], t.columns[0], *t.columns[2:])),
        "[InvoiceId] int NOT NULL",
    ),
    "the referenced table": (
        T,
        lambda t: with_constraint(t, "FK_InvoiceLine_Tenant", ref_table="Tenants"),
        "the file has [Tenant]; the canonical form has [Tenants]",
    ),
    "the name of a constraint": (
        T,
        lambda t: with_constraint(t, "PK_InvoiceLine", name="PK_sales_InvoiceLine"),
        "the file has [PK_InvoiceLine]; the canonical form has [PK_sales_InvoiceLine]",
    ),
    "the letters of a column name": (
        T,
        lambda t: with_column(t, "Sku", name="SKU"),
        "line 6: the canonical form has [SKU]; the file has other letters",
    ),
    "the letters of a schema name": (
        T,
        lambda t: replace(t, schema="Sales"),
        "line 2: the canonical form has [Sales]; the file has other letters",
    ),
    "the letters of a name inside an expression": (
        T,
        lambda t: with_constraint(
            t, "CK_InvoiceLine_Quantity", expression=sql("([quantity] > 0 AND [Quantity] <= 10000)")
        ),
        "the canonical form has [quantity]; the file has other letters",
    ),
    "an option of a key of a table type": (
        TT,
        lambda t: of_kind(t, Unique, options=()),
        "the file has WITH ( IGNORE_DUP_KEY = ON",
    ),
    "the DEFAULT of a column of a table type": (
        TT,
        lambda t: with_column(t, "Quantity", default=None),
        "the file has DEFAULT ( ( 1 ) );",
    ),
    "an index of a table type": (TT, lambda t: replace(t, indexes=()), "INDEX [IX_Sku] NONCLUSTERED"),
    "CYCLE": (
        "sequence_all_options",
        lambda s: replace(s, cycle=False),
        "the canonical form has NO;",
    ),
    "the cache size": (
        "sequence_all_options",
        lambda s: replace(s, cache_size=None),
        "the file has 50;",
    ),
    "NO of NO CACHE": (
        "sequence_descending_no_cache",
        lambda s: replace(s, cached=True),
        "the file has NO;",
    ),
    "the sign of a number": (
        "sequence_descending_no_cache",
        lambda s: replace(s, start=100),
        "the file has -;",
    ),
    "AUTHORIZATION": (
        "schema_with_owner",
        lambda s: replace(s, owner=None),
        "the file has AUTHORIZATION [dbo];",
    ),
    "NOT of an alias type": ("type_alias", lambda a: replace(a, nullable=True), "the file has NOT;"),
    "the target of a synonym": (
        "synonym",
        lambda s: replace(s, target_schema="archive"),
        "the file has [sales]; the canonical form has [archive]",
    ),
}


@pytest.mark.parametrize(("stem", "lose", "shown"), LOST.values(), ids=LOST.keys())
def test_a_token_that_the_parser_loses_or_fills_in_is_a_difference(
    stem: str, lose: Callable[[Any], Any], shown: str
):
    text, path = read(stem)
    (obj,) = parse_object_file(text, path)
    assert token_differences(text, emit_object_file(obj)) == []
    differences = token_differences(text, emit_object_file(lose(obj)))
    assert differences
    assert any(shown in d for d in differences), differences


# ------------------------------------------------------------------ NF000: what a difference says
def test_a_difference_names_the_line_of_the_file_and_the_tokens_of_both_sides():
    # the three shapes: only in the file, only in the canonical text, another token in its place
    asc = rewritten(
        ("([Status] DESC)", "([Status] DESC, [OrderId] ASC)"), ("WHERE", "-- a comment\n    WHERE")
    )
    assert token_roundtrip_differences(asc, PATH) == [
        "line 10: the file has ASC; the canonical form does not"
    ]
    bare = rewritten(("IDENTITY(1, 1)", "IDENTITY"))
    assert token_roundtrip_differences(bare, PATH) == [
        "line 2: the canonical form has ( 1 , 1 ); the file does not"
    ]
    (order,) = parse_object_file(CANONICAL, PATH)
    heap = emit_object_file(with_constraint(order, "PK_Order", clustered=False))
    assert token_differences(CANONICAL, heap) == [
        "line 5: the file has CLUSTERED; the canonical form has NONCLUSTERED"
    ]


def test_a_difference_counts_lines_as_the_file_has_them():
    crlf = "\ufeff" + rewritten(("([Status]);", "([Status] ASC);")).replace("\n", "\r\n")
    assert token_roundtrip_differences(crlf, PATH) == [
        "line 14: the file has ASC; the canonical form does not"
    ]


def test_a_difference_never_repeats_the_text_of_a_string_literal():
    path = "schema/tables/dbo.Account.sql"
    text = (
        "CREATE TABLE [dbo].[Account] (\n"
        "    [Name] nvarchar(20) NOT NULL CONSTRAINT [DF_Account_Name] DEFAULT (N'hunter2'),\n"
        "    CONSTRAINT [CK_Account_Name] CHECK ([Name] <> 'It''s')\n"
        ");\n"
    )
    (account,) = parse_object_file(text, path)
    assert token_roundtrip_differences(text, path) == []
    lost = replace(with_column(account, "Name", default=None), constraints=())
    shown = " ".join(token_differences(text, emit_object_file(lost)))
    assert "DEFAULT ( N'...'" in shown and "<> '...'" in shown
    assert "hunter2" not in shown and "It" not in shown


def test_a_long_difference_is_cut_and_says_so():
    (order,) = parse_object_file(CANONICAL, PATH)
    (difference,) = token_differences(CANONICAL, emit_object_file(replace(order, indexes=())))
    assert difference.startswith("line 8: the file has GO CREATE NONCLUSTERED INDEX [IX_Order_Note] ON")
    assert difference.endswith(" ...; the canonical form does not")


def test_nf000_is_not_the_finding_for_a_file_that_does_not_parse():
    with pytest.raises(ParseError):
        token_roundtrip_differences("CREATE TABLE [sales].[Order] ([OrderId] int);", PATH)
    with pytest.raises(ValueError):
        token_roundtrip_differences(CANONICAL, "schema/views/sales.Order.sql")


# ------------------------------------------------------------------ NF000 and temporal tables
TEMPORAL_PATH = "schema/tables/dbo.Team.sql"
TEMPORAL_CANONICAL = TEXTS["table_temporal_plain"].split("\n", 1)[1]
PERIOD_LINE = "    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])\n"
TEAM_KEY_LINE = "    CONSTRAINT [PK_Team] PRIMARY KEY CLUSTERED ([TeamId]),\n"
VERSIONING = "(SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Team_History]))"


def temporal_text(old: str, new: str) -> str:
    assert old in TEMPORAL_CANONICAL
    return TEMPORAL_CANONICAL.replace(old, new)


def test_the_temporal_base_text_of_these_tests_is_canonical_byte_for_byte():
    assert emit_object_file(parse_object_file(TEMPORAL_CANONICAL, TEMPORAL_PATH)[0]) == TEMPORAL_CANONICAL
    assert TEAM_KEY_LINE + PERIOD_LINE in TEMPORAL_CANONICAL


def test_the_period_is_an_element_of_the_table_body_and_its_place_is_free_like_that_of_a_constraint():
    before_the_key = temporal_text(
        TEAM_KEY_LINE + PERIOD_LINE, PERIOD_LINE.rstrip("\n") + ",\n" + TEAM_KEY_LINE.rstrip(",\n") + "\n"
    )
    assert tokens_of(before_the_key) != tokens_of(TEMPORAL_CANONICAL)
    assert token_roundtrip_differences(before_the_key, TEMPORAL_PATH) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # read, and the model holds nothing of it or another word: the file does not say what the model holds
        (VERSIONING, VERSIONING[:-2] + ", HISTORY_RETENTION_PERIOD = INFINITE))"),
        (")\nWITH", ") ON [PRIMARY]\nWITH"),
    ],
)
def test_a_temporal_token_that_the_canonical_text_does_not_hold_fails_nf000(old: str, new: str):
    text = temporal_text(old, new)
    parse_object_file(text, TEMPORAL_PATH)  # the parser reads it
    assert token_roundtrip_differences(text, TEMPORAL_PATH)


def test_a_singular_retention_unit_in_a_file_is_not_the_canonical_plural():
    singular = temporal_text(VERSIONING, VERSIONING[:-2] + ", HISTORY_RETENTION_PERIOD = 1 YEAR))")
    plural = singular.replace("1 YEAR", "1 YEARS")
    assert parse_object_file(singular, TEMPORAL_PATH) == parse_object_file(plural, TEMPORAL_PATH)
    assert token_roundtrip_differences(plural, TEMPORAL_PATH) == []
    (difference,) = token_roundtrip_differences(singular, TEMPORAL_PATH)
    assert "YEAR" in difference and "YEARS" in difference


def test_hidden_is_a_token_of_the_model_and_cannot_be_left_out_or_put_in():
    hidden = temporal_text("ROW START NOT NULL", "ROW START HIDDEN NOT NULL")
    assert parse_object_file(hidden, TEMPORAL_PATH) != parse_object_file(TEMPORAL_CANONICAL, TEMPORAL_PATH)
    assert token_roundtrip_differences(hidden, TEMPORAL_PATH) == []  # it says what its own model holds
    assert token_differences(hidden, TEMPORAL_CANONICAL)


# ------------------------------------------------------------------ dynamic data masking
def test_a_masked_column_of_every_fixture_is_written_back_with_the_same_function():
    stems = [stem for stem in OBJECT_FILES if "masked" in stem]
    assert len(stems) >= 3
    for stem in stems:
        text, path = read(stem)
        (table,) = [obj for obj in parse_object_file(text, path) if isinstance(obj, Table)]
        (again,) = [obj for obj in parse_object_file(emit_object_file(table), path) if isinstance(obj, Table)]
        masks = [(c.name, c.masked) for c in table.columns if c.masked is not None]
        assert masks and masks == [(c.name, c.masked) for c in again.columns if c.masked is not None]


def test_nf000_names_a_mask_that_the_file_states_in_another_way_than_the_canonical_text():
    text, path = read("table_masked_partial_and_random")
    assert token_roundtrip_differences(text, path) == []

    as_n_literal = text.replace("FUNCTION = 'default()'", "FUNCTION = N'default()'")

    assert as_n_literal != text and token_roundtrip_differences(as_n_literal, path)
