"""The batch classifier: what one migration batch destroys, locks or must not hold.

It reads tokens only. Every batch here goes through chain.parse_migration first, as in the tool.
"""

import re

import pytest

from azsqlcd.chain import MigrationBatch, parse_migration
from azsqlcd.lint import BATCH_OBJECT, BatchFacts, classify_batch

FILE = "0001__change.sql"


def batches(body: str, mode: str = "tx") -> tuple[MigrationBatch, ...]:
    text = f"-- azsqlcd:migration 0001__change\n-- azsqlcd:mode {mode}\n{body}\n"
    return parse_migration(text, FILE).batches


def facts(body: str, mode: str = "tx", created: tuple[str, ...] = ()) -> BatchFacts:
    (batch,) = batches(body, mode)
    return classify_batch(batch, mode, created)


def data(body: str) -> BatchFacts:
    return facts("-- azsqlcd:data\n" + body)


def raw(body: str) -> BatchFacts:
    return facts("-- azsqlcd:raw TABLE:[audit].[Log] reason: outside the model\n" + body)


def items(found: BatchFacts) -> list[tuple[str, str]]:
    return [(item.code, item.object) for item in found.needs_allow]


def codes(found: BatchFacts) -> list[str]:
    return [finding.code for finding in found.findings]


# ------------------------------------------------------------------ model batch: the statement class
@pytest.mark.parametrize(
    ("sql", "statement"),
    [
        ("CREATE TABLE [s].[t] ([a] int NOT NULL)", "CREATE TABLE"),
        ("create unique nonclustered index [ix] on [s].[t] ([a])", "CREATE INDEX"),
        ("CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t]", "CREATE INDEX"),
        ("CREATE SEQUENCE [s].[q] AS int START WITH 1 INCREMENT BY 1", "CREATE SEQUENCE"),
        ("CREATE SCHEMA [s] AUTHORIZATION [dbo]", "CREATE SCHEMA"),
        ("CREATE TYPE [s].[tt] AS TABLE ([a] int NOT NULL)", "CREATE TYPE"),
        ("CREATE SYNONYM [dbo].[x] FOR [s].[t]", "CREATE SYNONYM"),
        ("ALTER TABLE [s].[t] ADD [a] int NULL", "ALTER TABLE"),
        ("ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF)", "ALTER TABLE"),
        ("ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[t_History]))", "ALTER TABLE"),
        ("ALTER TABLE [s].[t] ALTER COLUMN [a] DROP MASKED", "ALTER TABLE"),
        ("ALTER SEQUENCE [s].[q] RESTART WITH 1", "ALTER SEQUENCE"),
        ("DROP TABLE [s].[t]", "DROP TABLE"),
        ("DROP INDEX [ix] ON [s].[t]", "DROP INDEX"),
        ("DROP SEQUENCE [s].[q]", "DROP SEQUENCE"),
        ("DROP SCHEMA [s]", "DROP SCHEMA"),
        ("DROP TYPE [s].[tt]", "DROP TYPE"),
        ("DROP SYNONYM [dbo].[x]", "DROP SYNONYM"),
        ("EXEC sys.sp_rename N'[s].[t].[a]', N'b', N'COLUMN'", "EXEC sp_rename"),
        ("/* DROP TABLE x */ -- UPDATE y\nCREATE TABLE [s].[t] ([a] int NOT NULL)", "CREATE TABLE"),
    ],
)
def test_the_statement_class_comes_from_the_leading_tokens(sql, statement):
    found = facts(sql, mode="nontx")
    assert found.statement == statement
    assert "MODEL_STATEMENT" not in codes(found)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE [s].[t] SET [a] = 1 WHERE [b] = 2",
        "INSERT INTO [s].[t] ([a]) VALUES (1)",
        "DROP VIEW [s].[v]",
        "DROP PROCEDURE [s].[p]",
        "CREATE OR ALTER VIEW [s].[v] AS SELECT 1 AS [a]",
        "CREATE UNIQUE TABLE [s].[t] ([a] int NOT NULL)",
        "ALTER INDEX [ix] ON [s].[t] REBUILD",
        "ALTER TABLE [s].[t] SWITCH TO [s].[u]",
        "ALTER TABLE [s].[t] REBUILD",
        "ALTER TABLE [s].[t] NOCHECK CONSTRAINT [fk]",
        # SET has two forms in the list; the closed rule does not look inside parentheses, so
        # the option list is read whole
        "ALTER TABLE [s].[t] SET (LOCK_ESCALATION = AUTO)",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF, FILESTREAM_ON = [fs])",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF (HISTORY_TABLE = [s].[h]))",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[h], LEDGER = ON))",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = (SELECT 1)))",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_RETENTION_PERIOD = 3 FORTNIGHTS))",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[h]), LOCK_ESCALATION = AUTO)",
        "ALTER TABLE [s].[t] SET SYSTEM_VERSIONING = OFF",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] ADD MASKED",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] ADD MASKED WITH (FUNCTION = @f)",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] ADD MASKED WITH (FUNCTION = 'default()', ONLINE = ON)",
        "EXEC [s].[usp_Fix]",
        "EXEC sys.sp_refreshview N'[s].[v]'",
        "SELECT 1",
        "DECLARE @a int",
    ],
)
def test_a_model_batch_outside_the_closed_list_is_an_error(sql):
    found = facts(sql)
    assert codes(found) == ["MODEL_STATEMENT"]
    assert found.needs_allow == ()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP INDEX [ix] ON [s].[t]; DROP TABLE [s].[t];",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nDROP TABLE [s].[u]",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nUPDATE [s].[t] SET [a] = 1",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nDELETE FROM [s].[t]",
        "CREATE TABLE [s].[t] ([a] int NOT NULL)\nINSERT INTO [s].[t] ([a]) VALUES (1)",
        "CREATE TABLE [s].[t] ([a] int NOT NULL)\nCREATE TABLE [s].[u] ([a] int NOT NULL)",
        "ALTER TABLE [s].[t] DROP COLUMN [a]\nALTER TABLE [s].[t] DROP COLUMN [b]",
        "DROP TABLE [s].[t]\nIF 1 = 1 SELECT 1",
        "CREATE SCHEMA [s]\nEXEC [s].[usp_Fix]",
        "DROP SYNONYM [dbo].[x]\nTRUNCATE TABLE [s].[t]",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF)\nDROP TABLE [s].[t]",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF) [s].[usp_Fix]",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[h]))\nDELETE FROM [s].[h]",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = OFF); ALTER TABLE [s].[t] DROP COLUMN [a]",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] DROP MASKED\nDROP TABLE [s].[t]",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] DROP MASKED, COLUMN [b]",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] ADD MASKED WITH (FUNCTION = 'default()') [s].[usp_Fix]",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nSET NOCOUNT ON\nDELETE FROM [s].[t]",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nSET IDENTITY_INSERT [s].[t] ON",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nWAITFOR DELAY '00:00:05'",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nTHROW 50000, 'x', 1",
        "CREATE SEQUENCE [s].[q] AS int START WITH 1 INCREMENT BY 1\nKILL 55",
    ],
)
def test_a_second_statement_cannot_hide_behind_the_first(sql):
    assert "MODEL_STATEMENT" in codes(facts(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [s].[t] WITH NOCHECK ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [s].[u] ([a]) "
        "ON DELETE CASCADE ON UPDATE SET NULL",
        "ALTER TABLE [s].[t] ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [s].[u] ([a]) "
        "ON DELETE SET DEFAULT ON UPDATE NO ACTION",
        "ALTER TABLE [s].[t] ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [s].[u] ([a]) "
        "ON UPDATE CASCADE ON DELETE NO ACTION",
        "ALTER TABLE [s].[t] DROP COLUMN IF EXISTS [a]",
        "DROP TABLE IF EXISTS [s].[t]",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] int NOT NULL",
        "ALTER TABLE [s].[t] ADD [a] int NOT NULL CONSTRAINT [df] DEFAULT ((SELECT 1));",
        "CREATE TABLE [s].[t] ([a] int NOT NULL, [update] AS ([a] + 1)) WITH (DATA_COMPRESSION = PAGE);",
    ],
)
def test_one_statement_with_words_of_other_statements_inside_is_one_statement(sql):
    assert "MODEL_STATEMENT" not in codes(facts(sql))


def test_model_batch_with_set_and_delete_is_a_second_statement():
    """SET NOCOUNT ON ends with ON. The DELETE after it is a statement, not ON DELETE of a foreign
    key, and SET is a statement unless it is the action ON DELETE SET NULL."""
    found = facts("ALTER TABLE [dbo].[A] ADD [c] int NULL\nSET NOCOUNT ON\nDELETE FROM [dbo].[T]")
    assert codes(found) == ["MODEL_STATEMENT"]
    assert found.findings[0].line == 4  # the SET line
    assert "MODEL_STATEMENT" in codes(
        facts("ALTER TABLE [dbo].[A] ADD [c] int NULL ON\nDELETE FROM [dbo].[T]")
    )
    assert "MODEL_STATEMENT" in codes(facts("DROP INDEX [ix] ON\nUPDATE [dbo].[T] SET [a] = 1"))


_SWITCH = "DISABLE TRIGGER [dbo].[trg_audit] ON [dbo].[t]"


@pytest.mark.parametrize(
    "sql",
    [
        # TQ-01: ENABLE and DISABLE are not reserved words and were in no list of statement words
        "ALTER TABLE [dbo].[t] ADD [c] int NULL\n" + _SWITCH,
        "CREATE TABLE [dbo].[t] ([a] int NOT NULL)\nDISABLE TRIGGER ALL ON DATABASE",
        "CREATE INDEX [ix] ON [dbo].[new] ([a])\nENABLE TRIGGER ALL ON [dbo].[t]",
        "CREATE INDEX [ix] ON [s].[t] ([a]) WHERE [a] > 0\n" + _SWITCH,
        "CREATE INDEX [ix] ON [s].[t] ([a]) WHERE [a] IS NOT NULL\n" + _SWITCH,
        "DROP TABLE [s].[t]\nENABLE TRIGGER [trg] ON [s].[u]",
        "DROP INDEX [ix] ON [s].[t]\n" + _SWITCH,
        "ALTER TABLE [s].[t] DROP COLUMN [a]\n" + _SWITCH,
        "ALTER TABLE [s].[t] DROP CONSTRAINT [ck]\n" + _SWITCH,
        "ALTER TABLE [s].[t] ALTER COLUMN [a] int NOT NULL\n" + _SWITCH,
        "ALTER TABLE [s].[t] ADD [a] int NOT NULL DEFAULT 0\n" + _SWITCH,
        "ALTER TABLE [s].[t] ADD [a] datetime NOT NULL DEFAULT getdate()\n" + _SWITCH,
        "ALTER TABLE [s].[t] ADD [a] nvarchar(1) NOT NULL DEFAULT N'x'\n" + _SWITCH,
        "ALTER SEQUENCE [s].[q] RESTART WITH 1\nDISABLE TRIGGER ALL ON DATABASE",
        "CREATE SEQUENCE [s].[q] AS int\n" + _SWITCH,
        "CREATE SYNONYM [dbo].[x] FOR [s].[t]\n" + _SWITCH,
        "CREATE SCHEMA [s]\nDISABLE TRIGGER ALL ON DATABASE",
        "CREATE SCHEMA [s] AUTHORIZATION [dbo]\nDISABLE TRIGGER ALL ON DATABASE",
        "CREATE TYPE [s].[tt] FROM int NOT NULL\n" + _SWITCH,
        "EXEC sys.sp_rename N'[s].[t].[a]', N'b', N'COLUMN'\nDISABLE TRIGGER ALL ON DATABASE",
        # IF EXISTS is a part of DROP ... IF EXISTS, so the word IF alone decides nothing
        "DROP TABLE [s].[t]\nIF EXISTS (SELECT 1 FROM [s].[u]) " + _SWITCH,
        # statements that start with a word of the model grammar, a name, a label or a variable
        "ALTER TABLE [s].[t] ADD [a] int NULL\n"
        "ADD SENSITIVITY CLASSIFICATION TO [s].[t].[a] WITH (LABEL = 'x')",
        "ALTER TABLE [s].[t] ADD [a] int NULL\n[dbo].[usp_x]",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nmy_label:",
        "ALTER TABLE [s].[t] ADD [a] int NULL\n@x = 1",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nRECONFIGURE",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nSHUTDOWN",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nBREAK",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nSETUSER",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nGET CONVERSATION GROUP @g FROM [q]",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nSET LOCK_TIMEOUT -1",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nSET DEADLOCK_PRIORITY HIGH",
        "ALTER TABLE [s].[t] ADD [a] int NULL\nDBCC FREEPROCCACHE",
        # a query in parentheses is a statement of its own
        "DROP TABLE [s].[t]\n(SELECT [dbo].[fn_x]())",
        "CREATE SYNONYM [dbo].[x] FOR [s].[t]\n(SELECT 1)",
        "ALTER TABLE [s].[t] DROP COLUMN [a]\n(SELECT 1)",
        "CREATE TABLE [s].[t] ([a] int NOT NULL)\n(SELECT 1)",
        "ALTER TABLE [s].[t] ADD [a] int NOT NULL DEFAULT 0\n(SELECT 1)",
        # the first statement is not complete; the text is still not one statement of the list
        "ALTER TABLE [s].[t] ADD [a] int NULL,\n" + _SWITCH,
        "ALTER TABLE [s].[t] ADD [a]\n" + _SWITCH,
    ],
)
def test_a_model_batch_with_a_second_statement_and_no_semicolon_is_refused(sql):
    """TQ-01. The rule is closed: outside parentheses every token needs a place in a statement of
    the closed list. A list of the words that start a statement can never be complete, because
    T-SQL needs no ';' and ENABLE, DISABLE and others are not reserved words."""
    found = facts(sql, mode="nontx")
    # one finding, at the line of the second statement
    assert [f.line for f in found.findings if f.code == "MODEL_STATEMENT"] == [4]


