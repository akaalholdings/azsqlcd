"""parse_statement: one migration model batch gives one operation from the closed list.

The statement fixtures (tests/fixtures/ddl/ok/stmt_*.sql and the bad ones without a path) run in
test_parse.py. These tests state the rules one by one.
"""

import pytest

from azsqlcd import model as masking
from azsqlcd.model import (
    AddColumn,
    AddConstraint,
    AlterColumn,
    AlterColumnProperty,
    AlterSequence,
    Check,
    Column,
    CreateIndex,
    CreateSchema,
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
    KeyColumn,
    PrimaryKey,
    RebuildTable,
    Rename,
    Schema,
    SetSystemVersioning,
    Synonym,
    Temporal,
    TypeRef,
    Unique,
)
from azsqlcd.parse import EXECUTION_OPTIONS, ParseError, parse_statement


def rejected(sql: str) -> ParseError:
    with pytest.raises(ParseError) as caught:
        parse_statement(sql)
    return caught.value


# ------------------------------------------------------------------ the closed list
@pytest.mark.parametrize(
    ("sql", "operation"),
    [
        ("CREATE SCHEMA [sales] AUTHORIZATION [dbo];", CreateSchema(Schema("sales", "dbo"))),
        ("DROP SCHEMA [sales];", DropSchema("sales")),
        ("DROP TYPE [dbo].[IdList];", DropType("dbo", "IdList")),
        ("DROP SEQUENCE [sales].[OrderNo];", DropSequence("sales", "OrderNo")),
        (
            "CREATE SYNONYM [dbo].[Cust] FOR [sales].[Customer];",
            CreateSynonym(Synonym("dbo", "Cust", "sales", "Customer")),
        ),
        ("DROP SYNONYM [dbo].[Cust];", DropSynonym("dbo", "Cust")),
        ("DROP TABLE [sales].[Order];", DropTable("sales", "Order")),
        ("ALTER TABLE [sales].[Order] DROP COLUMN [Stat];", DropColumn("sales", "Order", "Stat")),
        (
            "ALTER TABLE [sales].[Order] DROP CONSTRAINT [DF_Order_Stat];",
            DropConstraint("sales", "Order", "DF_Order_Stat"),
        ),
        ("DROP INDEX [IX_Order_Stat] ON [sales].[Order];", DropIndex("sales", "Order", "IX_Order_Stat")),
        ("ALTER SEQUENCE [sales].[OrderNo] INCREMENT BY 5;", AlterSequence("sales", "OrderNo", increment=5)),
        (
            "ALTER TABLE [sales].[Order] ADD [Note] nvarchar(50) NULL;",
            AddColumn("sales", "Order", Column("Note", TypeRef("nvarchar", length=50), True)),
        ),
        (
            "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(max) NOT NULL;",
            AlterColumn("sales", "Order", "Note", TypeRef("nvarchar", length="max"), False),
        ),
        (
            "ALTER TABLE [sales].[Order] ADD CONSTRAINT [CK_Order_Stat] CHECK ([Stat] < 9);",
            AddConstraint("sales", "Order", Check("CK_Order_Stat", Expression.from_sql("([Stat] < 9)"))),
        ),
        (
            "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';",
            Rename("column", ("sales", "Order", "Stat"), "Status"),
        ),
    ],
)
def test_a_statement_gives_exactly_the_operation_it_states(sql: str, operation: object):
    assert parse_statement(sql) == operation
    assert type(parse_statement(sql)) is type(operation)


def test_keywords_in_any_case_and_bare_names_give_the_same_operation():
    assert parse_statement("alter table sales.[order] drop column stat") == DropColumn(
        "sales", "Order", "Stat"
    )
    assert parse_statement("drop index ix on Sales.Orders") == DropIndex("sales", "orders", "IX")


def test_a_statement_may_follow_directive_comments():
    sql = "-- azsqlcd:allow DROP_TABLE [sales].[Order] reason: archived in r40\nDROP TABLE [sales].[Order];\n"
    assert parse_statement(sql) == DropTable("sales", "Order")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE [sales].[Order] CASCADE;",
        "DROP TABLE [sales].[Order]; DROP TABLE [sales].[OrderLine];",
        "DROP TABLE [sales].[Order], [sales].[OrderLine];",
        "DROP TABLE [sales].[Order];;",
        "CREATE SCHEMA [sales] CREATE TABLE [sales].[T] ([a] int NULL);",
        "ALTER TABLE [sales].[Order] DROP COLUMN [Stat] RESTRICT;",
        "DROP INDEX [IX] ON [sales].[Order] WITH (ONLINE = ON);",
        "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN', N'extra';",
    ],
)
def test_a_statement_with_trailing_tokens_is_rejected(sql: str):
    error = rejected(sql)
    assert error.code == "SYNTAX"
    assert "expected the end of the statement" in error.message


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- only a comment",
        "SELECT 1;",
        "UPDATE [sales].[Order] SET [Stat] = 1;",
        "CREATE OR ALTER PROCEDURE [dbo].[p] AS SELECT 1;",
        "CREATE VIEW [dbo].[v] AS SELECT 1 AS [x];",
        "ALTER INDEX [IX] ON [sales].[Order] REBUILD;",
        "DROP VIEW [dbo].[v];",
        "TRUNCATE TABLE [sales].[Order];",
        "GRANT SELECT ON SCHEMA::[sales] TO [reader];",
        "ALTER TABLE [sales].[Order] SWITCH TO [sales].[OrderArchive];",
        "ALTER TABLE [sales].[Order] REBUILD;",
        "ALTER TABLE [sales].[Order] ENABLE TRIGGER ALL;",
        "ALTER TABLE [sales].[Order] ADD COLUMN [Note] int NULL;",
        "ALTER TABLE [sales].[Order] DROP [DF_Order_Stat];",
        "EXEC sys.sp_executesql N'DROP TABLE [sales].[Order]';",
        "EXEC (N'DROP TABLE [sales].[Order]');",
    ],
)
def test_a_statement_outside_the_closed_grammar_is_a_syntax_error(sql: str):
    assert rejected(sql).code == "SYNTAX"


