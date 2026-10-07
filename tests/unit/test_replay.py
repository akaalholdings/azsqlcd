"""The proof engine: apply() and replay() on an in-memory model.

Each precondition has statements that it blocks and near misses that it lets through. The
statements are SQL text, read by the parser, because that is what `verify` replays.
"""

import random
import typing
from collections.abc import Iterator
from dataclasses import replace

import pytest

from azsqlcd import model as M
from azsqlcd.diff import GenRefused, diff
from azsqlcd.emit import emit_create_script, emit_operation
from azsqlcd.model import (
    AddColumn,
    AlterColumn,
    Column,
    CreateSchema,
    CreateSequence,
    CreateSynonym,
    CreateTable,
    CreateType,
    Identity,
    Model,
    Operation,
    PrimaryKey,
    Sequence,
    SetSystemVersioning,
    Table,
    Temporal,
    TypeRef,
    UnversionedTable,
)
from azsqlcd.parse import parse_statement
from azsqlcd.replay import ReplayError, apply, column_dependants, has_key, replay
from fixtures.pairs import loader


def model_of(*statements: str) -> Model:
    """A model from CREATE statements, as written: the code under test does not build its own input."""
    objects = []
    for text in statements:
        match parse_statement(text):
            case CreateTable(table=obj) | CreateSchema(schema=obj) | CreateType(type=obj):
                objects.append(obj)
            case CreateSequence(sequence=obj) | CreateSynonym(synonym=obj):
                objects.append(obj)
            case other:
                raise AssertionError(f"not a CREATE of an object: {other!r}")
    return Model(objects)


def run(model: Model, *statements: str) -> Model:
    return replay(model, [parse_statement(text) for text in statements])


def table(model: Model, name: str) -> Table:
    found = model[f"TABLE:[s].[{name}]"]
    assert isinstance(found, Table)
    return found


def column(owner: Table, name: str) -> Column:
    found = owner.column(name)
    assert found is not None
    return found


def sql(model: Model, name: str) -> str:
    """The table as one CREATE TABLE statement: a compact way to state a whole expected table."""
    return emit_operation(CreateTable(table(model, name)))


SEQUENCE = (
    "CREATE SEQUENCE [s].[{}] AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 100 NO CYCLE CACHE 10"
)
P = (
    "CREATE TABLE [s].[P] ([Id] int NOT NULL, [Code] varchar(10) NOT NULL, [A] int NOT NULL, "
    "[B] int NOT NULL, [Loose] int NOT NULL, [Half] int NULL, "
    "CONSTRAINT [PK_P] PRIMARY KEY CLUSTERED ([Id]), CONSTRAINT [UQ_P_Code] UNIQUE NONCLUSTERED ([Code]), "
    "INDEX [UX_P_AB] UNIQUE NONCLUSTERED ([A], [B]), "
    "INDEX [UX_P_Half] UNIQUE NONCLUSTERED ([Half]) WHERE [Half] IS NOT NULL)"
)
# one column for each kind of dependant
T = (
    "CREATE TABLE [s].[T] ([InIndex] int NOT NULL, [InInclude] int NOT NULL, [InFilter] int NULL, "
    "[InPk] int NOT NULL, [InUnique] int NOT NULL, [InFk] int NULL, [InCheck] int NOT NULL, "
    "[InComputed] int NOT NULL, [Referenced] int NOT NULL, "
    "[WithDefault] int NOT NULL CONSTRAINT [DF_T] DEFAULT ((0)), "
    "[No] int NOT NULL CONSTRAINT [DF_T_No] DEFAULT (NEXT VALUE FOR [s].[Seq]), "
    "[Tag] [s].[Code] NOT NULL, [Free] int NOT NULL, [Calc] AS ([InComputed] + 1), "
    "CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([InPk]), CONSTRAINT [UQ_T] UNIQUE NONCLUSTERED ([InUnique]), "
    "CONSTRAINT [UQ_T_Referenced] UNIQUE NONCLUSTERED ([Referenced]), "
    "CONSTRAINT [FK_T_P] FOREIGN KEY ([InFk]) REFERENCES [s].[P] ([Id]), "
    "CONSTRAINT [CK_T] CHECK ([InCheck] > 0 AND 'Free' <> N'[Free]'), "
    "INDEX [IX_T] NONCLUSTERED ([InIndex]) INCLUDE ([InInclude]) WHERE [InFilter] IS NOT NULL)"
)
U = (
    "CREATE TABLE [s].[U] ([Id] int NOT NULL, [TRef] int NOT NULL, [PCode] varchar(10) NULL, "
    "[Wide] varchar(20) NULL, [Up] int NULL, CONSTRAINT [PK_U] PRIMARY KEY CLUSTERED ([Id]), "
    "CONSTRAINT [FK_U_T] FOREIGN KEY ([TRef]) REFERENCES [s].[T] ([Referenced]), "
    "CONSTRAINT [FK_U_Up] FOREIGN KEY ([Up]) REFERENCES [s].[U] ([Id]))"
)
# a system-versioned temporal table; its history table [s].[V_History] is not an object of the model
V = (
    "CREATE TABLE [s].[V] ([Id] int NOT NULL, [Note] varchar(20) NULL, "
    "[From] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL, "
    "[To] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL, "
    "CONSTRAINT [PK_V] PRIMARY KEY CLUSTERED ([Id]), PERIOD FOR SYSTEM_TIME ([From], [To])) "
    "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [s].[V_History]))"
)
# dynamic data masking: one masked column, one plain column, one computed column
MASKED = (
    "CREATE TABLE [s].[Masked] ([Id] int NOT NULL, "
    "[Mail] varchar(100) MASKED WITH (FUNCTION = 'email()') NULL, "
    "[Plain] varchar(10) NULL, [Shown] AS ([Id] + 1))"
)
# wider table coverage: a compressed heap with every column property, and a clustered columnstore table
WIDE = (
    "CREATE TABLE [s].[Wide] ([Id] int IDENTITY(1, 1) NOT NULL, [Guid] uniqueidentifier ROWGUIDCOL NOT NULL, "
    "[Other] uniqueidentifier NOT NULL, [Thin] varchar(10) SPARSE NULL, [Fat] varchar(10) NULL, "
    "[Used] int NULL, [Num] int NOT NULL, [Calc] AS ([Id] + 1), "
    "INDEX [IX_Wide_Used] NONCLUSTERED ([Used])) WITH (DATA_COMPRESSION = PAGE)"
)
CCI = "CREATE TABLE [s].[Cci] ([a] int NOT NULL, [b] int NULL, INDEX [CCI] CLUSTERED COLUMNSTORE)"
PROPERTY = "ALTER TABLE [s].[Wide] ALTER COLUMN [{}] {}"
REBUILD = "ALTER TABLE [s].[{}] REBUILD WITH (DATA_COMPRESSION = {})"
ADD_MASKED = "ALTER TABLE [s].[{}] ALTER COLUMN [{}] ADD MASKED WITH (FUNCTION = 'default()')"
DROP_MASKED = "ALTER TABLE [s].[{}] ALTER COLUMN [{}] DROP MASKED"
VERSIONING_OFF = "ALTER TABLE [s].[V] SET (SYSTEM_VERSIONING = OFF)"
VERSIONING_ON = "ALTER TABLE [s].[{}] SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [{}].[{}]))"
WORLD = model_of(
    "CREATE SCHEMA [s]",
    "CREATE SCHEMA [empty]",
    "CREATE TYPE [s].[Code] FROM char(4) NOT NULL",
    "CREATE TYPE [s].[Unused] FROM int NOT NULL",
    "CREATE TYPE [s].[InRows] FROM int NOT NULL",
    "CREATE TYPE [s].[Rows] AS TABLE ([N] [s].[InRows] NOT NULL)",
    "CREATE TYPE [s].[Number] FROM int NOT NULL",
    SEQUENCE.format("Seq"),
    SEQUENCE.format("Idle"),
    SEQUENCE.format("Typed").replace("AS int", "AS [s].[Number]"),
    "CREATE SYNONYM [s].[Syn] FOR [s].[P]",
    "CREATE TABLE [s].[One] ([Only] int NULL)",
    P,
    T,
    U,
    V,
    MASKED,
    WIDE,
    CCI,
)
ADD_FK = "ALTER TABLE [s].[U] ADD CONSTRAINT [FK_X] FOREIGN KEY "
RENAME = "EXEC sys.sp_rename N'[s].{}', N'{}', N'{}'"
NEW_SEQUENCE = "CREATE SEQUENCE {} AS int START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 9 NO CYCLE NO CACHE"

