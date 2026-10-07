"""The emitter: the canonical text of objects and operations, and the create script.

These tests state the text rules one by one. The round trip through the parser over the fixture
tables and the NF000 token check are in test_roundtrip.py.
"""

from dataclasses import replace

import pytest

from azsqlcd import emit as emit_module
from azsqlcd import model as masking
from azsqlcd import names
from azsqlcd.emit import emit_create_script, emit_object_file, emit_operation
from azsqlcd.lex import split_batches
from azsqlcd.model import (
    AddColumn,
    AddConstraint,
    AliasType,
    AlterColumn,
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
    Model,
    PrimaryKey,
    Rename,
    Schema,
    Sequence,
    SetSystemVersioning,
    Synonym,
    Table,
    TableType,
    Temporal,
    TypeRef,
    Unique,
    UnversionedTable,
)
from azsqlcd.parse import parse_object_file, parse_statement

INT = TypeRef("int")


def sql(text: str) -> Expression:
    return Expression.from_sql(text)


def lines(text: str) -> list[str]:
    return [line.strip().rstrip(",") for line in text.splitlines()]


def table(*columns: Column, constraints: tuple = (), indexes: tuple = ()) -> Table:
    return Table("dbo", "T", columns or (Column("Id", INT, False),), constraints, indexes)


def column_line(column: Column) -> str:
    """The text of one column inside CREATE TABLE."""
    return lines(emit_object_file(table(column)))[1]


def expression_text(expression: Expression) -> str:
    """The text that the emitter writes for an expression, cut out of a DEFAULT statement."""
    head, tail = "ALTER TABLE [s].[t] ADD CONSTRAINT [DF] DEFAULT ", " FOR [c];"
    statement = emit_operation(AddConstraint("s", "t", DefaultConstraint("DF", expression), for_column="c"))
    assert statement.startswith(head) and statement.endswith(tail)
    return statement[len(head) : -len(tail)]


# ------------------------------------------------------------------ object files, exact text
@pytest.mark.parametrize(
    ("obj", "text"),
    [
        (Schema("audit"), "CREATE SCHEMA [audit];\n"),
        (Schema("sales", "dbo"), "CREATE SCHEMA [sales] AUTHORIZATION [dbo];\n"),
        (
            AliasType("dbo", "PhoneNumber", TypeRef("varchar", length=20), False),
            "CREATE TYPE [dbo].[PhoneNumber] FROM varchar(20) NOT NULL;\n",
        ),
        (
            AliasType("dbo", "Amount", TypeRef("decimal", precision=19, scale=4), True),
            "CREATE TYPE [dbo].[Amount] FROM decimal(19, 4) NULL;\n",
        ),
        (
            Sequence("dbo", "AuditSeq", TypeRef("bigint"), 1000, 5, 1, 9223372036854775807, True, True, 50),
            "CREATE SEQUENCE [dbo].[AuditSeq] AS bigint START WITH 1000 INCREMENT BY 5 "
            "MINVALUE 1 MAXVALUE 9223372036854775807 CYCLE CACHE 50;\n",
        ),
        (
            Sequence("dbo", "Counter", INT, 1, 1, 1, 2147483647, False, True),
            "CREATE SEQUENCE [dbo].[Counter] AS int START WITH 1 INCREMENT BY 1 "
            "MINVALUE 1 MAXVALUE 2147483647 NO CYCLE CACHE;\n",
        ),
        (
            Sequence(
                "sales", "No", TypeRef("decimal", precision=18, scale=0), -100, -1, -1000, -1, False, False
            ),
            "CREATE SEQUENCE [sales].[No] AS decimal(18, 0) START WITH -100 INCREMENT BY -1 "
            "MINVALUE -1000 MAXVALUE -1 NO CYCLE NO CACHE;\n",
        ),
        (
            Synonym("dbo", "LegacyOrder", "sales", "Order"),
            "CREATE SYNONYM [dbo].[LegacyOrder] FOR [sales].[Order];\n",
        ),
        (
            TableType(
                "dbo",
                "IdList",
                (Column("Id", INT, False, default=DefaultConstraint(None, sql("(0)"))),),
                (Check(None, sql("([Id] >= 0)")), PrimaryKey(None, True, (KeyColumn("Id"),))),
                (Index("IX_Id", False, False, (KeyColumn("Id", True),)),),
            ),
            "CREATE TYPE [dbo].[IdList] AS TABLE (\n"
            "    [Id] int NOT NULL DEFAULT (0),\n"
            "    PRIMARY KEY CLUSTERED ([Id]),\n"
            "    CHECK ([Id] >= 0),\n"
            "    INDEX [IX_Id] NONCLUSTERED ([Id] DESC)\n"
            ");\n",
        ),
    ],
    ids=lambda v: type(v).__name__ if not isinstance(v, str) else "",
)
def test_an_object_file_that_is_not_a_table_is_one_statement(obj, text):
    assert emit_object_file(obj) == text


def test_a_table_file_is_create_table_then_one_batch_for_each_index():
    order = Table(
        "sales",
        "Order",
        (
            Column("OrderId", INT, False, identity=Identity(1, 1)),
            Column(
                "Status", TypeRef("tinyint"), False, default=DefaultConstraint("DF_Order_Status", sql("(0)"))
            ),
            Column("Note", TypeRef("nvarchar", length="max"), True),
        ),
        (
            PrimaryKey("PK_Order", True, (KeyColumn("OrderId"),)),
            ForeignKey("FK_Order_Customer", ("OrderId",), "sales", "Customer", ("CustomerId",)),
        ),
        (
            Index(
                "IX_Order_Status",
                False,
                False,
                (KeyColumn("Status", True),),
                ("Note",),
                sql("[Status] > 0"),
                (("FILLFACTOR", "90"),),
            ),
        ),
    )
    assert emit_object_file(order) == (
        "CREATE TABLE [sales].[Order] (\n"
        "    [OrderId] int IDENTITY(1, 1) NOT NULL,\n"
        "    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0),\n"
        "    [Note] nvarchar(max) NULL,\n"
        "    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),\n"
        "    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([OrderId]) "
        "REFERENCES [sales].[Customer] ([CustomerId])\n"
        ");\n"
        "GO\n"
        "CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status] DESC)\n"
        "    INCLUDE ([Note])\n"
        "    WHERE [Status] > 0\n"
        "    WITH (FILLFACTOR = 90);\n"
    )