# ------------------------------------------------------------------ ALTER TABLE
@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [dbo].[T] ADD [a] int NULL, [b] int NULL;",
        "ALTER TABLE [dbo].[T] ADD [a] int NULL, CONSTRAINT [CK] CHECK ([a] > 0);",
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [CK] CHECK ([a] > 0), CONSTRAINT [CK2] CHECK ([a] < 9);",
        "ALTER TABLE [dbo].[T] DROP COLUMN [a], [b];",
        "ALTER TABLE [dbo].[T] DROP CONSTRAINT [CK], COLUMN [a];",
        "ALTER TABLE [dbo].[T] ADD [a] int NOT NULL CONSTRAINT [PK] PRIMARY KEY CLUSTERED;",
        "ALTER TABLE [dbo].[T] ADD [a] int NULL CONSTRAINT [FK] REFERENCES [dbo].[P] ([a]);",
        "ALTER TABLE [dbo].[T] ADD [a] int NULL INDEX [IX] NONCLUSTERED;",
    ],
)
def test_one_alter_table_statement_holds_one_action(sql: str):
    error = rejected(sql)
    assert error.code == "SYNTAX"
    assert "one action per statement" in error.message


def test_an_added_column_keeps_its_named_default_because_the_default_is_part_of_the_column():
    operation = parse_statement(
        "ALTER TABLE [sales].[Order] ADD [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0);"
    )
    assert isinstance(operation, AddColumn)
    assert operation.column.default == DefaultConstraint("DF_Order_Status", Expression.from_sql("(0)"))
    assert (operation.with_values, operation.column.nullable) == (False, False)


def test_with_values_is_recorded_on_the_added_column_and_nowhere_else():
    sql = "ALTER TABLE [dbo].[T] ADD [a] bit NULL CONSTRAINT [DF_T_a] DEFAULT (1) WITH VALUES;"
    operation = parse_statement(sql)
    assert isinstance(operation, AddColumn) and operation.with_values is True
    assert (
        rejected("CREATE TABLE [dbo].[T] ([a] bit NULL CONSTRAINT [DF_T_a] DEFAULT (1) WITH VALUES)").code
        == "SYNTAX"
    )
    assert (
        rejected("ALTER TABLE [dbo].[T] ADD CONSTRAINT [DF_T_a] DEFAULT (1) FOR [a] WITH VALUES;").code
        == "SYNTAX"
    )


@pytest.mark.parametrize(
    ("prefix", "recorded"),
    [("", None), ("WITH CHECK ", True), ("WITH NOCHECK ", False), ("with nocheck ", False)],
)
def test_with_check_and_with_nocheck_are_recorded_on_the_operation(prefix: str, recorded: bool | None):
    foreign = parse_statement(
        f"ALTER TABLE [dbo].[T] {prefix}ADD CONSTRAINT [FK_T] FOREIGN KEY ([p]) REFERENCES [dbo].[P] ([id]);"
    )
    check = parse_statement(f"ALTER TABLE [dbo].[T] {prefix}ADD CONSTRAINT [CK_T] CHECK ([p] > 0);")
    assert isinstance(foreign, AddConstraint) and isinstance(check, AddConstraint)
    assert (foreign.with_check, check.with_check) == (recorded, recorded)
    assert isinstance(foreign.constraint, ForeignKey) and isinstance(check.constraint, Check)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [dbo].[T] WITH CHECK ADD CONSTRAINT [PK] PRIMARY KEY CLUSTERED ([a]);",
        "ALTER TABLE [dbo].[T] WITH NOCHECK ADD CONSTRAINT [DF] DEFAULT (0) FOR [a];",
        "ALTER TABLE [dbo].[T] WITH NOCHECK ADD [a] int NULL;",
        "ALTER TABLE [dbo].[T] WITH CHECK DROP CONSTRAINT [CK];",
    ],
)
def test_with_check_goes_with_a_foreign_key_or_a_check_and_nothing_else(sql: str):
    assert rejected(sql).code == "SYNTAX"


def test_a_default_for_an_existing_column_names_the_column():
    operation = parse_statement(
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [DF_T_a] DEFAULT NEXT VALUE FOR [dbo].[S] FOR [a];"
    )
    assert isinstance(operation, AddConstraint)
    assert operation.for_column == "a"
    assert isinstance(operation.constraint, DefaultConstraint)
    assert operation.constraint.expression.tokens == ("NEXT", "VALUE", "FOR", "[dbo]", ".", "[S]")


def test_alter_column_states_type_collation_and_nullability_and_nothing_else():
    operation = parse_statement(
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Name] varchar(40) COLLATE Latin1_General_BIN2 NOT NULL"
        " WITH (ONLINE = ON);"
    )
    assert operation == AlterColumn(
        "dbo", "T", "Name", TypeRef("varchar", length=40), False, "Latin1_General_BIN2", (("ONLINE", "ON"),)
    )
    plain = parse_statement("ALTER TABLE [dbo].[T] ALTER COLUMN [Name] varchar(40) NULL;")
    assert isinstance(plain, AlterColumn) and (plain.collation, plain.exec_options) == (None, ())
    for sql in (
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Name] varchar(40) NULL CONSTRAINT [DF] DEFAULT ('x');",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Id] int IDENTITY(1,1) NOT NULL;",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Name] varchar(40) NULL WITH (MAXDOP = 2);",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Name] varchar(40) NULL WITH (FILLFACTOR = 90);",
    ):
        assert rejected(sql).code == "SYNTAX"
    for sql in (
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Total] ADD PERSISTED;",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Total] DROP PERSISTED;",
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Email] varchar(9) MASKED WITH (FUNCTION = 'email()') NULL;",
    ):
        assert rejected(sql).code == "UNSUPPORTED"