# (statement, object key of the error, a phrase of its message)
BLOCKED = [
    # ---- column properties, heap compression, columnstore
    (PROPERTY.format("Guid", "ADD ROWGUIDCOL"), "TABLE:[s].[Wide]", "[Guid] is already ROWGUIDCOL"),
    (PROPERTY.format("Other", "ADD ROWGUIDCOL"), "TABLE:[s].[Wide]", "more than one ROWGUIDCOL column"),
    (PROPERTY.format("Num", "ADD ROWGUIDCOL"), "TABLE:[s].[Wide]", "the data type uniqueidentifier"),
    (PROPERTY.format("Other", "DROP ROWGUIDCOL"), "TABLE:[s].[Wide]", "[Other] is not ROWGUIDCOL"),
    (PROPERTY.format("Used", "ADD SPARSE"), "TABLE:[s].[Wide]", "is blocked by index [IX_Wide_Used]"),
    (PROPERTY.format("Num", "ADD SPARSE"), "TABLE:[s].[Wide]", "a SPARSE column is NULL"),
    (PROPERTY.format("Id", "ADD SPARSE"), "TABLE:[s].[Wide]", "is blocked by computed column [Calc]"),
    (PROPERTY.format("Fat", "DROP SPARSE"), "TABLE:[s].[Wide]", "[Fat] is not SPARSE"),
    (PROPERTY.format("Calc", "ADD SPARSE"), "TABLE:[s].[Wide]", "is computed"),
    (PROPERTY.format("Nope", "ADD SPARSE"), "TABLE:[s].[Wide]", "[Nope]"),
    (PROPERTY.format("Num", "ADD NOT FOR REPLICATION"), "TABLE:[s].[Wide]", "has no IDENTITY"),
    (PROPERTY.format("Id", "DROP NOT FOR REPLICATION"), "TABLE:[s].[Wide]", "is not NOT FOR REPLICATION"),
    (PROPERTY.format("Thin", "varchar(20) NULL"), "TABLE:[s].[Wide]", "DROP SPARSE before this statement"),
    (PROPERTY.format("Guid", "uniqueidentifier NULL"), "TABLE:[s].[Wide]", "DROP ROWGUIDCOL before"),
    (REBUILD.format("Cci", "PAGE"), "TABLE:[s].[Cci]", "the clustered columnstore index [CCI]"),
    (REBUILD.format("Nope", "PAGE"), "TABLE:[s].[Nope]", "the table does not exist"),
    ("CREATE CLUSTERED INDEX [CX] ON [s].[Wide] ([Id])", "TABLE:[s].[Wide]", "it would inherit PAGE"),
    (
        "ALTER TABLE [s].[Wide] ADD CONSTRAINT [PK_Wide] PRIMARY KEY CLUSTERED ([Id])",
        "TABLE:[s].[Wide]",
        "states no DATA_COMPRESSION",
    ),
    (
        "CREATE CLUSTERED INDEX [CX] ON [s].[P] ([Code])",
        "TABLE:[s].[P]",
        "clustered index or key [PK_P] already",
    ),
    ("CREATE CLUSTERED COLUMNSTORE INDEX [C2] ON [s].[P]", "TABLE:[s].[P]", "[PK_P] already"),
    (
        "ALTER TABLE [s].[Cci] ADD CONSTRAINT [UQ_Cci] UNIQUE CLUSTERED ([a])",
        "TABLE:[s].[Cci]",
        "clustered index or key [CCI] already",
    ),
    (
        "CREATE NONCLUSTERED COLUMNSTORE INDEX [N2] ON [s].[Cci] ([a])",
        "TABLE:[s].[Cci]",
        "more than one columnstore index",
    ),
    ("CREATE NONCLUSTERED COLUMNSTORE INDEX [N2] ON [s].[P] ([Nope])", "TABLE:[s].[P]", "[Nope]"),
    # ---- dynamic data masking (each refusal was seen on the engine)
    (DROP_MASKED.format("Masked", "Plain"), "TABLE:[s].[Masked]", "does not have a masking function"),
    (DROP_MASKED.format("Masked", "Shown"), "TABLE:[s].[Masked]", "is computed"),
    (DROP_MASKED.format("Masked", "Nope"), "TABLE:[s].[Masked]", "[Nope]"),
    (DROP_MASKED.format("Nope", "Mail"), "TABLE:[s].[Nope]", "the table does not exist"),
    (ADD_MASKED.format("Masked", "Shown"), "TABLE:[s].[Masked]", "is computed"),
    (ADD_MASKED.format("Masked", "Nope"), "TABLE:[s].[Masked]", "[Nope]"),
    (ADD_MASKED.format("Nope", "Mail"), "TABLE:[s].[Nope]", "the table does not exist"),
    (ADD_MASKED.format("V", "From"), "TABLE:[s].[V]", "it is a period column"),
    # ---- system-versioned temporal tables (each refusal was seen on the engine, but the rename)
    ("DROP TABLE [s].[V]", "TABLE:[s].[V]", "SET (SYSTEM_VERSIONING = OFF) comes before DROP TABLE"),
    (
        "ALTER TABLE [s].[V] ALTER COLUMN [From] datetime2(3) NOT NULL",
        "TABLE:[s].[V]",
        "it is a period column",
    ),
    ("ALTER TABLE [s].[V] DROP COLUMN [To]", "TABLE:[s].[V]", "it is a period column"),
    (RENAME.format("[V].[To]", "Until", "COLUMN"), "TABLE:[s].[V]", "it is a period column"),
    (
        "ALTER TABLE [s].[V] DROP CONSTRAINT [PK_V]",
        "TABLE:[s].[V]",
        "PRIMARY KEY of a system-versioned table",
    ),
    ("ALTER TABLE [s].[One] SET (SYSTEM_VERSIONING = OFF)", "TABLE:[s].[One]", "is not system-versioned"),
    ("ALTER TABLE [s].[Nope] SET (SYSTEM_VERSIONING = OFF)", "TABLE:[s].[Nope]", "the table does not exist"),
    (VERSIONING_ON.format("V", "s", "V_History"), "TABLE:[s].[V]", "is system-versioned already"),
    (VERSIONING_ON.format("One", "s", "One_History"), "TABLE:[s].[One]", "has no period columns"),
    ("CREATE TABLE [s].[V_History] ([x] int NULL)", "TABLE:[s].[V_History]", "taken by the history table of"),
    (V.replace("[s].[V]", "[s].[V2]").replace("PK_V", "PK_V2"), "TABLE:[s].[V2]", "is the history table of"),
    (
        V.replace("[s].[V]", "[s].[V2]").replace("PK_V", "PK_V2").replace("[s].[V_History]", "[s].[P]"),
        "TABLE:[s].[V2]",
        "has the name of TABLE:[s].[P]",
    ),
    (
        V.replace("[s].[V]", "[s].[V2]").replace("PK_V", "PK_V2").replace("[s].[V_History]", "[nope].[H]"),
        "TABLE:[s].[V2]",
        "the schema [nope] does not exist",
    ),
    # ---- the object that a statement names exists
    ("DROP TABLE [s].[Nope]", "TABLE:[s].[Nope]", "the table does not exist"),
    ("DROP TABLE [s].[Syn]", "TABLE:[s].[Syn]", "the table does not exist"),
    ("DROP TABLE [s].[Rows]", "TABLE:[s].[Rows]", "the table does not exist"),
    ("DROP SCHEMA [nope]", "SCHEMA:[nope]", "the schema does not exist"),
    ("DROP SCHEMA [dbo]", "SCHEMA:[dbo]", "the schema does not exist"),
    ("DROP TYPE [s].[Nope]", "TYPE:[s].[Nope]", "the type does not exist"),
    ("DROP SEQUENCE [s].[Nope]", "SEQUENCE:[s].[Nope]", "the sequence does not exist"),
    ("ALTER SEQUENCE [s].[Nope] RESTART", "SEQUENCE:[s].[Nope]", "the sequence does not exist"),
    ("DROP SYNONYM [s].[Nope]", "SYNONYM:[s].[Nope]", "the synonym does not exist"),
    ("ALTER TABLE [s].[Nope] ADD [X] int NULL", "TABLE:[s].[Nope]", "the table does not exist"),
    ("ALTER TABLE [s].[T] ALTER COLUMN [Nope] int NULL", "TABLE:[s].[T]", "no column [Nope]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [Nope]", "TABLE:[s].[T]", "no column [Nope]"),
    ("ALTER TABLE [s].[T] DROP CONSTRAINT [Nope]", "TABLE:[s].[T]", "no constraint [Nope]"),
    ("ALTER TABLE [s].[T] DROP CONSTRAINT [IX_T]", "TABLE:[s].[T]", "no constraint [IX_T]"),
    ("ALTER TABLE [s].[U] DROP CONSTRAINT [CK_T]", "TABLE:[s].[U]", "no constraint [CK_T]"),
    ("DROP INDEX [Nope] ON [s].[T]", "TABLE:[s].[T]", "no index [Nope]"),
    ("DROP INDEX [PK_T] ON [s].[T]", "TABLE:[s].[T]", "it is PRIMARY KEY [PK_T], use DROP CONSTRAINT"),
    ("DROP INDEX [IX_T] ON [s].[U]", "TABLE:[s].[U]", "no index [IX_T]"),
    ("CREATE NONCLUSTERED INDEX [IX_X] ON [s].[Nope] ([Id])", "TABLE:[s].[Nope]", "the table does not exist"),
    ("CREATE NONCLUSTERED INDEX [IX_X] ON [s].[T] ([Nope])", "TABLE:[s].[T]", "names the column [Nope]"),
    (
        "CREATE NONCLUSTERED INDEX [IX_X] ON [s].[T] ([Free]) INCLUDE ([Nope])",
        "TABLE:[s].[T]",
        "column [Nope]",
    ),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [UQ_X] UNIQUE NONCLUSTERED ([Nope])",
        "TABLE:[s].[T]",
        "column [Nope]",
    ),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [DF_X] DEFAULT ((1)) FOR [Nope]",
        "TABLE:[s].[T]",
        "no column [Nope]",
    ),
    (ADD_FK + "([Nope]) REFERENCES [s].[P] ([Id])", "TABLE:[s].[U]", "names the column [Nope]"),
    # ---- the schema of a new object and the alias type of a column exist
    (
        "CREATE TABLE [nope].[New] ([Id] int NOT NULL)",
        "TABLE:[nope].[New]",
        "the schema [nope] does not exist",
    ),
    ("CREATE TYPE [nope].[New] FROM int NULL", "TYPE:[nope].[New]", "the schema [nope] does not exist"),
    (NEW_SEQUENCE.format("[nope].[New]"), "SEQUENCE:[nope].[New]", "the schema [nope] does not exist"),
    ("CREATE SYNONYM [nope].[New] FOR [s].[P]", "SYNONYM:[nope].[New]", "the schema [nope] does not exist"),
    (
        "CREATE TABLE [s].[New] ([Id] [s].[Nope] NOT NULL)",
        "TABLE:[s].[New]",
        "data type [s].[Nope] of column [Id]",
    ),
    (
        "CREATE TABLE [s].[New] ([Id] [s].[Rows] NOT NULL)",
        "TABLE:[s].[New]",
        "data type [s].[Rows] of column [Id]",
    ),
    ("CREATE TYPE [s].[New] AS TABLE ([C] [s].[Nope] NULL)", "TYPE:[s].[New]", "data type [s].[Nope]"),
    (
        "ALTER TABLE [s].[T] ADD [New] [s].[Nope] NULL",
        "TABLE:[s].[T]",
        "data type [s].[Nope] of column [New]",
    ),
    ("ALTER TABLE [s].[T] ALTER COLUMN [Free] [s].[Nope] NULL", "TABLE:[s].[T]", "data type [s].[Nope]"),
    (
        NEW_SEQUENCE.format("[s].[New]").replace("AS int", "AS [s].[Nope]"),
        "SEQUENCE:[s].[New]",
        "data type [s].[Nope] of the sequence",
    ),
    # ---- the columns inside a new table exist
    (
        "CREATE TABLE [s].[New] ([Id] int NOT NULL, CONSTRAINT [PK_New] PRIMARY KEY CLUSTERED ([Nope]))",
        "TABLE:[s].[New]",
        "PRIMARY KEY [PK_New] names the column [Nope]",
    ),
    (
        "CREATE TABLE [s].[New] ([Id] int NOT NULL, INDEX [IX_New] NONCLUSTERED ([Id]) INCLUDE ([Nope]))",
        "TABLE:[s].[New]",
        "index [IX_New] names the column [Nope]",
    ),
    # ---- a name that a statement creates is free
    ("CREATE SCHEMA [s]", "SCHEMA:[s]", "the schema exists already"),
    ("CREATE SCHEMA [S]", "SCHEMA:[S]", "the schema exists already"),
    ("CREATE TYPE [s].[Code] FROM int NOT NULL", "TYPE:[s].[Code]", "the type exists already"),
    ("CREATE TYPE [s].[Rows] FROM int NOT NULL", "TYPE:[s].[Rows]", "the type exists already"),
    ("CREATE TABLE [s].[P] ([Id] int NOT NULL)", "TABLE:[s].[P]", "[P] is taken by TABLE:[s].[P]"),
    ("CREATE TABLE [s].[Syn] ([Id] int NOT NULL)", "TABLE:[s].[Syn]", "[Syn] is taken by SYNONYM:[s].[Syn]"),
    ("CREATE TABLE [s].[Seq] ([Id] int NOT NULL)", "TABLE:[s].[Seq]", "[Seq] is taken by SEQUENCE:[s].[Seq]"),
    (
        "CREATE TABLE [s].[PK_P] ([Id] int NOT NULL)",
        "TABLE:[s].[PK_P]",
        "taken by a constraint of TABLE:[s].[P]",
    ),
    (
        "CREATE TABLE [s].[DF_T] ([Id] int NOT NULL)",
        "TABLE:[s].[DF_T]",
        "taken by a constraint of TABLE:[s].[T]",
    ),
    (
        "CREATE TABLE [s].[New] ([Id] int NOT NULL CONSTRAINT [DF_T] DEFAULT ((0)))",
        "TABLE:[s].[New]",
        "[DF_T] is taken by a constraint of TABLE:[s].[T]",
    ),
    (
        "CREATE TABLE [s].[New] ([Id] int NOT NULL, CONSTRAINT [New] CHECK ([Id] > 0))",
        "TABLE:[s].[New]",
        "the name of the table and of a constraint",
    ),
    (NEW_SEQUENCE.format("[s].[Seq]"), "SEQUENCE:[s].[Seq]", "[Seq] is taken by SEQUENCE:[s].[Seq]"),
    (NEW_SEQUENCE.format("[s].[P]"), "SEQUENCE:[s].[P]", "[P] is taken by TABLE:[s].[P]"),
    ("CREATE SYNONYM [s].[Syn] FOR [s].[U]", "SYNONYM:[s].[Syn]", "[Syn] is taken by SYNONYM:[s].[Syn]"),
    ("CREATE SYNONYM [s].[T] FOR [s].[U]", "SYNONYM:[s].[T]", "[T] is taken by TABLE:[s].[T]"),
    ("ALTER TABLE [s].[T] ADD [Free] int NULL", "TABLE:[s].[T]", "a column [Free] already"),
    ("ALTER TABLE [s].[T] ADD [FREE] int NULL", "TABLE:[s].[T]", "a column [FREE] already"),
    (
        "ALTER TABLE [s].[T] ADD [New] int NOT NULL CONSTRAINT [PK_P] DEFAULT ((0))",
        "TABLE:[s].[T]",
        "[PK_P] is taken by a constraint of TABLE:[s].[P]",
    ),
    ("ALTER TABLE [s].[T] ADD CONSTRAINT [CK_T] CHECK ([Free] > 0)", "TABLE:[s].[T]", "[CK_T] is taken"),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [PK_U] CHECK ([Free] > 0)",
        "TABLE:[s].[T]",
        "a constraint of TABLE:[s].[U]",
    ),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [U] CHECK ([Free] > 0)",
        "TABLE:[s].[T]",
        "[U] is taken by TABLE:[s].[U]",
    ),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [IX_T] UNIQUE NONCLUSTERED ([Free])",
        "TABLE:[s].[T]",
        "the table has an index named [IX_T]",
    ),
    ("CREATE NONCLUSTERED INDEX [IX_T] ON [s].[T] ([Free])", "TABLE:[s].[T]", "an index or key named [IX_T]"),
    ("CREATE NONCLUSTERED INDEX [PK_T] ON [s].[T] ([Free])", "TABLE:[s].[T]", "an index or key named [PK_T]"),
    # ---- one PRIMARY KEY, one DEFAULT of a column, and none on a computed column
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [PK_T2] PRIMARY KEY NONCLUSTERED ([Free])",
        "TABLE:[s].[T]",
        "the table has PRIMARY KEY [PK_T] already",
    ),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [DF_X] DEFAULT ((1)) FOR [WithDefault]",
        "TABLE:[s].[T]",
        "column [WithDefault] has DEFAULT [DF_T] already",
    ),
    (
        "ALTER TABLE [s].[T] ADD CONSTRAINT [DF_X] DEFAULT ((1)) FOR [Calc]",
        "TABLE:[s].[T]",
        "[Calc] is computed",
    ),
    ("ALTER TABLE [s].[T] ALTER COLUMN [Calc] int NULL", "TABLE:[s].[T]", "column [Calc] is computed"),
    # ---- a foreign key needs a key on exactly its referenced columns, and equal data types
    (
        ADD_FK + "([Id]) REFERENCES [s].[Nope] ([Id])",
        "TABLE:[s].[U]",
        "referenced table [s].[Nope] does not exist",
    ),
    (
        ADD_FK + "([Id]) REFERENCES [s].[Syn] ([Id])",
        "TABLE:[s].[U]",
        "referenced table [s].[Syn] does not exist",
    ),
    (ADD_FK + "([Id]) REFERENCES [s].[P] ([Nope])", "TABLE:[s].[U]", "[s].[P] has no column [Nope]"),
    (
        ADD_FK + "([Id]) REFERENCES [s].[P] ([Loose])",
        "TABLE:[s].[U]",
        "no PRIMARY KEY, UNIQUE constraint or unique",
    ),
    (ADD_FK + "([Id]) REFERENCES [s].[P] ([A])", "TABLE:[s].[U]", "unique index on ([A])"),
    (
        ADD_FK + "([Id], [TRef]) REFERENCES [s].[P] ([Id], [A])",
        "TABLE:[s].[U]",
        "unique index on ([Id], [A])",
    ),
    (ADD_FK + "([Id]) REFERENCES [s].[P] ([Half])", "TABLE:[s].[U]", "unique index on ([Half])"),
    (
        ADD_FK + "([Id]) REFERENCES [s].[P] ([Code])",
        "TABLE:[s].[U]",
        "[Id] and [s].[P].[Code] differ in data type",
    ),
    (ADD_FK + "([Wide]) REFERENCES [s].[P] ([Code])", "TABLE:[s].[U]", "[Wide] and [s].[P].[Code] differ"),
    (
        "CREATE TABLE [s].[New] ([PId] int NOT NULL, CONSTRAINT [FK_New] FOREIGN KEY ([PId]) "
        "REFERENCES [s].[Nope] ([Id]))",
        "TABLE:[s].[New]",
        "FOREIGN KEY [FK_New]: the referenced table [s].[Nope] does not exist",
    ),
    (
        "CREATE TABLE [s].[New] ([Id] int NOT NULL, [Up] int NULL, CONSTRAINT [FK_New] FOREIGN KEY ([Up]) "
        "REFERENCES [s].[New] ([Id]))",
        "TABLE:[s].[New]",
        "[s].[New] has no PRIMARY KEY",
    ),
    # ---- ALTER COLUMN is blocked by what uses the column
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InIndex] bigint NOT NULL",
        "TABLE:[s].[T]",
        "blocked by index [IX_T]",
    ),
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InInclude] bigint NOT NULL",
        "TABLE:[s].[T]",
        "blocked by index [IX_T]",
    ),
    ("ALTER TABLE [s].[T] ALTER COLUMN [InFilter] bigint NULL", "TABLE:[s].[T]", "blocked by index [IX_T]"),
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InPk] bigint NOT NULL",
        "TABLE:[s].[T]",
        "blocked by PRIMARY KEY [PK_T]",
    ),
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InUnique] bigint NOT NULL",
        "TABLE:[s].[T]",
        "blocked by UNIQUE [UQ_T]",
    ),
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InFk] bigint NULL",
        "TABLE:[s].[T]",
        "blocked by FOREIGN KEY [FK_T_P]",
    ),
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InCheck] bigint NOT NULL",
        "TABLE:[s].[T]",
        "blocked by CHECK [CK_T]",
    ),
    (
        "ALTER TABLE [s].[T] ALTER COLUMN [InComputed] bigint NOT NULL",
        "TABLE:[s].[T]",
        "computed column [Calc]",
    ),
    ("ALTER TABLE [s].[T] ALTER COLUMN [Referenced] bigint NOT NULL", "TABLE:[s].[T]", "[FK_U_T] of [s].[U]"),
    ("ALTER TABLE [s].[T] ALTER COLUMN [incheck] int NULL", "TABLE:[s].[T]", "blocked by CHECK [CK_T]"),
    (
        "ALTER TABLE [s].[U] ALTER COLUMN [Id] bigint NOT NULL",
        "TABLE:[s].[U]",
        "FOREIGN KEY [FK_U_Up] of [s].[U]",
    ),
    # ---- DROP COLUMN is blocked by the same, and by the DEFAULT of the column
    ("ALTER TABLE [s].[T] DROP COLUMN [InIndex]", "TABLE:[s].[T]", "blocked by index [IX_T]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InInclude]", "TABLE:[s].[T]", "blocked by index [IX_T]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InFilter]", "TABLE:[s].[T]", "blocked by index [IX_T]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InPk]", "TABLE:[s].[T]", "blocked by PRIMARY KEY [PK_T]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InUnique]", "TABLE:[s].[T]", "blocked by UNIQUE [UQ_T]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InFk]", "TABLE:[s].[T]", "blocked by FOREIGN KEY [FK_T_P]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InCheck]", "TABLE:[s].[T]", "blocked by CHECK [CK_T]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [InComputed]", "TABLE:[s].[T]", "blocked by computed column [Calc]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [Referenced]", "TABLE:[s].[T]", "FOREIGN KEY [FK_U_T] of [s].[U]"),
    ("ALTER TABLE [s].[T] DROP COLUMN [WithDefault]", "TABLE:[s].[T]", "blocked by DEFAULT [DF_T]"),
    ("ALTER TABLE [s].[One] DROP COLUMN [Only]", "TABLE:[s].[One]", "[Only] is the only column"),
    # ---- a drop is blocked by what depends on the object
    ("DROP TABLE [s].[T]", "TABLE:[s].[T]", "referenced by FOREIGN KEY [FK_U_T] of [s].[U]"),
    ("DROP TABLE [s].[P]", "TABLE:[s].[P]", "referenced by FOREIGN KEY [FK_T_P] of [s].[T]"),
    (
        "ALTER TABLE [s].[T] DROP CONSTRAINT [UQ_T_Referenced]",
        "TABLE:[s].[T]",
        "the key that FOREIGN KEY [FK_U_T]",
    ),
    ("ALTER TABLE [s].[P] DROP CONSTRAINT [PK_P]", "TABLE:[s].[P]", "PRIMARY KEY [PK_P] is the key that"),
    (
        "ALTER TABLE [s].[U] DROP CONSTRAINT [PK_U]",
        "TABLE:[s].[U]",
        "FOREIGN KEY [FK_U_Up] of [s].[U] references",
    ),
    ("DROP TYPE [s].[Code]", "TYPE:[s].[Code]", "used by column [Tag] of TABLE:[s].[T]"),
    ("DROP TYPE [s].[InRows]", "TYPE:[s].[InRows]", "used by column [N] of TYPE:[s].[Rows]"),
    ("DROP TYPE [s].[Number]", "TYPE:[s].[Number]", "used by SEQUENCE:[s].[Typed]"),
    ("DROP SEQUENCE [s].[Seq]", "SEQUENCE:[s].[Seq]", "used by DEFAULT [DF_T_No] of [s].[T]"),
    ("DROP SCHEMA [s]", "SCHEMA:[s]", "the schema is not empty"),
    # ---- sp_rename: the old name exists, the new name is free, no expression names the column
    (RENAME.format("[T].[Nope]", "X", "COLUMN"), "TABLE:[s].[T]", "no column [Nope]"),
    (RENAME.format("[Nope].[Free]", "X", "COLUMN"), "TABLE:[s].[Nope]", "the table does not exist"),
    (RENAME.format("[T].[Free]", "InPk", "COLUMN"), "TABLE:[s].[T]", "a column [InPk] already"),
    (RENAME.format("[T].[InCheck]", "X", "COLUMN"), "TABLE:[s].[T]", "blocked by CHECK [CK_T]"),
    (RENAME.format("[T].[InComputed]", "X", "COLUMN"), "TABLE:[s].[T]", "blocked by computed column [Calc]"),
    (RENAME.format("[T].[InFilter]", "X", "COLUMN"), "TABLE:[s].[T]", "blocked by index [IX_T]"),
    (RENAME.format("[T].[Nope]", "X", "INDEX"), "TABLE:[s].[T]", "no index [Nope]"),
    (RENAME.format("[T].[CK_T]", "X", "INDEX"), "TABLE:[s].[T]", "no index [CK_T]"),
    (RENAME.format("[T].[IX_T]", "PK_T", "INDEX"), "TABLE:[s].[T]", "an index or key named [PK_T] already"),
    (RENAME.format("[T].[PK_T]", "PK_P", "INDEX"), "TABLE:[s].[T]", "[PK_P] is taken by a constraint"),
    (RENAME.format("[Nope]", "X", "OBJECT"), "SCHEMA:[s]", "no table or constraint named [Nope]"),
    (RENAME.format("[IX_T]", "X", "OBJECT"), "SCHEMA:[s]", "no table or constraint named [IX_T]"),
    (RENAME.format("[Seq]", "X", "OBJECT"), "SCHEMA:[s]", "no table or constraint named [Seq]"),
    (RENAME.format("[T]", "U", "OBJECT"), "TABLE:[s].[T]", "[U] is taken by TABLE:[s].[U]"),
    (RENAME.format("[T]", "Syn", "OBJECT"), "TABLE:[s].[T]", "[Syn] is taken by SYNONYM:[s].[Syn]"),
    (RENAME.format("[T]", "PK_P", "OBJECT"), "TABLE:[s].[T]", "[PK_P] is taken by a constraint"),
    (RENAME.format("[CK_T]", "PK_U", "OBJECT"), "TABLE:[s].[T]", "[PK_U] is taken by a constraint"),
    (RENAME.format("[DF_T]", "P", "OBJECT"), "TABLE:[s].[T]", "[P] is taken by TABLE:[s].[P]"),
    (RENAME.format("[PK_T]", "IX_T", "OBJECT"), "TABLE:[s].[T]", "an index named [IX_T] already"),
    # ---- a schema that every database has cannot be created: no migration and no schema file for it
    ("CREATE SCHEMA [dbo]", "SCHEMA:[dbo]", "exists in every database"),
    ("CREATE SCHEMA [SYS]", "SCHEMA:[SYS]", "exists in every database"),
    ("CREATE SCHEMA [information_schema]", "SCHEMA:[information_schema]", "exists in every database"),
    ("CREATE SCHEMA [db_owner]", "SCHEMA:[db_owner]", "exists in every database"),
    ("CREATE SCHEMA [azsqlcd]", "SCHEMA:[azsqlcd]", "the schema of the tool"),
    # the old name, letter for letter: the engine says that the name is in use (error 15335)
    (RENAME.format("[T].[Free]", "Free", "COLUMN"), "TABLE:[s].[T]", "the new name is the old name"),
    (RENAME.format("[T].[IX_T]", "IX_T", "INDEX"), "TABLE:[s].[T]", "the new name is the old name"),
    (RENAME.format("[T].[PK_T]", "PK_T", "INDEX"), "TABLE:[s].[T]", "the new name is the old name"),
    (RENAME.format("[T]", "T", "OBJECT"), "TABLE:[s].[T]", "the new name is the old name"),
    (RENAME.format("[CK_T]", "CK_T", "OBJECT"), "TABLE:[s].[T]", "the new name is the old name"),
    (RENAME.format("[DF_T]", "DF_T", "OBJECT"), "TABLE:[s].[T]", "the new name is the old name"),
]

