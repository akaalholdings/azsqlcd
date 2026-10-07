"""The table-class side of the catalog: schemas, tables, types, sequences and synonyms.

Read-only. Every batch is one SELECT that starts with the tag /* azsqlcd:<name> */. The column
list of each result set stands in the docstring of the function that builds the batch; the tests
script a fake session with exactly these columns.

What the engine filters and what Python decides: the engine resolves the names of asked keys
(OBJECT_ID, TYPE_ID, SCHEMA_ID) and leaves out what Microsoft ships. Every other rule is decided
here on the rows (which index counts, which schema counts, what is outside the model), so that a
test can prove it.

Rules where the catalog cannot say what was declared:
  * Index and key options are in the model only when the catalog value is not the engine default.
    project_options() gives an option at the engine default the spelling of the declared object;
    a live option that is not the default stays, whether the declared object states it or not.
  * A column has a collation in the model only when it differs from the collation of the database.
  * A schema has an owner in the model only when the owner is not dbo.
  * A computed column is NOT NULL in the model when it is persisted and the catalog says it is not
    nullable. The engine may have derived that from the expression; declared again, it gives the
    same catalog row. Every other computed column has no nullability (None).
  * A constraint that the engine named gets the fixed name of the design and is listed in
    system_named. A capture never holds a name that the engine made.

System-versioned temporal tables: a table with sys.tables.temporal_type = 2 is a Table with
Temporal (period columns from sys.periods, the name of the history table from history_table_id,
the retention). Its history table (temporal_type = 1) is no object of the model and is not
unsupported: the engine owns it. read_model lists it in history_tables. A period on a table that
is not system-versioned, and GENERATED ALWAYS of any other kind (ledger), stay outside the model.

Capture format 1 of a table-class object is a flat dict, property -> JSON value. A table has one
property for each column, each constraint and each index ('column [c]', 'constraint [n]',
'index [n]'), and 'columns', the column names without their order. So a difference names the
sub-object, and a live index or constraint that the stored capture does not hold takes no part in
a comparison (catalog.project_capture): it is an unmanaged sub-object. A system-versioned table has
the property 'temporal' (period columns, history table, retention), and each of its period columns
has 'generated_always' and, when hidden, 'is_hidden'. These are written only for such a table: the
capture of every other table is what it was before the tool knew temporal tables.

Dynamic data masking: a column with sys.columns.is_masked = 1 has Column.masked, the text of
sys.masked_columns.masking_function as the engine stores it, and its capture has the property
value 'masking_function'. A column without a mask has neither, so a mask that is removed, added
or changed outside the tool is a difference of 'column [c]'. A masked column of a table type
stays outside the model.
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable
from collections.abc import Sequence as Seq
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from azsqlcd import names
from azsqlcd.catalog import KEY_CHUNK, MODULE_TYPES, flag, object_id_sql, type_list
from azsqlcd.errors import refused
from azsqlcd.lex import significant, tokenize
from azsqlcd.model import (
    AliasType,
    Check,
    Column,
    Computed,
    Constraint,
    DefaultConstraint,
    Expression,
    ForeignKey,
    Identity,
    Index,
    KeyColumn,
    Model,
    ModelObject,
    Options,
    PrimaryKey,
    Schema,
    Sequence,
    Synonym,
    Table,
    TableType,
    Temporal,
    TypeRef,
    Unique,
    builtin_shape,
    fold,
)
from azsqlcd.session import Session
from azsqlcd.state import SCHEMA, literal, rows

UNSUPPORTED = "UNSUPPORTED"
IDENTIFIER_MAX = 128  # sysname
# Schemas that every database has. None of them gets a schema file. dbo is always there for an
# object; an object in one of the others is outside the model.
BUILTIN_SCHEMAS = frozenset(
    fold(name)
    for name in (
        "dbo",
        "sys",
        "guest",
        "INFORMATION_SCHEMA",
        "db_owner",
        "db_accessadmin",
        "db_securityadmin",
        "db_ddladmin",
        "db_backupoperator",
        "db_datareader",
        "db_datawriter",
        "db_denydatareader",
        "db_denydatawriter",
        SCHEMA,
    )
)
# The value of each model option when the statement does not write it.
OPTION_DEFAULTS = {
    "ALLOW_PAGE_LOCKS": "ON",
    "ALLOW_ROW_LOCKS": "ON",
    "DATA_COMPRESSION": "NONE",
    "FILLFACTOR": "0",  # 0 and 100 are one fill factor
    "IGNORE_DUP_KEY": "OFF",
    "OPTIMIZE_FOR_SEQUENTIAL_KEY": "OFF",
    "PAD_INDEX": "OFF",
    "STATISTICS_NORECOMPUTE": "OFF",
    "XML_COMPRESSION": "OFF",
}
# The same for the options of a columnstore index.
COLUMNSTORE_OPTION_DEFAULTS = {"COMPRESSION_DELAY": "0", "DATA_COMPRESSION": "COLUMNSTORE"}
_COMPRESSION = {0: "NONE", 1: "ROW", 2: "PAGE"}  # sys.partitions.data_compression of a rowstore
_ROWSTORE = (1, 2)  # sys.indexes.type: clustered, nonclustered. 0 is a heap
_COLUMNSTORE = (5, 6)  # clustered columnstore, nonclustered columnstore; all others are outside the model
_COLUMNSTORE_COMPRESSION = {3: "COLUMNSTORE", 4: "COLUMNSTORE_ARCHIVE"}  # sys.partitions.data_compression
_HISTORY, _VERSIONED = 1, 2  # sys.tables.temporal_type; 0 is a table that is not temporal
_GENERATED = {0: None, 1: "ROW_START", 2: "ROW_END"}  # sys.columns.generated_always_type; others: ledger
# sys.tables.history_retention_period_unit -> the unit of the model; -1 is INFINITE
_RETENTION_UNITS = {3: "DAYS", 4: "WEEKS", 5: "MONTHS", 6: "YEARS"}

type Rows = list[tuple[Any, ...]]


@dataclass(frozen=True)
class SystemNamed:
    """A constraint that the engine named, and the name that the files give it."""

    table_key: str
    kind: str  # DEFAULT | CHECK | PRIMARY KEY | UNIQUE | FOREIGN KEY
    live_name: str
    fixed_name: str


@dataclass(frozen=True)
class Unsupported:
    """An object that is left unmanaged. reason holds names and catalog words, never expression text."""

    object_key: str
    code: str
    reason: str


@dataclass(frozen=True)
class CatalogRead:
    model: Model
    captures: dict[str, dict[str, Any]]  # key of the model -> capture format 1
    system_named: tuple[SystemNamed, ...]
    unsupported: tuple[Unsupported, ...]  # not in the model, no capture
    # (key of a history table, key of its current table). The engine owns a history table: it is
    # not in the model, has no capture and is not unsupported.
    history_tables: tuple[tuple[str, str], ...] = ()


class _Reject(ValueError):
    """The object has a property that the model cannot hold. The message is the reason."""


type _Built = tuple[ModelObject, dict[str, Any], list[SystemNamed]]  # object, capture, engine-made names
type _Keyed = PrimaryKey | Unique | Index


# ------------------------------------------------------------------ scope of one round of queries
@dataclass(frozen=True)
class _Scope:
    """What the queries read: every user object, or the objects of asked keys (id expressions)."""

    everything: bool
    tables: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    sequences: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    schemas: tuple[str, ...] = ()


def _scopes(keys: Collection[str] | None) -> list[_Scope]:
    if keys is None:
        return [_Scope(True)]
    scopes: list[_Scope] = []
    for chunk in itertools.batched(sorted(set(keys)), KEY_CHUNK):
        ids: dict[str, list[str]] = defaultdict(list)
        for key in chunk:
            kind, schema, name = names.parse_object_key(key)
            if kind == "SCHEMA":
                ids[kind].append(f"SCHEMA_ID({literal(name)})")
            elif kind == "TYPE" and schema is not None:
                ids[kind].append(f"TYPE_ID({literal(names.qualified(schema, name))})")
            elif kind in names.TABLE_CLASS_KINDS:
                ids[kind].append(object_id_sql(key))
            else:
                raise ValueError(f"not the key of a table-class object: {key!r}")
        scopes.append(
            _Scope(
                False,
                tuple(ids["TABLE"]),
                tuple(ids["TYPE"]),
                tuple(ids["SEQUENCE"]),
                tuple(ids["SYNONYM"]),
                tuple(ids["SCHEMA"]),
            )
        )
    return scopes


def _in(column: str, ids: Seq[str]) -> str:
    return f"{column} IN ({', '.join(ids)})" if ids else "1 = 0"


def _parents(scope: _Scope) -> str:
    """FROM item p ([object_id]): the tables and the table types of the scope.

    The columns, constraints and indexes of a table type hang on its type_table_object_id.
    """
    tables = "t.[is_ms_shipped] = 0" if scope.everything else _in("t.[object_id]", scope.tables)
    types = "tt.[is_user_defined] = 1" if scope.everything else _in("tt.[user_type_id]", scope.types)
    return (
        f"(SELECT t.[object_id] FROM sys.tables AS t WHERE {tables} UNION ALL "
        f"SELECT tt.[type_table_object_id] FROM sys.table_types AS tt WHERE {types}) AS p ([object_id])"
    )


def _has_tabulars(scope: _Scope) -> bool:
    return scope.everything or bool(scope.tables or scope.types)


# ------------------------------------------------------------------ the queries
def _schemas_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_schemas. One row for each schema:
    (name, owner_name or NULL when the principal cannot be seen, module_count).

    module_count = views, procedures, functions and DML triggers in the schema that Microsoft did
    not ship. Every schema is returned; this module decides which one counts.
    """
    if not (scope.everything or scope.schemas):
        return None
    where = "" if scope.everything else f" WHERE {_in('s.[schema_id]', scope.schemas)}"
    return (
        "/* azsqlcd:read_schemas */ SELECT s.[name], dp.[name], "
        "(SELECT COUNT(*) FROM sys.objects AS o WHERE o.[schema_id] = s.[schema_id] "
        f"AND o.[is_ms_shipped] = 0 AND o.[type] IN ({type_list(*names.MODULE_KINDS)})) "
        "FROM sys.schemas AS s "
        f"LEFT JOIN sys.database_principals AS dp ON dp.[principal_id] = s.[principal_id]{where};"
    )