# ------------------------------------------------------------------ normal form in migrations
@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("ALTER TABLE [dbo].[T] ADD [a] int;", "NF001"),
        ("ALTER TABLE [dbo].[T] ALTER COLUMN [a] bigint;", "NF001"),
        ("CREATE TYPE [dbo].[Code] FROM char(3);", "NF001"),
        ("ALTER TABLE [dbo].[T] ADD [a] int NOT NULL DEFAULT (0);", "NF002"),
        ("ALTER TABLE [dbo].[T] ADD PRIMARY KEY CLUSTERED ([a]);", "NF002"),
        ("ALTER TABLE [dbo].[T] ADD FOREIGN KEY ([a]) REFERENCES [dbo].[P] ([a]);", "NF002"),
        ("ALTER TABLE [dbo].[T] ADD CHECK ([a] > 0);", "NF002"),
        ("ALTER TABLE [dbo].[T] ADD DEFAULT (0) FOR [a];", "NF002"),
        ("ALTER TABLE [dbo].[T] ADD [a] varchar NULL;", "NF003"),
        ("ALTER TABLE [dbo].[T] ALTER COLUMN [a] decimal(9) NULL;", "NF003"),
        ("ALTER TABLE [dbo].[T] ALTER COLUMN [a] time NULL;", "NF003"),
        ("ALTER TABLE [dbo].[T] ADD [a] integer NULL;", "NF004"),
        ("ALTER TABLE [dbo].[T] ALTER COLUMN [a] national char varying(9) NULL;", "NF004"),
        (
            "CREATE SEQUENCE dbo.S AS dec(9,0) START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9 CYCLE",
            "NF004",
        ),
        ("ALTER TABLE [dbo].[T] ADD CONSTRAINT [PK] PRIMARY KEY ([a]);", "NF005"),
        ("ALTER TABLE [dbo].[T] ADD CONSTRAINT [UQ] UNIQUE ([a]);", "NF005"),
        ("CREATE INDEX [IX] ON [dbo].[T] ([a]);", "NF005"),
        ("CREATE UNIQUE INDEX [IX] ON [dbo].[T] ([a]);", "NF005"),
    ],
)
def test_the_normal_form_holds_in_a_migration_because_the_model_cannot_hold_an_implicit_value(
    sql: str, code: str
):
    assert rejected(sql).code == code