# near misses: each statement stands next to a blocked one and must replay
ACCEPTED = [
    # ---- column properties, heap compression, columnstore
    PROPERTY.format("Guid", "DROP ROWGUIDCOL"),
    PROPERTY.format("Fat", "ADD SPARSE"),
    PROPERTY.format("Thin", "DROP SPARSE"),
    PROPERTY.format("Id", "ADD NOT FOR REPLICATION"),
    PROPERTY.format("Fat", "varchar(20) NULL"),
    REBUILD.format("Wide", "ROW"),
    REBUILD.format("Wide", "NONE"),
    REBUILD.format("One", "PAGE"),
    REBUILD.format("P", "ROW"),  # a clustered table: the key then states the compression
    "CREATE CLUSTERED INDEX [CX] ON [s].[Wide] ([Id]) WITH (DATA_COMPRESSION = NONE)",
    "CREATE CLUSTERED COLUMNSTORE INDEX [CCI_Wide] ON [s].[Wide]",
    "CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI_P] ON [s].[P] ([A], [B]) WHERE [A] > 0",
    "DROP INDEX [CCI] ON [s].[Cci]",
    # ---- dynamic data masking
    DROP_MASKED.format("Masked", "Mail"),
    ADD_MASKED.format("Masked", "Mail"),  # a masked column: the mask is replaced
    ADD_MASKED.format("Masked", "Plain"),
    ADD_MASKED.format("V", "Note"),  # a column of a temporal table that is not a period column
    "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] varchar(200) NULL",
    "ALTER TABLE [s].[Masked] ADD [Tax] varchar(20) MASKED WITH (FUNCTION = 'default()') NULL",
    # ---- system-versioned temporal tables
    VERSIONING_OFF,
    V.replace("[s].[V]", "[s].[V2]").replace("PK_V", "PK_V2").replace("[s].[V_History]", "[empty].[V2]"),
    "ALTER TABLE [s].[V] ADD [More] int NULL",
    "ALTER TABLE [s].[V] ALTER COLUMN [Note] varchar(40) NULL",
    "ALTER TABLE [s].[V] DROP COLUMN [Note]",
    RENAME.format("[V].[Note]", "Remark", "COLUMN"),
    RENAME.format("[V]", "Versioned", "OBJECT"),
    # ---- exists
    "DROP TABLE [s].[U]",  # holds foreign keys, also one to itself; nothing references it
    "DROP TABLE [s].[One]",
    "DROP SCHEMA [empty]",
    "DROP TYPE [s].[Unused]",
    "DROP TYPE [s].[Rows]",
    "DROP SEQUENCE [s].[Idle]",  # no DEFAULT uses it
    "ALTER SEQUENCE [s].[Seq] RESTART",
    "DROP SYNONYM [s].[Syn]",
    "DROP INDEX [IX_T] ON [s].[T]",
    "DROP INDEX [UX_P_AB] ON [s].[P]",  # a unique index that no foreign key needs
    "ALTER TABLE [s].[T] DROP CONSTRAINT [CK_T]",
    "ALTER TABLE [s].[T] DROP CONSTRAINT [DF_T]",  # a DEFAULT is dropped by its name
    "ALTER TABLE [s].[T] DROP CONSTRAINT [UQ_T]",  # a key that no foreign key needs
    "ALTER TABLE [s].[T] DROP CONSTRAINT [FK_T_P]",
    # ---- schema and alias type
    "CREATE TABLE [dbo].[New] ([Id] int NOT NULL)",  # dbo needs no schema file
    "CREATE TABLE [DBO].[New] ([Id] int NOT NULL)",
    "CREATE TABLE [empty].[P] ([Id] int NOT NULL)",  # the name of a table of another schema
    "CREATE TABLE [s].[New] ([Id] [s].[Code] NOT NULL)",
    "CREATE TYPE [s].[New] AS TABLE ([C] [s].[Code] NULL)",
    "CREATE TYPE [dbo].[New] FROM int NULL",
    "ALTER TABLE [s].[T] ADD [New] [S].[CODE] NULL",
    "ALTER TABLE [s].[T] ALTER COLUMN [Free] [s].[Code] NULL",
    # ---- free names: another namespace is no collision
    "CREATE SCHEMA [new]",
    "CREATE TABLE [s].[Code] ([Id] int NOT NULL)",  # a type has its own namespace
    "CREATE TABLE [s].[IX_T] ([Id] int NOT NULL)",  # an index name belongs to its table
    "CREATE TYPE [s].[P] FROM int NOT NULL",
    "CREATE TABLE [empty].[New] ([Id] int NOT NULL, CONSTRAINT [PK_P] PRIMARY KEY CLUSTERED ([Id]))",
    NEW_SEQUENCE.format("[s].[New]"),
    NEW_SEQUENCE.format("[empty].[Seq]"),
    NEW_SEQUENCE.format("[s].[New]").replace("AS int", "AS [s].[Number]"),
    "CREATE SYNONYM [s].[New] FOR [nowhere].[Thing]",  # the target of a synonym is not checked
    "ALTER TABLE [s].[T] ADD [New] int NULL",
    "ALTER TABLE [s].[T] ADD [New] int NOT NULL CONSTRAINT [DF_T_New] DEFAULT ((0)) WITH VALUES",
    "ALTER TABLE [s].[T] ADD CONSTRAINT [CK_T2] CHECK ([Free] > 0)",
    "ALTER TABLE [s].[T] ADD CONSTRAINT [UQ_T_Free] UNIQUE NONCLUSTERED ([Free])",
    "CREATE NONCLUSTERED INDEX [CK_T] ON [s].[T] ([Free])",  # a CHECK is not in the index namespace
    "CREATE NONCLUSTERED INDEX [IX_T] ON [s].[U] ([Id])",  # the index name of another table
    "CREATE NONCLUSTERED INDEX [IX_X] ON [s].[T] ([Free]) INCLUDE ([InPk]) WITH (ONLINE = ON)",
    # ---- PRIMARY KEY and DEFAULT
    "ALTER TABLE [s].[One] ADD CONSTRAINT [PK_One] PRIMARY KEY CLUSTERED ([Only])",
    "ALTER TABLE [s].[T] ADD CONSTRAINT [DF_X] DEFAULT ((1)) FOR [Free]",
    # ---- foreign keys
    ADD_FK + "([Id]) REFERENCES [s].[P] ([Id])",  # PRIMARY KEY
    ADD_FK + "([PCode]) REFERENCES [s].[P] ([Code])",  # UNIQUE constraint
    ADD_FK + "([Id], [TRef]) REFERENCES [s].[P] ([A], [B])",  # unique index
    ADD_FK + "([Id], [TRef]) REFERENCES [s].[P] ([B], [A])",  # the columns of the key in another order
    ADD_FK + "([TRef]) REFERENCES [s].[U] ([Id])",  # the table itself
    "ALTER TABLE [s].[U] WITH NOCHECK ADD CONSTRAINT [FK_X] FOREIGN KEY ([Id]) REFERENCES [s].[P] ([Id])",
    "CREATE TABLE [s].[New] ([Id] int NOT NULL, [Up] int NULL, "
    "CONSTRAINT [PK_New] PRIMARY KEY CLUSTERED ([Id]), "
    "CONSTRAINT [FK_New] FOREIGN KEY ([Up]) REFERENCES [s].[New] ([Id]))",
    # ---- ALTER COLUMN and DROP COLUMN: a DEFAULT does not block ALTER COLUMN; a literal is not a column
    "ALTER TABLE [s].[T] ALTER COLUMN [Free] bigint NULL",
    "ALTER TABLE [s].[T] ALTER COLUMN [WithDefault] bigint NOT NULL",
    "ALTER TABLE [s].[T] ALTER COLUMN [No] bigint NOT NULL WITH (ONLINE = ON)",
    "ALTER TABLE [s].[T] DROP COLUMN [Free]",
    "ALTER TABLE [s].[T] DROP COLUMN [Calc]",  # a computed column that nothing uses
    "ALTER TABLE [s].[T] DROP COLUMN [Tag]",
    # ---- sp_rename
    RENAME.format("[T].[Free]", "Freed", "COLUMN"),
    RENAME.format("[T].[Free]", "FREE", "COLUMN"),  # the same name in other letters is free
    RENAME.format("[T].[InIndex]", "X", "COLUMN"),  # an index key follows the column
    RENAME.format("[T].[InPk]", "X", "COLUMN"),
    RENAME.format("[T].[Referenced]", "X", "COLUMN"),
    RENAME.format("[T].[WithDefault]", "X", "COLUMN"),
    RENAME.format("[T].[IX_T]", "IX_T2", "INDEX"),
    RENAME.format("[T].[IX_T]", "CK_T", "INDEX"),  # a CHECK is not in the index namespace
    RENAME.format("[T].[PK_T]", "PK_T2", "INDEX"),
    RENAME.format("[T]", "T2", "OBJECT"),
    RENAME.format("[T]", "Code", "OBJECT"),  # a type has its own namespace
    RENAME.format("[CK_T]", "CK_T2", "OBJECT"),
    RENAME.format("[DF_T]", "DF_T2", "OBJECT"),
    RENAME.format("[PK_T]", "PK_T2", "OBJECT"),
]