def test_a_table_file_has_no_inline_index_and_a_create_table_statement_has_every_index_inline():
    indexed = table(indexes=(Index("IX_T", False, False, (KeyColumn("Id"),)),))
    assert [b.text.split()[1] for b in split_batches(emit_object_file(indexed))] == ["TABLE", "NONCLUSTERED"]
    assert emit_operation(CreateTable(indexed)) == (
        "CREATE TABLE [dbo].[T] (\n    [Id] int NOT NULL,\n    INDEX [IX_T] NONCLUSTERED ([Id])\n);"
    )


# ------------------------------------------------------------------ names and types
def test_an_identifier_is_bracket_quoted_and_keeps_its_spelling():
    odd = Table(
        "my.schema",
        "Order Details]v2",
        (Column("select", INT, False), Column("MixedCase", INT, True), Column("Ünï cödé", INT, True)),
        (PrimaryKey("PK Order", True, (KeyColumn("select"),)),),
        (Index("IX]1", False, False, (KeyColumn("MixedCase"),), ("Ünï cödé",)),),
    )
    text = emit_object_file(odd)
    assert "CREATE TABLE [my.schema].[Order Details]]v2] (" in text
    assert "    [select] int NOT NULL," in text
    assert "    [MixedCase] int NULL," in text
    assert "CONSTRAINT [PK Order] PRIMARY KEY CLUSTERED ([select])" in text
    assert "INDEX [IX]]1] ON [my.schema].[Order Details]]v2] ([MixedCase])\n    INCLUDE ([Ünï cödé]);" in text
    assert parse_object_file(text, names.path_for("TABLE", odd.schema, odd.name)) == [odd]


@pytest.mark.parametrize(
    ("type_", "text"),
    [
        (TypeRef("INT"), "int"),
        (TypeRef("NVarChar", length="max"), "nvarchar(max)"),
        (TypeRef("varchar", length=8000), "varchar(8000)"),
        (TypeRef("decimal", precision=19, scale=4), "decimal(19, 4)"),
        (TypeRef("numeric", precision=38, scale=0), "numeric(38, 0)"),
        (TypeRef("datetime2", scale=0), "datetime2(0)"),
        (TypeRef("vector", length=1536), "vector(1536)"),
        (TypeRef("sysname"), "sysname"),
        (TypeRef("Country Code", "ref"), "[ref].[Country Code]"),
    ],
)
def test_a_built_in_type_is_bare_and_lower_case_and_an_alias_type_has_its_schema(type_, text):
    assert column_line(Column("c", type_, True)) == f"[c] {text} NULL"


def test_a_column_states_type_collation_identity_nullability_and_then_its_default():
    # the DEFAULT is last: an expression without parentheses must not run into NULL or NOT NULL
    code = Column(
        "Code",
        TypeRef("varchar", length=3),
        False,
        default=DefaultConstraint("DF_T_Code", sql("'x' COLLATE Latin1_General_BIN2")),
        collation="Latin1_General_100_BIN2",
    )
    assert column_line(code) == (
        "[Code] varchar(3) COLLATE Latin1_General_100_BIN2 NOT NULL "
        "CONSTRAINT [DF_T_Code] DEFAULT 'x' COLLATE Latin1_General_BIN2"
    )
    counter = Column("Id", TypeRef("bigint"), False, identity=Identity(-1, -10))
    assert column_line(counter) == "[Id] bigint IDENTITY(-1, -10) NOT NULL"


@pytest.mark.parametrize(
    ("computed", "nullable", "text"),
    [
        (Computed(sql("([a] * 2)")), None, "[c] AS ([a] * 2)"),
        (Computed(sql("([a] * 2)"), persisted=True), None, "[c] AS ([a] * 2) PERSISTED"),
        (Computed(sql("([a] * 2)"), persisted=True), False, "[c] AS ([a] * 2) PERSISTED NOT NULL"),
        (Computed(sql("[a] * 2")), None, "[c] AS [a] * 2"),
    ],
)
def test_a_computed_column_states_its_expression_and_what_is_persisted(computed, nullable, text):
    assert column_line(Column("c", None, nullable, computed=computed)) == text


# ------------------------------------------------------------------ expressions
@pytest.mark.parametrize(
    "tokens",
    [
        ("-", "-", "1"),  # '--' would start a comment
        ("1", "-", "-", "1"),
        ("/", "*", "x", "*", "/"),  # '/*' would start a comment
        ("N", "'x'"),  # N'x' would be one literal
        ("'a'", "'b'"),  # 'a''b' would be one literal
        ("[a]", "]"),
        ('"a"', '"b"'),
        ("1", ".", "x"),  # '1.' would be one number
        ("a", ".", "5"),  # '.5' would be one number
        ("1", "e5"),  # '1e5' would be one number
        ("0x", "F1"),  # '0xF1' would be one number
        ("<", "="),  # '<=' would be one operator
        ("<", ">"),
        ("!", "="),
        ("|", "|"),
        ("a", "$"),  # 'a$' would be one word
        ("[a]", ">", "$", "5"),
        ("$", "-", "5", "+", "\u00a3", "7.5"),
        ("$", "IDENTITY"),  # not a money literal: the space stays
        ("x", "@v"),
        ("(", "-", "1", ")", "-", "(", "-", ".5", ")"),
        ("N'line one\nGO\nline three'", "+", "N''''"),
        ("[dbo]", ".", "[f]", "(", "[a]", ",", "-", "1", ")"),
    ],
    ids=" ".join,
)
def test_an_expression_reads_back_as_the_tokens_that_the_model_holds(tokens):
    assert Expression.from_sql(expression_text(Expression(tokens))).tokens == tokens


@pytest.mark.parametrize(
    ("stored", "written"),
    [
        ("((0))", "((0))"),
        ("((-1))", "((-1))"),
        ("(getdate())", "(getdate())"),
        (
            "(CONVERT([date],dateadd(day,(-1),sysutcdatetime())))",
            "(CONVERT([date], dateadd(day, (-1), sysutcdatetime())))",
        ),
        ("([Status]IN(0,1,2)AND[Note]<>N'It''s')", "([Status] IN (0, 1, 2) AND [Note] <> N'It''s')"),
        ("(NEXT VALUE FOR[dbo].[OrderNo])", "(NEXT VALUE FOR [dbo].[OrderNo])"),
        ("([Net]-1+[dbo].[fn_Tax]([Net],-1))", "([Net] - 1 + [dbo].[fn_Tax]([Net], -1))"),
        ("(CASE WHEN[a]>(0)THEN'),('ELSE N'x'END)", "(CASE WHEN [a] > (0) THEN '),(' ELSE N'x' END)"),
        ("(COALESCE([a],[b])%(16))", "(COALESCE([a], [b]) % (16))"),
        ("([Sku]LIKE'LEGACY\\_%'ESCAPE'\\')", "([Sku] LIKE 'LEGACY\\_%' ESCAPE '\\')"),
        # a money literal has no space inside it: '$ 5' is what lint refuses
        ("([Value]>$5)", "([Value] > $5)"),
        ("([Value]>$ 5.25 AND[Value]<\u00a37)", "([Value] > $5.25 AND [Value] < \u00a37)"),
        ("([Value]>$-5)", "([Value] > $-5)"),
        ("([Value]>-$5)", "([Value] > - $5)"),
        ("($IDENTITY>0)", "($ IDENTITY > 0)"),
    ],
)
def test_an_expression_is_spaced_as_a_person_writes_it(stored, written):
    assert expression_text(sql(stored)) == written