# ------------------------------------------------------------------ options
def test_execution_options_are_kept_on_the_operation_and_out_of_the_model():
    operation = parse_statement(
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (ONLINE = ON, FILLFACTOR = 90,"
        " RESUMABLE = ON, MAX_DURATION = 60 MINUTES, MAXDOP = 4, SORT_IN_TEMPDB = OFF,"
        " DATA_COMPRESSION = ROW);"
    )
    assert isinstance(operation, CreateIndex)
    assert operation.index.options == (("DATA_COMPRESSION", "ROW"), ("FILLFACTOR", "90"))
    assert operation.exec_options == (
        ("MAXDOP", "4"),
        ("MAX_DURATION", "60"),
        ("ONLINE", "ON"),
        ("RESUMABLE", "ON"),
        ("SORT_IN_TEMPDB", "OFF"),
    )
    assert {name for name, _ in operation.exec_options} == EXECUTION_OPTIONS
    plain = parse_statement(
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (FILLFACTOR = 90, DATA_COMPRESSION = ROW)"
    )
    assert isinstance(plain, CreateIndex) and plain.index == operation.index
    assert plain != operation


def test_wait_at_low_priority_is_kept_with_its_two_settings():
    operation = parse_statement(
        "CREATE CLUSTERED INDEX [IX] ON [dbo].[T] ([a])"
        " WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = BLOCKERS)));"
    )
    assert isinstance(operation, CreateIndex)
    assert operation.exec_options == (
        ("ONLINE", "ON"),
        ("WAIT_AT_LOW_PRIORITY", "MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = BLOCKERS"),
    )
    for broken in (
        "WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES)))",
        "WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5, ABORT_AFTER_WAIT = LATER)))",
        "WITH (ONLINE = OFF (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5, ABORT_AFTER_WAIT = SELF)))",
        "WITH (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5, ABORT_AFTER_WAIT = SELF))",
    ):
        assert rejected(f"CREATE CLUSTERED INDEX [IX] ON [dbo].[T] ([a]) {broken};").code == "SYNTAX"


@pytest.mark.parametrize(
    ("options", "says"),
    [
        (
            "ONLINE = ON, RESUMABLE = ON, SORT_IN_TEMPDB = ON",
            "SORT_IN_TEMPDB = ON is not valid with RESUMABLE = ON",
        ),
        ("RESUMABLE = ON", "RESUMABLE = ON needs ONLINE = ON"),
        ("ONLINE = OFF, RESUMABLE = ON", "RESUMABLE = ON needs ONLINE = ON"),
        ("ONLINE = ON, MAX_DURATION = 5 MINUTES", "MAX_DURATION needs RESUMABLE = ON"),
        ("ONLINE = ON, RESUMABLE = OFF, MAX_DURATION = 5", "MAX_DURATION needs RESUMABLE = ON"),
    ],
)
def test_execution_options_that_the_engine_refuses_together_are_refused(options: str, says: str):
    # Not syntax errors for the engine: PARSEONLY passes them and the build fails when it runs. In a
    # nontx migration that is after the marker row is committed.
    for statement in (
        f"CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH ({options});",
        f"ALTER TABLE [dbo].[T] ADD CONSTRAINT [UQ] UNIQUE NONCLUSTERED ([a]) WITH ({options});",
    ):
        error = rejected(statement)
        assert error.code == "SYNTAX" and says in error.message
    for accepted in (
        "ONLINE = ON, RESUMABLE = ON, MAX_DURATION = 5 MINUTES, SORT_IN_TEMPDB = OFF",
        "ONLINE = OFF, SORT_IN_TEMPDB = ON",
        "RESUMABLE = OFF",
    ):
        parse_statement(f"CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH ({accepted});")


def test_online_alter_column_takes_no_low_priority_wait():
    # Learn, ALTER TABLE: "The WAIT_AT_LOW_PRIORITY option can't be used with online ALTER COLUMN."
    # The engine refuses the statement, so the parser refuses it before a plan can meet it.
    online = "ALTER TABLE [dbo].[T] ALTER COLUMN [x] bigint NOT NULL WITH (ONLINE = ON"
    operation = parse_statement(online + ");")
    assert isinstance(operation, AlterColumn) and operation.exec_options == (("ONLINE", "ON"),)
    error = rejected(online + " (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)));")
    assert error.code == "SYNTAX" and "WAIT_AT_LOW_PRIORITY is not valid on ALTER COLUMN" in error.message


def test_a_key_constraint_added_by_a_migration_takes_execution_options_too():
    operation = parse_statement(
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [UQ] UNIQUE NONCLUSTERED ([a], [b] DESC)"
        " WITH (ONLINE = ON, PAD_INDEX = ON);"
    )
    assert operation == AddConstraint(
        "dbo",
        "T",
        Unique("UQ", False, (KeyColumn("a"), KeyColumn("b", True)), (("PAD_INDEX", "ON"),)),
        exec_options=(("ONLINE", "ON"),),
    )


def test_an_execution_option_inside_create_table_is_rejected():
    error = rejected(
        "CREATE TABLE [dbo].[T] ([a] int NULL, CONSTRAINT [UQ] UNIQUE CLUSTERED ([a]) WITH (ONLINE = ON))"
    )
    assert (error.code, "execution option" in error.message) == ("SYNTAX", True)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (FILLFACTOR = 90, SHINY = ON);",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (ONLINE = MAYBE);",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (FILLFACTOR = HIGH);",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (FILLFACTOR = -5);",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (MAXDOP = 1.5);",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (ONLINE = ON, ONLINE = OFF);",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH ();",
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (PAD_INDEX);",
        "CREATE SEQUENCE dbo.S AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9 CYCLE CACHE ORDER;",
        "CREATE SYNONYM [dbo].[S] FOR [dbo].[T] WITH (FAST = ON);",
        "CREATE TYPE [dbo].[L] AS TABLE ([a] int NULL) WITH (ZIP = ON);",
    ],
)
def test_an_option_the_parser_does_not_know_is_an_error_not_ignored(sql: str):
    assert rejected(sql).code == "SYNTAX"


def test_a_refused_feature_is_refused_in_alter_table_as_well():
    for sql, feature in (
        ("ALTER TABLE [dbo].[T] ADD PERIOD FOR SYSTEM_TIME ([a], [b]);", "temporal tables"),
        (
            "ALTER TABLE [dbo].[T] ADD [a] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL;",
            "temporal tables",
        ),
        ("ALTER TABLE [dbo].[T] ADD [a] xml COLUMN_SET FOR ALL_SPARSE_COLUMNS;", "column sets"),
        ("ALTER TABLE [dbo].[T] ADD CONSTRAINT [PK] PRIMARY KEY CLUSTERED ([a]) ON [ps] ([a]);", "partition"),
        ("ALTER TABLE [dbo].[T] REBUILD PARTITION = 2 WITH (DATA_COMPRESSION = ROW);", "partitioned tables"),
    ):
        error = rejected(sql)
        assert (error.code, feature in error.message) == ("UNSUPPORTED", True), sql


def test_drop_existing_is_refused_by_name():
    error = rejected(
        "CREATE NONCLUSTERED INDEX [IX] ON [dbo].[T] ([a]) WITH (DROP_EXISTING = ON, ONLINE = ON);"
    )
    assert (error.code, "DROP_EXISTING" in error.message) == ("UNSUPPORTED", True)