@pytest.mark.parametrize(("statement", "key", "says"), BLOCKED, ids=[row[0][:70] for row in BLOCKED])
def test_a_statement_that_the_engine_would_refuse_is_blocked_with_the_object_and_the_reason(
    statement: str, key: str, says: str
):
    with pytest.raises(ReplayError) as caught:
        apply(WORLD, parse_statement(statement))
    assert caught.value.object_key == key
    assert says in caught.value.message
    assert caught.value.op_index == 0


@pytest.mark.parametrize("statement", ACCEPTED, ids=[text[:70] for text in ACCEPTED])
def test_a_near_miss_of_a_precondition_replays_and_changes_the_model(statement: str):
    before = WORLD.to_canonical_json()
    after = apply(WORLD, parse_statement(statement))
    assert WORLD.to_canonical_json() == before, "apply() returns a new model"
    # two of the statements change no property: RESTART, and a rename to the same name in other letters
    same = statement in ("ALTER SEQUENCE [s].[Seq] RESTART", RENAME.format("[T].[Free]", "FREE", "COLUMN"))
    assert (after == WORLD) == same


def test_every_operation_class_has_a_blocked_statement_and_a_near_miss():
    def classes(statements: list[str]) -> set[type]:
        return {type(parse_statement(text)) for text in statements}

    every = set(typing.get_args(Operation))
    assert classes([row[0] for row in BLOCKED]) == every
    assert classes(ACCEPTED) == every