def test_a_word_go_inside_an_expression_does_not_split_the_batch():
    go = table(
        Column("GO", INT, False, default=DefaultConstraint("DF_T_GO", sql("(N'a\nGO\nb')"))),
        Column("Twice", None, None, computed=Computed(Expression(("(", "GO", "*", "2", ")")))),
    )
    text = emit_object_file(go)
    assert len(split_batches(text)) == 1
    assert parse_object_file(text, "schema/tables/dbo.T.sql") == [go]


# ------------------------------------------------------------------ one order
PK = PrimaryKey("PK_T", False, (KeyColumn("Id"),), (("FILLFACTOR", "90"), ("DATA_COMPRESSION", "ROW")))
UQ = Unique("UQ_T", False, (KeyColumn("Id", True),))
CK_A = Check("ck_a", sql("([Id] > 0)"))
CK_B = Check("CK_B", sql("([Id] < 9)"))
FK_A = ForeignKey("FK_A", ("Id",), "dbo", "A", ("Id",), on_delete="CASCADE")
FK_B = ForeignKey("FK_B", ("Id",), "dbo", "B", ("Id",), on_update="SET NULL", on_delete="SET DEFAULT")
CIX = Index("ZZ_Clustered", True, True, (KeyColumn("Id"),))
IX_A = Index("ix_a", False, False, (KeyColumn("Id"),), ("B", "A"))
IX_B = Index("IX_B", False, False, (KeyColumn("Id"),))


def test_constraints_are_written_by_kind_and_then_by_name_whatever_the_case():
    text = emit_object_file(table(constraints=(FK_B, CK_B, UQ, FK_A, CK_A, PK)))
    assert [line.split()[1] for line in lines(text) if line.startswith("CONSTRAINT")] == [
        "[PK_T]",
        "[UQ_T]",
        "[ck_a]",
        "[CK_B]",
        "[FK_A]",
        "[FK_B]",
    ]


def test_the_clustered_index_is_the_first_index_batch_and_the_others_follow_by_name():
    text = emit_object_file(table(indexes=(IX_B, IX_A, CIX)))
    assert [b.text.split(" ON ")[0].split()[-1] for b in split_batches(text)[1:]] == [
        "[ZZ_Clustered]",
        "[ix_a]",
        "[IX_B]",
    ]


def test_the_text_of_a_table_does_not_depend_on_the_order_in_the_model():
    one = table(constraints=(PK, UQ, CK_A, CK_B, FK_A, FK_B), indexes=(CIX, IX_A, IX_B))
    other = table(
        constraints=(FK_B, CK_B, FK_A, replace(PK, options=tuple(reversed(PK.options))), CK_A, UQ),
        indexes=(IX_B, CIX, IX_A),
    )
    assert one == other
    assert emit_object_file(one) == emit_object_file(other)
    assert emit_operation(CreateTable(one)) == emit_operation(CreateTable(other))


def test_the_unnamed_constraints_of_a_table_type_have_one_order():
    checks = (Check(None, sql("([Id] > 0)")), Check(None, sql("([Id] < 9)")))
    one = TableType("dbo", "IdList", (Column("Id", INT, False),), checks)
    other = replace(one, constraints=tuple(reversed(checks)))
    assert emit_object_file(one) == emit_object_file(other)


def test_options_are_written_in_name_order_and_included_columns_as_the_model_holds_them():
    text = emit_object_file(table(constraints=(PK,), indexes=(IX_A,)))
    assert "WITH (DATA_COMPRESSION = ROW, FILLFACTOR = 90)" in text
    assert "INCLUDE ([B], [A])" in text


def test_no_action_is_never_written_and_every_other_action_is():
    quiet = ForeignKey("FK_Q", ("Id",), "dbo", "Q", ("Id",))
    text = emit_object_file(table(constraints=(quiet, FK_A, FK_B)))
    assert [line.split(" REFERENCES ")[1] for line in lines(text) if "REFERENCES" in line] == [
        "[dbo].[A] ([Id]) ON DELETE CASCADE",
        "[dbo].[B] ([Id]) ON DELETE SET DEFAULT ON UPDATE SET NULL",
        "[dbo].[Q] ([Id])",
    ]
    assert "NO ACTION" not in text