# ------------------------------------------------------------------ refused features
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE [db].[dbo].[T] ([a] int NULL);",
        "DROP TABLE [db].[dbo].[T];",
        "ALTER TABLE [db].[dbo].[T] DROP COLUMN [a];",
        "CREATE NONCLUSTERED INDEX [IX] ON [db].[dbo].[T] ([a]);",
        "CREATE SYNONYM [dbo].[S] FOR [server].[db].[dbo].[T];",
        "ALTER TABLE [dbo].[T] ADD [a] [db].[dbo].[Phone] NULL;",
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [FK] FOREIGN KEY ([a]) REFERENCES [db].[dbo].[P] ([a]);",
        "EXEC sys.sp_rename N'[db].[sales].[Order]', N'Orders', N'OBJECT';",
    ],
)
def test_a_three_part_name_is_refused_wherever_a_name_can_stand(sql: str):
    error = rejected(sql)
    assert (error.code, "three-part names" in error.message) == ("UNSUPPORTED", True)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE IF EXISTS [dbo].[T];",
        "DROP INDEX IF EXISTS [IX] ON [dbo].[T];",
        "DROP SEQUENCE IF EXISTS [dbo].[S];",
        "ALTER TABLE [dbo].[T] DROP COLUMN IF EXISTS [a];",
        "ALTER TABLE [dbo].[T] DROP CONSTRAINT IF EXISTS [CK];",
    ],
)
def test_a_conditional_drop_is_refused_because_replay_needs_one_outcome(sql: str):
    error = rejected(sql)
    assert (error.code, "IF EXISTS" in error.message) == ("UNSUPPORTED", True)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE [T] ([a] int NULL);",
        "DROP TABLE [T];",
        "ALTER TABLE [T] DROP COLUMN [a];",
        "CREATE NONCLUSTERED INDEX [IX] ON [T] ([a]);",
        "CREATE SYNONYM [dbo].[S] FOR [T];",
        "CREATE SEQUENCE [S] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9 CYCLE CACHE;",
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [FK] FOREIGN KEY ([a]) REFERENCES [P] ([a]);",
        "CREATE TABLE [#work] ([a] int NULL);",
    ],
)
def test_a_one_part_object_name_is_rejected(sql: str):
    error = rejected(sql)
    assert (error.code, "two parts" in error.message) == ("SYNTAX", True)


# ------------------------------------------------------------------ sequences
def test_create_sequence_states_every_property_in_any_order():
    one = parse_statement(
        "CREATE SEQUENCE dbo.S AS int START WITH 5 INCREMENT BY 2 MINVALUE 1 MAXVALUE 99 NO CYCLE CACHE 10;"
    )
    two = parse_statement(
        "CREATE SEQUENCE dbo.S CACHE 10 NO CYCLE MAXVALUE 99 MINVALUE 1 INCREMENT BY 2 START WITH 5 AS int"
    )
    assert one == two
    for missing in (
        "AS int",
        "START WITH 5",
        "INCREMENT BY 2",
        "MINVALUE 1",
        "MAXVALUE 99",
        "NO CYCLE",
        "CACHE 10",
    ):
        clauses = "AS int START WITH 5 INCREMENT BY 2 MINVALUE 1 MAXVALUE 99 NO CYCLE CACHE 10".replace(
            missing, ""
        )
        error = rejected(f"CREATE SEQUENCE [dbo].[S] {clauses};")
        assert (error.code, "missing" in error.message) == ("SYNTAX", True)


def test_alter_sequence_records_only_the_clauses_that_are_written():
    assert parse_statement("ALTER SEQUENCE [dbo].[S] RESTART WITH -5 NO CYCLE;") == AlterSequence(
        "dbo", "S", restart=True, restart_with=-5, cycle=False
    )
    assert parse_statement("ALTER SEQUENCE [dbo].[S] CACHE;") == AlterSequence("dbo", "S", cached=True)
    assert parse_statement("ALTER SEQUENCE [dbo].[S] CACHE 7;") == AlterSequence(
        "dbo", "S", cached=True, cache_size=7
    )
    assert parse_statement("ALTER SEQUENCE [dbo].[S] RESTART;") != parse_statement(
        "ALTER SEQUENCE [dbo].[S] RESTART WITH 1;"
    )
    for sql in (
        "ALTER SEQUENCE [dbo].[S];",
        "ALTER SEQUENCE [dbo].[S] AS bigint;",
        "ALTER SEQUENCE [dbo].[S] START WITH 1;",
        "ALTER SEQUENCE [dbo].[S] INCREMENT BY 1 INCREMENT BY 2;",
        "ALTER SEQUENCE [dbo].[S] NO MAXVALUE;",
        "ALTER SEQUENCE [dbo].[S] INCREMENT BY 1.5;",
    ):
        assert rejected(sql).code == "SYNTAX"


# ------------------------------------------------------------------ sp_rename
@pytest.mark.parametrize(
    ("sql", "rename"),
    [
        (
            "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN'",
            Rename("column", ("sales", "Order", "Stat"), "Status"),
        ),
        (
            "EXECUTE sys.sp_rename N'sales.Order.IX_a', N'IX_b', N'index';",
            Rename("index", ("sales", "Order", "IX_a"), "IX_b"),
        ),
        (
            "exec [SYS].[SP_RENAME] N'[sales].[Order]', N'Orders', N'OBJECT';",
            Rename("object", ("sales", "Order"), "Orders"),
        ),
        (
            "EXEC sys.sp_rename N'[my.schema].[Order Details]]v2].[a b]', N'it''s [new]', N'COLUMN';",
            Rename("column", ("my.schema", "Order Details]v2", "a b"), "it's [new]"),
        ),
    ],
)
def test_sp_rename_gives_the_kind_the_parts_of_the_old_name_and_the_new_name(sql: str, rename: Rename):
    found = parse_statement(sql)
    assert isinstance(found, Rename)
    assert (found.kind, found.old, found.new_name) == (rename.kind, rename.old, rename.new_name)