def _tabulars_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_tabulars. One row for each table and each table type:
    (object_id, kind 'TABLE' | 'TYPE', schema_name, name, temporal_type, ledger_type,
    is_memory_optimized, is_node, is_edge, is_external, has_fulltext_index, history_schema,
    history_table, history_retention_period, history_retention_period_unit, period_start_column,
    period_end_column, current_schema, current_table).

    object_id of a table type is its type_table_object_id. temporal_type: 0 not temporal, 1 a
    history table, 2 system-versioned. history_schema and history_table (sys.tables.history_table_id)
    are NULL for a table that is not system-versioned. history_retention_period_unit: -1 infinite,
    3 day, 4 week, 5 month, 6 year. period_start_column and period_end_column are the columns of
    the SYSTEM_TIME period of sys.periods, NULL for a table without one. current_schema and
    current_table name the table whose history table this table is, else NULL. All eight are NULL
    for a table type.
    """
    if not _has_tabulars(scope):
        return None
    tables = "t.[is_ms_shipped] = 0" if scope.everything else _in("t.[object_id]", scope.tables)
    types = "tt.[is_user_defined] = 1" if scope.everything else _in("tt.[user_type_id]", scope.types)
    return (
        "/* azsqlcd:read_tabulars */ SELECT t.[object_id], N'TABLE', s.[name], t.[name], "
        "t.[temporal_type], t.[ledger_type], t.[is_memory_optimized], t.[is_node], t.[is_edge], "
        "t.[is_external], CASE WHEN EXISTS (SELECT 1 FROM sys.fulltext_indexes AS f "
        "WHERE f.[object_id] = t.[object_id]) THEN 1 ELSE 0 END, "
        "hs.[name], ht.[name], t.[history_retention_period], t.[history_retention_period_unit], "
        "ps.[name], pe.[name], cs.[name], ct.[name] "
        "FROM sys.tables AS t JOIN sys.schemas AS s ON s.[schema_id] = t.[schema_id] "
        "LEFT JOIN sys.tables AS ht ON ht.[object_id] = t.[history_table_id] "
        "LEFT JOIN sys.schemas AS hs ON hs.[schema_id] = ht.[schema_id] "
        "LEFT JOIN sys.periods AS pd ON pd.[object_id] = t.[object_id] AND pd.[period_type] = 1 "
        "LEFT JOIN sys.columns AS ps "
        "ON ps.[object_id] = pd.[object_id] AND ps.[column_id] = pd.[start_column_id] "
        "LEFT JOIN sys.columns AS pe "
        "ON pe.[object_id] = pd.[object_id] AND pe.[column_id] = pd.[end_column_id] "
        "LEFT JOIN sys.tables AS ct ON ct.[history_table_id] = t.[object_id] "
        "LEFT JOIN sys.schemas AS cs ON cs.[schema_id] = ct.[schema_id] "
        f"WHERE {tables} "
        "UNION ALL SELECT tt.[type_table_object_id], N'TYPE', s.[name], tt.[name], 0, 0, "
        "tt.[is_memory_optimized], 0, 0, 0, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL "
        "FROM sys.table_types AS tt JOIN sys.schemas AS s ON s.[schema_id] = tt.[schema_id] "
        f"WHERE {types};"
    )


def _columns_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_columns. One row for each column of a table or table type:
    (object_id, column_id, name, type_schema, type_name, type_is_user_defined, type_is_assembly,
    max_length, precision, scale, is_nullable, collation_name, is_identity, seed_value,
    increment_value, identity_not_for_replication, is_computed, definition, is_persisted, is_sparse,
    is_column_set, is_masked, encryption_type, is_rowguidcol, is_filestream, generated_always_type,
    is_hidden, xml_collection_id, masking_function).

    collation_name is NULL when it is the collation of the database. seed_value and
    increment_value are sql_variant in the catalog and come as decimal(38, 0). The identity
    columns are NULL for a column without IDENTITY; definition and is_persisted are NULL for a
    column that is not computed. generated_always_type: 0 not generated, 1 AS ROW START,
    2 AS ROW END; every other value is a ledger column. masking_function is the text of
    sys.masked_columns for a column with dynamic data masking (is_masked = 1) and NULL for every
    other column.
    """
    if not _has_tabulars(scope):
        return None
    return (
        "/* azsqlcd:read_columns */ SELECT c.[object_id], c.[column_id], c.[name], ts.[name], ty.[name], "
        "ty.[is_user_defined], ty.[is_assembly_type], c.[max_length], c.[precision], c.[scale], "
        "c.[is_nullable], CASE WHEN c.[collation_name] <> "
        "CAST(DATABASEPROPERTYEX(DB_NAME(), N'Collation') AS nvarchar(128)) THEN c.[collation_name] END, "
        "c.[is_identity], CAST(ic.[seed_value] AS decimal(38, 0)), "
        "CAST(ic.[increment_value] AS decimal(38, 0)), ic.[is_not_for_replication], c.[is_computed], "
        "cc.[definition], cc.[is_persisted], c.[is_sparse], c.[is_column_set], c.[is_masked], "
        "c.[encryption_type], c.[is_rowguidcol], c.[is_filestream], c.[generated_always_type], "
        "c.[is_hidden], c.[xml_collection_id], mc.[masking_function] "
        f"FROM {_parents(scope)} JOIN sys.columns AS c ON c.[object_id] = p.[object_id] "
        "JOIN sys.types AS ty ON ty.[user_type_id] = c.[user_type_id] "
        "JOIN sys.schemas AS ts ON ts.[schema_id] = ty.[schema_id] "
        "LEFT JOIN sys.identity_columns AS ic "
        "ON ic.[object_id] = c.[object_id] AND ic.[column_id] = c.[column_id] "
        "LEFT JOIN sys.computed_columns AS cc "
        "ON cc.[object_id] = c.[object_id] AND cc.[column_id] = c.[column_id] "
        "LEFT JOIN sys.masked_columns AS mc "
        "ON mc.[object_id] = c.[object_id] AND mc.[column_id] = c.[column_id];"
    )


def _defaults_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_defaults. One row for each DEFAULT constraint:
    (parent_object_id, parent_column_id, name, definition, is_system_named).
    """
    if not _has_tabulars(scope):
        return None
    return (
        "/* azsqlcd:read_defaults */ SELECT d.[parent_object_id], d.[parent_column_id], d.[name], "
        f"d.[definition], d.[is_system_named] FROM {_parents(scope)} "
        "JOIN sys.default_constraints AS d ON d.[parent_object_id] = p.[object_id];"
    )


def _checks_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_checks. One row for each CHECK constraint:
    (parent_object_id, name, definition, is_system_named, is_disabled, is_not_trusted,
    is_not_for_replication).
    """
    if not _has_tabulars(scope):
        return None
    return (
        "/* azsqlcd:read_checks */ SELECT k.[parent_object_id], k.[name], k.[definition], "
        "k.[is_system_named], k.[is_disabled], k.[is_not_trusted], k.[is_not_for_replication] "
        f"FROM {_parents(scope)} JOIN sys.check_constraints AS k ON k.[parent_object_id] = p.[object_id];"
    )