# ------------------------------------------------------------------ operations, exact text
STATEMENTS = [
    (CreateSchema(Schema("reporting", "dbo")), "CREATE SCHEMA [reporting] AUTHORIZATION [dbo];"),
    (DropSchema("reporting"), "DROP SCHEMA [reporting];"),
    (
        CreateType(AliasType("dbo", "Postcode", TypeRef("varchar", length=8), True)),
        "CREATE TYPE [dbo].[Postcode] FROM varchar(8) NULL;",
    ),
    (DropType("dbo", "IdList"), "DROP TYPE [dbo].[IdList];"),
    (
        CreateSequence(Sequence("sales", "OrderNo", INT, 1, 1, 1, 2147483647, False, True, 100)),
        "CREATE SEQUENCE [sales].[OrderNo] AS int START WITH 1 INCREMENT BY 1 "
        "MINVALUE 1 MAXVALUE 2147483647 NO CYCLE CACHE 100;",
    ),
    (AlterSequence("sales", "OrderNo", restart=True), "ALTER SEQUENCE [sales].[OrderNo] RESTART;"),
    (
        AlterSequence("sales", "OrderNo", True, 500, 10, 1, 100000, True, True, 20),
        "ALTER SEQUENCE [sales].[OrderNo] RESTART WITH 500 INCREMENT BY 10 MINVALUE 1 MAXVALUE 100000 "
        "CYCLE CACHE 20;",
    ),
    (
        AlterSequence("sales", "OrderNo", increment=-1, cycle=False, cached=False),
        "ALTER SEQUENCE [sales].[OrderNo] INCREMENT BY -1 NO CYCLE NO CACHE;",
    ),
    (AlterSequence("sales", "OrderNo", cached=True), "ALTER SEQUENCE [sales].[OrderNo] CACHE;"),
    (DropSequence("sales", "OrderNo"), "DROP SEQUENCE [sales].[OrderNo];"),
    (
        CreateSynonym(Synonym("dbo", "Cust", "sales", "Customer")),
        "CREATE SYNONYM [dbo].[Cust] FOR [sales].[Customer];",
    ),
    (DropSynonym("dbo", "Cust"), "DROP SYNONYM [dbo].[Cust];"),
    (DropTable("sales", "LegacyOrder"), "DROP TABLE [sales].[LegacyOrder];"),
    (
        # the statement of the design, section (b)
        AddColumn(
            "sales",
            "Order",
            Column(
                "Status", TypeRef("tinyint"), False, default=DefaultConstraint("DF_Order_Status", sql("(0)"))
            ),
        ),
        "ALTER TABLE [sales].[Order] ADD [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0);",
    ),
    (
        AddColumn(
            "dbo",
            "Customer",
            Column("IsActive", TypeRef("bit"), True, default=DefaultConstraint("DF_IsActive", sql("1"))),
            with_values=True,
        ),
        "ALTER TABLE [dbo].[Customer] ADD [IsActive] bit NULL "
        "CONSTRAINT [DF_IsActive] DEFAULT 1 WITH VALUES;",
    ),
    (
        AddColumn(
            "dbo", "Customer", Column("Full", None, False, computed=Computed(sql("([a] + [b])"), True))
        ),
        "ALTER TABLE [dbo].[Customer] ADD [Full] AS ([a] + [b]) PERSISTED NOT NULL;",
    ),
    (
        AlterColumn(
            "dbo", "Customer", "Email", TypeRef("nvarchar", length=320), False, "Latin1_General_100_CI_AS"
        ),
        "ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] nvarchar(320) "
        "COLLATE Latin1_General_100_CI_AS NOT NULL;",
    ),
    (
        AlterColumn("sales", "Order", "Status", TypeRef("smallint"), True, exec_options=(("ONLINE", "ON"),)),
        "ALTER TABLE [sales].[Order] ALTER COLUMN [Status] smallint NULL WITH (ONLINE = ON);",
    ),
    (DropColumn("dbo", "Customer", "LegacyCode"), "ALTER TABLE [dbo].[Customer] DROP COLUMN [LegacyCode];"),
    (
        AddConstraint(
            "dbo",
            "Ticket",
            PrimaryKey("PK_Ticket", True, (KeyColumn("TicketId"),), (("DATA_COMPRESSION", "PAGE"),)),
            exec_options=(("MAXDOP", "2"), ("ONLINE", "ON")),
        ),
        "ALTER TABLE [dbo].[Ticket] ADD CONSTRAINT [PK_Ticket] PRIMARY KEY CLUSTERED ([TicketId]) "
        "WITH (DATA_COMPRESSION = PAGE, MAXDOP = 2, ONLINE = ON);",
    ),
    (
        AddConstraint(
            "dbo", "Customer", Unique("UQ_Email", False, (KeyColumn("Email", True), KeyColumn("Id")))
        ),
        "ALTER TABLE [dbo].[Customer] ADD CONSTRAINT [UQ_Email] UNIQUE NONCLUSTERED ([Email] DESC, [Id]);",
    ),
    (
        AddConstraint(
            "sales",
            "Line",
            ForeignKey("FK_Line_Order", ("T", "O"), "sales", "Order", ("T", "O"), "CASCADE", "SET NULL"),
            with_check=True,
        ),
        "ALTER TABLE [sales].[Line] WITH CHECK ADD CONSTRAINT [FK_Line_Order] FOREIGN KEY ([T], [O]) "
        "REFERENCES [sales].[Order] ([T], [O]) ON DELETE CASCADE ON UPDATE SET NULL;",
    ),
    (
        AddConstraint("sales", "Line", Check("CK_Qty", sql("([Quantity] < 500000)")), with_check=False),
        "ALTER TABLE [sales].[Line] WITH NOCHECK ADD CONSTRAINT [CK_Qty] CHECK ([Quantity] < 500000);",
    ),
    (
        AddConstraint(
            "dbo",
            "Customer",
            DefaultConstraint("DF_Created", sql("(SYSUTCDATETIME())")),
            for_column="CreatedUtc",
        ),
        "ALTER TABLE [dbo].[Customer] ADD CONSTRAINT [DF_Created] DEFAULT (SYSUTCDATETIME()) "
        "FOR [CreatedUtc];",
    ),
    (
        DropConstraint("dbo", "Customer", "DF_Created"),
        "ALTER TABLE [dbo].[Customer] DROP CONSTRAINT [DF_Created];",
    ),
    (
        # the statement of the design, section (e)
        CreateIndex(
            "azsqlcd",
            "step",
            Index(
                "UX_step_migration",
                True,
                False,
                (KeyColumn("migration_id"),),
                filter=sql("[migration_id] IS NOT NULL"),
            ),
        ),
        "CREATE UNIQUE NONCLUSTERED INDEX [UX_step_migration] ON [azsqlcd].[step] ([migration_id]) "
        "WHERE [migration_id] IS NOT NULL;",
    ),
    (
        CreateIndex(
            "sales",
            "Order",
            Index(
                "IX_Open",
                False,
                False,
                (KeyColumn("A"), KeyColumn("B", True)),
                ("C", "D"),
                None,
                (("FILLFACTOR", "90"),),
            ),
            (
                ("MAX_DURATION", "60"),
                ("MAXDOP", "4"),
                ("ONLINE", "ON"),
                ("RESUMABLE", "ON"),
                ("SORT_IN_TEMPDB", "OFF"),
                ("WAIT_AT_LOW_PRIORITY", "MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF"),
            ),
        ),
        "CREATE NONCLUSTERED INDEX [IX_Open] ON [sales].[Order] ([A], [B] DESC) INCLUDE ([C], [D]) "
        "WITH (FILLFACTOR = 90, MAXDOP = 4, MAX_DURATION = 60 MINUTES, "
        "ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)), "
        "RESUMABLE = ON, SORT_IN_TEMPDB = OFF);",
    ),
    (
        CreateIndex("dw", "Reading", Index("CIX", True, True, (KeyColumn("At", True),))),
        "CREATE UNIQUE CLUSTERED INDEX [CIX] ON [dw].[Reading] ([At] DESC);",
    ),
    (DropIndex("sales", "Order", "IX_Open"), "DROP INDEX [IX_Open] ON [sales].[Order];"),
    (
        Rename("column", ("sales", "Order", "Stat"), "Status"),
        "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';",
    ),
    (
        Rename("index", ("sales", "Order", "IX_Stat"), "IX_Status"),
        "EXEC sys.sp_rename N'[sales].[Order].[IX_Stat]', N'IX_Status', N'INDEX';",
    ),
    (
        Rename("table", ("sales", "Order"), "Order Header"),
        "EXEC sys.sp_rename N'[sales].[Order]', N'Order Header', N'OBJECT';",
    ),
    (
        Rename("constraint", ("sales", "DF_Stat"), "DF_Status"),
        "EXEC sys.sp_rename N'[sales].[DF_Stat]', N'DF_Status', N'OBJECT';",
    ),
    (
        Rename("object", ("sales", "Order"), "Orders"),
        "EXEC sys.sp_rename N'[sales].[Order]', N'Orders', N'OBJECT';",
    ),
    (
        # ] is doubled inside the brackets and ' inside the literal; the new name has no brackets
        Rename("column", ("my.schema", "Order Details]v2", "It's"), "Plain's [Name]"),
        "EXEC sys.sp_rename N'[my.schema].[Order Details]]v2].[It''s]', N'Plain''s [Name]', N'COLUMN';",
    ),
]