# ------------------------------------------------------------------ what a statement does to the model
def test_add_column_puts_the_column_last_with_its_default():
    after = run(WORLD, "ALTER TABLE [s].[One] ADD [New] int NOT NULL CONSTRAINT [DF_One_New] DEFAULT ((7))")
    assert sql(after, "One") == (
        "CREATE TABLE [s].[One] (\n"
        "    [Only] int NULL,\n"
        "    [New] int NOT NULL CONSTRAINT [DF_One_New] DEFAULT ((7))\n"
        ");"
    )


def test_alter_column_sets_type_nullability_and_collation_and_keeps_identity_and_default():
    before = model_of(
        "CREATE SCHEMA [s]",
        "CREATE TABLE [s].[W] ([Id] int IDENTITY(5, 2) NOT NULL, "
        "[Text] varchar(10) COLLATE Latin1_General_BIN2 NULL CONSTRAINT [DF_W] DEFAULT (''))",
    )
    after = run(
        before,
        "ALTER TABLE [s].[W] ALTER COLUMN [Id] bigint NOT NULL",
        "ALTER TABLE [s].[W] ALTER COLUMN [Text] nvarchar(20) NOT NULL",
    )
    identity, text = table(after, "W").columns
    assert (identity.type, identity.identity) == (parse_type("bigint"), Identity(5, 2))
    assert (text.type, text.nullable) == (parse_type("nvarchar(20)"), False)
    assert text.default is not None and text.default.name == "DF_W"
    # ALTER COLUMN states the whole of type, collation and nullability: no COLLATE means the default
    assert text.collation is None
    kept = run(before, "ALTER TABLE [s].[W] ALTER COLUMN [Text] varchar(50) COLLATE Latin1_General_BIN2 NULL")
    assert table(kept, "W").columns[1].collation == "Latin1_General_BIN2"


def parse_type(text: str) -> TypeRef:
    op = parse_statement(f"ALTER TABLE [s].[x] ALTER COLUMN [c] {text} NULL")
    assert isinstance(op, AlterColumn)
    return op.type


def test_drop_column_and_drop_constraint_remove_exactly_one_thing():
    after = run(WORLD, "ALTER TABLE [s].[T] DROP COLUMN [Free]", "ALTER TABLE [s].[T] DROP CONSTRAINT [DF_T]")
    before, now = table(WORLD, "T"), table(after, "T")
    assert [c.name for c in now.columns] == [c.name for c in before.columns if c.name != "Free"]
    assert column(now, "WithDefault").default is None and column(before, "WithDefault").default is not None
    assert now.constraints == before.constraints and now.indexes == before.indexes


def test_a_column_rename_is_followed_by_keys_indexes_and_foreign_keys_and_by_nothing_else():
    after = run(
        WORLD,
        RENAME.format("[T].[InPk]", "Key", "COLUMN"),
        RENAME.format("[T].[InIndex]", "Indexed", "COLUMN"),
        RENAME.format("[T].[InInclude]", "Included", "COLUMN"),
        RENAME.format("[T].[InFk]", "Parent", "COLUMN"),
        RENAME.format("[T].[Referenced]", "Target", "COLUMN"),
        RENAME.format("[T].[WithDefault]", "Defaulted", "COLUMN"),
    )
    expected = (
        T.replace("[InPk]", "[Key]")
        .replace("[InIndex]", "[Indexed]")
        .replace("[InInclude]", "[Included]")
        .replace("[InFk]", "[Parent]")
        .replace("[Referenced]", "[Target]")
        .replace("[WithDefault]", "[Defaulted]")
    )
    assert table(after, "T") == table(model_of(expected), "T")
    assert table(after, "U") == table(model_of(U.replace("[Referenced]", "[Target]")), "U")
    assert table(after, "P") == table(WORLD, "P")


def test_a_column_rename_reaches_the_foreign_key_of_the_table_itself_on_both_sides():
    after = run(
        WORLD, RENAME.format("[U].[Id]", "Key", "COLUMN"), RENAME.format("[U].[Up]", "Above", "COLUMN")
    )
    assert table(after, "U") == table(model_of(U.replace("[Id]", "[Key]").replace("[Up]", "[Above]")), "U")


def test_a_table_rename_is_followed_by_the_foreign_keys_that_reference_the_table():
    after = run(WORLD, RENAME.format("[T]", "T2", "OBJECT"), RENAME.format("[U]", "U2", "OBJECT"))
    assert "TABLE:[s].[T]" not in after and "TABLE:[s].[U]" not in after
    renamed_u = U.replace("[s].[T]", "[s].[T2]").replace("[s].[U]", "[s].[U2]")
    assert table(after, "U2") == table(model_of(renamed_u), "U2")
    assert table(after, "T2") == table(model_of(T.replace("[s].[T]", "[s].[T2]")), "T2")


def test_sp_rename_object_is_a_table_when_the_model_has_one_and_a_constraint_when_it_has_not():
    after = run(
        WORLD,
        RENAME.format("[One]", "Single", "OBJECT"),
        RENAME.format("[CK_T]", "CK_T_New", "OBJECT"),
        RENAME.format("[DF_T]", "DF_T_New", "OBJECT"),
        RENAME.format("[FK_U_T]", "FK_U_T_New", "OBJECT"),
    )
    assert "TABLE:[s].[Single]" in after and "TABLE:[s].[One]" not in after
    assert table(after, "T") == table(
        model_of(T.replace("[CK_T]", "[CK_T_New]").replace("[DF_T]", "[DF_T_New]")), "T"
    )
    assert table(after, "U") == table(model_of(U.replace("[FK_U_T]", "[FK_U_T_New]")), "U")


def test_the_rename_of_the_index_of_a_key_renames_the_constraint():
    after = run(
        WORLD,
        RENAME.format("[T].[PK_T]", "PK_T_New", "INDEX"),
        RENAME.format("[T].[IX_T]", "IX_New", "INDEX"),
    )
    assert table(after, "T") == table(
        model_of(T.replace("[PK_T]", "[PK_T_New]").replace("[IX_T]", "[IX_New]")), "T"
    )