def test_a_rename_of_an_object_does_not_say_table_or_constraint():
    found = parse_statement("EXEC sys.sp_rename N'[sales].[PK_Order]', N'PK_Orders', N'OBJECT';")
    assert isinstance(found, Rename) and found.kind == "object"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("EXEC sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status';", "SYNTAX"),
        ("EXEC sys.sp_rename '[sales].[Order].[Stat]', 'Status', 'COLUMN';", "SYNTAX"),
        (
            "EXEC sys.sp_rename @objname = N'[s].[t].[Stat]', @newname = N'Status', @objtype = N'COLUMN';",
            "SYNTAX",
        ),
        ("EXEC sys.sp_rename N'[sales].[Order].[Stat]', @new, N'COLUMN';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'[Status]', N'COLUMN';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'', N'COLUMN';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[Order].[Stat]', N'Status', N'COLUMN';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[Order]', N'Orders', N'OBJECT';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].[Order] x', N'Orders', N'OBJECT';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].', N'Orders', N'OBJECT';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].[Order', N'Orders', N'OBJECT';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMNS';", "SYNTAX"),
        ("EXEC sys.sp_rename N'[dbo].[Phone]', N'PhoneNumber', N'USERDATATYPE';", "UNSUPPORTED"),
        ("EXEC sys.sp_rename N'[sales].[Order].[st_1]', N'st_2', N'STATISTICS';", "UNSUPPORTED"),
    ],
)
def test_sp_rename_takes_three_n_literals_with_a_schema_qualified_old_name(sql: str, code: str):
    assert rejected(sql).code == code


def test_a_new_name_in_brackets_is_refused_because_sp_rename_would_keep_the_brackets():
    error = rejected("EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'[Status]', N'COLUMN';")
    assert "without brackets" in error.message


# ------------------------------------------------------------------ created objects
def test_create_table_in_a_migration_gives_the_same_table_as_the_object_file_would():
    operation = parse_statement(
        "CREATE TABLE [dbo].[T] ([Id] int IDENTITY NOT NULL CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED,"
        " [P] int NULL CONSTRAINT [FK_T_P] REFERENCES [dbo].[P] ([Id]) ON DELETE SET NULL);"
    )
    assert isinstance(operation, CreateTable)
    assert operation.table.key == "TABLE:[dbo].[T]"
    assert operation.table.constraint("PK_T") == PrimaryKey("PK_T", True, (KeyColumn("Id"),))
    assert operation.table.constraint("FK_T_P") == ForeignKey(
        "FK_T_P", ("P",), "dbo", "P", ("Id",), on_delete="SET NULL"
    )
    identity = operation.table.columns[0].identity
    assert identity is not None and (identity.seed, identity.increment) == (1, 1)


def test_create_type_gives_an_alias_type_or_a_table_type():
    alias = parse_statement("CREATE TYPE [dbo].[Code] FROM char(3) NOT NULL;")
    listing = parse_statement("CREATE TYPE [dbo].[Codes] AS TABLE ([Code] char(3) NOT NULL);")
    assert isinstance(alias, CreateType) and isinstance(listing, CreateType)
    assert (type(alias.type).__name__, type(listing.type).__name__) == ("AliasType", "TableType")
    assert rejected("CREATE TYPE [dbo].[Code] FROM [dbo].[Other] NOT NULL;").code == "SYNTAX"
    assert rejected("CREATE TYPE [dbo].[Point] EXTERNAL NAME [Geo].[Point];").code == "UNSUPPORTED"


# ------------------------------------------------------------------ errors
def test_a_parse_error_carries_code_message_line_and_column():
    error = rejected("ALTER TABLE [sales].[Order]\n    ADD [Status] tinyint NOT NULL\n    DEFAULT (0);")
    assert (error.code, error.line, error.column) == ("NF002", 3, 5)
    assert error.message.startswith("the DEFAULT of column [Status] needs a name")
    assert str(error).startswith("NF002 at 3:5: ")
    assert isinstance(error, ValueError)


def test_an_unsupported_error_names_the_feature():
    error = rejected("CREATE XML INDEX [XI] ON [dw].[Fact] ([Body]);")
    assert error.message == "not supported in this version: XML indexes"


def test_a_lexer_error_becomes_a_syntax_error():
    error = rejected("ALTER TABLE [dbo].[T]\nADD [a] int NULL /* never closed")
    assert (error.code, error.line, error.column) == ("SYNTAX", 2, 0)
    assert "unterminated" in error.message


# ------------------------------------------------------------------ system-versioned temporal tables
SET = "ALTER TABLE [dbo].[T] SET "
HISTORY = "HISTORY_TABLE = [dbo].[T_History]"


@pytest.mark.parametrize(
    ("sql", "operation"),
    [
        (SET + "(SYSTEM_VERSIONING = OFF)", SetSystemVersioning("dbo", "T", False)),
        (
            SET + f"(SYSTEM_VERSIONING = ON ({HISTORY}))",
            SetSystemVersioning("dbo", "T", True, "dbo", "T_History"),
        ),
        (
            SET + f"(SYSTEM_VERSIONING = ON ({HISTORY}, HISTORY_RETENTION_PERIOD = 1 YEAR))",
            SetSystemVersioning("dbo", "T", True, "dbo", "T_History", (1, "YEARS")),
        ),
        (
            SET + f"(SYSTEM_VERSIONING = ON ({HISTORY}, HISTORY_RETENTION_PERIOD = INFINITE))",
            SetSystemVersioning("dbo", "T", True, "dbo", "T_History"),
        ),
        # DATA_CONSISTENCY_CHECK says how the engine runs the statement: read, and not a property
        (
            SET + f"(SYSTEM_VERSIONING = ON ({HISTORY}, DATA_CONSISTENCY_CHECK = OFF))",
            SetSystemVersioning("dbo", "T", True, "dbo", "T_History"),
        ),
        (
            SET + f"(SYSTEM_VERSIONING = ON ({HISTORY}, DATA_CONSISTENCY_CHECK = ON, "
            "HISTORY_RETENTION_PERIOD = 3 MONTHS));",
            SetSystemVersioning("dbo", "T", True, "dbo", "T_History", (3, "MONTHS")),
        ),
    ],
)
def test_alter_table_set_system_versioning_is_one_operation(sql: str, operation: SetSystemVersioning):
    assert parse_statement(sql) == operation