@pytest.mark.parametrize(
    ("operation", "statement"), STATEMENTS, ids=lambda v: "" if isinstance(v, str) else type(v).__name__
)
def test_an_operation_is_exactly_this_statement(operation, statement):
    assert emit_operation(operation) == statement


@pytest.mark.parametrize("operation", [op for op, _ in STATEMENTS], ids=lambda op: type(op).__name__)
def test_an_operation_is_one_batch_that_ends_with_one_semicolon(operation):
    statement = emit_operation(operation)
    assert statement.endswith(";") and not statement.endswith(";;")
    assert len(split_batches(statement)) == 1
    parse_statement(statement)  # one statement: the parser takes nothing after it


def test_create_table_as_one_statement_carries_its_foreign_keys_and_indexes():
    statement = emit_operation(CreateTable(table(constraints=(PK, FK_A), indexes=(CIX, IX_A))))
    assert lines(statement) == [
        "CREATE TABLE [dbo].[T] (",
        "[Id] int NOT NULL",
        "CONSTRAINT [PK_T] PRIMARY KEY NONCLUSTERED ([Id]) WITH (DATA_COMPRESSION = ROW, FILLFACTOR = 90)",
        "CONSTRAINT [FK_A] FOREIGN KEY ([Id]) REFERENCES [dbo].[A] ([Id]) ON DELETE CASCADE",
        "INDEX [ZZ_Clustered] UNIQUE CLUSTERED ([Id])",
        "INDEX [ix_a] NONCLUSTERED ([Id]) INCLUDE ([B], [A])",
        ");",
    ]


# ------------------------------------------------------------------ what has no SQL spelling is refused
NO_SPELLING = {
    "a named constraint in a table type": lambda: emit_object_file(
        TableType("dbo", "L", (Column("Id", INT, False),), (PrimaryKey("PK_L", True, (KeyColumn("Id"),)),))
    ),
    "a named default in a table type": lambda: emit_object_file(
        TableType("dbo", "L", (Column("Id", INT, False, default=DefaultConstraint("DF_L", sql("(0)"))),))
    ),
    "an unnamed constraint to add": lambda: emit_operation(
        AddConstraint("dbo", "T", PrimaryKey(None, True, (KeyColumn("Id"),)))
    ),
    "an unnamed default on a new column": lambda: emit_operation(
        AddColumn("dbo", "T", Column("c", INT, True, default=DefaultConstraint(None, sql("(0)"))))
    ),
    "a collation that is not one word": lambda: emit_object_file(
        table(Column("c", TypeRef("char", length=1), True, collation="Latin1 NULL; DROP TABLE [x]"))
    ),
    "a collation that is a keyword": lambda: emit_operation(
        AlterColumn("dbo", "T", "c", TypeRef("char", length=1), True, collation="NULL")
    ),
    "NOT NULL on a computed column that is not persisted": lambda: emit_object_file(
        table(Column("c", None, False, computed=Computed(sql("([a])"))))
    ),
    "NULL on a computed column": lambda: emit_object_file(
        table(Column("c", None, True, computed=Computed(sql("([a])"), persisted=True)))
    ),
    "WITH VALUES without a default": lambda: emit_operation(
        AddColumn("dbo", "T", Column("c", INT, True), with_values=True)
    ),
    "a restart value without RESTART": lambda: emit_operation(AlterSequence("dbo", "S", restart_with=5)),
    "a cache size without CACHE": lambda: emit_operation(AlterSequence("dbo", "S", cache_size=5)),
    "a cache size with NO CACHE": lambda: emit_operation(
        AlterSequence("dbo", "S", cached=False, cache_size=5)
    ),
    "a sequence with NO CACHE and a size": lambda: emit_object_file(
        Sequence("dbo", "S", INT, 1, 1, 1, 9, False, False, 5)
    ),
    "ALTER SEQUENCE that changes nothing": lambda: emit_operation(AlterSequence("dbo", "S")),
    "an execution option that is not known": lambda: emit_operation(
        CreateIndex("dbo", "T", IX_B, (("DROP_EXISTING", "ON"),))
    ),
    "an execution option with a value that is not its own": lambda: emit_operation(
        CreateIndex("dbo", "T", IX_B, (("ONLINE", "ON); DROP TABLE [x]; --"),))
    ),
    "a number option that is not a number": lambda: emit_operation(
        CreateIndex("dbo", "T", IX_B, (("MAXDOP", "ON"),))
    ),
    "an execution option that is given twice": lambda: emit_operation(
        CreateIndex("dbo", "T", IX_B, (("ONLINE", "ON"), ("ONLINE", "OFF")))
    ),
    "WAIT_AT_LOW_PRIORITY without ONLINE = ON": lambda: emit_operation(
        CreateIndex(
            "dbo", "T", IX_B, (("WAIT_AT_LOW_PRIORITY", "MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF"),)
        )
    ),
    "WAIT_AT_LOW_PRIORITY in another form": lambda: emit_operation(
        CreateIndex("dbo", "T", IX_B, (("ONLINE", "ON"), ("WAIT_AT_LOW_PRIORITY", "MAX_DURATION = 5")))
    ),
    "WAIT_AT_LOW_PRIORITY on ALTER COLUMN, where the engine does not allow it": lambda: emit_operation(
        AlterColumn(
            "dbo",
            "T",
            "c",
            INT,
            False,
            None,
            (("ONLINE", "ON"), ("WAIT_AT_LOW_PRIORITY", "MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF")),
        )
    ),
    "execution options on a CHECK constraint": lambda: emit_operation(
        AddConstraint("dbo", "T", CK_A, exec_options=(("ONLINE", "ON"),))
    ),
    "execution options on a DEFAULT constraint": lambda: emit_operation(
        AddConstraint(
            "dbo", "T", DefaultConstraint("DF", sql("(0)")), for_column="c", exec_options=(("ONLINE", "ON"),)
        )
    ),
    "an empty name": lambda: emit_operation(DropTable("dbo", "")),
    "an empty new name": lambda: emit_operation(Rename("object", ("dbo", "T"), "")),
    "text where a number belongs": lambda: emit_object_file(
        Sequence("dbo", "S", INT, "1; DROP TABLE [x]", 1, 1, 9, False, False)  # type: ignore[arg-type]
    ),
    "text where a scale belongs": lambda: emit_object_file(
        table(Column("c", TypeRef("datetime2", scale="3) NULL; DROP TABLE [x] --"), True))  # type: ignore[arg-type]
    ),
}