def test_what_follows_a_semicolon_is_a_second_statement_at_its_own_line():
    found = facts("ALTER TABLE [s].[t] ADD [a] int NULL;\nENABLE TRIGGER [trg] ON [s].[t]")
    assert [(f.code, f.line) for f in found.findings] == [("MODEL_STATEMENT", 4)]
    assert "ENABLE starts a second statement" in found.findings[0].message
    assert codes(facts("ALTER TABLE [s].[t] ADD [a] int NULL;;")) == ["MODEL_STATEMENT"]
    assert codes(facts("ALTER TABLE [s].[t] ADD [a] int NULL;")) == []


@pytest.mark.parametrize(
    "sql",
    [
        # names without brackets, also names that are words of the grammar and not reserved
        "ALTER TABLE dbo.Settings ADD Value nvarchar(100) NULL, Start datetime2(0) NULL, "
        "Action int NOT NULL DEFAULT 0, Disable bit NULL",
        "ALTER TABLE s.t ADD c int NOT NULL CONSTRAINT df DEFAULT (0) WITH VALUES, "
        "d nvarchar(10) COLLATE Latin1_General_CI_AS NULL",
        "ALTER TABLE s.t ADD c AS (a + b) PERSISTED NOT NULL",
        "ALTER TABLE s.t ADD c int IDENTITY(1,1) NOT NULL",
        "ALTER TABLE s.t ADD c uniqueidentifier ROWGUIDCOL NOT NULL CONSTRAINT df DEFAULT newid()",
        "ALTER TABLE s.t ADD c dbo.MyType NULL",
        "ALTER TABLE s.t ADD [c] [dbo].[MyType] NULL, [d] [int] NULL",
        "ALTER TABLE s.t ADD c double precision NULL",
        "ALTER TABLE s.t ADD ValidFrom datetime2 GENERATED ALWAYS AS ROW START HIDDEN NOT NULL "
        "DEFAULT sysutcdatetime(), ValidTo datetime2 GENERATED ALWAYS AS ROW END HIDDEN NOT NULL "
        "DEFAULT CONVERT(datetime2, '9999-12-31'), PERIOD FOR SYSTEM_TIME (ValidFrom, ValidTo)",
        "ALTER TABLE s.t ADD c nvarchar(10) MASKED WITH (FUNCTION = 'default()') NULL",
        "ALTER TABLE s.t ADD c int NULL DEFAULT NEXT VALUE FOR s.q",
        "ALTER TABLE s.t ADD c int NOT NULL DEFAULT -1",
        "ALTER TABLE s.t ADD c nvarchar(5) NOT NULL DEFAULT N'a' + N'b'",
        "ALTER TABLE s.t ADD c datetime NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE s.t ADD CONSTRAINT df DEFAULT 0 FOR c",
        "ALTER TABLE s.t ADD CONSTRAINT pk PRIMARY KEY CLUSTERED (a) WITH (ONLINE = ON) ON [PRIMARY]",
        "ALTER TABLE s.t ADD CONSTRAINT pk PRIMARY KEY (a) WITH FILLFACTOR = 90",
        "ALTER TABLE s.t WITH CHECK ADD CONSTRAINT ck CHECK NOT FOR REPLICATION (a > 0)",
        "ALTER TABLE s.t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES s.u (a) "
        "ON DELETE CASCADE NOT FOR REPLICATION",
        "ALTER TABLE s.t ADD c int NULL CONSTRAINT fk REFERENCES s.u (id) ON UPDATE SET DEFAULT",
        "ALTER TABLE s.t ADD INDEX ix NONCLUSTERED (a)",
        "ALTER TABLE s.t ADD c int SPARSE NULL, cs xml COLUMN_SET FOR ALL_SPARSE_COLUMNS",
        "ALTER TABLE s.t ALTER COLUMN c nvarchar(20) COLLATE Latin1_General_CI_AS NOT NULL "
        "WITH (ONLINE = ON)",
        "ALTER TABLE s.t ALTER COLUMN c ADD PERSISTED",
        "ALTER TABLE s.t ALTER COLUMN c ADD MASKED WITH (FUNCTION = 'default()')",
        "ALTER TABLE s.t ALTER COLUMN c ADD MASKED WITH (FUNCTION = 'partial(1, \"XXX\", 0)');",
        "ALTER TABLE s.t ALTER COLUMN c DROP MASKED;",
        "alter table s.t alter column c drop masked",
        "ALTER TABLE s.t SET (SYSTEM_VERSIONING = OFF);",
        "ALTER TABLE s.t SET (SYSTEM_VERSIONING = ON)",
        "ALTER TABLE [s].[t] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[t_History], "
        "HISTORY_RETENTION_PERIOD = 30 DAYS));",
        "alter table s.t set (system_versioning = on\n    (history_table = s.t_History, "
        "data_consistency_check = off, history_retention_period = 1 week))",
        "ALTER TABLE s.t SET (SYSTEM_VERSIONING = ON (DATA_CONSISTENCY_CHECK = ON, HISTORY_TABLE = s.h, "
        "HISTORY_RETENTION_PERIOD = INFINITE))",
        "CREATE TABLE [s].[t] (\n    [Id] int NOT NULL,\n"
        "    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL,\n"
        "    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,\n"
        "    [Mail] nvarchar(100) MASKED WITH (FUNCTION = 'email()') NULL,\n"
        "    CONSTRAINT [PK_t] PRIMARY KEY CLUSTERED ([Id]),\n"
        "    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])\n)\n"
        "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[t_History], "
        "HISTORY_RETENTION_PERIOD = 2 YEARS));",
        "ALTER TABLE s.t ALTER COLUMN c dbo.MyType NULL",
        "ALTER TABLE s.t ALTER COLUMN [c] [nvarchar](20) NOT NULL;",
        "ALTER TABLE s.t DROP CONSTRAINT IF EXISTS a, COLUMN b, c",
        "ALTER TABLE s.t DROP a",
        "ALTER TABLE s.t DROP PERIOD FOR SYSTEM_TIME",
        "ALTER TABLE s.t DROP CONSTRAINT pk WITH (ONLINE = ON)",
        "CREATE UNIQUE NONCLUSTERED INDEX ix ON s.t (a DESC) INCLUDE (b, c) WHERE a IS NOT NULL "
        "AND b IN (1, 2) AND c = N'x' AND d >= 5 WITH (ONLINE = ON) ON [PRIMARY]",
        "CREATE CLUSTERED COLUMNSTORE INDEX cci ON s.t ORDER (a) WITH (DATA_COMPRESSION = COLUMNSTORE)",
        "CREATE INDEX ix ON s.t (a) WHERE a BETWEEN 1 AND 5 OR b LIKE N'x%' OR NOT c = 1",
        "CREATE INDEX ix ON s.t (a) WHERE a > -1 AND b <> 0x00 AND c = CONVERT(date, '20200101')",
        "CREATE INDEX ix ON s.t (a) WHERE a > (0) AND b IN (1, 2)",
        "CREATE TABLE s.t (a int NOT NULL) ON ps (a) WITH (DATA_COMPRESSION = PAGE)",
        "CREATE TABLE s.t (a int NOT NULL, b varbinary(max) NULL) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY]",
        "CREATE TABLE s.n (a int NOT NULL) AS NODE",
        "CREATE SEQUENCE s.q AS decimal(18, 0) START WITH -5 INCREMENT BY 1 MINVALUE -5 NO MAXVALUE "
        "NO CYCLE CACHE 10",
        "ALTER SEQUENCE s.q RESTART WITH 1 INCREMENT BY 2 NO MINVALUE MAXVALUE 100 CYCLE NO CACHE",
        "CREATE SCHEMA s AUTHORIZATION dbo",
        "CREATE TYPE s.tt FROM nvarchar(10) NOT NULL",
        "CREATE TYPE s.tt AS TABLE (a int NOT NULL) WITH (MEMORY_OPTIMIZED = OFF)",
        "CREATE SYNONYM dbo.x FOR s.t",
        "DROP INDEX IF EXISTS ix ON s.t WITH (ONLINE = ON)",
        "DROP INDEX ix ON s.t, ix2 ON s.u",
        "DROP INDEX s.t.ix",
        "DROP TABLE IF EXISTS s.t, s.u;",
        "DROP SYNONYM IF EXISTS dbo.x",
        "DROP SEQUENCE s.q",
        "DROP TYPE IF EXISTS s.tt",
        "DROP SCHEMA IF EXISTS s",
        "EXEC sys.sp_rename N'[s].[t].[a]', N'b', N'COLUMN';",
        "EXECUTE sp_rename @objname = N'[s].[t]', @newname = N'u'",
        "EXEC sys.sp_rename N'[s].[t]', N'u', 'OBJECT'",
    ],
)
def test_one_statement_of_the_closed_list_has_a_place_for_each_of_its_tokens(sql):
    """The near miss of the closed rule: it reads tokens, not a grammar, and must not refuse a
    statement of the list for a name without brackets or for an option of the statement."""
    assert "MODEL_STATEMENT" not in codes(facts(sql, mode="nontx"))


# ------------------------------------------------------------------ model batch: items that need an allow
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("DROP TABLE [sales].[Old]", [("DROP_TABLE", "[sales].[Old]")]),
        ("drop table sales.Old", [("DROP_TABLE", "[sales].[Old]")]),
        ('DROP TABLE IF EXISTS "sales"."Old"', [("DROP_TABLE", "[sales].[Old]")]),
        ("DROP TABLE [sales].[a]]b]", [("DROP_TABLE", "[sales].[a]]b]")]),
        ("DROP SEQUENCE [sales].[OrderNo]", [("DROP_SEQUENCE", "[sales].[OrderNo]")]),
        ("DROP TYPE [sales].[Line_tt]", [("DROP_TYPE", "[sales].[Line_tt]")]),
        ("DROP SCHEMA [staging]", [("DROP_SCHEMA", "[staging]")]),
        ("ALTER TABLE [sales].[Order] DROP COLUMN [Stat]", [("DROP_COLUMN", "[sales].[Order].[Stat]")]),
        (
            "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(400) NULL",
            [("ALTER_COLUMN_LOSSY", "[sales].[Order].[Note]")],
        ),
        (
            "EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN'",
            [("RENAME", "[sales].[Order].[Stat]")],
        ),
        ("EXECUTE sp_rename 'sales.Order', 'Orders'", [("RENAME", "[sales].[Order]")]),
        ("DROP SYNONYM [dbo].[x]", []),
        ("CREATE TABLE [sales].[New] ([a] int NOT NULL)", []),
        # the engine stops writing history: declared. Switching it on destroys nothing
        ("ALTER TABLE [sales].[Price] SET (SYSTEM_VERSIONING = OFF)", [("TEMPORAL_OFF", "[sales].[Price]")]),
        ("alter table sales.Price set (system_versioning = off);", [("TEMPORAL_OFF", "[sales].[Price]")]),
        ("ALTER TABLE [sales].[Price] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [sales].[Price_H]))", []),
        # every reader sees the real values after DROP MASKED: declared. No stored value changes,
        # so neither form is ALTER_COLUMN_LOSSY
        (
            "ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] DROP MASKED",
            [("UNMASK", "[sales].[Buyer].[Mail]")],
        ),
        ("alter table sales.Buyer alter column Mail drop masked;", [("UNMASK", "[sales].[Buyer].[Mail]")]),
        ("ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] ADD MASKED WITH (FUNCTION = 'email()')", []),
        # a column that is named MASKED is still an ALTER COLUMN of the usual kind
        (
            "ALTER TABLE [sales].[Buyer] ALTER COLUMN [MASKED] bit NOT NULL",
            [("ALTER_COLUMN_LOSSY", "[sales].[Buyer].[MASKED]")],
        ),
        (
            "ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] ADD PERSISTED",
            [("ALTER_COLUMN_LOSSY", "[sales].[Buyer].[Mail]")],
        ),
    ],
)
def test_a_destructive_statement_reports_its_code_and_object(sql, expected):
    assert items(facts(sql, mode="nontx")) == expected