def _indexes_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_indexes. One row for each row of sys.indexes, the heap included:
    (object_id, index_id, name, type, type_desc, is_unique, is_primary_key, is_unique_constraint,
    constraint_is_system_named, is_hypothetical, auto_created, has_filter, filter_definition,
    fill_factor, is_padded, ignore_dup_key, allow_row_locks, allow_page_locks,
    optimize_for_sequential_key, no_recompute, is_disabled, data_space_type, partition_count,
    data_compression, xml_compression, compression_delay).

    name is NULL for a heap. constraint_is_system_named is NULL for an index that is not the
    index of a PRIMARY KEY or UNIQUE constraint. no_recompute (sys.stats) and data_space_type
    (sys.data_spaces.type: FG, PS, ...) can be NULL. partition_count, data_compression (the
    highest of the partitions) and xml_compression come from sys.partitions; the last two are NULL
    when the index has no partition row.
    """
    if not _has_tabulars(scope):
        return None
    partitions = (
        "FROM sys.partitions AS pt WHERE pt.[object_id] = i.[object_id] AND pt.[index_id] = i.[index_id]"
    )
    return (
        "/* azsqlcd:read_indexes */ SELECT i.[object_id], i.[index_id], i.[name], i.[type], i.[type_desc], "
        "i.[is_unique], i.[is_primary_key], i.[is_unique_constraint], kc.[is_system_named], "
        "i.[is_hypothetical], i.[auto_created], i.[has_filter], i.[filter_definition], i.[fill_factor], "
        "i.[is_padded], i.[ignore_dup_key], i.[allow_row_locks], i.[allow_page_locks], "
        "i.[optimize_for_sequential_key], st.[no_recompute], i.[is_disabled], ds.[type], "
        f"(SELECT COUNT(*) {partitions}), (SELECT MAX(pt.[data_compression]) {partitions}), "
        f"(SELECT MAX(CAST(pt.[xml_compression] AS int)) {partitions}), i.[compression_delay] "
        f"FROM {_parents(scope)} JOIN sys.indexes AS i ON i.[object_id] = p.[object_id] "
        "LEFT JOIN sys.key_constraints AS kc "
        "ON kc.[parent_object_id] = i.[object_id] AND kc.[unique_index_id] = i.[index_id] "
        "LEFT JOIN sys.stats AS st ON st.[object_id] = i.[object_id] AND st.[stats_id] = i.[index_id] "
        "LEFT JOIN sys.data_spaces AS ds ON ds.[data_space_id] = i.[data_space_id];"
    )


def _index_columns_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_index_columns. One row for each column of an index:
    (object_id, index_id, key_ordinal, index_column_id, is_descending_key, is_included_column,
    column_name, column_store_order_ordinal). key_ordinal is 0 for a column that is not a key
    column. column_store_order_ordinal is above 0 for an ORDER column of a columnstore index.
    """
    if not _has_tabulars(scope):
        return None
    return (
        "/* azsqlcd:read_index_columns */ SELECT ic.[object_id], ic.[index_id], ic.[key_ordinal], "
        "ic.[index_column_id], ic.[is_descending_key], ic.[is_included_column], c.[name], "
        "ic.[column_store_order_ordinal] "
        f"FROM {_parents(scope)} JOIN sys.index_columns AS ic ON ic.[object_id] = p.[object_id] "
        "JOIN sys.columns AS c ON c.[object_id] = ic.[object_id] AND c.[column_id] = ic.[column_id];"
    )


def _foreign_keys_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_foreign_keys. One row for each column of a FOREIGN KEY constraint:
    (parent_object_id, name, is_system_named, referenced_schema, referenced_table,
    delete_referential_action_desc, update_referential_action_desc, is_disabled, is_not_trusted,
    is_not_for_replication, constraint_column_id, column_name, referenced_column_name).

    The action is NO_ACTION, CASCADE, SET_NULL or SET_DEFAULT.
    """
    if not _has_tabulars(scope):
        return None
    return (
        "/* azsqlcd:read_foreign_keys */ SELECT fk.[parent_object_id], fk.[name], fk.[is_system_named], "
        "rs.[name], rt.[name], fk.[delete_referential_action_desc], fk.[update_referential_action_desc], "
        "fk.[is_disabled], fk.[is_not_trusted], fk.[is_not_for_replication], fkc.[constraint_column_id], "
        "pc.[name], rc.[name] "
        f"FROM {_parents(scope)} JOIN sys.foreign_keys AS fk ON fk.[parent_object_id] = p.[object_id] "
        "JOIN sys.objects AS rt ON rt.[object_id] = fk.[referenced_object_id] "
        "JOIN sys.schemas AS rs ON rs.[schema_id] = rt.[schema_id] "
        "JOIN sys.foreign_key_columns AS fkc ON fkc.[constraint_object_id] = fk.[object_id] "
        "JOIN sys.columns AS pc "
        "ON pc.[object_id] = fkc.[parent_object_id] AND pc.[column_id] = fkc.[parent_column_id] "
        "JOIN sys.columns AS rc "
        "ON rc.[object_id] = fkc.[referenced_object_id] AND rc.[column_id] = fkc.[referenced_column_id];"
    )


def _alias_types_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_alias_types. One row for each user-defined type that is not a table type:
    (schema_name, name, base_type_name, is_assembly_type, max_length, precision, scale, is_nullable).

    base_type_name is the built-in type whose user_type_id is the system_type_id of the type;
    NULL for a CLR type.
    """
    if not (scope.everything or scope.types):
        return None
    where = "" if scope.everything else f" AND {_in('ty.[user_type_id]', scope.types)}"
    return (
        "/* azsqlcd:read_alias_types */ SELECT s.[name], ty.[name], bt.[name], ty.[is_assembly_type], "
        "ty.[max_length], ty.[precision], ty.[scale], ty.[is_nullable] "
        "FROM sys.types AS ty JOIN sys.schemas AS s ON s.[schema_id] = ty.[schema_id] "
        "LEFT JOIN sys.types AS bt ON bt.[user_type_id] = ty.[system_type_id] "
        f"WHERE ty.[is_user_defined] = 1 AND ty.[is_table_type] = 0{where};"
    )