@pytest.mark.parametrize("write", NO_SPELLING.values(), ids=NO_SPELLING.keys())
def test_a_value_that_has_no_sql_spelling_is_refused_and_not_written(write):
    with pytest.raises(ValueError):
        write()


# ------------------------------------------------------------------ the create script
def replay(script: list[str]) -> Model:
    """The model that the statements of a create script build, one after the other."""
    model = Model()

    def changed(schema: str, name: str, **added: tuple) -> Model:
        found = model[names.object_key("TABLE", schema, name)]
        assert isinstance(found, Table)
        more = {field: (*getattr(found, field), *items) for field, items in added.items()}
        return model.replace(replace(found, **more))

    for statement in script:
        operation = parse_statement(statement)
        match operation:
            case CreateSchema():
                model = model.add(operation.schema)
            case CreateType():
                model = model.add(operation.type)
            case CreateSequence():
                model = model.add(operation.sequence)
            case CreateSynonym():
                model = model.add(operation.synonym)
            case CreateTable():
                model = model.add(operation.table)
            case CreateIndex():
                model = changed(operation.schema, operation.table, indexes=(operation.index,))
            case AddConstraint():
                model = changed(operation.schema, operation.table, constraints=(operation.constraint,))
            case _:
                raise AssertionError(f"not a statement of a create script: {statement}")
    return model


CHICKEN = Table(
    "farm",
    "Chicken",
    (Column("ChickenId", INT, False), Column("EggId", INT, True)),
    (
        PrimaryKey("PK_Chicken", True, (KeyColumn("ChickenId"),)),
        ForeignKey("FK_Chicken_Egg", ("EggId",), "farm", "Egg", ("EggId",)),
    ),
    (Index("IX_Chicken_Egg", False, False, (KeyColumn("EggId"),)),),
)
EGG = Table(
    "farm",
    "Egg",
    (
        Column("EggId", INT, False),
        Column("ChickenId", INT, False),
        Column("Weight", TypeRef("Grams", "farm"), True),
    ),
    (
        ForeignKey("FK_Egg_Chicken", ("ChickenId",), "farm", "Chicken", ("ChickenId",), on_delete="CASCADE"),
        ForeignKey("FK_Egg_Brood", ("ChickenId",), "farm", "Chicken", ("ChickenId",)),
    ),
    (
        Index("IX_Egg_Chicken", False, False, (KeyColumn("ChickenId"),)),
        Index("CIX_Egg", True, True, (KeyColumn("EggId"),)),
    ),
)
FARM = Model(
    [
        Synonym("dbo", "Hen", "farm", "Chicken"),
        EGG,
        CHICKEN,
        Sequence("farm", "EggNo", INT, 1, 1, 1, 999, False, False),
        TableType("farm", "EggList", (Column("Weight", TypeRef("Grams", "farm"), True),)),
        AliasType("farm", "Grams", TypeRef("decimal", precision=9, scale=2), True),
        Schema("farm", "dbo"),
    ]
)


def test_the_create_script_makes_each_object_after_what_it_needs():
    script = emit_create_script(FARM)
    assert [statement.split(" (")[0].split(" FROM ")[0].split(" AS ")[0] for statement in script] == [
        "CREATE SCHEMA [farm] AUTHORIZATION [dbo];",
        "CREATE TYPE [farm].[Grams]",
        "CREATE TYPE [farm].[EggList]",
        "CREATE SEQUENCE [farm].[EggNo]",
        "CREATE TABLE [farm].[Chicken]",
        "CREATE TABLE [farm].[Egg]",
        "CREATE NONCLUSTERED INDEX [IX_Chicken_Egg] ON [farm].[Chicken]",
        "CREATE UNIQUE CLUSTERED INDEX [CIX_Egg] ON [farm].[Egg]",
        "CREATE NONCLUSTERED INDEX [IX_Egg_Chicken] ON [farm].[Egg]",
        "ALTER TABLE [farm].[Chicken] ADD CONSTRAINT [FK_Chicken_Egg] FOREIGN KEY",
        "ALTER TABLE [farm].[Egg] ADD CONSTRAINT [FK_Egg_Brood] FOREIGN KEY",
        "ALTER TABLE [farm].[Egg] ADD CONSTRAINT [FK_Egg_Chicken] FOREIGN KEY",
        "CREATE SYNONYM [dbo].[Hen] FOR [farm].[Chicken];",
    ]


def test_two_tables_that_reference_each_other_are_created_before_any_foreign_key():
    script = emit_create_script(Model([CHICKEN, EGG]))
    creates = [s for s in script if s.startswith("CREATE TABLE")]
    assert len(creates) == 2
    assert not any("FOREIGN KEY" in s or "REFERENCES" in s for s in creates)
    last_table = max(script.index(s) for s in creates)
    foreign_keys = [script.index(s) for s in script if "FOREIGN KEY" in s]
    assert len(foreign_keys) == 3 and min(foreign_keys) > last_table
    assert all(s.startswith("ALTER TABLE") for s in script if "FOREIGN KEY" in s)