def test_a_drop_of_several_tables_reports_each_table():
    found = facts("DROP TABLE [s].[a],\n  [s].[b], c")
    assert items(found) == [("DROP_TABLE", "[s].[a]"), ("DROP_TABLE", "[s].[b]"), ("DROP_TABLE", "[c]")]
    assert [item.line for item in found.needs_allow] == [3, 4, 4]


def test_a_drop_list_of_alter_table_separates_columns_from_constraints():
    found = facts("ALTER TABLE [s].[t] DROP COLUMN [a], [b], CONSTRAINT [ck], [df], COLUMN IF EXISTS [c]")
    assert items(found) == [
        ("DROP_COLUMN", "[s].[t].[a]"),
        ("DROP_COLUMN", "[s].[t].[b]"),
        ("DROP_COLUMN", "[s].[t].[c]"),
    ]
    assert codes(found) == ["DROP_CONSTRAINT", "DROP_CONSTRAINT"]
    assert {finding.severity for finding in found.findings} == {"warning"}


def test_drop_without_the_word_column_is_a_constraint_drop():
    found = facts("ALTER TABLE [s].[t] DROP [ck_or_column]")
    assert (items(found), codes(found)) == ([], ["DROP_CONSTRAINT"])


def test_drop_index_is_a_warning_and_needs_no_allow_line():
    found = facts("DROP INDEX [ix] ON [s].[t]")
    assert (items(found), [(f.code, f.severity) for f in found.findings]) == ([], [("DROP_INDEX", "warning")])


def test_every_alter_column_needs_an_allow_line_in_v0_1():
    """The lexer cannot tell a widening from a narrowing, so each one is declared."""
    for definition in (
        "int NOT NULL",
        "nvarchar(max) NULL",
        "bigint NULL",
        "varchar(10) COLLATE Latin1_General_CI_AS",
    ):
        found = facts(f"ALTER TABLE [s].[t] ALTER COLUMN [a] {definition}")
        assert items(found) == [("ALTER_COLUMN_LOSSY", "[s].[t].[a]")]


@pytest.mark.parametrize(
    "sql",
    [
        "EXEC sys.sp_rename @objname = N'[s].[t].[a]', @newname = N'b'",
        "EXEC sys.sp_rename @old, N'b'",
        "EXEC sys.sp_rename N'sales.Order Line.Stat', N'Status', N'COLUMN'",
        "DROP TABLE",
        "DROP TABLE @t",
        "ALTER TABLE [s].[t] ALTER COLUMN",
        "ALTER TABLE [s].[t] DROP COLUMN",
        "CREATE INDEX [ix] ([a])",
    ],
)
def test_a_statement_whose_object_cannot_be_read_is_an_error_not_a_guess(sql):
    found = facts(sql)
    assert "STATEMENT_UNREADABLE" in codes(found)
    assert found.needs_allow == ()


# ------------------------------------------------------------------ long locks
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX [ix] ON [sales].[Order] ([a])",
        "CREATE UNIQUE CLUSTERED INDEX [ix] ON sales.[Order] ([a])",
        "CREATE NONCLUSTERED COLUMNSTORE INDEX [ix] ON [sales].[Order] ([a])",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [pk] PRIMARY KEY CLUSTERED ([a])",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [uq] UNIQUE NONCLUSTERED ([a])",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [ck] CHECK ([a] > 0)",
        "ALTER TABLE [sales].[Order] WITH CHECK ADD CONSTRAINT [ck] CHECK ([a] > 0)",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [sales].[c] ([a])",
        "ALTER TABLE [sales].[Order] ADD [a] int NULL CONSTRAINT [fk] REFERENCES [sales].[c] ([a])",
        "ALTER TABLE [sales].[Order] ADD [a] int NULL CONSTRAINT [uq] UNIQUE",
        "ALTER TABLE [sales].[Order] WITH NOCHECK ADD CONSTRAINT [pk] PRIMARY KEY CLUSTERED ([a])",
    ],
)
def test_a_statement_that_reads_every_row_under_a_lock_needs_long_lock_in_a_tx_migration(sql):
    assert items(facts(sql, mode="tx")) == [("LONG_LOCK", "[sales].[Order]")]
    assert items(facts(sql, mode="nontx")) == []


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [sales].[Order] WITH NOCHECK ADD CONSTRAINT [ck] CHECK ([a] > 0)",
        "ALTER TABLE [sales].[Order] WITH NOCHECK ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [s].[c]",
        "ALTER TABLE [sales].[Order] ADD [a] int NULL",
        "ALTER TABLE [sales].[Order] ADD [a] int NOT NULL CONSTRAINT [df] DEFAULT (0)",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [df] DEFAULT ('CHECK UNIQUE') FOR [a]",
        "ALTER TABLE [sales].[Order] ADD [check] AS ([a] + 1), [unique] int NULL",
        "CREATE TABLE [sales].[Order] ([a] int NOT NULL CONSTRAINT [pk] PRIMARY KEY CLUSTERED)",
    ],
)
def test_a_statement_that_reads_no_rows_needs_no_long_lock(sql):
    assert items(facts(sql, mode="tx")) == []


def test_a_table_that_the_same_migration_created_takes_no_long_lock():
    create, index, other = batches(
        "CREATE TABLE [sales].[Refund] ([a] int NOT NULL)\nGO\n"
        'CREATE INDEX [ix] ON "SALES".refund ([a])\nGO\n'
        "CREATE INDEX [ix] ON [sales].[Order] ([a])"
    )
    created = classify_batch(create, "tx").creates_tables
    assert created == ("[sales].[Refund]",)
    assert items(classify_batch(index, "tx", created)) == []
    assert items(classify_batch(index, "tx")) == [("LONG_LOCK", "[SALES].[refund]")]
    assert items(classify_batch(other, "tx", created)) == [("LONG_LOCK", "[sales].[Order]")]


def test_an_online_build_in_a_nontx_migration_is_asked_to_wait_at_low_priority():
    build = "CREATE INDEX [ix] ON [s].[t] ([a]) WITH (ONLINE = ON"
    wait = " (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)))"
    without = facts(build + ")", mode="nontx")
    assert [(f.code, f.severity, f.line) for f in without.findings] == [("NTX004", "warning", 3)]
    assert codes(facts(build + wait, mode="nontx")) == []
    assert codes(facts("CREATE INDEX [ix] ON [s].[t] ([a]) WITH (ONLINE = OFF)", mode="nontx")) == []
    assert "NTX004" not in codes(facts(build + ")", mode="tx"))


def test_online_alter_column_gets_no_ntx004():
    """The engine refuses WAIT_AT_LOW_PRIORITY on an online ALTER COLUMN, so the rule cannot ask for it."""
    alter = "ALTER TABLE [dbo].[t] ALTER COLUMN [x] bigint NOT NULL WITH (ONLINE = ON)"
    assert codes(facts(alter, mode="nontx")) == []
    # the rule still holds for a key that the same statement class builds online
    key = "ALTER TABLE [dbo].[t] ADD CONSTRAINT [pk] PRIMARY KEY CLUSTERED ([x]) WITH (ONLINE = ON)"
    assert codes(facts(key, mode="nontx")) == ["NTX004"]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX [ix] ON [s].[t] ([a]) WITH (ONLINE = ON, RESUMABLE = ON)",
        "CREATE INDEX [ix] ON [s].[t] ([a]) WITH (resumable=on, MAX_DURATION = 60 MINUTES, ONLINE = ON)",
        "ALTER TABLE [s].[t] ADD CONSTRAINT [pk] PRIMARY KEY CLUSTERED ([a]) "
        "WITH (ONLINE = ON, RESUMABLE = ON)",
    ],
)
def test_resumable_is_refused_in_a_transactional_migration(sql):
    """The engine refuses RESUMABLE = ON inside an explicit transaction; a tx migration runs in one."""
    found = facts(sql, mode="tx")
    assert codes(found) == ["NTX006"]
    assert found.findings[0].severity == "error"
    assert "NTX006" not in codes(facts(sql, mode="nontx"))
    off = re.sub(r"(?i)resumable\s*=\s*on", "RESUMABLE = OFF", sql)
    assert "NTX006" not in codes(facts(off, mode="tx"))


def test_resumable_is_refused_in_a_raw_batch_of_a_transactional_migration():
    rebuild = "ALTER INDEX [ix] ON [audit].[Log] REBUILD WITH (ONLINE = ON, RESUMABLE = ON)"
    assert codes(raw(rebuild)) == ["NTX006"]
    assert codes(raw("SELECT N'RESUMABLE = ON' -- RESUMABLE = ON")) == []


# ------------------------------------------------------------------ data batch
@pytest.mark.parametrize(
    ("sql", "objects"),
    [
        ("UPDATE [s].[t] SET [a] = 1", ["[s].[t]"]),
        ("DELETE FROM [s].[t]", ["[s].[t]"]),
        ("DELETE [s].[t]", ["[s].[t]"]),
        ("DELETE TOP (10) PERCENT FROM s.t", ["[s].[t]"]),
        ("UPDATE TOP ((SELECT 5)) [s].[t] SET [a] = 1", ["[s].[t]"]),
        ("UPDATE o SET [a] = 1 FROM [s].[t] AS o JOIN [s].[u] AS u ON u.[id] = o.[id]", ["[o]"]),
        ("DELETE FROM @rows", ["@rows"]),
        ("UPDATE [s].[t] SET [a] = (SELECT MAX([b]) FROM [s].[u] WHERE [c] = 1)", ["[s].[t]"]),
        ("DELETE FROM [s].[t] OUTPUT deleted.[a] INTO @log", ["[s].[t]"]),
        ("UPDATE [s].[t] SET [a] = 1\nSELECT [a] FROM [s].[t] WHERE [a] = 1", ["[s].[t]"]),
        ("DELETE FROM [s].[t]\nDELETE FROM [s].[u] WHERE [a] = 1", ["[s].[t]"]),
        ("DELETE FROM [s].[t]; DELETE FROM [s].[u] WHERE [a] = 1", ["[s].[t]"]),
        ("IF @n > 0 DELETE FROM [s].[t] ELSE SELECT 1 WHERE 1 = 1", ["[s].[t]"]),
        (
            "BEGIN UPDATE [s].[t] SET [a] = CASE WHEN [b] = 1 THEN 2 END END\nSELECT 1 WHERE 1 = 1",
            ["[s].[t]"],
        ),
        ("WITH d AS (SELECT TOP (10) * FROM [s].[t] WHERE [a] = 1) DELETE FROM d", ["[d]"]),
        ("DELETE FROM [s].[t]; UPDATE [s].[u] SET [a] = 1;", ["[s].[t]", "[s].[u]"]),
        ("UPDATE [s].[t] SET [a] = 1 WHERE [b] = 2", []),
        ("DELETE FROM [s].[t] WHERE [b] = 2", []),
        ("UPDATE [s].[t] SET [a] = CASE WHEN [b] = 1 THEN 2 ELSE 3 END WHERE [c] = 4", []),
        ("UPDATE [s].[t] SET [a] = 1 FROM [s].[t] JOIN (SELECT 1 AS x) AS q ON 1 = 1 WHERE [b] = 2", []),
        ("DELETE FROM [s].[t] WHERE CURRENT OF cur", []),
        ("UPDATE STATISTICS [s].[t]", []),
        ("DECLARE cur CURSOR FOR SELECT [a] FROM [s].[t] FOR UPDATE OF [a]", []),
        (
            "MERGE [s].[t] AS t USING [s].[u] AS u ON t.[id] = u.[id] "
            "WHEN MATCHED THEN UPDATE SET t.[a] = u.[a] WHEN NOT MATCHED BY SOURCE THEN DELETE;",
            [],
        ),
    ],
)
def test_update_and_delete_need_a_where_of_their_own(sql, objects):
    found = data(sql)
    assert items(found) == [("DATA_NO_WHERE", obj) for obj in objects]
    assert codes(found) == []