@pytest.mark.parametrize(
    ("sql", "code", "says"),
    [
        (SET + "(SYSTEM_VERSIONING = ON)", "NF006", "history table by name"),
        (SET + "(SYSTEM_VERSIONING = ON (HISTORY_TABLE = T_History))", "NF006", "two parts"),
        (SET + "(SYSTEM_VERSIONING = ON (DATA_CONSISTENCY_CHECK = OFF))", "NF006", "history table by name"),
        (
            SET + f"(SYSTEM_VERSIONING = ON ({HISTORY}, DATA_CONSISTENCY_CHECK = MAYBE))",
            "SYNTAX",
            "ON or OFF",
        ),
        (SET + f"(SYSTEM_VERSIONING = OFF ({HISTORY}))", "SYNTAX", "')'"),
        (SET + "(SYSTEM_VERSIONING = OFF), ADD [x] int NULL", "SYNTAX", "one action per statement"),
        (SET + "(LOCK_ESCALATION = AUTO)", "SYNTAX", "expected SYSTEM_VERSIONING"),
        (SET + "(SYSTEM_VERSIONING = OFF, LOCK_ESCALATION = AUTO)", "SYNTAX", "')'"),
        ("ALTER TABLE [dbo].[T] DROP PERIOD FOR SYSTEM_TIME", "UNSUPPORTED", "DROP PERIOD FOR SYSTEM_TIME"),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [A] ADD HIDDEN",
            "UNSUPPORTED",
            "HIDDEN flag of a period column",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [A] DROP HIDDEN",
            "UNSUPPORTED",
            "HIDDEN flag of a period column",
        ),
    ],
)
def test_every_other_temporal_statement_is_refused_and_names_what_it_is(sql: str, code: str, says: str):
    error = rejected(sql)
    assert (error.code, says in error.message) == (code, True), error.message


def test_create_table_of_a_migration_reads_a_temporal_table_and_refuses_the_consistency_check():
    create = (
        "CREATE TABLE [dbo].[T] ([Id] int NOT NULL CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED, "
        "[A] datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL, "
        "[B] datetime2(7) GENERATED ALWAYS AS ROW END HIDDEN NOT NULL, PERIOD FOR SYSTEM_TIME ([A], [B])) "
        "WITH (SYSTEM_VERSIONING = ON ({}))"
    )
    operation = parse_statement(create.format(HISTORY))
    assert isinstance(operation, CreateTable)
    assert operation.table.temporal == Temporal("A", "B", "dbo", "T_History")
    assert [c.hidden for c in operation.table.columns] == [False, True, True]
    error = rejected(create.format(HISTORY + ", DATA_CONSISTENCY_CHECK = OFF"))
    assert error.code == "UNSUPPORTED" and "DATA_CONSISTENCY_CHECK in CREATE TABLE" in error.message


# ------------------------------------------------------------------ dynamic data masking
def test_add_masked_and_drop_masked_are_operations_of_their_own_and_keep_the_function_as_text():
    add = parse_statement(
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Phone] ADD MASKED WITH (FUNCTION = 'partial(1, \"X''x\", 0)');"
    )
    drop = parse_statement("alter table dbo.T alter column Phone drop masked")

    # the text between the quotes as written: the engine stores the doubled quote as two characters
    assert add == masking.MaskColumn("dbo", "T", "Phone", "partial(1, \"X''x\", 0)")
    assert drop == masking.UnmaskColumn("dbo", "T", "Phone")
    # the engine reads a plain literal and an N literal alike
    assert parse_statement(
        "ALTER TABLE [dbo].[T] ALTER COLUMN [Phone] ADD MASKED WITH (FUNCTION = N'default()')"
    ) == (masking.MaskColumn("dbo", "T", "Phone", "default()"))


def test_a_column_is_added_with_its_mask_in_the_place_where_the_engine_reads_it():
    op = parse_statement(
        "ALTER TABLE [dbo].[T] ADD [a] varchar(9) MASKED WITH (FUNCTION = 'default()') NULL;"
    )

    assert isinstance(op, AddColumn) and op.column.masked == "default()"
    for misplaced in (
        "ALTER TABLE [dbo].[T] ADD [a] varchar(9) NULL MASKED WITH (FUNCTION = 'default()');",
        "ALTER TABLE [dbo].[T] ADD [a] int IDENTITY(1, 1) MASKED WITH (FUNCTION = 'default()') NOT NULL;",
        "ALTER TABLE [dbo].[T] ADD [a] int NOT NULL CONSTRAINT [DF] DEFAULT (0) "
        "MASKED WITH (FUNCTION = 'default()');",
    ):
        error = rejected(misplaced)
        assert error.code == "SYNTAX" and "must directly follow the data type" in error.message