def test_a_rename_in_other_letters_changes_the_spelling_and_not_the_identity():
    after = run(WORLD, RENAME.format("[T].[Free]", "FREE", "COLUMN"), RENAME.format("[One]", "ONE", "OBJECT"))
    assert after == WORLD  # names compare without case
    assert column(table(after, "T"), "free").name == "FREE"
    assert table(after, "one").name == "ONE"


def test_alter_sequence_changes_the_properties_that_it_writes():
    def sequence(model: Model) -> Sequence:
        found = model["SEQUENCE:[s].[Seq]"]
        assert isinstance(found, Sequence)
        return found

    def altered(clauses: str) -> Sequence:
        return sequence(run(WORLD, f"ALTER SEQUENCE [s].[Seq] {clauses}"))

    before = sequence(WORLD)
    assert (before.start, before.increment, before.cached, before.cache_size) == (1, 1, True, 10)
    assert altered("RESTART") == before
    assert altered("RESTART WITH 50").start == 50  # the catalog start value follows RESTART WITH
    assert altered("INCREMENT BY 5").increment == 5
    assert (altered("MINVALUE -5 MAXVALUE 500").minvalue, altered("MINVALUE -5 MAXVALUE 500").maxvalue) == (
        -5,
        500,
    )
    assert altered("CYCLE").cycle is True
    assert (altered("NO CACHE").cached, altered("NO CACHE").cache_size) == (False, None)
    assert (altered("CACHE").cached, altered("CACHE").cache_size) == (True, None)  # the engine default size
    assert (altered("CACHE 99").cached, altered("CACHE 99").cache_size) == (True, 99)
    only_one = altered("INCREMENT BY 5")
    assert (only_one.start, only_one.maxvalue, only_one.cache_size) == (1, 100, 10)


def test_how_a_statement_runs_is_not_part_of_the_model():
    plain = "CREATE NONCLUSTERED INDEX [IX_X] ON [s].[T] ([Free])"
    online = plain + " WITH (ONLINE = ON, MAXDOP = 2)"
    assert run(WORLD, plain) == run(WORLD, online)
    fk = "ADD CONSTRAINT [FK_X] FOREIGN KEY ([Id]) REFERENCES [s].[P] ([Id])"
    written = [f"ALTER TABLE [s].[U] {check}{fk}" for check in ("", "WITH CHECK ", "WITH NOCHECK ")]
    assert run(WORLD, written[0]) == run(WORLD, written[1]) == run(WORLD, written[2])
    add = "ALTER TABLE [s].[T] ADD [New] int NULL CONSTRAINT [DF_New] DEFAULT ((0))"
    assert run(WORLD, add) == run(WORLD, add + " WITH VALUES")


# ------------------------------------------------------------------ keys and their foreign keys
def test_no_key_on_the_columns_of_a_foreign_key_can_go_while_the_foreign_key_stands():
    # PP-6. The engine binds a foreign key to one index (sys.foreign_keys.key_index_id) and
    # refuses to drop that one (error 3725), whatever other key has the same columns. The model
    # does not hold which of the two it is, so neither can go while the foreign key stands.
    model = model_of(
        "CREATE SCHEMA [s]",
        "CREATE TABLE [s].[K] ([Id] int NOT NULL, [Other] int NOT NULL, "
        "CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([Id]), INDEX [UX_K] UNIQUE NONCLUSTERED ([Id]), "
        "INDEX [UX_K_Other] UNIQUE NONCLUSTERED ([Other]))",
        "CREATE TABLE [s].[C] ([KId] int NOT NULL, "
        "CONSTRAINT [FK_C_K] FOREIGN KEY ([KId]) REFERENCES [s].[K] ([Id]))",
    )
    for statement, says in (
        (
            "ALTER TABLE [s].[K] DROP CONSTRAINT [PK_K]",
            "PRIMARY KEY [PK_K] is the key that FOREIGN KEY [FK_C_K]",
        ),
        (
            "DROP INDEX [UX_K] ON [s].[K]",
            "index [UX_K] is the key that FOREIGN KEY [FK_C_K] of [s].[C] references",
        ),
    ):
        with pytest.raises(ReplayError) as caught:
            run(model, statement)
        assert caught.value.object_key == "TABLE:[s].[K]"
        assert says in caught.value.message
    run(model, "DROP INDEX [UX_K_Other] ON [s].[K]")  # near miss: a key on other columns
    # the order that the engine takes: the foreign key goes, the key goes, the foreign key comes
    # back on the key that stays
    again = "ALTER TABLE [s].[C] ADD CONSTRAINT [FK_C_K] FOREIGN KEY ([KId]) REFERENCES [s].[K] ([Id])"
    drop = "ALTER TABLE [s].[C] DROP CONSTRAINT [FK_C_K]"
    after = run(model, drop, "ALTER TABLE [s].[K] DROP CONSTRAINT [PK_K]", again)
    assert has_key(table(after, "K"), ["Id"]) and table(after, "C") == table(model, "C")
    with pytest.raises(ReplayError, match="has no PRIMARY KEY, UNIQUE constraint or unique index"):
        run(model, drop, "ALTER TABLE [s].[K] DROP CONSTRAINT [PK_K]", "DROP INDEX [UX_K] ON [s].[K]", again)
    # with the foreign key gone, both can go
    run(
        model,
        "ALTER TABLE [s].[C] DROP CONSTRAINT [FK_C_K]",
        "DROP INDEX [UX_K] ON [s].[K]",
        "DROP TABLE [s].[K]",
    )


def test_a_foreign_key_accepts_an_alias_type_and_the_built_in_type_under_it():
    model = model_of(
        "CREATE SCHEMA [s]",
        "CREATE TYPE [s].[Code] FROM char(4) NOT NULL",
        "CREATE TYPE [s].[Long] FROM char(8) NOT NULL",
        "CREATE TABLE [s].[K] ([Code] char(4) NOT NULL, CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([Code]))",
        "CREATE TABLE [s].[C] ([Short] [s].[Code] NOT NULL, [Other] [s].[Long] NOT NULL)",
    )
    add = "ALTER TABLE [s].[C] ADD CONSTRAINT [FK_C_K] FOREIGN KEY ([{}]) REFERENCES [s].[K] ([Code])"
    run(model, add.format("Short"))
    with pytest.raises(ReplayError, match="differ in data type"):
        run(model, add.format("Other"))


def test_a_foreign_key_between_columns_of_two_declared_collations_is_blocked():
    def model(child: str) -> Model:
        return model_of(
            "CREATE SCHEMA [s]",
            "CREATE TABLE [s].[K] ([Code] varchar(4) COLLATE Latin1_General_BIN2 NOT NULL, "
            "CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([Code]))",
            f"CREATE TABLE [s].[C] ([Code] varchar(4) {child} NOT NULL)",
        )

    add = "ALTER TABLE [s].[C] ADD CONSTRAINT [FK_C_K] FOREIGN KEY ([Code]) REFERENCES [s].[K] ([Code])"
    with pytest.raises(ReplayError, match="differ in data type"):
        run(model("COLLATE Latin1_General_CI_AS"), add)
    run(model("COLLATE latin1_general_bin2"), add)
    run(model(""), add)  # not declared: the model does not know the collation of the database


def test_a_one_part_sequence_name_in_a_default_means_schema_dbo():
    def model(reference: str) -> Model:
        next_value = f"NEXT VALUE FOR {reference}"
        return model_of(
            "CREATE SCHEMA [s]",
            NEW_SEQUENCE.format("[dbo].[Seq]"),
            NEW_SEQUENCE.format("[s].[Seq]"),
            f"CREATE TABLE [s].[N] ([No] int NOT NULL CONSTRAINT [DF_N] DEFAULT ({next_value}))",
        )

    with pytest.raises(ReplayError, match="used by DEFAULT \\[DF_N\\]"):
        run(model("[Seq]"), "DROP SEQUENCE [dbo].[Seq]")
    run(model("[Seq]"), "DROP SEQUENCE [s].[Seq]")
    with pytest.raises(ReplayError, match="used by DEFAULT \\[DF_N\\]"):
        run(model("s.seq"), "DROP SEQUENCE [s].[Seq]")
    run(model("s.seq"), "DROP SEQUENCE [dbo].[Seq]")


def test_column_dependants_lists_what_uses_a_column_and_not_its_default():
    assert column_dependants(WORLD, "s", "T", "Referenced") == [
        "UNIQUE [UQ_T_Referenced]",
        "FOREIGN KEY [FK_U_T] of [s].[U]",
    ]
    assert column_dependants(WORLD, "s", "T", "InFilter") == ["index [IX_T]"]
    assert column_dependants(WORLD, "s", "T", "WithDefault") == []
    assert column_dependants(WORLD, "S", "t", "FREE") == []  # 'Free' and N'[Free]' in the CHECK are literals
    with pytest.raises(ReplayError):
        column_dependants(WORLD, "s", "Nope", "Free")


def test_has_key_is_true_for_exactly_the_column_set_of_a_key():
    parent = table(WORLD, "P")
    assert has_key(parent, ["Id"]) and has_key(parent, ["CODE"])
    assert has_key(parent, ["A", "B"]) and has_key(parent, ["b", "a"])
    assert not has_key(parent, ["A"]) and not has_key(parent, ["A", "B", "Id"])
    assert not has_key(parent, ["Half"])  # a unique index with a filter is not a key
    assert not has_key(parent, ["Loose"]) and not has_key(parent, [])


# ------------------------------------------------------------------ replay
def test_replay_applies_the_statements_in_order_and_reports_which_one_failed():
    statements = [
        "ALTER TABLE [s].[U] DROP CONSTRAINT [FK_U_T]",
        "DROP TABLE [s].[T]",
        "DROP TABLE [s].[P]",
        "DROP TABLE [s].[P]",
    ]
    after = run(WORLD, *statements[:3])
    assert "TABLE:[s].[T]" not in after and "TABLE:[s].[P]" not in after and "TABLE:[s].[U]" in after
    with pytest.raises(ReplayError) as caught:
        run(WORLD, *statements)
    error = caught.value
    assert (error.op_index, error.object_key, error.message) == (
        3,
        "TABLE:[s].[P]",
        "the table does not exist",
    )
    assert str(error) == "statement 4: TABLE:[s].[P]: the table does not exist"
    with pytest.raises(ReplayError) as caught:
        run(WORLD, *reversed(statements[:3]))  # the same statements in an order that the engine refuses
    assert caught.value.op_index == 0


def test_replay_of_no_statement_gives_the_same_model():
    assert replay(WORLD, []) is WORLD


# ------------------------------------------------------------------ the proof over models that nobody wrote
def without_one_part(model: Model) -> Iterator[Model]:
    """The model without one object, one column, one constraint or one index."""
    for key, obj in model.items():
        yield model.remove(key)
        if not isinstance(obj, Table):
            continue
        for part in ("columns", "constraints", "indexes"):
            items = getattr(obj, part)
            for i in range(len(items) if part != "columns" or len(items) > 1 else 0):
                try:
                    smaller = replace(obj, **{part: items[:i] + items[i + 1 :]})
                except ValueError:
                    # a temporal table without a period column or without its key is no table of the model
                    assert obj.temporal is not None
                    continue
                yield model.replace(smaller)