@pytest.mark.parametrize(
    ("sql", "obj"),
    [
        ("SET NOCOUNT ON\nDELETE FROM [dbo].[Customer]", "[dbo].[Customer]"),
        ("SET NOCOUNT ON\nUPDATE [dbo].[Customer] SET [Email] = NULL", "[dbo].[Customer]"),
        ("SET IDENTITY_INSERT [s].[t] ON DELETE [s].[t]", "[s].[t]"),
        ("SET ANSI_WARNINGS ON\nDELETE TOP (5) FROM [s].[t]", "[s].[t]"),
        ("SET NOCOUNT ON\nDELETE FROM [s].[t] OUTPUT deleted.[a] INTO @log", "[s].[t]"),
        # the words of a foreign key action do not make the statement an action
        ("SET NOCOUNT ON\nUPDATE [s].[no] SET [action] = 1", "[s].[no]"),
        ("SET NOCOUNT ON\nDELETE [cascade]", "[cascade]"),
    ],
)
def test_delete_without_where_after_set_option_on_needs_an_allow_line(sql, obj):
    """ON before DELETE or UPDATE is a foreign key action only with CASCADE, NO ACTION, SET NULL or
    SET DEFAULT after it. A statement that ends with ON hides nothing."""
    found = data(sql)
    assert items(found) == [("DATA_NO_WHERE", obj)]
    assert codes(found) == []
    assert items(data(sql + " WHERE [a] = 1")) == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("[dbo].[usp_purge_all] @confirm = 1;", [("EXEC_PROC", "[dbo].[usp_purge_all]")]),
        ("dbo.usp_purge_all", [("EXEC_PROC", "[dbo].[usp_purge_all]")]),
        ("usp_purge_all 1, 'x'", [("EXEC_PROC", "[usp_purge_all]")]),
        ('"dbo"."usp_purge_all"', [("EXEC_PROC", "[dbo].[usp_purge_all]")]),
        ("sp_addrolemember 'db_owner', 'someone'", [("EXEC_PROC", "[sp_addrolemember]")]),
        ("sys.sp_addrolemember 'db_owner', 'someone'", [("EXEC_PROC", "[sys].[sp_addrolemember]")]),
        ("-- a note\n/* and a block */\n[dbo].[usp_purge_all]", [("EXEC_PROC", "[dbo].[usp_purge_all]")]),
        (";[dbo].[usp_purge_all]", [("EXEC_PROC", "[dbo].[usp_purge_all]")]),
        ("retry: [dbo].[usp_purge_all]", [("EXEC_PROC", "[dbo].[usp_purge_all]")]),
        ("[select]", [("EXEC_PROC", "[select]")]),  # a quoted name is never a keyword
        ("disable.usp_x", [("EXEC_PROC", "[disable].[usp_x]")]),  # a schema with the name of a statement
        ("enable 1", [("EXEC_PROC", "[enable]")]),  # ENABLE is a statement only before TRIGGER
        ("sp_executesql N'DELETE FROM [s].[t]'", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        (
            "[dbo].[usp_a]; EXEC [dbo].[usp_b]",
            [("EXEC_PROC", "[dbo].[usp_a]"), ("EXEC_PROC", "[dbo].[usp_b]")],
        ),
    ],
)
def test_procedure_call_without_exec_as_first_statement_needs_exec_proc_allow(sql, expected):
    """T-SQL runs a procedure without EXECUTE when the call is the first statement of the batch; the
    directive line above it is a comment."""
    found = data(sql)
    assert items(found) == expected
    assert codes(found) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select [usp_x] from [s].[t]",
        "WITH d AS (SELECT 1 AS [a]) SELECT [a] FROM d",
        "SET NOCOUNT ON; SELECT 1",
        "DECLARE @a int = 1",
        "IF 1 = 1 SELECT 1",
        "BEGIN SELECT 1 END",
        "WHILE 1 = 0 BREAK",
        "PRINT N'x'",
        "THROW 50000, N'x', 1",
        "retry: SELECT 1",
        ";WITH d AS (SELECT 1 AS [a]) SELECT [a] FROM d",
        "INSERT INTO [s].[t] ([a]) VALUES (1)",
        "MERGE [s].[t] AS t USING [s].[u] AS u ON t.[a] = u.[a] WHEN MATCHED THEN UPDATE SET t.[b] = u.[b];",
        "WAITFOR DELAY '00:00:01'",
    ],
)
def test_a_data_batch_that_starts_with_a_statement_word_calls_no_procedure(sql):
    found = data(sql)
    assert items(found) == []
    assert codes(found) == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("TRUNCATE TABLE [s].[t]", [("TRUNCATE", "[s].[t]")]),
        ("EXEC [s].[usp_Fix] @a = 1", [("EXEC_PROC", "[s].[usp_Fix]")]),
        ("EXECUTE s.usp_Fix", [("EXEC_PROC", "[s].[usp_Fix]")]),
        ("DECLARE @rc int; EXEC @rc = usp_Fix", [("EXEC_PROC", "[usp_Fix]")]),
        ("INSERT INTO [s].[t] ([a]) EXEC [s].[usp_Rows]", [("EXEC_PROC", "[s].[usp_Rows]")]),
        ("EXEC ('DELETE FROM [s].[t]')", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        ("EXECUTE(@sql)", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        ("EXEC @procedure_name", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        ("EXEC sp_executesql @sql", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        ("EXEC [sys].[sp_executesql] @sql, N'@a int', @a = 1", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        ("EXEC (@a); EXEC (@b); EXEC sp_executesql @c", [("DYNAMIC_SQL", BATCH_OBJECT)]),
        ("INSERT INTO [s].[t] ([a]) VALUES (N'EXEC (x); TRUNCATE TABLE y')", []),
    ],
)
def test_truncate_procedure_calls_and_dynamic_sql_need_an_allow_line(sql, expected):
    found = data(sql)
    assert items(found) == expected
    assert codes(found) == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE #work ([a] int NOT NULL)",
        "ALTER TABLE [s].[t] ADD [a] int NULL",
        "DROP TABLE #work",
        "CREATE INDEX [ix] ON [s].[t] ([a])",
        "EXEC sys.sp_rename N'[s].[t].[a]', N'b', N'COLUMN'",
        "EXEC [sp_rename] 'a', 'b'",
    ],
)
def test_a_data_batch_holds_no_ddl(sql):
    found = data(sql)
    assert codes(found) == ["DATA_DDL"]
    assert found.needs_allow == ()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [a] INTO [s].[copy] FROM [s].[t]",
        "SELECT [a], (SELECT MAX([b]) FROM [s].[u]) AS [m] INTO copy FROM [s].[t]",
        "DISABLE TRIGGER [s].[tr] ON [s].[t]",
        "ENABLE TRIGGER ALL ON [s].[t]",
        "DBCC CHECKIDENT ('s.t', RESEED, 1)",
    ],
)
def test_a_data_batch_creates_no_table_switches_no_trigger_and_runs_no_dbcc(sql):
    assert codes(data(sql)) == ["DATA_FORBIDDEN"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [a] AS OUTPUT INTO [dbo].[Copy] FROM [dbo].[T]",
        "SELECT [a] OUTPUT INTO [dbo].[Copy] FROM [dbo].[T]",
        "SELECT [a] AS output, [b] INTO Copy FROM [dbo].[T]",
        "UPDATE [s].[t] SET [a] = 1 WHERE [b] = 2 SELECT [a] AS OUTPUT INTO [dbo].[Copy] FROM [s].[t]",
        "DELETE FROM [s].[t] WHERE [b] = 2; SELECT [a] AS OUTPUT INTO [dbo].[Copy] FROM [s].[t]",
    ],
)
def test_select_into_permanent_table_with_output_alias_is_data_forbidden(sql):
    """OUTPUT is not a reserved word, so it can be a column alias. INTO belongs to the OUTPUT clause
    only when INSERT, UPDATE, DELETE or MERGE stands before it with no SELECT between."""
    assert codes(data(sql)) == ["DATA_FORBIDDEN"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [a] INTO #open FROM [s].[t]",
        "SELECT [a] INTO [#open] FROM [s].[t]",
        "INSERT INTO [s].[t] ([a]) SELECT [a] FROM [s].[u]",
        "INSERT TOP (5) INTO [s].[t] ([a]) SELECT [a] FROM [s].[u]",
        "MERGE INTO [s].[t] AS t USING [s].[u] AS u ON t.[a] = u.[a] "
        "WHEN NOT MATCHED THEN INSERT ([a]) VALUES (u.[a]);",
        "UPDATE [s].[t] SET [a] = 1 OUTPUT inserted.[a] INTO [s].[log] ([a]) WHERE [b] = 2",
        "DELETE FROM [s].[t] OUTPUT deleted.[a] INTO [s].[log] ([a]) WHERE [b] = 2",
        "INSERT INTO [s].[t] ([a]) OUTPUT inserted.[a] INTO [s].[log] ([a]) SELECT [a] FROM [s].[u]",
        "MERGE [s].[t] AS t USING [s].[u] AS u ON t.[a] = u.[a] WHEN MATCHED THEN UPDATE SET t.[b] = u.[b] "
        "OUTPUT inserted.[a] INTO [s].[log] ([a]);",
        "SELECT [a] AS OUTPUT INTO #open FROM [s].[t]",
        "FETCH NEXT FROM cur INTO @a",
    ],
)
def test_into_of_another_statement_is_not_select_into(sql):
    assert codes(data(sql)) == []


# ------------------------------------------------------------------ every batch: forbidden tokens
FORBIDDEN = [
    "BEGIN TRANSACTION",
    "BEGIN TRAN",
    "begin tran t1",
    "BEGIN DISTRIBUTED TRANSACTION",
    "COMMIT",
    "COMMIT TRANSACTION",
    "ROLLBACK",
    "ROLLBACK TRAN",
    "SAVE TRANSACTION sp1",
    "SAVE TRAN sp1",
    "RAISERROR('x', 16, 1)",
    "RETURN",
    "GOTO done",
    "SET NOEXEC ON",
    "SET PARSEONLY ON",
    "SET XACT_ABORT OFF",
    "SET XACT_ABORT ON",
    "SET IMPLICIT_TRANSACTIONS ON",
    "SET ANSI_DEFAULTS ON",
    "SET ROWCOUNT 1",
    "SET FMTONLY ON",
    "USE [other]",
    ":r other.sql",
    "  :setvar x 1",
]


@pytest.mark.parametrize("sql", FORBIDDEN)
def test_a_token_that_takes_the_run_away_from_the_tool_is_forbidden_in_every_batch(sql):
    for found in (data(sql), raw(sql), facts(sql)):
        assert codes(found) == ["FORBIDDEN_TOKEN"]
        assert found.findings[0].severity == "error"


@pytest.mark.parametrize(
    "sql",
    [
        "SET NOCOUNT, XACT_ABORT OFF",
        "SET XACT_ABORT, NOCOUNT OFF",
        "SET ANSI_NULLS, NOEXEC ON",
        "SET NOCOUNT,PARSEONLY ON",
        "SET NOCOUNT, ANSI_WARNINGS, IMPLICIT_TRANSACTIONS ON",
        "set ansi_padding, ansi_defaults on",
        "SET /* x */ NOCOUNT -- y\n, XACT_ABORT ON",
    ],
)
def test_set_option_list_with_xact_abort_is_a_forbidden_token(sql):
    """One SET statement can hold a list of options; each option of the list is read."""
    for found in (data(sql), raw(sql), facts(sql)):
        assert codes(found) == ["FORBIDDEN_TOKEN"]


