"""Read-only catalog queries: fence facts, module captures, dependants, approver facts.

Every batch of this module is one SELECT that starts with the tag /* azsqlcd:<function> */, a
stable handle for a test rule and for whoever reads the query log of the server. A name reaches
batch text as a bracket-quoted name inside an N'...' literal, and the engine resolves it
(OBJECT_ID), so no rule of the catalog collation is repeated in Python.

A capture is a flat dict, property name -> JSON value. It is compared property by property, and
a difference is reported as the property name and two hashes, never as text (A25). A capture
never holds an object_id, a date or a name that the engine made.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Any

from azsqlcd import names
from azsqlcd.errors import refused
from azsqlcd.session import Session
from azsqlcd.sqlerrors import SqlError
from azsqlcd.state import SCHEMA, capture_sha256, literal, rows

APPLOCK_RESOURCE = "azsqlcd:deploy"  # the session applock of deploy; plan only tests it (A4)
KEY_CHUNK = 500  # keys in one batch; more keys are read in several batches
# broken_references: the engine could not bind the module. The read of
# sys.dm_sql_referenced_entities raises 2020 alone (a procedure names a table that does not exist),
# or 207 (a column is gone) or 208 (an object is gone) before it; the driver returns only the first
# error (live spike X3). The finding is ERROR_<number of the error that arrived>.
REFERENCES_NOT_BOUND = "ERROR_2020"
NOT_BOUND_NUMBERS = frozenset({2020, 207, 208})
NOT_BOUND_FINDINGS = frozenset(f"ERROR_{number}" for number in NOT_BOUND_NUMBERS)
# sys.objects.type of an object that has columns: only such a reference can have a column that is not found
COLUMN_OWNER_TYPES = frozenset({"U", "V", "IF", "TF"})

# sys.objects.type -> kind. FN, IF and TF are the three sub-kinds of FUNCTION.
MODULE_TYPES = {
    "V": "VIEW",
    "P": "PROCEDURE",
    "FN": "FUNCTION",
    "IF": "FUNCTION",
    "TF": "FUNCTION",
    "TR": "TRIGGER",
}
OBJECT_TYPES = {**MODULE_TYPES, "U": "TABLE", "SO": "SEQUENCE", "SN": "SYNONYM"}
# sys.objects.type of a CLR module -> the kind of its object key. A CLR module has no definition text.
CLR_TYPES = {"PC": "PROCEDURE", "FS": "FUNCTION", "FT": "FUNCTION", "AF": "FUNCTION", "TA": "TRIGGER"}


# ------------------------------------------------------------------ helpers
def one_row(session: Session, batch: str) -> tuple[Any, ...]:
    """The row of a SELECT that always gives exactly one row. Anything else is a defect, not a fact."""
    found = rows(session, batch)
    if len(found) != 1:
        raise RuntimeError(f"a catalog query gave {len(found)} rows where exactly one is possible")
    return found[0]


def flag(value: object) -> bool:
    """A bit column as bool. NULL is refused: a flag that is not known is not False."""
    if value not in (0, 1):
        raise TypeError(f"not a bit value: {value!r}")
    return bool(value)


def type_list(*kinds: str) -> str:
    """The sys.objects.type codes of the kinds, for IN (...)."""
    return ", ".join(names.sql_literal(code) for code, kind in OBJECT_TYPES.items() if kind in kinds)


def object_id_sql(key: str) -> str:
    """OBJECT_ID(N'[schema].[name]') for the key of an object that sys.objects holds."""
    kind, schema, name = names.parse_object_key(key)
    if schema is None or kind not in OBJECT_TYPES.values():
        raise ValueError(f"not the key of an object in sys.objects: {key!r}")
    return f"OBJECT_ID({literal(names.qualified(schema, name))})"


def object_source(keys: Sequence[str]) -> str:
    """FROM item k ([requested_key], [object_id]): one row for each key, as the engine resolves it.

    [object_id] is NULL for a name that does not exist. A query joins on it and selects
    [requested_key], so a row comes back under the exact key that was asked for.
    """
    values = ", ".join(f"({literal(key)}, {object_id_sql(key)})" for key in keys)
    return f"(VALUES {values}) AS k ([requested_key], [object_id])"


def _chunks(keys: Collection[str]) -> list[tuple[str, ...]]:
    return list(batched(sorted(set(keys)), KEY_CHUNK))


# ------------------------------------------------------------------ fence and lock
@dataclass(frozen=True)
class FenceFacts:
    engine_edition: int  # 5 = Azure SQL Database
    updateability: str  # READ_WRITE or READ_ONLY
    db_name: str
    collation_name: str  # collation of the database; the catalog of Azure SQL Database can have another
    case_sensitive: bool  # measured on the catalog: a name comparison tells SYS from sys


# SERVERPROPERTY and DATABASEPROPERTYEX give sql_variant, which a driver may not read: always CAST.
_FENCE_FACTS = (
    "/* azsqlcd:fence_facts */ SELECT CAST(SERVERPROPERTY(N'EngineEdition') AS int), "
    "CAST(DATABASEPROPERTYEX(DB_NAME(), N'Updateability') AS nvarchar(128)), DB_NAME(), "
    "CAST(DATABASEPROPERTYEX(DB_NAME(), N'Collation') AS nvarchar(128)), "
    "CASE WHEN EXISTS (SELECT 1 FROM sys.schemas WHERE [name] = N'SYS') THEN 0 ELSE 1 END;"
)


def fence_facts(session: Session) -> FenceFacts:
    """The facts that the fence of every run checks (Part 2 (e)). This function decides nothing."""
    edition, updateability, db_name, collation, case_sensitive = one_row(session, _FENCE_FACTS)
    if None in (edition, updateability, db_name, collation):
        raise RuntimeError("the engine gave NULL for a fence fact")
    return FenceFacts(int(edition), str(updateability), str(db_name), str(collation), flag(case_sensitive))


def applock_test(session: Session) -> bool:
    """True when the deploy lock is free, so a run row with status running is dead (A4).

    False means a run holds the lock now. Plan never takes the lock; it only asks.
    """
    (free,) = one_row(
        session,
        "/* azsqlcd:applock_test */ "
        f"SELECT APPLOCK_TEST(N'public', {names.sql_literal(APPLOCK_RESOURCE)}, N'Exclusive', N'Session');",
    )
    return flag(free)


def service_objective(session: Session) -> str:
    """The service objective of the database, for example S3 or GP_S_Gen5_2 (approver fact)."""
    (objective,) = one_row(
        session,
        "/* azsqlcd:service_objective */ "
        "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), N'ServiceObjective') AS nvarchar(128));",
    )
    if objective is None:
        raise RuntimeError("the engine gave NULL for the service objective")
    return str(objective)


# ------------------------------------------------------------------ modules
def _modules_batch(source: str | None) -> str:
    if source is None:
        requested, origin = "CAST(NULL AS nvarchar(300))", "sys.objects AS o"
        scope = f" AND o.[is_ms_shipped] = 0 AND s.[name] <> {names.sql_literal(SCHEMA)}"
    else:
        requested, origin = (
            "k.[requested_key]",
            f"{source} JOIN sys.objects AS o ON o.[object_id] = k.[object_id]",
        )
        scope = ""
    return (
        f"/* azsqlcd:capture_modules */ SELECT {requested}, s.[name], o.[name], RTRIM(o.[type]), "
        "m.[definition], m.[uses_ansi_nulls], m.[uses_quoted_identifier], m.[is_schema_bound], "
        "m.[execute_as_principal_id], "
        "HAS_PERMS_BY_NAME(QUOTENAME(s.[name]) + N'.' + QUOTENAME(o.[name]), N'OBJECT', N'VIEW DEFINITION'), "
        "ps.[name], p.[name], t.[is_disabled], "
        "(SELECT STRING_AGG(CONCAT(te.[type_desc], N':', te.[is_first], N':', te.[is_last]), N';') "
        "FROM sys.trigger_events AS te WHERE te.[object_id] = o.[object_id]) "
        f"FROM {origin} "
        "JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] "
        "JOIN sys.sql_modules AS m ON m.[object_id] = o.[object_id] "
        "LEFT JOIN sys.triggers AS t ON t.[object_id] = o.[object_id] "
        "LEFT JOIN sys.objects AS p ON p.[object_id] = t.[parent_id] "
        "LEFT JOIN sys.schemas AS ps ON ps.[schema_id] = p.[schema_id] "
        f"WHERE o.[type] IN ({type_list(*names.MODULE_KINDS)}){scope};"
    )


def _trigger_events(text: str | None) -> dict[str, dict[str, bool]]:
    """'INSERT:0:0;UPDATE:1:0' -> {event: {is_first, is_last}} (sys.trigger_events, A13)."""
    events: dict[str, dict[str, bool]] = {}
    for part in text.split(";") if text else ():
        event, first, last = part.split(":")
        events[event] = {"is_first": flag(int(first)), "is_last": flag(int(last))}
    return events


def capture_modules(session: Session, keys: Collection[str] | None) -> dict[str, dict[str, Any]]:
    """Capture format 1 of views, procedures, functions and DML triggers.

    keys None: every module that is not shipped by Microsoft, outside schema azsqlcd, under the
    key that the catalog names give. keys given: those that exist, each under the key that was
    asked for; a key without an object is absent from the result.

    Properties: kind, type (V P FN IF TF TR), definition (line ends as LF; None when encrypted),
    is_encrypted, uses_ansi_nulls, uses_quoted_identifier, is_schema_bound,
    execute_as_principal_id. A trigger also has parent ('[schema].[name]'), is_disabled and
    events ({event: {is_first, is_last}}).

    A NULL definition means WITH ENCRYPTION only when this principal has VIEW DEFINITION on the
    object. Without it the tool cannot tell, and stops: ToolError REFUSED NO_VIEW_DEFINITION.
    """
    if keys is None:
        found = rows(session, _modules_batch(None))
    else:
        found = [
            row for chunk in _chunks(keys) for row in rows(session, _modules_batch(object_source(chunk)))
        ]
    captures: dict[str, dict[str, Any]] = {}
    hidden: list[str] = []
    for row in found:
        requested, schema, name, type_code, definition, ansi_nulls, quoted_identifier, schema_bound = row[:8]
        execute_as, can_view, parent_schema, parent_name, is_disabled, events = row[8:]
        type_code = str(type_code).strip()
        kind = MODULE_TYPES[type_code]
        key = requested or names.object_key(kind, schema, name)
        if definition is None and can_view != 1:
            hidden.append(key)
            continue
        capture: dict[str, Any] = {
            "kind": kind,
            "type": type_code,
            "definition": None
            if definition is None
            else definition.replace("\r\n", "\n").replace("\r", "\n"),
            "is_encrypted": definition is None,
            "uses_ansi_nulls": flag(ansi_nulls),
            "uses_quoted_identifier": flag(quoted_identifier),
            "is_schema_bound": flag(schema_bound),
            "execute_as_principal_id": None if execute_as is None else int(execute_as),
        }
        if kind == "TRIGGER":
            capture["parent"] = names.qualified(parent_schema, parent_name)
            capture["is_disabled"] = flag(is_disabled)
            capture["events"] = _trigger_events(events)
        captures[key] = capture
    if hidden:
        raise refused(
            "NO_VIEW_DEFINITION",
            f"the definition of {len(hidden)} module(s) cannot be read and this principal has no "
            f"VIEW DEFINITION on them; first: {sorted(hidden)[0]}",
            objects=sorted(hidden),
        )
    return captures


# The type that list_user_objects gives the history table of a system-versioned table, in place of U.
# The engine owns a history table: it has no kind, so it is never managed and never unmanaged.
HISTORY_TABLE_TYPE = "U/HISTORY"


@dataclass(frozen=True)
class UserObject:
    kind: str | None  # a kind of names.KINDS; None for a type the tool does not manage (a constraint, ...)
    schema: str
    name: str
    type: str  # sys.objects.type; HISTORY_TABLE_TYPE for the history table of a temporal table


def list_user_objects(session: Session) -> list[UserObject]:
    """Every row of sys.objects that is not shipped by Microsoft and not in schema azsqlcd.

    The names of one schema are one namespace, so this list answers "is this name taken"
    (constraints included), and its rows with a kind are the candidates for the unmanaged list.
    The history table of a system-versioned table takes a name and has no kind: the engine owns it,
    the file of its temporal table names it, and plan and drift do not list it as unmanaged.
    Schemas and types are not objects of sys.objects and are not in the list.
    """
    found = rows(
        session,
        "/* azsqlcd:list_user_objects */ SELECT s.[name], o.[name], "
        "CASE WHEN EXISTS (SELECT 1 FROM sys.tables AS t "
        "WHERE t.[object_id] = o.[object_id] AND t.[temporal_type] = 1) "
        f"THEN {names.sql_literal(HISTORY_TABLE_TYPE)} ELSE RTRIM(o.[type]) END "
        "FROM sys.objects AS o JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] "
        f"WHERE o.[is_ms_shipped] = 0 AND s.[name] <> {names.sql_literal(SCHEMA)} "
        "ORDER BY s.[name], o.[name];",
    )
    return [
        UserObject(OBJECT_TYPES.get(str(code).strip()), str(schema), str(name), str(code).strip())
        for schema, name, code in found
    ]


def object_exists(session: Session, key: str) -> bool:
    """True when the catalog holds an object with this name and this kind. Sees its own transaction (A18)."""
    kind = names.parse_object_key(key)[0]
    (count,) = one_row(
        session,
        "/* azsqlcd:object_exists */ SELECT COUNT(*) FROM sys.objects AS o "
        f"WHERE o.[object_id] = {object_id_sql(key)} AND o.[type] IN ({type_list(kind)});",
    )
    return flag(count)


# ------------------------------------------------------------------ indexed views
def has_index(session: Session, key: str) -> bool:
    """True when sys.indexes holds an index (index_id > 0) of the object: an indexed view.

    ALTER VIEW drops every index of the view, so the runner asks this before an unbind or an
    ALTER. The batch always gives one row; no answer is a defect and is never read as "no index".
    """
    (found,) = one_row(
        session,
        "/* azsqlcd:has_index */ SELECT CASE WHEN EXISTS (SELECT 1 FROM sys.indexes AS i "
        f"WHERE i.[object_id] = {object_id_sql(key)} AND i.[index_id] > 0) THEN 1 ELSE 0 END;",
    )
    return flag(found)


def indexed_views(session: Session) -> list[str]:
    """The keys of the views that have an index, under the names of the catalog, in key order."""
    found = rows(
        session,
        "/* azsqlcd:indexed_views */ SELECT s.[name], v.[name] FROM sys.views AS v "
        "JOIN sys.schemas AS s ON s.[schema_id] = v.[schema_id] WHERE v.[is_ms_shipped] = 0 "
        "AND EXISTS (SELECT 1 FROM sys.indexes AS i WHERE i.[object_id] = v.[object_id] "
        "AND i.[index_id] > 0);",
    )
    return sorted(names.object_key("VIEW", schema, name) for schema, name in found)


# ------------------------------------------------------------------ export facts
SIGNED, NUMBERED, CLR, DATABASE_TRIGGER = "SIGNED", "NUMBERED", "CLR", "DATABASE_TRIGGER"  # ExportFact.fact
INDEXED_VIEW = "INDEXED_VIEW"  # ExportFact.fact
NO_SUCH_OBJECT = 208  # the error number of "Invalid object name"


@dataclass(frozen=True)
class ExportFact:
    """One thing that sys.sql_modules alone does not tell about the modules of a database."""

    fact: str  # SIGNED | NUMBERED | CLR | DATABASE_TRIGGER | INDEXED_VIEW
    schema: str | None  # None for a database DDL trigger: it belongs to no schema
    name: str
    type: str  # sys.objects.type; for a database DDL trigger sys.triggers.type


# A database DDL trigger has no row in sys.objects. An indexed view is a view with a row in
# sys.indexes; a plain view has none.
_EXPORT_FACTS = (
    "/* azsqlcd:export_facts */ "
    f"SELECT {names.sql_literal(SIGNED)}, s.[name], o.[name], RTRIM(o.[type]) FROM sys.crypt_properties AS c "
    "JOIN sys.objects AS o ON o.[object_id] = c.[major_id] "
    "JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] WHERE c.[class] = 1 "
    f"UNION ALL SELECT {names.sql_literal(CLR)}, s.[name], o.[name], RTRIM(o.[type]) FROM sys.objects AS o "
    "JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] "
    f"WHERE o.[is_ms_shipped] = 0 AND s.[name] <> {names.sql_literal(SCHEMA)} "
    f"AND o.[type] IN ({', '.join(names.sql_literal(code) for code in CLR_TYPES)}) "
    f"UNION ALL SELECT {names.sql_literal(DATABASE_TRIGGER)}, NULL, t.[name], RTRIM(t.[type]) "
    "FROM sys.triggers AS t WHERE t.[parent_class] = 0 AND t.[is_ms_shipped] = 0 "
    f"UNION ALL SELECT {names.sql_literal(INDEXED_VIEW)}, s.[name], o.[name], RTRIM(o.[type]) "
    "FROM sys.objects AS o JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] "
    f"WHERE o.[is_ms_shipped] = 0 AND s.[name] <> {names.sql_literal(SCHEMA)} AND o.[type] = N'V' "
    "AND EXISTS (SELECT 1 FROM sys.indexes AS i WHERE i.[object_id] = o.[object_id] AND i.[index_id] > 0);"
)
# sys.numbered_procedures holds the procedures ;2 and up under the id of procedure ;1. Microsoft
# Learn does not list the view for Azure SQL Database, so it has a batch of its own: a database
# without the view has no numbered procedure, and the other facts are still read.
_NUMBERED_PROCEDURES = (
    "/* azsqlcd:numbered_procedures */ "
    f"SELECT {names.sql_literal(NUMBERED)}, s.[name], o.[name], RTRIM(o.[type]) "
    "FROM sys.numbered_procedures AS n "
    "JOIN sys.objects AS o ON o.[object_id] = n.[object_id] "
    "JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id];"
)


def _numbered_procedures(session: Session) -> list[tuple[Any, ...]]:
    try:
        return rows(session, _NUMBERED_PROCEDURES)
    except SqlError as error:
        if error.number != NO_SUCH_OBJECT:
            raise
        return []


def export_facts(session: Session) -> list[ExportFact]:
    """What export must know beyond capture_modules (design (c) 3).

    SIGNED: an object with a row in sys.crypt_properties (a deploy drops the signature). NUMBERED:
    a procedure with numbered versions (;2 and up). CLR: a CLR module outside schema azsqlcd.
    DATABASE_TRIGGER: a database DDL trigger. INDEXED_VIEW: a view with an index (ALTER VIEW drops
    every index of the view). The rows come back as the engine gives them; this function decides
    nothing. Two batches: the numbered procedures are read alone, and error 208 on that batch
    (the database has no sys.numbered_procedures) means that there is none.
    """
    facts: list[ExportFact] = []
    reads = (
        (rows(session, _EXPORT_FACTS), (SIGNED, CLR, DATABASE_TRIGGER, INDEXED_VIEW)),
        (_numbered_procedures(session), (NUMBERED,)),
    )
    for found, kinds in reads:
        for fact, schema, name, type_code in found:
            if fact not in kinds:
                raise RuntimeError(f"the export facts hold a row of the kind {fact!r}")
            if name is None or (schema is None) != (fact == DATABASE_TRIGGER):
                raise RuntimeError(f"the export facts hold a {fact} row without its name or its schema")
            facts.append(
                ExportFact(
                    str(fact), None if schema is None else str(schema), str(name), str(type_code).strip()
                )
            )
    return facts


# ------------------------------------------------------------------ dependants
@dataclass(frozen=True)
class Dependant:
    key: str
    kind: str  # VIEW | PROCEDURE | FUNCTION | TRIGGER
    schema_bound: bool


def dependants_of(session: Session, table_keys: Collection[str]) -> list[Dependant]:
    """The modules that reference one of the objects (sys.sql_expression_dependencies), by key."""
    found: dict[str, Dependant] = {}
    for chunk in _chunks(table_keys):
        batch = (
            "/* azsqlcd:dependants_of */ SELECT DISTINCT s.[name], o.[name], RTRIM(o.[type]), "
            "m.[is_schema_bound] FROM sys.sql_expression_dependencies AS d "
            "JOIN sys.objects AS o ON o.[object_id] = d.[referencing_id] "
            "JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] "
            "JOIN sys.sql_modules AS m ON m.[object_id] = o.[object_id] "
            "WHERE d.[referenced_class] = 1 "  # an object or a column; a type id can equal an object id
            f"AND d.[referenced_id] IN ({', '.join(object_id_sql(key) for key in chunk)}) "
            f"AND o.[type] IN ({type_list(*names.MODULE_KINDS)});"
        )
        for schema, name, type_code, schema_bound in rows(session, batch):
            kind = MODULE_TYPES[str(type_code).strip()]
            key = names.object_key(kind, schema, name)
            found[key] = Dependant(key, kind, flag(schema_bound))
    return [found[key] for key in sorted(found)]


def table_columns(session: Session, table_keys: Collection[str]) -> dict[str, frozenset[str]]:
    """Column names of the tables that exist, by asked key. A table that is gone has no entry."""
    found: dict[str, set[str]] = {}
    for chunk in _chunks(table_keys):
        batch = (
            "/* azsqlcd:table_columns */ SELECT k.[requested_key], c.[name] "
            f"FROM {object_source(chunk)} JOIN sys.columns AS c ON c.[object_id] = k.[object_id];"
        )
        for requested, name in rows(session, batch):
            found.setdefault(str(requested), set()).add(str(name))
    return {key: frozenset(columns) for key, columns in found.items()}


def broken_references(session: Session, module_key: str) -> list[str]:
    """What sys.dm_sql_referenced_entities reports as broken for one module (A12). Empty = sound.

    An entry is a reason, and a referenced name when there is one: ERROR_2020, ERROR_207 or
    ERROR_208 (NOT_BOUND_FINDINGS: the engine could not bind the module), UNRESOLVED [s].[n] (no
    such object, and not caller dependent), COLUMNS_NOT_FOUND [s].[n], INCOMPLETE [s].[n].[c].
    Names only, never definition text.

    COLUMNS_NOT_FOUND counts only for a referenced object that has columns (a table, a view, a
    table-valued function). The engine gives is_all_columns_found = 0 for every reference to a
    procedure, a scalar function, a sequence and a type, and for a name that it cannot resolve
    (measured on Azure SQL Database): none of these has a column that could be missing.

    A sound module can still have findings: the engine gives is_all_columns_found = 0 for a table
    that a statement uses together with a #temp table, an unresolved one-part entity for the alias
    of a #temp table in UPDATE alias ... FROM, and error 2020 for some sound procedures (measured,
    RO-2). So a result is never read alone as "broken by this release": the caller compares the
    result after a change with the result before it (tables.Hooks.dependant_findings).

    A module that does not bind makes the read itself fail (measured, live spike X3). For a view,
    a procedure or an inline function that uses a dropped column the engine raises 207 (Invalid
    column name) and then 2020; for a view whose table is gone, 208 and then 2020; for a procedure
    that names a table that does not exist, 2020 alone. The driver returns only the first error,
    so 207 and 208 on this batch mean what 2020 means. Any other error is raised.

    These errors do not end the open transaction, also with XACT_ABORT ON: after the failed read
    @@TRANCOUNT and XACT_STATE() are still (1, 1), and later reads see the state of the unit of
    work. (A failed sp_refreshsqlmodule of the same module does end the transaction.)
    """
    kind, schema, name = names.parse_object_key(module_key)
    if schema is None or kind not in names.MODULE_KINDS:
        raise ValueError(f"not a module key: {module_key!r}")
    batch = (
        "/* azsqlcd:broken_references */ SELECT r.[referenced_schema_name], r.[referenced_entity_name], "
        "r.[referenced_minor_name], r.[referenced_id], r.[is_caller_dependent], r.[is_all_columns_found], "
        "r.[is_incomplete], RTRIM(ro.[type]) "
        f"FROM sys.dm_sql_referenced_entities({literal(names.qualified(schema, name))}, N'OBJECT') AS r "
        # class 1 = an object or a column; the id of a type can equal the id of an object
        "LEFT JOIN sys.objects AS ro ON ro.[object_id] = r.[referenced_id] AND r.[referenced_class] = 1;"
    )
    try:
        found = rows(session, batch)
    except SqlError as error:
        if error.number not in NOT_BOUND_NUMBERS:
            raise
        return [f"ERROR_{error.number}"]
    broken: set[str] = set()
    for row in found:  # a row of another width is a defect: the unpacking raises ValueError
        ref_schema, entity, minor, referenced_id, caller_dependent, all_columns_found, incomplete, code = row
        referenced_type = None if code is None else str(code).strip()  # char(2): 'U ' as the driver gives it
        referenced = ".".join(names.quote(part) for part in (ref_schema, entity) if part)
        if referenced_id is None and caller_dependent == 0:
            broken.add(f"UNRESOLVED {referenced}")
        if all_columns_found == 0 and referenced_type in COLUMN_OWNER_TYPES:
            broken.add(f"COLUMNS_NOT_FOUND {referenced}")
        if incomplete == 1:
            broken.add(f"INCOMPLETE {referenced}" + (f".{names.quote(minor)}" if minor else ""))
    return sorted(broken)


# ------------------------------------------------------------------ approver facts
@dataclass(frozen=True)
class TableFacts:
    rows: int  # heap or clustered index, every partition
    reserved_pages: int  # table and all its indexes


def table_facts(session: Session, table_keys: Collection[str]) -> dict[str, TableFacts]:
    """Row count and reserved pages (sys.dm_db_partition_stats) of the tables that exist, by asked key."""
    facts: dict[str, TableFacts] = {}
    for chunk in _chunks(table_keys):
        batch = (
            "/* azsqlcd:table_facts */ SELECT k.[requested_key], f.[row_count], f.[reserved_page_count] "
            f"FROM {object_source(chunk)} JOIN ("
            "SELECT p.[object_id], "
            "SUM(CASE WHEN p.[index_id] IN (0, 1) THEN p.[row_count] ELSE 0 END) AS [row_count], "
            "SUM(p.[reserved_page_count]) AS [reserved_page_count] "
            "FROM sys.dm_db_partition_stats AS p GROUP BY p.[object_id]"
            ") AS f ON f.[object_id] = k.[object_id];"
        )
        for key, row_count, reserved_pages in rows(session, batch):
            facts[key] = TableFacts(int(row_count), int(reserved_pages))
    return facts


# ------------------------------------------------------------------ comparison rule (Part 2 (e))
@dataclass(frozen=True)
class Difference:
    property: str
    stored_sha256: str
    live_sha256: str | None  # None: the live capture does not have the property


def project_capture(live: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """The live capture, cut to the properties of the stored capture.

    A newer tool captures more than an older one stored. What the stored capture never held
    cannot have drifted, so it takes no part in the comparison.
    """
    return {name: live[name] for name in stored if name in live}


def capture_differences(stored: dict[str, Any], live: dict[str, Any]) -> list[Difference]:
    """Drift, precondition and read-back: the properties whose stored and live values differ.

    Values are compared as canonical JSON. The result holds property names and hashes only (A25).
    """
    seen = project_capture(live, stored)
    differences: list[Difference] = []
    for name in sorted(stored):
        stored_hash = capture_sha256({name: stored[name]})
        live_hash = capture_sha256({name: seen[name]}) if name in seen else None
        if stored_hash != live_hash:
            differences.append(Difference(name, stored_hash, live_hash))
    return differences


# ====================================================================================================
# table-class objects
# ====================================================================================================
# Captures of schemas, tables (columns, constraints, indexes), types, sequences and synonyms, and the
# model that table_model = true reads from the catalog, go below this line. Reuse rows, one_row, flag,
# type_list, object_id_sql and object_source; keep every capture flat, so that project_capture and
# capture_differences hold for it unchanged.