def fits_together(model: Model) -> bool:
    try:
        return replay(Model(), [parse_statement(text) for text in emit_create_script(model)]) == model
    except ReplayError:
        return False


def proven_or_refused(source: Model, target: Model) -> bool:
    """True when diff wrote a migration and its text replays to the target; False when diff refused.
    Any other end (another model, a statement that does not replay, a crash) fails the test."""
    try:
        ops = diff(source, target)
    except GenRefused:
        return False
    assert replay(source, [parse_statement(emit_operation(op)) for op in ops]) == target
    return True


def test_a_fixture_model_and_the_same_model_without_one_part_are_proven_both_ways_or_refused():
    """A dropped table, column, key, foreign key, check or index, and the way back: each fixture
    model gives tens of small changes. Then pairs of the cut models, which share names and differ
    in several parts at once."""
    cut = [
        (model, smaller)
        for case in loader.CASES
        for side in ("base", "head")
        for model in [loader.model(case, side)]
        for smaller in without_one_part(model)
        if fits_together(smaller)
    ]
    assert len(cut) > 400
    proven = sum(
        proven_or_refused(model, smaller) + proven_or_refused(smaller, model) for model, smaller in cut
    )
    assert proven > 800  # most single changes are written; the rest are refused, never wrong
    pick = random.Random(20261007)
    pairs = [(pick.choice(cut)[1], pick.choice(cut)[1]) for _ in range(1500)]
    assert sum(proven_or_refused(source, target) for source, target in pairs) > 1000


# ------------------------------------------------------------------ operations that the parser never gives
# TQ-11: the parser refuses these texts, so only an operation made in code reaches the checks.
HAND_MADE = [
    (
        M.AddConstraint("s", "T", M.Check(None, M.Expression(("[Free]", ">", "0")))),
        "TABLE:[s].[T]",
        "a constraint of a table needs a name",
    ),
    (M.AlterSequence("s", "Seq", cache_size=5), "SEQUENCE:[s].[Seq]", "a cache size goes with CACHE"),
    (M.Rename("object", ("s", "P"), ""), "SCHEMA:[s]", "the new name of a rename is empty"),
    (
        M.AddColumn(
            "s",
            "T",
            M.Column("New", TypeRef("int"), True, default=M.DefaultConstraint(None, M.Expression(("0",)))),
        ),
        "TABLE:[s].[T]",
        "the DEFAULT of column [New] needs a name",
    ),
]


@pytest.mark.parametrize(("op", "key", "says"), HAND_MADE, ids=[row[2] for row in HAND_MADE])
def test_an_operation_made_in_code_that_the_parser_would_refuse_is_blocked(
    op: Operation, key: str, says: str
):
    with pytest.raises(ReplayError) as caught:
        apply(WORLD, op)
    assert (caught.value.object_key, caught.value.message) == (key, says)


# ------------------------------------------------------------------ system-versioned temporal tables
def test_versioning_off_then_drop_table_removes_a_temporal_table_and_nothing_else():
    after = run(WORLD, VERSIONING_OFF, "DROP TABLE [s].[V]")
    assert after == WORLD.remove("TABLE:[s].[V]")


def test_versioning_off_leaves_the_period_columns_and_a_table_that_no_table_file_can_describe():
    off = table(run(WORLD, VERSIONING_OFF), "V")
    assert isinstance(off, UnversionedTable) and off.temporal is None
    assert [c.generated for c in off.columns] == [None, None, "ROW_START", "ROW_END"]
    # so a migration that ends here is never equal to a head model: the proof fails, as it must
    assert run(WORLD, VERSIONING_OFF) != WORLD
    assert ("TABLE:[s].[V]", "class") in run(WORLD, VERSIONING_OFF).diff_paths(WORLD)
    with pytest.raises(ReplayError, match="it is a period column"):
        run(WORLD, VERSIONING_OFF, "ALTER TABLE [s].[V] DROP COLUMN [To]")


def test_versioning_off_and_on_again_by_hand_gives_the_table_with_the_history_table_of_the_statement():
    assert run(WORLD, VERSIONING_OFF, VERSIONING_ON.format("V", "s", "V_History")) == WORLD
    moved = run(
        WORLD,
        VERSIONING_OFF,
        VERSIONING_ON.format("V", "empty", "Archive")[:-2] + ", HISTORY_RETENTION_PERIOD = 2 YEARS))",
    )
    assert table(moved, "V").temporal == Temporal("From", "To", "empty", "Archive", (2, "YEARS"))
    assert moved != WORLD


def test_versioning_on_needs_the_primary_key_and_a_history_table_that_is_not_an_object():
    off = run(WORLD, VERSIONING_OFF)
    with pytest.raises(ReplayError, match="has no PRIMARY KEY"):
        run(off, "ALTER TABLE [s].[V] DROP CONSTRAINT [PK_V]", VERSIONING_ON.format("V", "s", "V_History"))
    with pytest.raises(ReplayError, match=r"has the name of TABLE:\[s\]\.\[P\]"):
        run(off, VERSIONING_ON.format("V", "s", "P"))
    with pytest.raises(ReplayError, match="the history table .* is the table itself"):
        run(off, VERSIONING_ON.format("V", "s", "V"))


def test_a_period_column_is_not_added_by_add_column():
    add = AddColumn("s", "One", Column("Start", TypeRef("datetime2", scale=7), False, generated="ROW_START"))
    with pytest.raises(ReplayError, match="a period column is not added by ADD"):
        apply(WORLD, add)
    assert isinstance(apply(WORLD, SetSystemVersioning("s", "V", False))["TABLE:[s].[V]"], UnversionedTable)


# ------------------------------------------------------------------ dynamic data masking
def test_add_masked_sets_or_replaces_the_mask_and_drop_masked_removes_it():
    replaced = run(WORLD, ADD_MASKED.format("Masked", "Mail"))
    new = run(WORLD, ADD_MASKED.format("Masked", "Plain"))
    gone = run(WORLD, DROP_MASKED.format("Masked", "Mail"))

    assert column(table(WORLD, "Masked"), "Mail").masked == "email()"
    assert column(table(replaced, "Masked"), "Mail").masked == "default()"
    assert column(table(new, "Masked"), "Plain").masked == "default()"
    assert column(table(gone, "Masked"), "Mail").masked is None
    # nothing else of the table changes
    assert replace(column(table(gone, "Masked"), "Mail"), masked="email()") == column(
        table(WORLD, "Masked"), "Mail"
    )


def test_alter_column_removes_the_mask_of_the_column_as_the_engine_does():
    # seen on Azure SQL Database for a wider type, another type, NOT NULL and another collation
    for statement in (
        "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] varchar(200) NULL",
        "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] varchar(100) NOT NULL",
        "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] nvarchar(100) NULL",
        "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] varchar(100) COLLATE Latin1_General_100_BIN2 NULL",
    ):
        after = run(WORLD, statement)
        assert column(table(after, "Masked"), "Mail").masked is None, statement

    again = run(
        WORLD,
        "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] varchar(200) NULL",
        ADD_MASKED.format("Masked", "Mail"),
    )
    assert column(table(again, "Masked"), "Mail").masked == "default()"


def test_drop_masked_after_an_alter_column_of_the_column_is_blocked_because_the_mask_is_gone():
    with pytest.raises(ReplayError) as caught:
        run(
            WORLD,
            "ALTER TABLE [s].[Masked] ALTER COLUMN [Mail] varchar(200) NULL",
            DROP_MASKED.format("Masked", "Mail"),
        )

    assert caught.value.op_index == 1 and "does not have a masking function" in caught.value.message


def test_a_rename_and_a_drop_of_a_masked_column_take_the_mask_with_the_column():
    renamed = run(WORLD, RENAME.format("[Masked].[Mail]", "Email", "COLUMN"))
    dropped = run(WORLD, "ALTER TABLE [s].[Masked] DROP COLUMN [Mail]")

    assert column(table(renamed, "Masked"), "Email").masked == "email()"
    assert table(dropped, "Masked").column("Mail") is None


# ------------------------------------------------------------------ wider table coverage
def test_a_heap_keeps_the_compression_of_the_clustered_index_that_is_dropped():
    model = model_of(
        "CREATE SCHEMA [s]",
        "CREATE TABLE [s].[H] ([a] int NOT NULL, [b] int NOT NULL, "
        "CONSTRAINT [PK_H] PRIMARY KEY CLUSTERED ([a]) WITH (DATA_COMPRESSION = PAGE))",
    )
    heap = run(model, "ALTER TABLE [s].[H] DROP CONSTRAINT [PK_H]")
    assert table(heap, "H").compression == "PAGE"
    # the new clustered index says what it wants; the heap has no compression of its own after it
    with pytest.raises(ReplayError, match="it would inherit PAGE"):
        run(heap, "CREATE CLUSTERED INDEX [CX_H] ON [s].[H] ([b])")
    again = run(heap, "CREATE CLUSTERED INDEX [CX_H] ON [s].[H] ([b]) WITH (DATA_COMPRESSION = ROW)")
    assert table(again, "H").compression is None
    assert run(again, "DROP INDEX [CX_H] ON [s].[H]") == run(heap, REBUILD.format("H", "ROW"))
    # a nonclustered key or index changes nothing of the heap
    keyed = run(heap, "ALTER TABLE [s].[H] ADD CONSTRAINT [UQ_H] UNIQUE NONCLUSTERED ([b])")
    assert table(keyed, "H").compression == "PAGE"
    # a clustered columnstore index takes the heap as it is and leaves a heap without compression
    columnstore = run(heap, "CREATE CLUSTERED COLUMNSTORE INDEX [CCI_H] ON [s].[H]")
    assert table(columnstore, "H").compression is None
    assert table(run(columnstore, "DROP INDEX [CCI_H] ON [s].[H]"), "H").compression is None


def test_rebuild_sets_the_compression_of_the_heap_or_of_the_clustered_key():
    assert table(run(WORLD, REBUILD.format("Wide", "ROW")), "Wide").compression == "ROW"
    assert table(run(WORLD, REBUILD.format("Wide", "NONE")), "Wide").compression is None
    key = table(run(WORLD, REBUILD.format("P", "NONE")), "P").constraint("PK_P")
    assert isinstance(key, PrimaryKey) and key.options == (("DATA_COMPRESSION", "NONE"),)