@pytest.mark.parametrize(
    "sql",
    [
        "SET NOCOUNT, ANSI_WARNINGS, ARITHABORT ON",
        "SET IDENTITY_INSERT [s].[t] ON",
        "SET LOCK_TIMEOUT 1000",
        "DECLARE @xact_abort int; SET @xact_abort = 1",
        "UPDATE [s].[t] SET xact_abort = 1, fmtonly = 2 WHERE [a] = 1",
        "UPDATE t SET t.implicit_transactions = 1 FROM [s].[t] AS t WHERE t.[a] = 1",
        "SELECT N'SET NOCOUNT, XACT_ABORT OFF' -- SET NOEXEC ON",
    ],
)
def test_a_set_statement_without_a_forbidden_option_and_an_assignment_are_not_findings(sql):
    assert codes(data(sql)) == []


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN TRY SELECT 1 END TRY BEGIN CATCH THROW; END CATCH",
        "BEGIN SELECT 1 END",
        "SET NOCOUNT ON",
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SELECT [a] FROM [s].[t] WHERE [b] = 2 OPTION (USE HINT ('FORCE_LEGACY_CARDINALITY_ESTIMATION'))",
        "SELECT N'COMMIT', 'ROLLBACK; RETURN' -- GOTO done\n/* RAISERROR USE BEGIN TRAN */",
        'SELECT [commit], "return", [use] FROM [s].[rollback]',
        "SELECT @commit, @@TRANCOUNT, RETURNS FROM [s].[t]",
        "SELECT 'a\n:r not a directive'",
        "SELECT geography::Point(1, 2, 4326)",
        "retry: SELECT 1",
    ],
)
def test_a_forbidden_word_in_a_string_comment_or_quoted_name_is_not_a_finding(sql):
    assert codes(data(sql)) == []
    assert codes(raw(sql)) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT [a] FROM [other].[dbo].[t]",
        "SELECT [a] FROM other.dbo.t",
        "SELECT [a] FROM [srv].[other].[dbo].[t]",
        "SELECT [a] FROM other..t",
        "SELECT [dbo].[t].[a] FROM [dbo].[t]",
        "SELECT other.dbo.fn_x.method(1)",
    ],
)
def test_a_name_with_three_parts_is_refused_in_every_batch(sql):
    assert codes(data(sql)) == ["THREE_PART_NAME"]
    assert codes(raw(sql)) == ["THREE_PART_NAME"]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO [other].[dbo].[T] (a) VALUES (1);",
        "INSERT [other].[dbo].[T] ([a]) SELECT 1",
        "INSERT INTO other.dbo.T(a) VALUES (1)",
        "SELECT [a] FROM other.dbo.fn_rows(1) AS r",
        "SELECT t.[a] FROM [s].[t] AS t JOIN other.dbo.fn_rows(1) AS r ON r.[a] = t.[a]",
        "MERGE INTO other.dbo.T (a) USING [s].[u] AS u ON 1 = 0 WHEN NOT MATCHED THEN INSERT (a) VALUES (1);",
    ],
)
def test_insert_into_three_part_name_with_column_list_is_refused(sql):
    """After INTO, FROM or JOIN a name is an object: '(' after it starts a column list or the
    arguments of a function of another database, not a method call on a column."""
    assert codes(data(sql)) == ["THREE_PART_NAME"]
    assert codes(raw(sql)) == ["THREE_PART_NAME"]


def test_a_three_part_table_name_in_a_model_batch_with_a_column_list_is_refused():
    assert "THREE_PART_NAME" in codes(facts("CREATE TABLE [other].[dbo].[T] ([a] int NOT NULL)"))
    assert "THREE_PART_NAME" in codes(facts("CREATE INDEX [ix] ON [other].[dbo].[T] ([a])", mode="nontx"))
    foreign = "ALTER TABLE [s].[t] ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [other].[dbo].[u] ([a])"
    assert "THREE_PART_NAME" in codes(facts(foreign, mode="nontx"))
    assert codes(facts("CREATE INDEX [ix] ON [s].[t] ([a])", mode="nontx")) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.[a], [s].[fn_x](t.[a]) FROM [s].[t] AS t",
        "SELECT n.c.value('.', 'int') FROM @doc.nodes('/r') AS n(c)",
        "SELECT @doc.value('(/r)[1]', 'int'), 1.5, .5",
        "SELECT N'[other].[dbo].[t]' -- other.dbo.t",
        "INSERT INTO [s].[t] ([a]) VALUES (1)",
        "INSERT [s].[t] ([a]) SELECT [a] FROM [s].[fn_rows](1) AS r JOIN [s].[fn_more](2) AS m ON 1 = 1",
        "SELECT t.[a] FROM [s].[t] AS t JOIN [s].[u] AS u ON t.[geo].STDistance(u.[geo]) < 5",
    ],
)
def test_a_two_part_name_and_a_method_call_are_not_three_part_names(sql):
    assert codes(data(sql)) == []


# ------------------------------------------------------------------ security statements (A11)
SECURITY = [
    "GRANT SELECT ON [s].[t] TO [reader]",
    "GRANT UPDATE, DELETE ON [s].[t] TO [writer]",
    "DENY DELETE ON [s].[t] TO [reader]",
    "REVOKE SELECT ON [s].[t] FROM [reader]",
    "EXECUTE AS USER = 'loader'",
    "EXEC AS CALLER",
    "REVERT",
    "ALTER ROLE [db_datareader] ADD MEMBER [reader]",
    "ALTER AUTHORIZATION ON SCHEMA::[s] TO [dbo]",
    "CREATE USER [reader] WITHOUT LOGIN",
]


@pytest.mark.parametrize("sql", SECURITY)
def test_users_roles_and_permissions_are_refused_outside_a_raw_batch(sql):
    for found in (data(sql), facts(sql)):
        assert codes(found) == ["SECURITY_STATEMENT"]
        assert found.needs_allow == ()
    assert codes(raw(sql)) == []


def test_create_schema_with_authorization_is_not_a_security_statement():
    assert codes(facts("CREATE SCHEMA [s] AUTHORIZATION [dbo]")) == []


# ------------------------------------------------------------------ raw batch
def test_a_raw_batch_always_needs_allow_raw_for_its_object():
    found = raw("ALTER TABLE [audit].[Log] SET (SYSTEM_VERSIONING = OFF);\nDROP TABLE [audit].[LogHistory];")
    assert items(found) == [("RAW", "TABLE:[audit].[Log]")]
    assert (found.statement, codes(found)) == ("", [])


# ------------------------------------------------------------------ shape of the result
def test_lines_of_items_and_findings_are_lines_of_the_migration_file():
    first, second = batches(
        "CREATE TABLE [s].[t] ([a] int NOT NULL)\nGO\n\n-- note\nDROP TABLE [s].[old]\n;COMMIT"
    )
    assert classify_batch(first, "tx").needs_allow == ()
    found = classify_batch(second, "tx", path="migrations/0001__change.sql")
    assert [(item.code, item.line) for item in found.needs_allow] == [("DROP_TABLE", 7)]
    assert [(f.code, f.path, f.line) for f in found.findings] == [
        ("FORBIDDEN_TOKEN", "migrations/0001__change.sql", 8)
    ]


def test_one_object_is_one_item_however_often_the_batch_names_it():
    found = data("DELETE FROM [s].[t]; DELETE FROM s.T; TRUNCATE TABLE [s].[t]; TRUNCATE TABLE [S].[T];")
    assert items(found) == [("DATA_NO_WHERE", "[s].[t]"), ("TRUNCATE", "[s].[t]")]


def test_an_unknown_mode_or_kind_is_refused_not_guessed():
    (batch,) = batches("DROP TABLE [s].[t]")
    with pytest.raises(ValueError, match="mode"):
        classify_batch(batch, "transactional")
    with pytest.raises(ValueError, match="kind"):
        classify_batch(MigrationBatch("ddl", batch.text, 1, ()), "tx")
    with pytest.raises(ValueError, match="object"):
        classify_batch(MigrationBatch("raw", batch.text, 1, ()), "tx")
    with pytest.raises(ValueError, match="no statement"):
        classify_batch(MigrationBatch("model", "-- only a comment", 1, ()), "tx")


# ------------------------------------------------------------------ review 2: the drop list (LFP-01)
def errors(found: BatchFacts) -> list[str]:
    return [finding.code for finding in found.findings if finding.severity == "error"]


@pytest.mark.parametrize(
    ("sql", "columns"),
    [
        (
            "ALTER TABLE [dbo].[Customer] DROP CONSTRAINT [PK_Customer] WITH (ONLINE = ON), COLUMN [Email];",
            ["[dbo].[Customer].[Email]"],
        ),
        (
            "ALTER TABLE [dbo].[Customer] DROP PERIOD FOR SYSTEM_TIME, COLUMN [ValidFrom], COLUMN [ValidTo];",
            ["[dbo].[Customer].[ValidFrom]", "[dbo].[Customer].[ValidTo]"],
        ),
        (
            "ALTER TABLE [dbo].[Customer] DROP CONSTRAINT IF EXISTS [a] WITH (MAXDOP = 1, ONLINE = OFF), "
            "[b], COLUMN IF EXISTS [c], PERIOD FOR SYSTEM_TIME, COLUMN [d]",
            ["[dbo].[Customer].[c]", "[dbo].[Customer].[d]"],
        ),
        ("alter table dbo.Customer drop period for system_time, column Email", ["[dbo].[Customer].[Email]"]),
    ],
)
def test_a_column_after_any_element_of_a_drop_list_needs_its_allow_line(sql, columns):
    """LFP-01. WITH (...) after a constraint name and PERIOD FOR SYSTEM_TIME are elements of the
    list; the columns after them were not read, so they passed with no allow line."""
    found = facts(sql)
    assert items(found) == [("DROP_COLUMN", column) for column in columns]
    assert errors(found) == []


def test_drop_period_is_not_read_as_a_constraint_with_the_name_period():
    found = facts("ALTER TABLE [s].[t] DROP PERIOD FOR SYSTEM_TIME")
    assert [(f.code, f.severity) for f in found.findings] == [("DROP_CONSTRAINT", "warning")]
    assert "PERIOD FOR SYSTEM_TIME" in found.findings[0].message
    assert "[PERIOD]" not in found.findings[0].message


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [s].[t] DROP CONSTRAINT [ck] WITH (ONLINE = ON) COLUMN [a]",
        "ALTER TABLE [s].[t] DROP CONSTRAINT [ck] [s].[usp_Fix]",
        "ALTER TABLE [s].[t] DROP COLUMN [a] WITH (ONLINE = ON), COLUMN [b]",
        "ALTER TABLE [s].[t] DROP PERIOD FOR SYSTEM_TIME COLUMN [a]",
        "ALTER TABLE [s].[t] DROP CONSTRAINT [ck] WITH (ONLINE = ON) WITH (MAXDOP = 1), COLUMN [a]",
        "ALTER TABLE [s].[t] DROP COLUMN [a] FOR SYSTEM_TIME, COLUMN [b]",
    ],
)
def test_a_token_after_an_element_of_a_drop_list_that_is_no_comma_is_a_stray_token(sql):
    """Nothing follows an element but ',', one ';' or the end. Any other token ends the read of the
    list, and what is not read cannot ask for an allow line: so it is an error."""
    assert errors(facts(sql)) == ["MODEL_STATEMENT"]


# ------------------------------------------------------------------ review 2: a warning hides no error
@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [dbo].[T] DROP CONSTRAINT Throw\nDELETE FROM [dbo].[Customer];",
        "ALTER TABLE [dbo].[Customer] DROP CONSTRAINT IF EXISTS Throw\nDROP TABLE [dbo].[Invoice];",
        "ALTER TABLE [dbo].[T] DROP Kill\nTRUNCATE TABLE [dbo].[Customer]",
        "ALTER TABLE [dbo].[T] DROP CONSTRAINT [ck], Throw\nDROP TABLE [dbo].[Invoice]",
        "ALTER TABLE [dbo].[T] DROP CONSTRAINT Throw [dbo].[usp_Fix]",
        "ALTER TABLE [dbo].[T] DROP CONSTRAINT Waitfor\nUPDATE [dbo].[Customer] SET [a] = 1",
    ],
)
def test_a_warning_on_a_token_never_takes_the_place_of_an_error(sql):
    """LFP-02. The DROP_CONSTRAINT warning stood on the name token; the error 'starts a second
    statement' for the same token was dropped as a duplicate, and the closed rule never ran."""
    found = facts(sql)
    assert "MODEL_STATEMENT" in errors(found)
    assert "DROP_CONSTRAINT" in codes(found)  # the warning stays