def _sequences_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_sequences. One row for each sequence:
    (schema_name, name, type_schema, type_name, type_is_user_defined, precision, scale, start_value,
    increment, minimum_value, maximum_value, is_cycling, is_cached, cache_size).

    The four values are sql_variant in the catalog and come as decimal(38, 0). cache_size is NULL
    for NO CACHE and for the cache size of the engine.
    """
    if not (scope.everything or scope.sequences):
        return None
    where = "q.[is_ms_shipped] = 0" if scope.everything else _in("q.[object_id]", scope.sequences)
    return (
        "/* azsqlcd:read_sequences */ SELECT s.[name], q.[name], ts.[name], ty.[name], ty.[is_user_defined], "
        "q.[precision], q.[scale], CAST(q.[start_value] AS decimal(38, 0)), "
        "CAST(q.[increment] AS decimal(38, 0)), CAST(q.[minimum_value] AS decimal(38, 0)), "
        "CAST(q.[maximum_value] AS decimal(38, 0)), q.[is_cycling], q.[is_cached], q.[cache_size] "
        "FROM sys.sequences AS q JOIN sys.schemas AS s ON s.[schema_id] = q.[schema_id] "
        "JOIN sys.types AS ty ON ty.[user_type_id] = q.[user_type_id] "
        f"JOIN sys.schemas AS ts ON ts.[schema_id] = ty.[schema_id] WHERE {where};"
    )


def _synonyms_sql(scope: _Scope) -> str | None:
    """Tag azsqlcd:read_synonyms. One row for each synonym: (schema_name, name, base_object_name)."""
    if not (scope.everything or scope.synonyms):
        return None
    where = "y.[is_ms_shipped] = 0" if scope.everything else _in("y.[object_id]", scope.synonyms)
    return (
        "/* azsqlcd:read_synonyms */ SELECT s.[name], y.[name], y.[base_object_name] "
        f"FROM sys.synonyms AS y JOIN sys.schemas AS s ON s.[schema_id] = y.[schema_id] WHERE {where};"
    )


# query name -> (the batch for a scope, the number of columns of its result set)
_QUERIES: dict[str, tuple[Callable[[_Scope], str | None], int]] = {
    "schemas": (_schemas_sql, 3),
    "tabulars": (_tabulars_sql, 19),
    "columns": (_columns_sql, 29),
    "defaults": (_defaults_sql, 5),
    "checks": (_checks_sql, 7),
    "indexes": (_indexes_sql, 26),
    "index_columns": (_index_columns_sql, 8),
    "foreign_keys": (_foreign_keys_sql, 13),
    "alias_types": (_alias_types_sql, 8),
    "sequences": (_sequences_sql, 14),
    "synonyms": (_synonyms_sql, 3),
}


# ------------------------------------------------------------------ values
def _integer(value: object) -> int:
    """A whole number of a decimal column as int. Anything else is a defect of the query, not a fact."""
    if isinstance(value, bool) or not isinstance(value, int | Decimal | str):
        raise TypeError(f"not a whole number: {value!r}")
    try:
        number = int(value)
    except ValueError:
        raise TypeError(f"not a whole number: {value!r}") from None
    if isinstance(value, Decimal) and value != number:
        raise TypeError(f"not a whole number: {value!r}")
    return number


def _expression(text: str | None, what: str) -> Expression:
    """The tokens of catalog expression text. The catalog hides the text from a principal that has
    no VIEW DEFINITION on the object: the tool then knows nothing, and stops."""
    if text is None:
        raise refused(
            "NO_VIEW_DEFINITION",
            f"the definition of {what} cannot be read: this principal has no VIEW DEFINITION on it",
        )
    return Expression.from_sql(text)


def _on(value: object) -> bool:
    """A bit column of an outer join: NULL means the joined row does not exist."""
    return value is not None and flag(value)


def _type_ref(
    schema: str,
    name: str,
    user_defined: object,
    assembly: object,
    max_length: int,
    precision: int,
    scale: int,
) -> TypeRef:
    """The data type in the units of a declaration. sys.columns holds a length in bytes."""
    if flag(user_defined):
        if flag(assembly):
            raise _Reject(f"the CLR type {names.qualified(schema, name)}")
        return TypeRef(name, schema)
    shape = builtin_shape(name)
    if shape in ("length", "length_or_max"):
        if max_length == -1:
            return TypeRef(name, length="max")
        low = name.lower()
        if low == "vector":  # 8 bytes, then 4 bytes for each dimension
            if max_length <= 8 or (max_length - 8) % 4:
                raise _Reject("a vector whose size is not 8 + 4 bytes for each dimension")
            return TypeRef(name, length=(max_length - 8) // 4)
        if low in ("nchar", "nvarchar"):
            if max_length % 2:
                raise _Reject(f"{name} with an odd byte length")
            return TypeRef(name, length=max_length // 2)
        return TypeRef(name, length=max_length)
    if shape == "precision_scale":
        return TypeRef(name, precision=precision, scale=scale)
    if shape == "scale":
        return TypeRef(name, scale=scale)
    return TypeRef(name)  # ValueError for a name that is not a built-in type


def _options(row: tuple[Any, ...]) -> Options:
    """The options of an index row, as declared: only what is not the default of the engine."""
    fill_factor, padded, ignore_dup, row_locks, page_locks, sequential, no_recompute = row[13:20]
    compression, xml_compression = row[23], row[24]
    if compression not in (None, *_COMPRESSION):
        raise _Reject("a rowstore index with a compression that is not NONE, ROW or PAGE")
    found = {
        "FILLFACTOR": str(int(fill_factor)) if int(fill_factor) not in (0, 100) else None,
        "PAD_INDEX": "ON" if flag(padded) else None,
        "IGNORE_DUP_KEY": "ON" if flag(ignore_dup) else None,
        "STATISTICS_NORECOMPUTE": "ON" if _on(no_recompute) else None,
        "ALLOW_ROW_LOCKS": None if flag(row_locks) else "OFF",
        "ALLOW_PAGE_LOCKS": None if flag(page_locks) else "OFF",
        "OPTIMIZE_FOR_SEQUENTIAL_KEY": "ON" if flag(sequential) else None,
        "DATA_COMPRESSION": _COMPRESSION[compression] if compression else None,
        "XML_COMPRESSION": "ON" if xml_compression else None,
    }
    return tuple(sorted((name, value) for name, value in found.items() if value is not None))


def _columnstore_options(row: tuple[Any, ...]) -> Options:
    """The options of a columnstore index row, as declared: only what is not the engine default."""
    compression, xml_compression, delay = row[23], row[24], row[25]
    if compression not in (None, *_COLUMNSTORE_COMPRESSION) or xml_compression:
        raise _Reject("a columnstore index with a compression that is not COLUMNSTORE or COLUMNSTORE_ARCHIVE")
    found = {
        "COMPRESSION_DELAY": str(int(delay)) if delay else None,
        "DATA_COMPRESSION": "COLUMNSTORE_ARCHIVE" if compression == 4 else None,
    }
    return tuple(sorted((name, value) for name, value in found.items() if value is not None))


# ------------------------------------------------------------------ one table or table type
@dataclass
class _Parts:
    """What one table or table type gives beside its columns: capture, names, renames."""

    key: str
    table: str
    is_type: bool  # a table type: its constraints have no name in the model
    taken: set[str]  # case-folded constraint and index names of the table that a human gave
    capture: dict[str, Any]
    versioned: bool = False  # system-versioned: only such a table has period columns
    system_named: list[SystemNamed] = field(default_factory=list)

    def name(self, kind: str, live: str, system: bool, fixed: str) -> str:
        """The name of a constraint in the capture (and in the model of a table)."""
        if not system:
            return live
        if len(fixed) > IDENTIFIER_MAX:
            raise _Reject(f"the fixed name of a {kind} constraint is longer than {IDENTIFIER_MAX} characters")
        self.taken.add(fold(fixed))
        if not self.is_type:
            self.system_named.append(SystemNamed(self.key, kind, live, fixed))
        return fixed

    def numbered(self, prefix: str) -> str:
        """prefix_<n> with the lowest n that no constraint of the table has."""
        return next(name for n in itertools.count(1) if fold(name := f"{prefix}_{n}") not in self.taken)

    def put(self, label: str, value: dict[str, Any]) -> None:
        if label in self.capture:
            raise _Reject(f"two of its sub-objects come to the name {label}")
        self.capture[label] = value

    def model_name(self, name: str) -> str | None:
        return None if self.is_type else name


def _refused_properties(row: tuple[Any, ...]) -> list[str]:
    """What a column row holds that the model cannot."""
    column_set, encryption, filestream, xml_collection = row[20], row[22], row[24], row[27]
    found = (
        ("a column set", flag(column_set)),
        ("Always Encrypted", encryption is not None),
        ("FILESTREAM", flag(filestream)),
        ("typed xml", bool(xml_collection)),
    )
    return [text for text, present in found if present]


def _generated(parts: _Parts, row: tuple[Any, ...]) -> tuple[str | None, bool]:
    """(generated, hidden) of a column row: the period columns of a system-versioned table."""
    name, code, hidden = row[2], int(row[25] or 0), flag(row[26])
    if code not in _GENERATED:
        raise _Reject(f"column {names.quote(name)}: GENERATED ALWAYS of a kind that is not a period (ledger)")
    generated = _GENERATED[code]
    if generated is None:
        if hidden:
            raise _Reject(f"column {names.quote(name)}: HIDDEN on a column that is not a period column")
        return None, False
    if not parts.versioned:
        raise _Reject(
            f"column {names.quote(name)}: GENERATED ALWAYS AS {generated.replace('_', ' ')} on a table "
            "that is not system-versioned"
        )
    return generated, hidden


def _masked(parts: _Parts, row: tuple[Any, ...]) -> str | None:
    """The masking function of a column row, as sys.masked_columns holds it; None without a mask."""
    name, masked, function = row[2], flag(row[21]), row[28]
    if not masked and function is None:
        return None
    shown = f"column {names.quote(name)}: dynamic data masking"
    if parts.is_type:
        raise _Reject(f"{shown} in a table type")
    if flag(row[16]):
        raise _Reject(f"{shown} on a computed column")
    if not masked or not isinstance(function, str) or not function.strip():
        raise _Reject(f"{shown} whose masking function cannot be read")
    return function


def _columns(parts: _Parts, column_rows: Rows, default_rows: Rows) -> tuple[Column, ...]:
    defaults = {row[1]: row for row in default_rows}
    columns: list[Column] = []
    for row in sorted(column_rows, key=lambda r: r[1]):
        column_id, name, type_schema, type_name, user_defined, assembly = row[1:7]
        max_length, precision, scale, is_nullable, collation, is_identity, seed, increment = row[7:15]
        identity_nfr, is_computed, definition, is_persisted, is_sparse = row[15:20]
        is_rowguidcol = row[23]
        refused = _refused_properties(row)
        if refused:
            raise _Reject(f"column {names.quote(name)}: {', '.join(refused)}")
        generated, hidden = _generated(parts, row)
        masked = _masked(parts, row)
        computed, nullable = flag(is_computed), flag(is_nullable)
        persisted = computed and flag(is_persisted)
        identity = (
            Identity(_integer(seed), _integer(increment), _on(identity_nfr)) if flag(is_identity) else None
        )
        # A fact that an older capture did not hold is written only when it is set: the capture
        # of a column that does not use it stays what it was, so no stored capture drifts.
        used = {
            "identity_not_for_replication": identity is not None and identity.not_for_replication,
            "is_rowguidcol": flag(is_rowguidcol),
            "is_sparse": flag(is_sparse),
        }
        # written only for a period column, so that the capture of every other column stays as it was
        period: dict[str, Any] = {} if generated is None else {"generated_always": generated}
        parts.put(
            f"column {names.quote(name)}",
            {
                "type": type_name,
                "type_schema": type_schema if flag(user_defined) else None,
                "max_length": int(max_length),
                "precision": int(precision),
                "scale": int(scale),
                "is_nullable": nullable,
                "identity": None if identity is None else [identity.seed, identity.increment],
                "is_computed": computed,
                "is_persisted": persisted,
                "definition": definition if computed else None,
                "collation": collation,
                **period,
                **({"is_hidden": True} if hidden else {}),
                # written only for a masked column: the capture of every other column stays as it was
                **({} if masked is None else {"masking_function": masked}),
                **{key: True for key, value in used.items() if value},
            },
        )
        if computed:
            expression = _expression(definition, f"column {names.quote(name)} of {parts.key}")
            columns.append(
                Column(
                    name,
                    None,
                    False if persisted and not nullable else None,
                    computed=Computed(expression, persisted),
                )
            )
            continue
        default: DefaultConstraint | None = None
        if column_id in defaults:
            live, text, system = defaults[column_id][2:5]
            final = parts.name("DEFAULT", live, parts.is_type or flag(system), f"DF_{parts.table}_{name}")
            parts.put(
                f"constraint {names.quote(final)}", {"kind": "DEFAULT", "column": name, "expression": text}
            )
            expression = _expression(text, f"the DEFAULT of column {names.quote(name)} of {parts.key}")
            default = DefaultConstraint(parts.model_name(final), expression)
        type_ = _type_ref(type_schema, type_name, user_defined, assembly, max_length, precision, scale)
        columns.append(
            Column(
                name,
                type_,
                nullable,
                identity,
                default,
                None,
                collation,
                generated=generated,
                hidden=hidden,
                masked=masked,
                rowguidcol=flag(is_rowguidcol),
                sparse=flag(is_sparse),
            )
        )
    parts.capture["columns"] = sorted((c.name for c in columns), key=fold)
    return tuple(columns)


def _keys_and_indexes(
    parts: _Parts, index_rows: Rows, index_column_rows: Rows
) -> tuple[list[Constraint], list[Index], str | None]:
    """(keys, indexes, DATA_COMPRESSION of the heap: ROW, PAGE or None)."""
    heap: str | None = None
    columns_of: dict[Any, Rows] = defaultdict(list)
    for row in index_column_rows:
        columns_of[row[1]].append(row)
    constraints: list[Constraint] = []
    indexes: list[Index] = []
    for row in sorted(index_rows, key=lambda r: r[1]):
        index_id, name, type_code, type_desc, unique, primary, unique_constraint, system = row[1:9]
        hypothetical, auto_created, has_filter, filter_text = row[9:13]
        disabled, data_space, partition_count, compression, xml_compression = row[20:25]
        kind = int(type_code)
        if kind != 0 and (flag(hypothetical) or flag(auto_created)):
            continue  # never part of the table: a tuning tool made it, not a release
        # sys.data_spaces.type is char(2); a driver can give a char value with padding
        if (data_space is not None and str(data_space).strip() == "PS") or int(partition_count) > 1:
            raise _Reject("it is partitioned")
        if kind == 0:
            if xml_compression:
                raise _Reject("a heap with XML compression")
            if compression not in (None, *_COMPRESSION):
                raise _Reject("a heap with a compression that is not NONE, ROW or PAGE")
            heap = _COMPRESSION[compression] if compression else None
            continue
        members = columns_of[index_id]
        if kind in _COLUMNSTORE:
            if flag(primary) or flag(unique_constraint):
                raise _Reject("a key constraint on a columnstore index")
            ordered = sorted((r for r in members if r[7]), key=lambda r: r[7])
            order = tuple(KeyColumn(r[6]) for r in ordered)
            # a clustered columnstore index holds every column of the table: it has no list
            listed = () if kind == 5 else tuple(r[6] for r in sorted(members, key=lambda r: r[3]))
            options = _columnstore_options(row)
            filtered = flag(has_filter)
            parts.put(
                f"index {names.quote(name)}",
                {
                    "columnstore": True,
                    "unique": False,
                    "clustered": kind == 5,
                    "columns": [[c.name, False] for c in order],
                    "options": dict(options),
                    "is_disabled": flag(disabled),
                    "included": sorted(listed, key=fold),
                    "filter": filter_text if filtered else None,
                },
            )
            what = f"index {names.quote(name)} of {parts.key}"
            where = _expression(filter_text, what) if filtered else None
            indexes.append(Index(name, False, kind == 5, order, listed, where, options, True))
            continue
        if kind not in _ROWSTORE:
            raise _Reject(f"an index of the type {type_desc}")
        keys = [r for r in sorted(members, key=lambda r: r[2]) if int(r[2]) > 0]
        key_columns = tuple(KeyColumn(r[6], flag(r[4])) for r in keys)
        included = tuple(r[6] for r in sorted(members, key=lambda r: r[3]) if flag(r[5]))
        options = _options(row)
        shape = {
            "clustered": kind == 1,
            "columns": [[c.name, c.descending] for c in key_columns],
            "options": dict(options),
            "is_disabled": flag(disabled),
        }
        if flag(primary) or flag(unique_constraint):
            what = "PRIMARY KEY" if flag(primary) else "UNIQUE"
            fixed = (
                f"PK_{parts.table}" if flag(primary) else "_".join(["UQ", parts.table, *(r[6] for r in keys)])
            )
            final = parts.name(what, name, parts.is_type or flag(system), fixed)
            parts.put(f"constraint {names.quote(final)}", {"kind": what, **shape})
            cls = PrimaryKey if flag(primary) else Unique
            constraints.append(cls(parts.model_name(final), kind == 1, key_columns, options))
            continue
        filtered = flag(has_filter)
        parts.put(
            f"index {names.quote(name)}",
            {
                "unique": flag(unique),
                **shape,
                "included": sorted(included, key=fold),
                "filter": filter_text if filtered else None,
            },
        )
        where = _expression(filter_text, f"index {names.quote(name)} of {parts.key}") if filtered else None
        indexes.append(Index(name, flag(unique), kind == 1, key_columns, included, where, options))
    return constraints, indexes, heap


def _checks(parts: _Parts, check_rows: Rows) -> list[Constraint]:
    found: list[Constraint] = []
    # the engine names have no meaning, so the number of a fixed name follows the definition
    for row in sorted(check_rows, key=lambda r: (parts.is_type or flag(r[3]), r[2])):
        name, definition, system, disabled, not_trusted, not_for_replication = row[1:7]
        system = parts.is_type or flag(system)
        final = parts.name("CHECK", name, system, parts.numbered(f"CK_{parts.table}") if system else "")
        parts.put(
            f"constraint {names.quote(final)}",
            {
                "kind": "CHECK",
                "expression": definition,
                "is_disabled": flag(disabled),
                "is_not_trusted": flag(not_trusted),
                "is_not_for_replication": flag(not_for_replication),
            },
        )
        expression = _expression(definition, f"a CHECK constraint of {parts.key}")
        found.append(Check(parts.model_name(final), expression, flag(not_for_replication)))
    return found


def _foreign_keys(parts: _Parts, rows_: Rows) -> list[Constraint]:
    grouped: dict[str, Rows] = defaultdict(list)
    for row in rows_:
        grouped[row[1]].append(row)
    keys: list[tuple[bool, str, tuple[str, ...], tuple[str, ...], tuple[Any, ...]]] = []
    for group in grouped.values():
        group.sort(key=lambda r: r[10])
        first = group[0]
        keys.append(
            (flag(first[2]), fold(first[4]), tuple(r[11] for r in group), tuple(r[12] for r in group), first)
        )
    found: list[Constraint] = []
    for system, _, columns, ref_columns, first in sorted(keys, key=lambda k: k[:4]):
        name, ref_schema, ref_table, on_delete, on_update = first[1], first[3], first[4], first[5], first[6]
        disabled, not_trusted, not_for_replication = first[7:10]
        fixed = parts.numbered(f"FK_{parts.table}_{ref_table}") if system else ""
        final = parts.name("FOREIGN KEY", name, system, fixed)
        key = ForeignKey(
            final,
            columns,
            ref_schema,
            ref_table,
            ref_columns,
            on_delete.replace("_", " "),
            on_update.replace("_", " "),
            flag(not_for_replication),
        )
        parts.put(
            f"constraint {names.quote(final)}",
            {
                "kind": "FOREIGN KEY",
                "columns": list(columns),
                "ref_schema": ref_schema,
                "ref_table": ref_table,
                "ref_columns": list(ref_columns),
                "on_delete": key.on_delete,
                "on_update": key.on_update,
                "is_disabled": flag(disabled),
                "is_not_trusted": flag(not_trusted),
                "is_not_for_replication": flag(not_for_replication),
            },
        )
        found.append(key)
    return found


def _retention(period: object, unit: object) -> tuple[int, str] | None:
    """HISTORY_RETENTION_PERIOD of a system-versioned table; None is INFINITE."""
    if unit is None or _integer(unit) == -1:
        return None
    name = _RETENTION_UNITS.get(_integer(unit))
    if name is None or period is None:
        raise _Reject("a history retention period in a unit that is not DAY, WEEK, MONTH or YEAR")
    return _integer(period), name


def _tabular(head: tuple[Any, ...], child: dict[str, Rows]) -> _Built:
    """The model object and the capture of one table or table type. ValueError: outside the model."""
    kind, schema, name, temporal_type, ledger, memory, node, edge, external, fulltext = head[1:11]
    history_schema, history_table, retention_period, retention_unit, period_start, period_end = head[11:17]
    is_type = kind == "TYPE"
    temporal_type = int(temporal_type or 0)
    versioned = temporal_type == _VERSIONED
    period = any(value is not None for value in (period_start, period_end))
    outside = (
        # read_model takes a history table out before this point: it is owned by its current table
        ("a history table of a temporal table", temporal_type == _HISTORY),
        (
            "a temporal table of a kind that the tool does not know",
            temporal_type not in (0, _HISTORY, _VERSIONED),
        ),
        (
            "a period (PERIOD FOR SYSTEM_TIME) on a table that is not system-versioned",
            period and not versioned,
        ),
        (
            "a system-versioned temporal table whose period or history table cannot be read",
            versioned and None in (history_schema, history_table, period_start, period_end),
        ),
        ("a ledger table", bool(ledger)),
        ("memory-optimized", flag(memory)),
        ("a graph table", flag(node) or flag(edge)),
        ("an external table", flag(external)),
        ("a full-text index", flag(fulltext)),
    )
    reasons = [text for text, present in outside if present]
    if reasons:
        raise _Reject(", ".join(reasons))
    human = [row[2] for row in child["defaults"] if not flag(row[4])]
    human += [row[1] for row in child["checks"] if not flag(row[3])]
    human += [row[1] for row in child["foreign_keys"] if not flag(row[2])]
    human += [row[2] for row in child["indexes"] if row[2] is not None and not _on(row[8])]
    parts = _Parts(
        names.object_key(kind, schema, name),
        name,
        is_type,
        set() if is_type else {fold(n) for n in human},
        {"kind": kind, "class": "table"} if is_type else {"kind": kind},
        versioned,
    )
    columns = _columns(parts, child["columns"], child["defaults"])
    constraints, indexes, heap = _keys_and_indexes(parts, child["indexes"], child["index_columns"])
    if not is_type:
        # a property of its own, always there: a capture that an older tool stored does not hold
        # it, and what a stored capture does not hold takes no part in a comparison
        parts.capture["heap_compression"] = heap
    constraints += _checks(parts, child["checks"])
    constraints += _foreign_keys(parts, child["foreign_keys"])
    if is_type:
        return TableType(schema, name, columns, tuple(constraints), tuple(indexes)), parts.capture, []
    temporal: Temporal | None = None
    if versioned:
        retention = _retention(retention_period, retention_unit)
        temporal = Temporal(period_start, period_end, history_schema, history_table, retention)
        parts.capture["temporal"] = {
            "period_start": period_start,
            "period_end": period_end,
            "history_schema": history_schema,
            "history_table": history_table,
            "retention": None if retention is None else list(retention),
        }
    table = Table(schema, name, columns, tuple(constraints), tuple(indexes), temporal, heap)
    return table, parts.capture, parts.system_named


# ------------------------------------------------------------------ the other kinds
def _alias_type(row: tuple[Any, ...]) -> _Built:
    schema, name, base, assembly, max_length, precision, scale, is_nullable = row
    if flag(assembly) or base is None:
        raise _Reject("a CLR type")
    obj = AliasType(
        schema, name, _type_ref("sys", base, 0, 0, max_length, precision, scale), flag(is_nullable)
    )
    capture = {
        "kind": "TYPE",
        "class": "alias",
        "base_type": base,
        "max_length": int(max_length),
        "precision": int(precision),
        "scale": int(scale),
        "is_nullable": obj.nullable,
    }
    return obj, capture, []


# Capture properties that a drift comparison leaves out, by the kind of the object.
# start_value of a sequence (RO-3): on a real Azure SQL Database sys.sequences.start_value equals
# current_value on every sequence, and the database reseeds with ALTER SEQUENCE ... RESTART WITH.
# If RESTART WITH moves start_value, the value changes whenever the application reseeds, and a
# drift report of it is noise that hides real drift. Not proven by a write yet: the live spike
# decides (CREATE SEQUENCE, ALTER SEQUENCE ... RESTART WITH n, read start_value). Until then the
# capture and the model keep the value, so read-back and the baseline report still compare START
# WITH with the file; only drift (tables.Hooks.table_drift) leaves it out.
NOT_IN_DRIFT: dict[str, frozenset[str]] = {"SEQUENCE": frozenset({"start_value"})}


def _sequence(row: tuple[Any, ...]) -> _Built:
    schema, name, type_schema, type_name, user_defined, precision, scale = row[:7]
    start, increment, minimum, maximum = (_integer(value) for value in row[7:11])
    cycling, cached, cache_size = flag(row[11]), flag(row[12]), row[13]
    size = _integer(cache_size) if cached and cache_size is not None else None
    type_ = _type_ref(type_schema, type_name, user_defined, 0, 0, precision, scale)
    obj = Sequence(schema, name, type_, start, increment, minimum, maximum, cycling, cached, size)
    capture = {
        "kind": "SEQUENCE",
        "type": type_name,
        "type_schema": type_schema if flag(user_defined) else None,
        "precision": int(precision),
        "scale": int(scale),
        "start_value": start,
        "increment": increment,
        "minimum_value": minimum,
        "maximum_value": maximum,
        "is_cycling": cycling,
        "is_cached": cached,
        "cache_size": size,
    }
    return obj, capture, []


def _synonym(row: tuple[Any, ...]) -> _Built:
    schema, name, base = row
    tokens = significant(tokenize(base))
    parts = [t.value for t in tokens[::2] if t.kind in ("word", "bident", "qident")]
    dots = [t for t in tokens[1::2] if t.kind == "op" and t.text == "."]
    if len(parts) + len(dots) != len(tokens) or len(parts) != len(dots) + 1:
        raise _Reject("a base object name that is not a name")
    if len(parts) != 2:
        raise _Reject(f"a base object name with {len(parts)} part(s); the model holds [schema].[name]")
    return Synonym(schema, name, parts[0], parts[1]), {"kind": "SYNONYM", "base_object_name": base}, []


# ------------------------------------------------------------------ read_model
def _clashes(
    objects: Iterable[ModelObject], system_named: Iterable[SystemNamed], taken: Collection[tuple[str, str]]
) -> dict[str, str]:
    """Case-folded table key -> a fixed name of the table that something else in its schema has too.

    Constraints, tables, sequences, synonyms and modules of one schema are one namespace. taken:
    case-folded (schema, name) of what the schema holds outside the model. A fixed name is never
    the live name of its own constraint, so a name of taken is always the name of another object.
    """
    owners: Counter[tuple[str, str]] = Counter()
    for obj in objects:
        if isinstance(obj, Schema | AliasType | TableType):
            continue
        owners[fold(obj.schema), fold(obj.name)] += 1
        if isinstance(obj, Table):
            used = [c.name for c in obj.constraints] + [c.default.name for c in obj.columns if c.default]
            owners.update((fold(obj.schema), fold(name)) for name in used if name)
    found: dict[str, str] = {}
    for item in system_named:
        schema = names.parse_object_key(item.table_key)[1] or ""
        name = fold(schema), fold(item.fixed_name)
        if owners[name] > 1 or name in taken:
            found[fold(item.table_key)] = item.fixed_name
    return found


def database_collation(session: Session) -> str | None:
    """The collation of the database; None when the engine gives no answer.

    Tag azsqlcd:database_collation. The column reader gives no collation for a column that has
    this one, so a comparison with a file that states it needs the name.
    """
    found = rows(
        session,
        "/* azsqlcd:database_collation */ "
        "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), N'Collation') AS nvarchar(128));",
    )
    return str(found[0][0]) if len(found) == 1 and found[0][0] is not None else None


def history_tables(session: Session) -> dict[str, str]:
    """Key of each history table of the database -> key of its system-versioned table. Read-only.

    Tag azsqlcd:history_tables. Rows: (history_schema, history_table, schema_name, name).

    A history table is a row of sys.objects, but it is no object that a release manages: the
    engine owns it. A list of live objects without a managed row leaves these keys out.
    """
    found = rows(
        session,
        "/* azsqlcd:history_tables */ SELECT hs.[name], h.[name], s.[name], t.[name] "
        "FROM sys.tables AS t JOIN sys.schemas AS s ON s.[schema_id] = t.[schema_id] "
        "JOIN sys.tables AS h ON h.[object_id] = t.[history_table_id] "
        "JOIN sys.schemas AS hs ON hs.[schema_id] = h.[schema_id] "
        f"WHERE t.[temporal_type] = {_VERSIONED};",
    )
    return {
        names.object_key("TABLE", history_schema, history): names.object_key("TABLE", schema, name)
        for history_schema, history, schema, name in found
    }


def read_model(
    session: Session, keys: Collection[str] | None = None, *, taken: Collection[tuple[str, str]] = ()
) -> CatalogRead:
    """The table-class objects of the catalog as a model, with their captures.

    keys None: every schema, table, type, sequence and synonym that Microsoft did not ship,
    outside schema azsqlcd. A schema is in the model when it holds an object of the model or a
    module, and is not a built-in schema. keys given: the objects of these keys that exist; an
    asked schema is in the model when it exists and is not built in. The objects come back under
    the key that the catalog names give; match an asked key with model.fold.

    A history table of a system-versioned table is in `history_tables` with the key of its current
    table and nowhere else: the engine owns it. The schema of the history table of a table of the
    model is in the model, as the schema of the table is.

    An object with a property that the model cannot hold is in `unsupported` with the code
    UNSUPPORTED and a reason; it is not in the model and has no capture. So is a table with an
    engine-named constraint whose fixed name is not free in its schema: another object of the
    model has it, an object that was read and is outside the model has it, or it is in `taken`,
    the (schema, name) pairs that the caller knows as taken (export gives every row of sys.objects).
    """
    found: dict[str, Rows] = defaultdict(list)
    for scope in _scopes(keys):
        for name, (build, width) in _QUERIES.items():
            batch = build(scope)
            fetched = [] if batch is None else rows(session, batch)
            if any(len(row) != width for row in fetched):
                # a row of another shape would be read as an object outside the model; it is a defect
                raise RuntimeError(f"the catalog query {name} gave a row that has not {width} columns")
            found[name] += fetched

    objects: dict[str, ModelObject] = {}
    captures: dict[str, dict[str, Any]] = {}
    system_named: list[SystemNamed] = []
    unsupported: list[Unsupported] = []

    outside = {(fold(schema), fold(name)) for schema, name in taken}  # names that are not free

    def add(kind: str, schema: str, name: str, build: Callable[[], _Built]) -> bool:
        key = names.object_key(kind, schema, name)
        if fold(schema) == fold(SCHEMA):
            return True  # the state of the tool itself
        try:
            if fold(schema) in BUILTIN_SCHEMAS and fold(schema) != "dbo":
                raise _Reject(f"it is in the built-in schema {names.quote(schema)}")
            obj, capture, renames = build()
        except ValueError as error:  # also what a constructor of the model refuses
            unsupported.append(Unsupported(key, UNSUPPORTED, str(error)))
            if kind != "TYPE":  # a type is no object of sys.objects
                outside.add((fold(schema), fold(name)))
            return False
        objects[key], captures[key] = obj, capture
        system_named.extend(renames)
        return True

    children: dict[str, dict[Any, Rows]] = {}
    for name in ("columns", "defaults", "checks", "indexes", "index_columns", "foreign_keys"):
        by_parent: dict[Any, Rows] = defaultdict(list)
        for row in found[name]:
            by_parent[row[0]].append(row)
        children[name] = by_parent
    history: list[tuple[str, str]] = []
    for head in found["tabulars"]:
        child = {name: by_parent[head[0]] for name, by_parent in children.items()}
        if head[1] == "TABLE" and int(head[4] or 0) == _HISTORY and None not in head[17:19]:
            if fold(head[2]) != fold(SCHEMA):
                current = names.object_key("TABLE", head[17], head[18])
                history.append((names.object_key("TABLE", head[2], head[3]), current))
                outside.add((fold(head[2]), fold(head[3])))  # its name is not free in its schema
            continue
        if not add(head[1], head[2], head[3], lambda head=head, child=child: _tabular(head, child)):
            # outside the model, and its constraints still hold their names in the schema
            used = [row[2] for row in child["defaults"]] + [row[1] for row in child["checks"]]
            used += [row[1] for row in child["foreign_keys"]]
            used += [row[2] for row in child["indexes"] if row[2] is not None and (row[6] or row[7])]
            if head[1] == "TABLE":
                outside |= {(fold(head[2]), fold(name)) for name in used}
    for row in found["alias_types"]:
        add("TYPE", row[0], row[1], lambda row=row: _alias_type(row))
    for row in found["sequences"]:
        add("SEQUENCE", row[0], row[1], lambda row=row: _sequence(row))
    for row in found["synonyms"]:
        add("SYNONYM", row[0], row[1], lambda row=row: _synonym(row))

    clashes = _clashes(objects.values(), system_named, outside)
    for key in [key for key in objects if fold(key) in clashes]:
        clash = names.quote(clashes[fold(key)])
        reason = f"the fixed name {clash} of one of its constraints is taken in its schema"
        unsupported.append(Unsupported(key, UNSUPPORTED, reason))
        del objects[key], captures[key]
    system_named = [item for item in system_named if fold(item.table_key) not in clashes]

    used = {fold(obj.schema) for obj in objects.values() if not isinstance(obj, Schema)}
    # CREATE TABLE of a temporal table needs the schema of its history table
    used |= {
        fold(obj.temporal.history_schema)
        for obj in objects.values()
        if isinstance(obj, Table) and obj.temporal
    }
    for name, owner, module_count in found["schemas"]:
        key = names.object_key("SCHEMA", None, name)
        if fold(name) in BUILTIN_SCHEMAS or not (keys is not None or fold(name) in used or int(module_count)):
            continue
        if owner is None:
            unsupported.append(Unsupported(key, UNSUPPORTED, "its owner cannot be read"))
            continue
        objects[key] = Schema(name, None if fold(owner) == "dbo" else owner)
        captures[key] = {"kind": "SCHEMA", "owner": owner}

    return CatalogRead(
        Model(objects.values()),
        captures,
        tuple(sorted(system_named, key=lambda item: (fold(item.table_key), fold(item.fixed_name)))),
        tuple(sorted(unsupported, key=lambda item: fold(item.object_key))),
        tuple(sorted(history, key=lambda item: fold(item[0]))),
    )


# ------------------------------------------------------------------ comparison with what is declared
def _declared(live: Options, declared: Options, defaults: dict[str, str]) -> Options:
    """The live options for a comparison with the declared ones.

    A stated option: the live value, or the engine default in the declared spelling. A live option
    that the declaration does not state is not the engine default (_options leaves a default out),
    so it stays and is a difference (A14): IGNORE_DUP_KEY = ON, a fill factor, a compression.
    """
    have = dict(live)
    found: list[tuple[str, str]] = []
    for name, wanted in declared:
        value = have.pop(name, defaults[name])
        if name == "FILLFACTOR" and value == "0" and wanted in ("0", "100"):
            value = wanted
        found.append((name, value))
    if have:
        found = sorted(found + list(have.items()))
    return tuple(found)


def _counterpart(item: _Keyed, declared: Iterable[Constraint | Index]) -> _Keyed | None:
    """The declared item of the same class with the same name; without names, with the same columns."""
    for other in declared:
        if not isinstance(other, PrimaryKey | Unique | Index) or type(other) is not type(item):
            continue
        if isinstance(other, Index) and isinstance(item, Index) and other.columnstore != item.columnstore:
            continue  # another kind of index under the name: the options of one are not those of the other
        if item.name is None or other.name is None:
            if item.name is None and other.name is None and other.columns == item.columns:
                return other
        elif fold(other.name) == fold(item.name):
            return other
    return None


def project_options(live_object: ModelObject, declared_object: ModelObject) -> ModelObject:
    """The live object, cut down to what the declared object states, for a comparison.

    Index and key options: an option that the catalog holds at the engine default gets the
    declared spelling of that default, or is left out when the declared item does not state it.
    A live option that is not the engine default stays, stated or not: what a file does not state
    is the default. An item with no declared counterpart is not changed. A schema: the owner is
    compared only when the declared schema states one; a live owner that read_model left out is dbo.
    """
    if isinstance(live_object, Schema) and isinstance(declared_object, Schema):
        return replace(live_object, owner=declared_object.owner and (live_object.owner or "dbo"))
    if not isinstance(live_object, Table | TableType) or not isinstance(declared_object, Table | TableType):
        return live_object

    def cut[T: (PrimaryKey, Unique, Index)](item: T, declared: Iterable[Constraint | Index]) -> T:
        other = _counterpart(item, declared)
        if other is None:
            return item
        columnstore = isinstance(item, Index) and item.columnstore
        defaults = COLUMNSTORE_OPTION_DEFAULTS if columnstore else OPTION_DEFAULTS
        return replace(item, options=_declared(item.options, other.options, defaults))

    constraints: list[Constraint] = []
    for constraint in live_object.constraints:
        if isinstance(constraint, PrimaryKey):
            constraint = cut(constraint, declared_object.constraints)
        elif isinstance(constraint, Unique):
            constraint = cut(constraint, declared_object.constraints)
        constraints.append(constraint)
    indexes = tuple(cut(i, declared_object.indexes) for i in live_object.indexes)
    return replace(live_object, constraints=tuple(constraints), indexes=indexes)


# ------------------------------------------------------------------ rename script
def rename_constraints_sql(system_named: Iterable[SystemNamed]) -> str:
    """One EXEC sys.sp_rename for each constraint, each in its own batch. Metadata only.

    A constraint is an object of the schema of its table. The rename of a PRIMARY KEY or UNIQUE
    constraint renames its index too. The text is for a human to review and run; '' when there is
    nothing to rename.
    """
    batches: list[str] = []
    for item in sorted(system_named, key=lambda i: (fold(i.table_key), fold(i.fixed_name))):
        schema = names.parse_object_key(item.table_key)[1]
        if schema is None:
            raise ValueError(f"not the key of a table: {item.table_key!r}")
        old = names.sql_literal(names.qualified(schema, item.live_name))
        batches.append(f"EXEC sys.sp_rename {old}, {names.sql_literal(item.fixed_name)}, N'OBJECT';\nGO\n")
    return "".join(batches)


# ------------------------------------------------------------------ sub-objects: blockers, names
def _index_label(schema: str, table: str, quoted_name: str) -> str:
    return f"INDEX {names.qualified(schema, table)}.{quoted_name}"


def _foreign_key_label(schema: str, table: str, quoted_name: str) -> str:
    return f"FOREIGN KEY {names.qualified(schema, table)}.{quoted_name}"


def blockers(session: Session, table_key: str, column: str | None) -> list[str]:
    """What hangs on a column of a table, or on the table as a whole (plan step 8 a). Sorted labels.

    Tag azsqlcd:table_blockers. Rows: (kind 'INDEX' | 'STATISTICS' | 'FOREIGN KEY' | 'MODULE',
    schema_name, parent_name, name). parent_name is the table of the index, of the statistics or
    of the foreign key; for MODULE it is the module and name is its sys.objects.type.

    column given: every index with the column (key or included), every statistics object that a
    user made with the column, every foreign key with the column on either side, every
    schema-bound module that uses the column. column None: every foreign key of another table that
    references the table, every schema-bound module on the table.

    Labels: 'INDEX [s].[t].[name]', 'STATISTICS [s].[t].[name]', 'FOREIGN KEY [s].[t].[name]'
    (t = the table that has the key), 'SCHEMABOUND <module key>'. recorded_sub_objects() gives the
    same labels for what a capture holds. A table that does not exist gives [].
    """
    kind, schema, _ = names.parse_object_key(table_key)
    if kind != "TABLE" or schema is None:
        raise ValueError(f"not the key of a table: {table_key!r}")
    table = object_id_sql(table_key)
    owner = (
        "JOIN sys.objects AS o ON o.[object_id] = {0} JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id]"
    )
    modules = (
        # sys.objects.type has a collation of its own; a UNION with name columns needs one stated
        "SELECT N'MODULE', s.[name], o.[name], RTRIM(o.[type]) COLLATE DATABASE_DEFAULT "
        "FROM sys.sql_expression_dependencies AS d "
        f"{owner.format('d.[referencing_id]')} WHERE d.[referenced_id] = {table} "
        f"AND d.[is_schema_bound_reference] = 1 AND o.[type] IN ({type_list(*names.MODULE_KINDS)})"
    )
    foreign_keys = (
        "SELECT N'FOREIGN KEY', s.[name], o.[name], fk.[name] FROM sys.foreign_keys AS fk "
        f"{owner.format('fk.[parent_object_id]')} WHERE "
    )
    if column is None:
        branches = [
            f"{foreign_keys}fk.[referenced_object_id] = {table} AND fk.[parent_object_id] <> {table}",
            modules,
        ]
    else:
        column_id = f"COLUMNPROPERTY({table}, {literal(column)}, N'ColumnId')"
        branches = [
            "SELECT N'INDEX', s.[name], o.[name], i.[name] FROM sys.indexes AS i "
            f"{owner.format('i.[object_id]')} WHERE i.[object_id] = {table} AND i.[type] <> 0 "
            "AND EXISTS (SELECT 1 FROM sys.index_columns AS ic WHERE ic.[object_id] = i.[object_id] "
            f"AND ic.[index_id] = i.[index_id] AND ic.[column_id] = {column_id})",
            "SELECT N'STATISTICS', s.[name], o.[name], st.[name] FROM sys.stats AS st "
            f"{owner.format('st.[object_id]')} WHERE st.[object_id] = {table} AND st.[user_created] = 1 "
            "AND EXISTS (SELECT 1 FROM sys.stats_columns AS sc WHERE sc.[object_id] = st.[object_id] "
            f"AND sc.[stats_id] = st.[stats_id] AND sc.[column_id] = {column_id})",
            f"{foreign_keys}EXISTS (SELECT 1 FROM sys.foreign_key_columns AS fc "
            "WHERE fc.[constraint_object_id] = fk.[object_id] AND ("
            f"(fc.[parent_object_id] = {table} AND fc.[parent_column_id] = {column_id}) OR "
            f"(fc.[referenced_object_id] = {table} AND fc.[referenced_column_id] = {column_id})))",
            f"{modules} AND d.[referenced_minor_id] = {column_id}",
        ]
    batch = (
        "/* azsqlcd:table_blockers */ SELECT x.[kind], x.[schema_name], x.[parent_name], x.[name] FROM ("
        + " UNION ALL ".join(branches)
        + ") AS x ([kind], [schema_name], [parent_name], [name]);"
    )
    found: set[str] = set()
    for what, owner_schema, parent, name in rows(session, batch):
        if what == "MODULE":
            module_kind = MODULE_TYPES[str(name).strip()]
            found.add(f"SCHEMABOUND {names.object_key(module_kind, owner_schema, parent)}")
        elif what == "FOREIGN KEY":
            found.add(_foreign_key_label(owner_schema, parent, names.quote(name)))
        elif what in ("INDEX", "STATISTICS"):
            found.add(f"{what} {names.qualified(owner_schema, parent)}.{names.quote(name)}")
        else:
            raise RuntimeError(f"the blocker query gave the kind {what!r}")
    return sorted(found)


def recorded_sub_objects(table_key: str, capture: dict[str, Any]) -> set[str]:
    """The indexes and foreign keys that the capture of a table holds, as blockers() labels them.

    A PRIMARY KEY or UNIQUE constraint is an index of the table.
    """
    _, schema, table = names.parse_object_key(table_key)
    if schema is None:
        raise ValueError(f"not the key of a table: {table_key!r}")
    found: set[str] = set()
    for label, value in capture.items():
        if label.startswith("index "):
            found.add(_index_label(schema, table, label.removeprefix("index ")))
        elif label.startswith("constraint ") and isinstance(value, dict):
            quoted = label.removeprefix("constraint ")
            if value.get("kind") in ("PRIMARY KEY", "UNIQUE"):
                found.add(_index_label(schema, table, quoted))
            elif value.get("kind") == "FOREIGN KEY":
                found.add(_foreign_key_label(schema, table, quoted))
    return found


def names_present(
    session: Session,
    *,
    objects: Collection[tuple[str, str]] = (),
    indexes: Collection[tuple[str, str, str]] = (),
    types: Collection[tuple[str, str]] = (),
    schemas: Collection[str] = (),
) -> set[str]:
    """Which of the names exist in the catalog now (A20). The engine resolves each name.

    Tag azsqlcd:names_present. Rows: (n), the position of a name that exists in the VALUES list
    of the batch.

    objects: (schema, name) of anything that sys.objects holds: a table, a sequence, a synonym, a
    module, a constraint. indexes: (schema, table, index name). types: (schema, name). schemas:
    name. The result holds 'OBJECT [s].[n]', 'INDEX [s].[t].[n]', 'TYPE [s].[n]', 'SCHEMA [n]'.
    """
    probes: list[tuple[str, str]] = []
    for schema, name in sorted(set(objects)):
        qualified = names.qualified(schema, name)
        probes.append((f"OBJECT {qualified}", f"OBJECT_ID({literal(qualified)})"))
    for schema, table, name in sorted(set(indexes)):
        table_id = f"OBJECT_ID({literal(names.qualified(schema, table))})"
        label = _index_label(schema, table, names.quote(name))
        probes.append((label, f"INDEXPROPERTY({table_id}, {literal(name)}, N'IndexID')"))
    for schema, name in sorted(set(types)):
        qualified = names.qualified(schema, name)
        probes.append((f"TYPE {qualified}", f"TYPE_ID({literal(qualified)})"))
    for name in sorted(set(schemas)):
        probes.append((f"SCHEMA {names.quote(name)}", f"SCHEMA_ID({literal(name)})"))
    present: set[str] = set()
    for chunk in itertools.batched(probes, KEY_CHUNK):
        values = ", ".join(f"({n}, {probe})" for n, (_, probe) in enumerate(chunk))
        batch = (
            f"/* azsqlcd:names_present */ SELECT v.[n] FROM (VALUES {values}) AS v ([n], [id]) "
            "WHERE v.[id] IS NOT NULL;"
        )
        present.update(chunk[int(n)][0] for (n,) in rows(session, batch))
    return present