def test_every_batch_of_the_create_script_is_one_statement_and_together_they_build_the_model():
    script = emit_create_script(FARM)
    assert all(len(split_batches(statement)) == 1 and statement.endswith(";") for statement in script)
    assert replay(script) == FARM


def test_the_create_script_does_not_depend_on_the_order_in_which_the_model_was_built():
    assert emit_create_script(Model(reversed(list(FARM.values())))) == emit_create_script(FARM)
    assert emit_create_script(Model()) == []


# ------------------------------------------------------------------ TQ-11: refusals with their reason
def test_alter_sequence_with_a_value_that_has_no_clause_is_refused_with_the_reason():
    # another clause is written, so "changes nothing" is not what refuses these
    with pytest.raises(ValueError, match="restart_with goes with restart"):
        emit_operation(AlterSequence("dbo", "S", restart_with=5, increment=2))
    with pytest.raises(ValueError, match="cache_size goes with cached"):
        emit_operation(AlterSequence("dbo", "S", increment=2, cache_size=5))
    assert (
        emit_operation(AlterSequence("dbo", "S", increment=2)) == "ALTER SEQUENCE [dbo].[S] INCREMENT BY 2;"
    )


def test_an_expression_is_not_written_when_its_text_would_read_back_as_other_tokens(monkeypatch):
    # The spacing rule keeps two tokens apart. The emitter does not trust it: it reads its own text
    # back. With a rule that joins everything, '-' '-' becomes a comment and nothing is written.
    check = AddConstraint("dbo", "T", Check("CK", Expression(("(", "[a]", ">", "1", "-", "-", "1", ")"))))
    assert emit_operation(check) == "ALTER TABLE [dbo].[T] ADD CONSTRAINT [CK] CHECK ([a] > 1 - -1);"
    monkeypatch.setattr(emit_module, "_tight", lambda toks, i: True)
    with pytest.raises(ValueError, match="reads back as the same tokens"):
        emit_operation(check)


# ------------------------------------------------------------------ system-versioned temporal tables
def temporal_table(name: str = "t", hidden: bool = True, retention: tuple[int, str] | None = None) -> Table:
    period = TypeRef("datetime2", scale=7)
    return Table(
        "s",
        name,
        (
            Column("Id", TypeRef("int"), False),
            Column("ValidFrom", period, False, generated="ROW_START", hidden=hidden),
            Column("ValidTo", period, False, generated="ROW_END", hidden=hidden),
        ),
        (PrimaryKey(f"PK_{name}", True, (KeyColumn("Id"),)),),
        temporal=Temporal("ValidFrom", "ValidTo", "s", f"{name}_History", retention),
    )


def test_a_temporal_table_file_has_the_one_canonical_text_byte_for_byte():
    assert emit_object_file(temporal_table()) == (
        "CREATE TABLE [s].[t] (\n"
        "    [Id] int NOT NULL,\n"
        "    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL,\n"
        "    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END HIDDEN NOT NULL,\n"
        "    CONSTRAINT [PK_t] PRIMARY KEY CLUSTERED ([Id]),\n"
        "    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])\n"
        ")\n"
        "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[t_History]));\n"
    )


def test_hidden_and_the_retention_are_written_only_when_the_model_holds_them():
    plain = emit_object_file(temporal_table(hidden=False))
    assert "HIDDEN" not in plain and "HISTORY_RETENTION_PERIOD" not in plain
    assert "    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,\n" in plain
    kept = emit_object_file(temporal_table(retention=(6, "MONTHS")))
    assert kept.endswith(
        "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[t_History], "
        "HISTORY_RETENTION_PERIOD = 6 MONTHS));\n"
    )


def test_a_default_of_a_period_column_is_written_after_not_null():
    table = temporal_table()
    start = replace(
        table.columns[1], default=DefaultConstraint("DF_t_From", Expression.from_sql("(sysutcdatetime())"))
    )
    text = emit_object_file(replace(table, columns=(table.columns[0], start, table.columns[2])))
    assert (
        "[ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL "
        "CONSTRAINT [DF_t_From] DEFAULT (sysutcdatetime()),\n"
    ) in text
    (again,) = parse_object_file(text, "schema/tables/s.t.sql")
    assert again.columns[1] == start


def test_set_system_versioning_is_one_alter_table_statement():
    off = SetSystemVersioning("s", "t", False)
    on = SetSystemVersioning("s", "t", True, "s", "t History", (1, "YEARS"))
    assert emit_operation(off) == "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF);"
    assert emit_operation(on) == (
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON "
        "(HISTORY_TABLE = [s].[t History], HISTORY_RETENTION_PERIOD = 1 YEARS));"
    )
    assert parse_statement(emit_operation(off)) == off and parse_statement(emit_operation(on)) == on


def test_a_table_with_period_columns_and_versioning_off_has_no_create_table():
    table = temporal_table()
    off = UnversionedTable(table.schema, table.name, table.columns, table.constraints)
    with pytest.raises(ValueError, match="versioning is off"):
        emit_operation(CreateTable(off))


def test_the_create_script_makes_temporal_tables_whole_and_their_foreign_keys_afterwards():
    parent = temporal_table("p")
    child = temporal_table("c")
    key = ForeignKey("FK_c_p", ("Id",), "s", "p", ("Id",))
    child = replace(child, constraints=(*child.constraints, key))
    script = emit_create_script(Model([Schema("s"), parent, child]))
    kinds = [" ".join(text.split()[:2]) for text in script]
    assert kinds == ["CREATE SCHEMA", "CREATE TABLE", "CREATE TABLE", "ALTER TABLE"]
    # [s].[c] comes before the table it references: its foreign key is a statement of its own
    assert "FOREIGN KEY" not in script[1] and script[1].startswith("CREATE TABLE [s].[c]")
    assert all("SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s]." in text for text in script[1:3])
    assert script[3].startswith("ALTER TABLE [s].[c] ADD CONSTRAINT [FK_c_p] FOREIGN KEY")


# ------------------------------------------------------------------ dynamic data masking
def masked_table(*columns: masking.Column) -> masking.Table:
    return masking.Table("dbo", "T", columns)