def test_a_name_that_is_a_statement_word_gets_the_hint_to_write_brackets():
    """LFP-07 (a). THROW is not reserved, so Throw can be a name; the message says how to write it."""
    for sql in ("ALTER TABLE dbo.T ADD Throw int NULL;", "ALTER TABLE dbo.Throw ADD c int NULL;"):
        found = facts(sql)
        assert errors(found) == ["MODEL_STATEMENT"]
        assert "If it is a name, write it in brackets" in found.findings[0].message
    assert errors(facts("ALTER TABLE dbo.[Throw] ADD [Throw] int NULL;")) == []


# ------------------------------------------------------------------ review 2: operators and money (LFP-03)
EXPRESSIONS = [
    "ALTER TABLE [dbo].[T] ADD [IsX] AS [Flags] & 4",
    "ALTER TABLE [dbo].[T] ADD [IsX] AS [Flags] | 4",
    "ALTER TABLE [dbo].[T] ADD [IsX] AS ~[Flags]",
    "ALTER TABLE [dbo].[T] ADD [c] int NOT NULL DEFAULT 1 ^ 3",
    "ALTER TABLE [dbo].[T] ADD [c] int NOT NULL DEFAULT ~1 | 2 & 3 ^ 4",
    "CREATE INDEX [ix] ON [dbo].[T] ([a]) WHERE [Value] > $5",
    "CREATE INDEX [ix] ON [dbo].[T] ([a]) WHERE [Value] > $5.25 AND [Flags] & 4 = 4",
    "CREATE INDEX [ix] ON [dbo].[T] ([a]) WHERE [Value] > £5",
    "ALTER TABLE [dbo].[T] ADD [c] money NOT NULL DEFAULT $0.5",
    "ALTER TABLE [dbo].[T] ADD [c] money NOT NULL DEFAULT -$1",
    "ALTER TABLE [dbo].[T] ADD [c] money NOT NULL DEFAULT €10",
    "ALTER TABLE [dbo].[T] ADD [c] int NOT NULL DEFAULT @@SPID",  # LFP-07 (e)
]


@pytest.mark.parametrize("sql", EXPRESSIONS)
def test_a_bitwise_operator_and_a_money_literal_have_a_place_in_an_expression_without_parentheses(sql):
    """LFP-03. gen writes these (the table-file check accepts them), so lint must read them."""
    for text in (sql, sql + ";"):
        assert errors(facts(text, mode="nontx")) == []


@pytest.mark.parametrize("sql", EXPRESSIONS)
@pytest.mark.parametrize("second", ["\nDROP TABLE [s].[u]", "\n" + _SWITCH, " [s].[usp_Fix]", "; SELECT 1"])
def test_an_expression_without_parentheses_still_ends_the_statement(sql, second):
    assert "MODEL_STATEMENT" in errors(facts(sql + second, mode="nontx"))


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [dbo].[T] ADD [c] int NULL $ [s].[usp_Fix]",
        "ALTER TABLE [dbo].[T] ADD [c] int NULL & DISABLE TRIGGER ALL ON DATABASE",
        "ALTER TABLE [dbo].[T] ADD [c] int NOT NULL DEFAULT @x",
        "ALTER TABLE [dbo].[T] ADD [c] int NULL\n@@x = 1 " + _SWITCH,
    ],
)
def test_an_operator_or_a_money_sign_gives_no_place_to_a_second_statement(sql):
    assert "MODEL_STATEMENT" in errors(facts(sql, mode="nontx"))


# ------------------------------------------------------------------ review 2: wider table coverage (LFP-04)
WIDER_FORMS = [
    "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = NONE)",
    "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = ROW)",
    "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = PAGE)",
    "alter table s.t rebuild with (data_compression = page)",
    "ALTER TABLE [s].[t] REBUILD WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, "
    "ABORT_AFTER_WAIT = SELF)), MAXDOP = 4, DATA_COMPRESSION = NONE)",
    "ALTER TABLE [s].[t] REBUILD WITH (SORT_IN_TEMPDB = ON, DATA_COMPRESSION = ROW, ONLINE = OFF)",
    "ALTER TABLE [s].[t] ALTER COLUMN [c] ADD SPARSE",
    "ALTER TABLE [s].[t] ALTER COLUMN [c] DROP SPARSE",
    "ALTER TABLE [s].[t] ALTER COLUMN [c] ADD ROWGUIDCOL",
    "ALTER TABLE [s].[t] ALTER COLUMN [c] DROP ROWGUIDCOL",
    "ALTER TABLE [s].[t] ALTER COLUMN [c] ADD NOT FOR REPLICATION",
    "ALTER TABLE [s].[t] ALTER COLUMN [c] DROP NOT FOR REPLICATION",
    "alter table sales.Document alter column Notes drop sparse",
    "ALTER TABLE s.t ALTER COLUMN c ADD PERSISTED",
    "ALTER TABLE s.t ALTER COLUMN c DROP PERSISTED",
    "CREATE TABLE [s].[t] ([g] uniqueidentifier ROWGUIDCOL NOT NULL, [n] nvarchar(400) SPARSE NULL, "
    "[i] bigint IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL, "
    "CONSTRAINT [ck] CHECK NOT FOR REPLICATION ([i] > 0), CONSTRAINT [fk] FOREIGN KEY ([i]) "
    "REFERENCES [s].[u] ([i]) ON DELETE CASCADE NOT FOR REPLICATION) WITH (DATA_COMPRESSION = PAGE)",
    "CREATE TABLE [s].[t] ([a] int NOT NULL) WITH (DATA_COMPRESSION = ROW)",
    "CREATE TABLE [s].[t] (\n    [a] int NOT NULL\n) WITH (DATA_COMPRESSION = NONE)",
    "ALTER TABLE [s].[t] ADD [g] uniqueidentifier ROWGUIDCOL NOT NULL CONSTRAINT [df] "
    "DEFAULT (newsequentialid())",
    "ALTER TABLE [s].[t] ADD [x] varchar(200) SPARSE NULL",
    "ALTER TABLE [s].[t] ADD [i] bigint IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL",
    "ALTER TABLE [s].[t] WITH NOCHECK ADD CONSTRAINT [ck] CHECK NOT FOR REPLICATION ([a] > 0)",
    "ALTER TABLE [s].[t] ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [s].[u] ([a]) "
    "ON UPDATE CASCADE NOT FOR REPLICATION",
    "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t]",
    "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t] WITH (MAXDOP = 2, ONLINE = ON) ON [PRIMARY]",
    "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t] ORDER ([a], [b]) "
    "WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE)",
    "CREATE NONCLUSTERED COLUMNSTORE INDEX [ncci] ON [s].[t] ([a], [b]) WHERE [b] > 0 "
    "WITH (COMPRESSION_DELAY = 30 MINUTES)",
    "CREATE NONCLUSTERED COLUMNSTORE INDEX [ncci] ON [s].[t] ([a], [b])\n    WHERE [a] > 100\n"
    "    WITH (COMPRESSION_DELAY = 10, DATA_COMPRESSION = COLUMNSTORE_ARCHIVE)",
    "CREATE COLUMNSTORE INDEX [ncci] ON [s].[t] ([a])",
]
SECONDS = [
    "\nDROP TABLE [s].[u]",
    "\nDELETE FROM [s].[u]",
    "\n" + _SWITCH,
    " [s].[usp_Fix]",
    "; DROP TABLE [s].[u]",
]


@pytest.mark.parametrize("sql", WIDER_FORMS)
def test_each_form_of_the_wider_table_coverage_is_one_statement_of_the_closed_list(sql):
    """LFP-04. emit writes these forms and the parser reads them, so the closed rule holds them."""
    for mode in ("tx", "nontx"):
        for text in (sql, sql + ";"):
            found = facts(text, mode=mode)
            assert errors(found) == []
            assert found.statement in ("ALTER TABLE", "CREATE TABLE", "CREATE INDEX")


@pytest.mark.parametrize("sql", WIDER_FORMS)
@pytest.mark.parametrize("second", SECONDS)
def test_each_form_of_the_wider_table_coverage_followed_by_a_second_statement_is_refused(sql, second):
    for mode in ("tx", "nontx"):
        assert "MODEL_STATEMENT" in errors(facts(sql + second, mode=mode))


@pytest.mark.parametrize("sql", [form for form in WIDER_FORMS if " REBUILD " in form or " COLUMN " in form])
def test_a_rebuild_and_a_column_property_are_read_to_their_end(sql):
    """These forms end at a fixed token, so a query in parentheses after them has no place either."""
    assert "MODEL_STATEMENT" in errors(facts(sql + "\n(SELECT 1)"))
    assert "MODEL_STATEMENT" in errors(facts(sql + " WITH (ONLINE = ON)"))


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [s].[t] REBUILD WITH (ONLINE = ON)",
        "ALTER TABLE [s].[t] REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = PAGE)",
        "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = COLUMNSTORE)",
        "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = PAGE, FILLFACTOR = 90)",
        "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = PAGE ON PARTITIONS (1))",
        "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = PAGE, DATA_COMPRESSION = ROW)",
        "ALTER TABLE [s].[t] REBUILD WITH (DATA_COMPRESSION = (SELECT 1))",
        "ALTER TABLE [s].[t] REBUILD WITH DATA_COMPRESSION = PAGE",
    ],
)
def test_a_rebuild_that_is_not_the_compression_of_the_table_is_outside_the_closed_list(sql):
    found = facts(sql)
    assert codes(found) == ["MODEL_STATEMENT"]
    assert found.needs_allow == ()


def test_a_table_rebuild_reads_every_row_and_needs_long_lock_in_a_tx_migration():
    sql = "ALTER TABLE [sales].[Heap] REBUILD WITH (DATA_COMPRESSION = PAGE);"
    assert items(facts(sql, mode="tx")) == [("LONG_LOCK", "[sales].[Heap]")]
    assert items(facts(sql, mode="nontx")) == []
    assert items(facts(sql, mode="tx", created=("[sales].[Heap]",))) == []
    # an online rebuild in a nontx migration is asked to wait at low priority, as an index build
    online = "ALTER TABLE [sales].[Heap] REBUILD WITH (DATA_COMPRESSION = PAGE, ONLINE = ON)"
    assert codes(facts(online, mode="nontx")) == ["NTX004"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("ADD SPARSE", [("LONG_LOCK", "[s].[t]")]),  # every row is written again
        ("DROP SPARSE", [("LONG_LOCK", "[s].[t]")]),
        ("ADD ROWGUIDCOL", []),
        ("DROP ROWGUIDCOL", []),
        ("ADD NOT FOR REPLICATION", []),
        ("DROP NOT FOR REPLICATION", []),
        ("ADD PERSISTED", [("ALTER_COLUMN_LOSSY", "[s].[t].[c]")]),
        ("DROP PERSISTED", [("ALTER_COLUMN_LOSSY", "[s].[t].[c]")]),
    ],
)
def test_a_column_property_changes_no_value_so_it_is_no_lossy_alter_column(change, expected):
    found = facts(f"ALTER TABLE [s].[t] ALTER COLUMN [c] {change};")
    assert (items(found), codes(found)) == (expected, [])
    assert items(facts(f"ALTER TABLE [s].[t] ALTER COLUMN [c] {change}", mode="nontx")) == [
        item for item in expected if item[0] != "LONG_LOCK"
    ]


def test_an_online_columnstore_build_is_not_asked_for_a_low_priority_wait():
    """A columnstore index takes no WAIT_AT_LOW_PRIORITY clause, so NTX004 cannot ask for it."""
    sql = "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t] WITH (MAXDOP = 2, ONLINE = ON)"
    assert codes(facts(sql, mode="nontx")) == []