@pytest.mark.parametrize(
    ("sql", "code", "says"),
    [
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = '');",
            "SYNTAX",
            "cannot be empty",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED (FUNCTION = 'default()');",
            "SYNTAX",
            "WITH after MASKED",
        ),
        ("ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED;", "SYNTAX", "WITH after MASKED"),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = @f);",
            "SYNTAX",
            "string literal",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = default());",
            "SYNTAX",
            "string literal",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = 'default()', ONLINE = ON);",
            "SYNTAX",
            "expected ')'",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = 'default()') "
            "WITH (ONLINE = ON);",
            "SYNTAX",
            "end of the statement",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] DROP MASKED WITH (ONLINE = ON);",
            "SYNTAX",
            "end of the statement",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] DROP MASKED, COLUMN [d];",
            "SYNTAX",
            "one action per statement",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = 'DEFAULT()');",
            "NF004",
            "'default()'",
        ),
        (
            "ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD MASKED WITH (FUNCTION = 'partial(1,\"x\",0)');",
            "NF004",
            "'partial(1, \"x\", 0)'",
        ),
        (
            "ALTER TABLE [dbo].[T] ADD [c] decimal(9, 2) MASKED WITH (FUNCTION = 'random(1, 5)') NULL;",
            "NF004",
            "'random(1.00, 5.00)'",
        ),
    ],
)
def test_a_masking_statement_outside_the_grammar_is_refused_with_its_reason(sql: str, code: str, says: str):
    error = rejected(sql)

    assert (error.code, says in error.message) == (code, True), error.message


# ------------------------------------------------------------------ wider table coverage
def test_alter_column_adds_and_drops_a_column_property():
    for word in ("ROWGUIDCOL", "SPARSE", "NOT FOR REPLICATION"):
        for verb in ("ADD", "DROP"):
            operation = parse_statement(f"ALTER TABLE [dbo].[T] ALTER COLUMN [c] {verb} {word};")
            assert operation == AlterColumnProperty("dbo", "T", "c", verb == "ADD", word)
    # no execution option goes with a property, and a word that is not a property is not read
    assert rejected("ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD SPARSE WITH (ONLINE = ON);").code == "SYNTAX"
    assert rejected("ALTER TABLE [dbo].[T] ALTER COLUMN [c] ADD NOT NULL;").code == "UNSUPPORTED"


def test_rebuild_states_the_compression_and_keeps_how_it_runs_apart():
    operation = parse_statement(
        "ALTER TABLE [dbo].[T] REBUILD WITH (SORT_IN_TEMPDB = ON, DATA_COMPRESSION = PAGE, MAXDOP = 2);"
    )
    assert operation == RebuildTable("dbo", "T", "PAGE", (("MAXDOP", "2"), ("SORT_IN_TEMPDB", "ON")))
    assert parse_statement("ALTER TABLE [dbo].[T] REBUILD WITH (DATA_COMPRESSION = NONE);") == RebuildTable(
        "dbo", "T", "NONE"
    )
    # a rebuild that states no compression is maintenance; RESUMABLE is not an option of REBUILD here
    assert "maintenance" in rejected("ALTER TABLE [dbo].[T] REBUILD;").message
    assert (
        rejected("ALTER TABLE [dbo].[T] REBUILD WITH (DATA_COMPRESSION = ROW, RESUMABLE = ON);").code
        == "SYNTAX"
    )
    assert rejected("ALTER TABLE [dbo].[T] REBUILD WITH (DATA_COMPRESSION = COLUMNSTORE);").code == "SYNTAX"


def test_a_columnstore_index_keeps_its_list_order_filter_and_options_apart():
    clustered = parse_statement(
        "CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON [dw].[Fact] ORDER ([d], [s]) "
        "WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE, MAXDOP = 2);"
    )
    assert isinstance(clustered, CreateIndex)
    index = clustered.index
    assert (index.columnstore, index.clustered, index.unique, index.included) == (True, True, False, ())
    assert [c.name for c in index.columns] == ["d", "s"]
    assert index.options == (("DATA_COMPRESSION", "COLUMNSTORE_ARCHIVE"),)
    assert clustered.exec_options == (("MAXDOP", "2"),)
    nonclustered = parse_statement(
        "CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI] ON [dw].[Fact] ([a], [b]) WHERE [a] > 0 "
        "WITH (COMPRESSION_DELAY = 30 MINUTES);"
    )
    assert isinstance(nonclustered, CreateIndex)
    index = nonclustered.index
    assert (index.columnstore, index.clustered, index.included, index.columns) == (
        True,
        False,
        ("a", "b"),
        (),
    )
    assert index.filter is not None and index.options == (("COMPRESSION_DELAY", "30"),)
    # a rowstore execution option that a columnstore build does not take
    sql = "CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON [dw].[Fact] WITH (SORT_IN_TEMPDB = ON);"
    assert rejected(sql).code == "SYNTAX"
    assert rejected("CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI] ON [dw].[Fact];").code == "SYNTAX"


def test_not_for_replication_and_xml_compression_are_read_in_alter_table():
    check = parse_statement("ALTER TABLE [dbo].[T] ADD CONSTRAINT [CK] CHECK NOT FOR REPLICATION ([a] > 0);")
    assert isinstance(check, AddConstraint) and isinstance(check.constraint, Check)
    assert check.constraint.not_for_replication is True
    key = parse_statement(
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [FK] FOREIGN KEY ([a]) REFERENCES [dbo].[P] ([a]) "
        "ON DELETE CASCADE NOT FOR REPLICATION;"
    )
    assert isinstance(key, AddConstraint) and isinstance(key.constraint, ForeignKey)
    assert (key.constraint.on_delete, key.constraint.not_for_replication) == ("CASCADE", True)
    column = parse_statement("ALTER TABLE [dbo].[T] ADD [Id] int IDENTITY NOT FOR REPLICATION NOT NULL;")
    assert isinstance(column, AddColumn) and column.column.identity == Identity(1, 1, True)
    primary = parse_statement(
        "ALTER TABLE [dbo].[T] ADD CONSTRAINT [PK] PRIMARY KEY CLUSTERED ([a]) WITH (XML_COMPRESSION = ON);"
    )
    assert isinstance(primary, AddConstraint) and isinstance(primary.constraint, PrimaryKey)
    assert primary.constraint.options == (("XML_COMPRESSION", "ON"),)