def test_the_mask_is_written_directly_after_the_type_and_collate_where_the_engine_reads_it():
    # Azure SQL Database refuses MASKED WITH after IDENTITY, after NULL / NOT NULL, after DEFAULT
    # and before COLLATE with "Incorrect syntax"
    table = masked_table(
        masking.Column("Id", masking.TypeRef("int"), False, masking.Identity(1, 1), masked="default()"),
        masking.Column(
            "Code",
            masking.TypeRef("varchar", length=20),
            False,
            default=masking.DefaultConstraint("DF_T_Code", masking.Expression.from_sql("('x')")),
            collation="Latin1_General_100_BIN2",
            masked='partial(1, "XXXX", 0)',
        ),
    )

    lines = emit_object_file(table).splitlines()

    assert lines[1].strip() == "[Id] int MASKED WITH (FUNCTION = 'default()') IDENTITY(1, 1) NOT NULL,"
    assert lines[2].strip() == (
        "[Code] varchar(20) COLLATE Latin1_General_100_BIN2 "
        "MASKED WITH (FUNCTION = 'partial(1, \"XXXX\", 0)') "
        "NOT NULL CONSTRAINT [DF_T_Code] DEFAULT ('x')"
    )
    assert parse_object_file(emit_object_file(table), "schema/tables/dbo.T.sql") == [table]


def test_a_quote_in_the_masking_function_is_written_as_the_model_holds_it_doubled():
    # sys.masked_columns.masking_function holds '' for a quote; the model holds that text
    function = 'partial(0, "it\'\'s ""x""", 2)'
    table = masked_table(
        masking.Column("Note", masking.TypeRef("nvarchar", length=30), True, masked=function)
    )
    add = masking.MaskColumn("dbo", "T", "Note", function)

    assert f"FUNCTION = '{function}'" in emit_object_file(table)
    assert parse_object_file(emit_object_file(table), "schema/tables/dbo.T.sql") == [table]
    assert emit_operation(add).endswith(f"ADD MASKED WITH (FUNCTION = '{function}');")
    assert parse_statement(emit_operation(add)) == add


def test_the_two_masking_operations_are_one_alter_column_statement_each():
    assert emit_operation(masking.MaskColumn("sales", "Buyer", "Mail", "email()")) == (
        "ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] ADD MASKED WITH (FUNCTION = 'email()');"
    )
    assert emit_operation(masking.UnmaskColumn("sales", "Buyer", "Mail")) == (
        "ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] DROP MASKED;"
    )


def test_a_masking_function_that_would_not_arrive_as_written_is_not_emitted():
    for function in ("default()\x00", 'partial(1, "\\\n", 0)'):
        with pytest.raises(ValueError):
            emit_operation(masking.MaskColumn("dbo", "T", "C", function))


# ------------------------------------------------------------------ wider table coverage
def test_the_column_options_of_the_wider_coverage_have_one_place_each():
    from azsqlcd.model import AlterColumnProperty, RebuildTable

    table = Table(
        "s",
        "T",
        (
            Column("Id", TypeRef("int"), False, identity=Identity(1, 1, True)),
            Column("Guid", TypeRef("uniqueidentifier"), False, rowguidcol=True),
            Column("Note", TypeRef("varchar", length=9), True, collation="Latin1_General_BIN2", sparse=True),
            Column("Card", TypeRef("char", length=4), True, masked="default()", sparse=True),
        ),
        (
            Check("CK_T", Expression.from_sql("([Id] > 0)"), True),
            ForeignKey("FK_T", ("Id",), "s", "P", ("Id",), "CASCADE", not_for_replication=True),
        ),
        compression="PAGE",
    )
    assert emit_operation(CreateTable(table)) == (
        "CREATE TABLE [s].[T] (\n"
        "    [Id] int IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL,\n"
        "    [Guid] uniqueidentifier ROWGUIDCOL NOT NULL,\n"
        "    [Note] varchar(9) COLLATE Latin1_General_BIN2 SPARSE NULL,\n"
        "    [Card] char(4) SPARSE MASKED WITH (FUNCTION = 'default()') NULL,\n"
        "    CONSTRAINT [CK_T] CHECK NOT FOR REPLICATION ([Id] > 0),\n"
        "    CONSTRAINT [FK_T] FOREIGN KEY ([Id]) REFERENCES [s].[P] ([Id]) ON DELETE CASCADE "
        "NOT FOR REPLICATION\n"
        ")\n"
        "WITH (DATA_COMPRESSION = PAGE);"
    )
    assert emit_operation(AlterColumnProperty("s", "T", "Id", False, "NOT FOR REPLICATION")) == (
        "ALTER TABLE [s].[T] ALTER COLUMN [Id] DROP NOT FOR REPLICATION;"
    )
    assert emit_operation(RebuildTable("s", "T", "NONE", (("ONLINE", "ON"), ("MAXDOP", "2")))) == (
        "ALTER TABLE [s].[T] REBUILD WITH (DATA_COMPRESSION = NONE, MAXDOP = 2, ONLINE = ON);"
    )
    with pytest.raises(ValueError, match="REBUILD takes"):
        emit_operation(RebuildTable("s", "T", "ROW", (("RESUMABLE", "ON"),)))


def test_a_columnstore_index_is_written_with_its_list_order_filter_and_options():
    clustered = Index(
        "CCI",
        False,
        True,
        (KeyColumn("d"), KeyColumn("s")),
        options=(("DATA_COMPRESSION", "COLUMNSTORE_ARCHIVE"),),
        columnstore=True,
    )
    assert emit_operation(CreateIndex("dw", "F", clustered, (("MAXDOP", "2"),))) == (
        "CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON [dw].[F] ORDER ([d], [s]) "
        "WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE, MAXDOP = 2);"
    )
    nonclustered = Index(
        "NCCI",
        False,
        False,
        (),
        ("a", "b"),
        Expression.from_sql("[a] > 0"),
        (("COMPRESSION_DELAY", "10"),),
        True,
    )
    assert emit_operation(CreateIndex("dw", "F", nonclustered)) == (
        "CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI] ON [dw].[F] ([a], [b]) WHERE [a] > 0 "
        "WITH (COMPRESSION_DELAY = 10);"
    )
    with pytest.raises(ValueError, match="ONLINE and MAXDOP only"):
        emit_operation(CreateIndex("dw", "F", clustered, (("SORT_IN_TEMPDB", "ON"),)))
    table = Table(
        "dw",
        "F",
        (Column("a", TypeRef("int"), False),),
        indexes=(Index("C", False, True, (), columnstore=True),),
    )
    assert "    INDEX [C] CLUSTERED COLUMNSTORE\n" in emit_operation(CreateTable(table))


def test_a_money_literal_in_a_generated_statement_has_no_space_after_the_currency_symbol():
    index = parse_statement("CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([A]) WHERE [Value] > $5")
    assert emit_operation(index) == "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([A]) WHERE [Value] > $5;"
    assert parse_statement(emit_operation(index)) == index