# ------------------------------------------------------------------ live run: NOT NULL and no DEFAULT
def test_a_not_null_column_with_no_default_on_a_table_that_exists_is_a_warning():
    """E2E-9. The statement passes in an empty database and fails on the first table with rows."""
    found = facts("ALTER TABLE [stock].[Warehouse] ADD [Region] varchar(10) NOT NULL;")
    assert [(f.code, f.severity, f.line) for f in found.findings] == [("NNL001", "warning", 3)]
    message = found.findings[0].message
    assert "[Region]" in message and "fails on a table that has rows" in message
    assert "add a DEFAULT, or add the column NULL, fill it and alter it" in message
    assert found.needs_allow == ()


def test_each_not_null_column_of_a_list_without_default_has_its_own_warning():
    found = facts(
        "ALTER TABLE [s].[t] ADD\n    [a] int NULL,\n    [b] decimal(10, 2) NOT NULL,\n"
        "    [c] int NOT NULL CONSTRAINT [df] DEFAULT (0),\n    Region varchar(10) not null,\n"
        "    CONSTRAINT [ck] CHECK ([a] IS NOT NULL)",
        mode="nontx",
    )
    assert [(f.code, f.line) for f in found.findings] == [("NNL001", 5), ("NNL001", 7)]
    assert "[b]" in found.findings[0].message and "[Region]" in found.findings[1].message


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE [s].[t] ADD [a] int NULL",
        "ALTER TABLE [s].[t] ADD [a] int NOT NULL CONSTRAINT [df] DEFAULT (0)",
        "ALTER TABLE [s].[t] ADD [a] int NOT NULL DEFAULT 0",
        "ALTER TABLE [s].[t] ADD [a] int NOT NULL DEFAULT (0) WITH VALUES",
        "ALTER TABLE [s].[t] ADD [a] int IDENTITY(1, 1) NOT NULL",
        "ALTER TABLE [s].[t] ADD [a] bigint IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL",
        "ALTER TABLE [s].[t] ADD [a] AS ([b] + 1) PERSISTED NOT NULL",
        "ALTER TABLE [s].[t] ADD [a] rowversion NOT NULL",
        "ALTER TABLE [s].[t] ADD [a] timestamp NOT NULL",
        "ALTER TABLE [s].[t] ADD [a] int NOT FOR REPLICATION NULL",
        "ALTER TABLE [s].[t] ADD CONSTRAINT [ck] CHECK ([a] IS NOT NULL)",
        "ALTER TABLE [s].[t] ADD CONSTRAINT [df] DEFAULT 0 FOR [a]",
        "ALTER TABLE [s].[t] ADD CONSTRAINT [pk] PRIMARY KEY NONCLUSTERED ([a])",
        "ALTER TABLE [s].[t] ALTER COLUMN [a] int NOT NULL",
        "CREATE TABLE [s].[t] ([a] int NOT NULL)",
    ],
)
def test_a_column_that_the_engine_can_fill_or_a_statement_that_adds_no_column_is_no_nnl001(sql):
    assert "NNL001" not in codes(facts(sql, mode="nontx"))


def test_a_not_null_column_on_a_table_of_the_same_migration_is_no_nnl001():
    """The table is new, so it has no rows when the column is added."""
    create, add, other = batches(
        "CREATE TABLE [stock].[Warehouse] ([Id] int NOT NULL)\nGO\n"
        "ALTER TABLE stock.warehouse ADD [Region] varchar(10) NOT NULL\nGO\n"
        "ALTER TABLE [stock].[Bin] ADD [Region] varchar(10) NOT NULL"
    )
    created = classify_batch(create, "tx").creates_tables
    assert codes(classify_batch(add, "tx", created)) == []
    assert codes(classify_batch(other, "tx", created)) == ["NNL001"]


# ------------------------------------------------------------------ review 2: rare valid forms (LFP-07)
@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE dbo.T ADD [X] national character varying(20) NULL",
        "ALTER TABLE dbo.T ADD [X] national char varying(20) NULL, [Y] national character(5) NULL",
        "ALTER TABLE dbo.T ADD [X] character varying(20) NULL, [Y] char varying(max) NULL",
        "ALTER TABLE dbo.T ADD [X] national text NULL, [Y] binary varying(8) NULL",
        "ALTER TABLE dbo.T ALTER COLUMN [X] national character varying(20) NOT NULL",
        "CREATE TABLE [dbo].[T] ([a] int NULL CONSTRAINT [FK] REFERENCES [dbo].[R] ([a]) ON DELETE SET NULL, "
        "Noexec int NULL);",
        "ALTER TABLE [dbo].[T] ADD [a] int NULL CONSTRAINT [FK] REFERENCES [dbo].[R] ([a]) "
        "ON UPDATE SET DEFAULT, Rowcount int NULL, Xact_Abort bit NULL",
        "DROP INDEX [dbo].[Order].[IX_Order_Cust]",
        "DROP INDEX IF EXISTS dbo.T.ix, dbo.U.ix2",
    ],
)
def test_a_valid_rare_form_is_not_refused(sql):
    assert errors(facts(sql, mode="nontx")) == []


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("ALTER TABLE dbo.T ADD [X] national DISABLE TRIGGER ALL ON DATABASE", "MODEL_STATEMENT"),
        ("ALTER TABLE dbo.T ADD [X] national character\n" + _SWITCH, "MODEL_STATEMENT"),
        ("DROP INDEX [other].[dbo].[Order].[IX_Order_Cust]", "THREE_PART_NAME"),
        ("DROP INDEX [ix] ON [other].[dbo].[Order]", "THREE_PART_NAME"),
        ("DROP TABLE [other].[dbo].[Order]", "THREE_PART_NAME"),
        ("ALTER TABLE [s].[t] ADD [a] int NULL\nSET NOEXEC ON", "FORBIDDEN_TOKEN"),
        ("ALTER TABLE [s].[t] ADD [a] int NULL ON\nDELETE SET NOEXEC ON", "MODEL_STATEMENT"),
    ],
)
def test_the_rare_forms_open_no_way_around_the_rules(sql, code):
    assert code in errors(facts(sql, mode="nontx"))


@pytest.mark.parametrize("second", ["SHUTDOWN", "RECONFIGURE", "[dbo].[usp_Fix]", "dbo.p 1", "SETUSER"])
def test_nothing_follows_the_action_set_default_as_if_it_were_an_expression(second):
    """DEFAULT takes an expression in a column definition; in ON DELETE SET DEFAULT it takes none,
    so a name after it is a second statement."""
    fk = (
        "ALTER TABLE [s].[t] ADD CONSTRAINT [fk] FOREIGN KEY ([a]) REFERENCES [s].[u] ([a]) "
        "ON DELETE SET DEFAULT"
    )
    assert errors(facts(fk, mode="nontx")) == []
    assert errors(facts(fk + " ON UPDATE SET DEFAULT NOT FOR REPLICATION;", mode="nontx")) == []
    assert "MODEL_STATEMENT" in errors(facts(f"{fk}\n{second}", mode="nontx"))
    assert "MODEL_STATEMENT" in errors(facts(f"{fk} {second}", mode="nontx"))


# ------------------------------------------------------------------ the old form of DROP INDEX in any place
RAW_NONTX = "-- azsqlcd:raw TABLE:[audit].[Log] reason: outside the model\n"


def three_part(found: BatchFacts) -> list[str]:
    """The name of each THREE_PART_NAME finding, as its message shows it."""
    return [f.message.partition(":")[0] for f in found.findings if f.code == "THREE_PART_NAME"]


@pytest.mark.parametrize(
    "sql",
    [
        "IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Order_Cust' "
        "AND object_id = OBJECT_ID(N'sales.Order'))\n    DROP INDEX [sales].[Order].[IX_Order_Cust];",
        "IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_Order_Cust')\nBEGIN\n"
        "    DROP INDEX sales.[Order].IX_Order_Cust;\nEND",
        "DROP INDEX [sales].[Order].[IX_A];\nDROP INDEX [sales].[Order].[IX_B];",
        "IF INDEXPROPERTY(OBJECT_ID(N'sales.Order'), N'IX', 'IndexID') IS NOT NULL\n"
        "    DROP INDEX sales.[Order].IX",
        "IF 1 = 1 DROP INDEX s.t.a, s.u.b",
        "IF 1 = 1 DROP INDEX IF EXISTS s.t.a",
        "IF 1 = 1 DROP INDEX IF EXISTS s.t.a, s.u.b;",
        "IF 1 = 0 SELECT 1 ELSE DROP INDEX s.t.a",
        "IF 1 = 1 BEGIN IF 2 = 2 BEGIN DROP INDEX s.t.a; DROP INDEX s.u.b END END",
        "BEGIN TRY DROP INDEX s.t.a; END TRY BEGIN CATCH THROW; END CATCH",
        "SELECT 1 FROM [s].[t] DROP INDEX s.t.a",
        # the rule does not decide if the engine takes both forms in one list
        "IF 1 = 1 DROP INDEX s.t.a, [ix] ON [s].[t]",
        "IF 1 = 1 DROP INDEX [ix] ON [s].[t] WITH (ONLINE = ON, MAXDOP = 2), s.t.a",
    ],
)
def test_the_old_form_of_drop_index_is_no_three_part_name_wherever_the_statement_stands(sql):
    """DROP INDEX schema.table.index names an index of this database: with a database name the old
    form has four parts. The statement is the same one behind a condition, in a block and after
    another statement, so the batch is not refused there."""
    assert codes(raw(sql)) == []
    assert set(codes(data(sql))) == {"DATA_DDL"}  # the rule of a data batch, not of the name


@pytest.mark.parametrize(
    ("sql", "refused"),
    [
        ("IF 1 = 1 DROP INDEX [other].[s].[t].[ix]", ["[other].[s].[t].[ix]"]),
        ("IF 1 = 1 DROP INDEX s.t.a, [other].[s].[t].[ix]", ["[other].[s].[t].[ix]"]),
        ("IF 1 = 1 DROP INDEX [ix] ON [other].[s].[t]", ["[other].[s].[t]"]),
        ("IF 1 = 1 DROP INDEX [ix] ON [s].[t], [ix2] ON [other].[s].[u]", ["[other].[s].[u]"]),
        ("IF EXISTS (SELECT 1 FROM [other].[dbo].[T]) DROP INDEX [s].[t].[ix]", ["[other].[dbo].[T]"]),
        ("DROP INDEX [s].[t].[ix]; SELECT 1 FROM [other].[dbo].[T]", ["[other].[dbo].[T]"]),
        ("IF 1 = 1 DROP TABLE [other].[s].[t]", ["[other].[s].[t]"]),
        ("IF 1 = 1 DROP VIEW [other].[s].[v]", ["[other].[s].[v]"]),
        ("IF 1 = 1 DROP PROCEDURE [other].[s].[p]", ["[other].[s].[p]"]),
        # a comma list of another statement, after a DROP INDEX statement of the same batch
        ("DROP INDEX s.t.a; SELECT a.b.c, d.e.f FROM [s].[u] AS a", ["[a].[b].[c]", "[d].[e].[f]"]),
        ("DROP INDEX s.t.a SELECT a.[x], d.e.f FROM [s].[u] AS a", ["[d].[e].[f]"]),
        (
            "DROP INDEX s.t.a; INSERT INTO [s].[u] ([x], [y]) VALUES (a.b.c, d.e.f)",
            ["[a].[b].[c]", "[d].[e].[f]"],
        ),
        ("DROP INDEX s.t.a; EXEC [s].[p] a.b.c, d.e.f", ["[a].[b].[c]", "[d].[e].[f]"]),
        # what the old form cannot hold: a call, a fourth dot, ON after three parts
        ("IF 1 = 1 DROP INDEX [a].[b].[c](1)", ["[a].[b].[c]"]),
        ("IF 1 = 1 DROP INDEX s.t.a, [a].[b].[c](1), s.u.b", ["[a].[b].[c]", "[s].[u].[b]"]),
        ("IF 1 = 1 DROP INDEX a.b.c..d", ["[a].[b].[c]"]),
        ("IF 1 = 1 DROP INDEX [a].[b].[c] ON [s].[t]", ["[a].[b].[c]"]),
        # DROP INDEX that is no statement: the action of ALTER TABLE, and inside parentheses
        ("ALTER TABLE [s].[t] DROP INDEX a.b.c", ["[a].[b].[c]"]),
        ("ALTER TABLE [s].[t] ADD [c] int NULL, DROP INDEX [other].[dbo].[T]", ["[other].[dbo].[T]"]),
        ("ALTER TABLE [s].[t] DROP COLUMN [c], DROP INDEX [other].[dbo].[T]", ["[other].[dbo].[T]"]),
        ("DROP INDEX s.t.a, DROP INDEX other.dbo.T", ["[other].[dbo].[T]"]),
        ("SELECT 1 WHERE EXISTS (DROP INDEX a.b.c)", ["[a].[b].[c]"]),
        # the new form: '(' after the object starts the next statement, it is no call
        ("DROP INDEX IF EXISTS [ix] ON [other].[dbo].[T]\n(SELECT 1)", ["[other].[dbo].[T]"]),
        (
            "IF 1 = 1 DROP INDEX [ix] ON [s].[t], [ix2] ON [other].[dbo].[T] (SELECT 1)",
            ["[other].[dbo].[T]"],
        ),
    ],
)
def test_only_the_names_of_a_drop_index_list_are_exempt_from_three_part_name(sql, refused):
    """The exemption is for a name that is an element of the list of a DROP INDEX statement, with
    three parts and nothing after them. Every other name of the batch is read as before."""
    assert three_part(raw(sql)) == refused
    assert three_part(data(sql)) == refused