def test_a_column_property_changes_that_property_and_nothing_else():
    wide = table(WORLD, "Wide")
    after = table(
        run(WORLD, PROPERTY.format("Guid", "DROP ROWGUIDCOL"), PROPERTY.format("Other", "ADD ROWGUIDCOL")),
        "Wide",
    )
    assert (column(after, "Guid").rowguidcol, column(after, "Other").rowguidcol) == (False, True)
    sparse = table(
        run(WORLD, PROPERTY.format("Thin", "DROP SPARSE"), PROPERTY.format("Fat", "ADD SPARSE")), "Wide"
    )
    assert (column(sparse, "Thin").sparse, column(sparse, "Fat").sparse) == (False, True)
    replicated = table(run(WORLD, PROPERTY.format("Id", "ADD NOT FOR REPLICATION")), "Wide")
    identity = column(replicated, "Id").identity
    assert identity is not None and (identity.seed, identity.increment, identity.not_for_replication) == (
        1,
        1,
        True,
    )
    assert replicated.columns[1:] == wide.columns[1:]
    # the type form of ALTER COLUMN after DROP, then ADD: the way that the diff writes
    widened = run(
        WORLD,
        PROPERTY.format("Thin", "DROP SPARSE"),
        PROPERTY.format("Thin", "varchar(20) NULL"),
        PROPERTY.format("Thin", "ADD SPARSE"),
    )
    thin = column(table(widened, "Wide"), "Thin")
    assert thin.sparse and thin.type is not None and thin.type.length == 20


# ------------------------------------------------------------------ ALTER COLUMN that only raises a length
# One variable-length column for each kind of user. Seen live on Azure SQL Database: the engine
# accepts a longer varchar, nvarchar or varbinary under UNIQUE, CHECK and an index (key or INCLUDE).
LENGTHS = (
    "CREATE TABLE [s].[L] ([Id] int NOT NULL, [InUnique] varchar(20) NOT NULL, [InCheck] nvarchar(5) NULL, "
    "[InIndex] varbinary(16) NOT NULL, [InInclude] varchar(10) NULL, [InFilter] varchar(10) NULL, "
    "[InPk] varchar(10) NOT NULL, [InFk] varchar(10) NULL, [InComputed] varchar(10) NULL, "
    "[InColumnstore] varchar(10) NULL, [Referenced] varchar(10) NOT NULL, "
    "[Collated] varchar(10) COLLATE Latin1_General_BIN2 NULL, [Tagged] [s].[Tag] NULL, "
    "[Calc] AS ([InComputed] + 'x'), "
    "CONSTRAINT [PK_L] PRIMARY KEY CLUSTERED ([InPk]), CONSTRAINT [UQ_L] UNIQUE NONCLUSTERED ([InUnique]), "
    "CONSTRAINT [UQ_L_Referenced] UNIQUE NONCLUSTERED ([Referenced]), "
    "CONSTRAINT [CK_L] CHECK (LEN([InCheck]) > 0), "
    "CONSTRAINT [FK_L_K] FOREIGN KEY ([InFk]) REFERENCES [s].[K] ([Code]), "
    "INDEX [IX_L] UNIQUE NONCLUSTERED ([InIndex]) INCLUDE ([InInclude]), "
    "INDEX [IX_L_Filter] NONCLUSTERED ([Id]) WHERE [InFilter] IS NOT NULL, "
    "INDEX [IX_L_Collated] NONCLUSTERED ([Collated]), INDEX [IX_L_Tagged] NONCLUSTERED ([Tagged]), "
    "INDEX [CS_L] NONCLUSTERED COLUMNSTORE ([InColumnstore]))"
)
LENGTH_WORLD = model_of(
    "CREATE SCHEMA [s]",
    "CREATE TYPE [s].[Tag] FROM varchar(10) NULL",
    "CREATE TYPE [s].[LongTag] FROM varchar(20) NULL",
    "CREATE TABLE [s].[K] ([Code] varchar(10) NOT NULL, CONSTRAINT [PK_K] PRIMARY KEY CLUSTERED ([Code]))",
    LENGTHS,
    "CREATE TABLE [s].[M] ([Ref] varchar(10) NOT NULL, "
    "CONSTRAINT [FK_M_L] FOREIGN KEY ([Ref]) REFERENCES [s].[L] ([Referenced]))",
)
ALTER_L = "ALTER TABLE [s].[L] ALTER COLUMN [{}] {}"


@pytest.mark.parametrize(
    ("name", "to"),
    [
        ("InUnique", "varchar(40) NOT NULL"),
        ("InUnique", "varchar(20) NOT NULL"),  # equal size is no change, and the engine takes it
        ("InCheck", "nvarchar(10) NULL"),
        ("InIndex", "varbinary(32) NOT NULL"),
        ("InInclude", "varchar(20) NULL"),
        ("Collated", "varchar(20) COLLATE Latin1_General_BIN2 NULL"),
    ],
)
def test_alter_column_that_only_raises_a_length_passes_under_unique_check_and_index(name: str, to: str):
    after = run(LENGTH_WORLD, ALTER_L.format(name, to))
    changed = column(table(after, "L"), name)
    assert emit_operation(AlterColumn("s", "L", name, changed.type, changed.nullable, changed.collation)) == (
        ALTER_L.format(name, to) + ";"
    )
    # nothing else of the table moves: the constraint and the index stay as they are
    assert table(after, "L").constraints == table(LENGTH_WORLD, "L").constraints
    assert table(after, "L").indexes == table(LENGTH_WORLD, "L").indexes


@pytest.mark.parametrize(
    ("name", "to", "blocker"),
    [
        # the same column, and every part of the rule that is not kept
        ("InUnique", "varchar(10) NOT NULL", "UNIQUE [UQ_L]"),  # shorter
        ("InUnique", "nvarchar(40) NOT NULL", "UNIQUE [UQ_L]"),  # another data type
        ("InUnique", "char(40) NOT NULL", "UNIQUE [UQ_L]"),
        ("InUnique", "varchar(40) NULL", "UNIQUE [UQ_L]"),  # nullability changes
        ("InUnique", "varchar(max) NOT NULL", "UNIQUE [UQ_L]"),  # the engine refuses (max) under a key
        ("InInclude", "varchar(max) NULL", "index [IX_L]"),
        ("Collated", "varchar(20) NULL", "index [IX_L_Collated]"),  # no COLLATE resets the collation
        ("Tagged", "[s].[LongTag] NULL", "index [IX_L_Tagged]"),  # an alias type states no length
        # a longer column, and the users that block it all the same
        ("InPk", "varchar(20) NOT NULL", "PRIMARY KEY [PK_L]"),
        ("InFk", "varchar(20) NULL", "FOREIGN KEY [FK_L_K]"),
        ("Referenced", "varchar(20) NOT NULL", "FOREIGN KEY [FK_M_L] of [s].[M]"),
        ("InComputed", "varchar(20) NULL", "computed column [Calc]"),
        ("InFilter", "varchar(20) NULL", "index [IX_L_Filter]"),  # live: the engine refuses it
        ("InColumnstore", "varchar(20) NULL", "index [CS_L]"),
    ],
)
def test_alter_column_stays_blocked_when_it_is_more_than_a_longer_length_or_the_user_is_a_key(
    name: str, to: str, blocker: str
):
    with pytest.raises(ReplayError) as caught:
        run(LENGTH_WORLD, ALTER_L.format(name, to))
    assert f"ALTER COLUMN [{name}] is blocked by" in caught.value.message
    assert blocker in caught.value.message


def test_column_dependants_lists_every_user_unless_it_is_asked_for_a_longer_length_only():
    every = column_dependants(LENGTH_WORLD, "s", "L", "Referenced")
    assert every == ["UNIQUE [UQ_L_Referenced]", "FOREIGN KEY [FK_M_L] of [s].[M]"]
    assert column_dependants(LENGTH_WORLD, "s", "L", "Referenced", widening=True) == [
        "FOREIGN KEY [FK_M_L] of [s].[M]"
    ]
    assert column_dependants(LENGTH_WORLD, "s", "L", "InUnique", widening=True) == []
    assert column_dependants(LENGTH_WORLD, "s", "L", "InCheck", widening=True) == []


# ------------------------------------------------------------------ temporal: what ADD cannot add
@pytest.mark.parametrize(
    ("definition", "says"),
    [("[Z] AS ([Id] + 1)", "is computed"), ("[Z] int IDENTITY(1, 1) NOT NULL", "has IDENTITY")],
)
def test_a_computed_or_identity_column_is_not_added_while_system_versioning_is_on(definition: str, says: str):
    add = f"ALTER TABLE [s].[V] ADD {definition}"
    with pytest.raises(ReplayError, match="SYSTEM_VERSIONING is ON") as caught:
        run(WORLD, add)
    assert caught.value.object_key == "TABLE:[s].[V]" and says in caught.value.message
    # a plain column is added as on any table, and so is this one after versioning is off
    assert column(table(run(WORLD, "ALTER TABLE [s].[V] ADD [Z] int NULL"), "V"), "Z").type == parse_type(
        "int"
    )
    again = run(WORLD, VERSIONING_OFF, add, VERSIONING_ON.format("V", "s", "V_History"))
    assert table(again, "V").temporal == table(WORLD, "V").temporal
    assert column(table(again, "V"), "Z") is not None
    # and on a table that is not versioned nothing blocks it
    assert column(table(run(WORLD, f"ALTER TABLE [s].[One] ADD {definition}"), "One"), "Z") is not None


# ------------------------------------------------------------------ temporal: the name of a history table
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE SYNONYM [s].[V_History] FOR [s].[P]",
        NEW_SEQUENCE.format("[s].[V_History]"),
        "ALTER TABLE [s].[One] ADD CONSTRAINT [V_History] CHECK ([Only] > 0)",
        "ALTER TABLE [s].[One] ADD [N] int NOT NULL CONSTRAINT [V_History] DEFAULT ((0))",
        RENAME.format("[One]", "V_History", "OBJECT"),
        "CREATE TABLE [s].[New] ([Id] int NOT NULL, CONSTRAINT [V_History] PRIMARY KEY CLUSTERED ([Id]))",
    ],
)
def test_no_object_takes_the_name_of_the_history_table_of_a_table_that_is_versioned_now(statement: str):
    with pytest.raises(
        ReplayError, match=r"\[V_History\] is taken by the history table of TABLE:\[s\]\.\[V\]"
    ):
        run(WORLD, statement)
    # after versioning is off and the table is dropped the model does not know the name any more
    assert run(WORLD, VERSIONING_OFF, "DROP TABLE [s].[V]", statement) is not None


def test_drop_schema_is_blocked_by_the_history_table_of_a_table_of_another_schema():
    world = model_of(
        "CREATE SCHEMA [s]", "CREATE SCHEMA [hist]", V.replace("[s].[V_History]", "[hist].[V_History]")
    )
    with pytest.raises(
        ReplayError, match=r"the schema is not empty: it holds the history table \[hist\]"
    ) as caught:
        run(world, "DROP SCHEMA [hist]")
    assert caught.value.object_key == "SCHEMA:[hist]" and "TABLE:[s].[V]" in caught.value.message