@pytest.mark.parametrize(
    "sql",
    [
        "DROP STATISTICS s.t.stat",
        "IF 1 = 1 DROP STATISTICS [s].[t].[stat], s.u.stat2",
        "DROP INDEX s.t.a; DROP STATISTICS s.t.stat;",
    ],
)
def test_drop_statistics_with_schema_table_and_statistics_is_no_three_part_name(sql):
    """DROP STATISTICS table.statistics: with the schema the name has three parts and stays in this
    database, as the old form of DROP INDEX."""
    assert codes(raw(sql)) == []


@pytest.mark.parametrize(
    ("sql", "refused"),
    [
        ("IF 1 = 1 DROP STATISTICS other.s.t.stat", ["[other].[s].[t].[stat]"]),
        ("DROP STATISTICS s.t.stat, other.s.t.stat2", ["[other].[s].[t].[stat2]"]),
        ("DROP STATISTICS s.t.stat; SELECT 1 FROM other.dbo.T", ["[other].[dbo].[T]"]),
        ("DROP STATISTICS s.t.stat SELECT a.[x], d.e.f FROM [s].[u] AS a", ["[d].[e].[f]"]),
        ("UPDATE STATISTICS other.dbo.T", ["[other].[dbo].[T]"]),
    ],
)
def test_only_the_names_of_a_drop_statistics_list_are_exempt_from_three_part_name(sql, refused):
    assert three_part(raw(sql)) == refused
    assert three_part(data(sql)) == refused


def test_a_model_batch_reads_the_list_of_drop_index_by_the_same_rule():
    assert codes(facts("DROP INDEX [s].[t].[ix]")) == ["DROP_INDEX"]
    assert codes(facts("DROP INDEX IF EXISTS s.t.a, s.u.b;")) == ["DROP_INDEX"]
    assert "THREE_PART_NAME" in codes(facts("DROP INDEX [a].[b].[c](1)"))
    assert "THREE_PART_NAME" in codes(facts("DROP INDEX [a].[b].[c] ON [s].[t]"))
    assert "THREE_PART_NAME" in codes(facts("DROP INDEX s.t.a, [other].[s].[t].[ix]"))


# ------------------------------------------------------------------ NTX004 in a batch of several statements
def test_an_online_columnstore_build_behind_a_condition_is_not_asked_for_a_low_priority_wait():
    """The exemption of test_an_online_columnstore_build_is_not_asked_for_a_low_priority_wait is for
    the statement that holds ONLINE = ON, not for the first statement of the batch."""
    build = "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t] WITH (ONLINE = ON)"
    guard = "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'cci')\n    "
    assert codes(facts(RAW_NONTX + guard + build, mode="nontx")) == []
    assert codes(facts(RAW_NONTX + f"BEGIN\n    {build};\nEND", mode="nontx")) == []
    nonclustered = "CREATE NONCLUSTERED COLUMNSTORE INDEX [ncci] ON [s].[t] ([a]) WITH (ONLINE = ON)"
    assert codes(facts(RAW_NONTX + guard + nonclustered, mode="nontx")) == []


@pytest.mark.parametrize(
    "first",
    [
        "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t];",
        "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t] WITH (ONLINE = ON);",
        "CREATE CLUSTERED COLUMNSTORE INDEX [cci] ON [s].[t] WITH (ONLINE = ON)",  # no ';'
    ],
)
def test_a_columnstore_build_does_not_hide_the_online_build_after_it(first):
    found = facts(RAW_NONTX + first + "\nCREATE INDEX [ix] ON [s].[u] ([a]) WITH (ONLINE = ON)", mode="nontx")
    assert [(f.code, f.line) for f in found.findings] == [("NTX004", 5)]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE INDEX COLUMNSTORE ON [s].[t] ([a]) WITH (ONLINE = ON)",
        "IF 1 = 1 CREATE INDEX COLUMNSTORE ON [s].[t] ([a]) WITH (ONLINE = ON)",
        "CREATE NONCLUSTERED INDEX COLUMNSTORE ON [s].[t] ([a]) WITH (ONLINE = ON)",
    ],
)
def test_a_rowstore_index_with_the_name_columnstore_is_asked_for_a_low_priority_wait(sql):
    """COLUMNSTORE is no reserved word. The exemption needs the words COLUMNSTORE INDEX."""
    assert codes(facts(RAW_NONTX + sql, mode="nontx")) == ["NTX004"]


WAIT_BUILD = (
    "CREATE INDEX [ix1] ON [s].[t] ([a]) "
    "WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1 MINUTES, ABORT_AFTER_WAIT = SELF)));"
)
ONLINE_BUILD = "CREATE INDEX [ix2] ON [s].[u] ([a]) WITH (ONLINE = ON);"
ALTER_COLUMN = "ALTER TABLE [s].[t] ALTER COLUMN [a] bigint NOT NULL"


@pytest.mark.parametrize(
    ("sql", "line"),
    [
        (f"{WAIT_BUILD}\n{ONLINE_BUILD}", 5),
        (f"{ONLINE_BUILD}\n{WAIT_BUILD}", 4),
        (f"{ALTER_COLUMN};\n{ONLINE_BUILD}", 5),
        (f"{ALTER_COLUMN}\n{ONLINE_BUILD}", 5),  # no ';'
        (f"{ONLINE_BUILD}\n{ALTER_COLUMN};", 4),
        (f"{ALTER_COLUMN} WITH (ONLINE = ON);\n{ONLINE_BUILD}", 5),
    ],
)
def test_another_statement_of_the_batch_does_not_hide_an_online_build_with_no_wait(sql, line):
    """NTX004 reads the statement that holds ONLINE = ON. A low-priority wait or an ALTER COLUMN in
    another statement of the batch says nothing about this build."""
    found = facts(RAW_NONTX + sql, mode="nontx")
    assert [(f.code, f.line) for f in found.findings] == [("NTX004", line)]


@pytest.mark.parametrize(
    "sql",
    [
        f"IF 1 = 1 {WAIT_BUILD}",
        f"{WAIT_BUILD}\n{WAIT_BUILD}",
        f"IF 1 = 1 {ALTER_COLUMN} WITH (ONLINE = ON)",
        f"{WAIT_BUILD}\n{ALTER_COLUMN} WITH (ONLINE = ON);",
        # DROP is a word of ALTER COLUMN here, it starts no statement
        "IF 1 = 1 ALTER TABLE [s].[t] ALTER COLUMN [a] DROP SPARSE WITH (ONLINE = ON)",
    ],
)
def test_ntx004_keeps_its_exemptions_for_each_statement_of_a_batch(sql):
    assert codes(facts(RAW_NONTX + sql, mode="nontx")) == []


def test_a_low_priority_wait_is_the_clause_of_its_online_option():
    """ONLINE = ON (WAIT_AT_LOW_PRIORITY (...)) is the form that the tool writes and reads. The word
    in another place of the statement is not the wait of this build."""
    apart = "CREATE INDEX [ix] ON [s].[t] ([a]) WITH (ONLINE = ON, WAIT_AT_LOW_PRIORITY (MAX_DURATION = 1))"
    assert codes(facts(RAW_NONTX + apart, mode="nontx")) == ["NTX004"]


# ------------------------------------------------------------------ pilot: parentheses that do not balance
# The file of the pilot: a stray '[k])' line gives one ')' more than '('. Only the engine refused it.
ONE_TOO_MANY = "CREATE TABLE [s].[t] (\n [a] int NOT NULL,\n [q] numeric(23,5) NULL\n [k])\n)"
NEVER_CLOSED = "CREATE TABLE [s].[t] (\n [a] int NOT NULL,\n [q] numeric(23,5) NULL\n"
RAW_TABLE = "-- azsqlcd:raw TABLE:[s].[t] reason: outside the model\n"


def unbalanced(found: BatchFacts) -> list[tuple[str, int]]:
    return [(f.severity, f.line) for f in found.findings if f.code == "PAR001"]


@pytest.mark.parametrize("mode", ["tx", "nontx"])
@pytest.mark.parametrize("prefix", ["", RAW_TABLE, "-- azsqlcd:data\n"], ids=["model", "raw", "data"])
def test_a_closing_parenthesis_that_closes_nothing_is_an_error_in_every_kind_of_batch(prefix, mode):
    """No T-SQL batch is valid with parentheses that do not balance, so the kind does not matter."""
    lines_above = 2 + prefix.count("\n")
    found = facts(prefix + ONE_TOO_MANY, mode=mode)
    assert unbalanced(found) == [("error", lines_above + 5)]
    (finding,) = [f for f in found.findings if f.code == "PAR001"]
    assert f"line {lines_above + 5}" in finding.message and "closes nothing" in finding.message


@pytest.mark.parametrize("mode", ["tx", "nontx"])
@pytest.mark.parametrize("prefix", ["", RAW_TABLE, "-- azsqlcd:data\n"], ids=["model", "raw", "data"])
def test_an_opening_parenthesis_that_is_never_closed_is_an_error_in_every_kind_of_batch(prefix, mode):
    lines_above = 2 + prefix.count("\n")
    found = facts(prefix + NEVER_CLOSED, mode=mode)
    assert unbalanced(found) == [("error", lines_above + 1)]
    (finding,) = [f for f in found.findings if f.code == "PAR001"]
    assert f"line {lines_above + 1}" in finding.message and "never closed" in finding.message


def test_the_finding_names_the_first_parenthesis_that_cannot_have_a_partner():
    # ')' before any '(': the count is equal at the end and the batch is still invalid
    found = raw("SELECT 1 ) + ( 2;")
    assert unbalanced(found) == [("error", 4)]
    assert "closes nothing" in found.findings[-1].message
    # two '(' stay open: the finding names the first of them and says how many
    found = raw("UPDATE [s].[t]\nSET [a] = (1 + (2\nWHERE [b] = (3);")
    assert unbalanced(found) == [("error", 5)]
    assert "2 '(' are never closed" in next(f.message for f in found.findings if f.code == "PAR001")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE [s].[t] SET [a] = N'(' WHERE [b] = ')))'",
        "UPDATE [s].[t] SET [a] = 1 WHERE [b] = 2 -- ) not a parenthesis (\n AND [c] = 3 /* (( */",
        "UPDATE [s].[t] SET [a (] = 1 WHERE [b)]]] = 2",
        'UPDATE [s].[t] SET "a)" = 1 WHERE "(" = 2',
        "UPDATE [s].[t] SET [a] = ((1 + (2)) * (3)) WHERE [b] IN (SELECT (4))",
    ],
)
def test_a_parenthesis_in_a_string_a_comment_or_a_quoted_name_is_not_counted(sql):
    for found in (data(sql), raw(sql)):
        assert unbalanced(found) == []


def test_the_parenthesis_finding_does_not_take_away_another_finding_of_the_batch():
    found = data("COMMIT;\nDELETE FROM [s].[t] WHERE [a] = (1;")
    assert sorted(codes(found)) == ["FORBIDDEN_TOKEN", "PAR001"]
