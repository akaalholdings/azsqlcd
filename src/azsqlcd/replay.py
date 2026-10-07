"""Operations applied to an in-memory model, with precondition checks: the proof engine.

`verify` replays the statements of a migration on the model of the base revision and requires the
result to equal the model of the head revision. Before an operation changes the model it must pass
the checks that the engine makes on the objects that the model holds. A failed check is a
ReplayError: it names the object and says what blocks the statement.

What is checked. Only modelled objects count: modules, rows and unmanaged objects are unknown here.
  * What a statement names exists: the table, column, constraint, index, sequence, type, synonym
    or schema; the schema of a new object (dbo always exists); the alias type of a column; the
    columns of a key, an index and a foreign key.
  * What a statement creates has a free name. Tables, sequences, synonyms and constraints share
    one namespace in a schema; indexes, PRIMARY KEY and UNIQUE share one in a table.
  * ALTER COLUMN and DROP COLUMN are blocked by an index, PRIMARY KEY, UNIQUE, FOREIGN KEY, CHECK
    or computed column that uses the column, and DROP COLUMN also by the DEFAULT of the column.
    One exception, the rule of the engine for ALTER COLUMN: a varchar, nvarchar or varbinary
    column whose data type, nullability and collation stay and whose length stays or grows (not
    to max) passes under a UNIQUE constraint, a CHECK constraint and a rowstore index that has it
    as key or included column (only_widens). A PRIMARY KEY, a FOREIGN KEY (also one that
    references the column), a computed column, an index filter and a columnstore index block it
    all the same. In every other case the check is stricter than the engine (design (b), proof
    step 5).
  * A foreign key needs its target table, a PRIMARY KEY, UNIQUE constraint or unique index
    without a filter on exactly the referenced columns, and equal column data types.
  * A drop is blocked by what depends on the object: a table by a foreign key of another table,
    a key by a foreign key on exactly its columns (also when another key has the same columns:
    the engine binds a foreign key to one index, and the model does not hold which), a type by a
    column, a sequence by a DEFAULT, a schema by an object in it.
  * The new name of a rename is free. sp_rename of a column is blocked by a CHECK, computed
    column or index filter that names the column (the engine refuses it: error 15336).
  * System-versioned temporal tables. CREATE TABLE names a history table in a schema that exists,
    under a name that no object of the model has. SET (SYSTEM_VERSIONING = OFF) needs a temporal
    table; after it the table keeps its period columns (model.UnversionedTable) until DROP TABLE
    or SET (SYSTEM_VERSIONING = ON (...)), which needs the period columns and a PRIMARY KEY. DROP
    TABLE of a temporal table is blocked while versioning is on (engine error 13552). ALTER
    COLUMN, DROP COLUMN and sp_rename of a period column are blocked, and so is the drop of the
    PRIMARY KEY of a temporal table. ADD of a computed column or of an IDENTITY column is blocked
    while versioning is on (the engine refuses both). The history table is never an object of the
    model, but its name is taken while the table is versioned: no object, constraint or rename
    takes it, and DROP SCHEMA is blocked by a history table in the schema.
  * Dynamic data masking. ADD MASKED and DROP MASKED need a column that is not computed and not a
    period column; DROP MASKED needs a mask. ADD MASKED on a masked column replaces the mask.
    ALTER COLUMN with a data type removes the mask of the column, as the engine does.

A column reference in an expression is found by its token: any identifier token with the name of
the column counts. A function or type of the same name is not told apart, so a check can block
too much and never too little.

Public API: ReplayError, apply, replay, column_dependants, only_widens, has_key.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from typing import NoReturn, assert_never

from azsqlcd import names
from azsqlcd.model import (
    TABLE_COMPRESSIONS,
    AddColumn,
    AddConstraint,
    AliasType,
    AlterColumn,
    AlterColumnProperty,
    AlterSequence,
    Check,
    Column,
    Constraint,
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
    Index,
    KeyColumn,
    MaskColumn,
    Model,
    Operation,
    PrimaryKey,
    RebuildTable,
    Rename,
    Schema,
    Sequence,
    SetSystemVersioning,
    Table,
    TableType,
    Temporal,
    TypeRef,
    Unique,
    UnmaskColumn,
    UnversionedTable,
    fold,
)

_KIND = {PrimaryKey: "PRIMARY KEY", Unique: "UNIQUE", ForeignKey: "FOREIGN KEY", Check: "CHECK"}
# The data types whose length ALTER COLUMN can raise under UNIQUE, CHECK and an index.
_WIDENABLE = frozenset({"varchar", "nvarchar", "varbinary"})
_SHOWN = 3  # names that one message lists


# The schemas that the engine creates in every database, case-folded. None of them has a schema
# file; dbo is the one that holds objects of the model.
ENGINE_SCHEMAS = frozenset(
    {
        "dbo",
        "sys",
        "guest",
        "information_schema",
        "db_owner",
        "db_accessadmin",
        "db_securityadmin",
        "db_ddladmin",
        "db_backupoperator",
        "db_datareader",
        "db_datawriter",
        "db_denydatareader",
        "db_denydatawriter",
    }
)
TOOL_SCHEMA = "azsqlcd"  # state.SCHEMA: the tables of the tool, never an object of the model


class ReplayError(Exception):
    """An operation that the model does not accept.

    op_index is the position of the operation in the list given to replay(), from 0; apply()
    reports 0. object_key is the object that the statement works on. message says what blocks
    the statement; it holds names and never expression text.
    """

    def __init__(self, op_index: int, object_key: str, message: str) -> None:
        super().__init__(f"statement {op_index + 1}: {object_key}: {message}")
        self.op_index = op_index
        self.object_key = object_key
        self.message = message


def _blocked(key: str, message: str) -> NoReturn:
    raise ReplayError(0, key, message)


def _same(a: str, b: str) -> bool:
    return fold(a) == fold(b)


def _some(items: list[str]) -> str:
    more = len(items) - _SHOWN
    return ", ".join(items[:_SHOWN]) + (f" and {more} more" if more > 0 else "")


def _label(constraint: Constraint | DefaultConstraint) -> str:
    kind = "DEFAULT" if isinstance(constraint, DefaultConstraint) else _KIND[type(constraint)]
    assert constraint.name is not None  # Table has checked it
    return f"{kind} {names.quote(constraint.name)}"


# ------------------------------------------------------------------ lookups
def _tables(model: Model) -> Iterator[Table]:
    return (obj for obj in model.values() if isinstance(obj, Table))


def _table(model: Model, schema: str, name: str) -> Table:
    key = names.object_key("TABLE", schema, name)
    table = model.get(key)
    if not isinstance(table, Table):
        _blocked(key, "the table does not exist")
    return table


def _column(table: Table, name: str) -> Column:
    column = table.column(name)
    if column is None:
        _blocked(table.key, f"the table has no column {names.quote(name)}")
    return column


def _foreign_keys(table: Table) -> Iterator[ForeignKey]:
    return (c for c in table.constraints if isinstance(c, ForeignKey))


def _references(key: ForeignKey, schema: str, table: str) -> bool:
    return _same(key.ref_schema, schema) and _same(key.ref_table, table)


def _constraint_names(table: Table) -> Iterator[str]:
    """Names in the namespace of the schema: the constraints of the table and its DEFAULTs."""
    for constraint in table.constraints:
        if constraint.name is not None:
            yield constraint.name
    for column in table.columns:
        if column.default is not None and column.default.name is not None:
            yield column.default.name


def _holder(model: Model, schema: str, name: str) -> str | None:
    """What has the name in the object namespace of the schema; None when the name is free."""
    for obj in model.values():
        if isinstance(obj, Schema | AliasType | TableType) or not _same(obj.schema, schema):
            continue
        if _same(obj.name, name):
            return obj.key
        if isinstance(obj, Table) and any(_same(found, name) for found in _constraint_names(obj)):
            return f"a constraint of {obj.key}"
    return None


def _has_index_name(table: Table, name: str) -> bool:
    return table.index(name) is not None or isinstance(table.constraint(name), PrimaryKey | Unique)


# ------------------------------------------------------------------ what uses a column
def _name_token(name: str) -> str:
    """The form in which Expression.comparison holds an identifier."""
    return Expression((names.quote(name),)).comparison[0]


def _constraint_columns(constraint: Constraint) -> list[str]:
    if isinstance(constraint, PrimaryKey | Unique):
        return [k.name for k in constraint.columns]
    return list(constraint.columns) if isinstance(constraint, ForeignKey) else []


def _expression_users(table: Table, column: str, *, checks: bool = True) -> list[str]:
    """CHECK constraints (not with checks=False), computed columns and index filters of the table
    that name the column."""
    token = _name_token(column)

    def uses(expression: Expression | None) -> bool:
        return expression is not None and token in expression.comparison

    found = [_label(c) for c in table.constraints if checks and isinstance(c, Check) and uses(c.expression)]
    found += [
        f"computed column {names.quote(c.name)}"
        for c in table.columns
        if c.computed is not None and not _same(c.name, column) and uses(c.computed.expression)
    ]
    return found + [f"index {names.quote(i.name)}" for i in table.indexes if uses(i.filter)]


def only_widens(column: Column, type_: TypeRef, nullable: bool, collation: str | None) -> bool:
    """True when ALTER COLUMN to this definition only keeps or raises the length of the column.

    The rule of the engine for a column under an index, a UNIQUE or a CHECK constraint: the
    column is varchar, nvarchar or varbinary, the data type does not change, the new size is
    equal to or larger than the old size, and nullability does not change. Not to (max) and not
    for an alias type: the engine refuses (max), and an alias type states no length. The collation
    stays too: ALTER COLUMN without COLLATE gives the column the collation of the database.
    """
    old = column.type
    if old is None or old.schema is not None or type_.schema is not None:
        return False
    if old.name.lower() != type_.name.lower() or old.name.lower() not in _WIDENABLE:
        return False
    if not isinstance(old.length, int) or not isinstance(type_.length, int) or type_.length < old.length:
        return False
    return column.nullable == nullable and fold(column.collation or "") == fold(collation or "")


def column_dependants(
    model: Model, schema: str, table: str, column: str, *, widening: bool = False
) -> list[str]:
    """What the model holds that uses the column, for example 'index [IX_Order_Status]'.

    Indexes (key, included column, filter), PRIMARY KEY, UNIQUE, FOREIGN KEY (also one of another
    table that references the column), CHECK and computed columns. The DEFAULT of the column is
    not in the list. ReplayError when the table does not exist.

    widening=True gives what blocks an ALTER COLUMN that only raises a length (only_widens):
    not a UNIQUE constraint, not a CHECK constraint and not a rowstore index that has the column
    as key or included column. Seen on Azure SQL Database: the engine refuses the longer column
    under an index filter, a foreign key (on either side) and a computed column. A PRIMARY KEY
    blocks by the documented rule, and a columnstore index because only a nonclustered one was
    tested.
    """
    owner = _table(model, schema, table)
    found = [
        _label(c)
        for c in owner.constraints
        if not (widening and isinstance(c, Unique))
        and any(_same(name, column) for name in _constraint_columns(c))
    ]
    found += [
        f"index {names.quote(i.name)}"
        for i in owner.indexes
        if not (widening and not i.columnstore)
        and any(_same(name, column) for name in (*(k.name for k in i.columns), *i.included))
    ]
    found += _expression_users(owner, column, checks=not widening)
    for other in _tables(model):
        found += [
            f"{_label(key)} of {names.qualified(other.schema, other.name)}"
            for key in _foreign_keys(other)
            if _references(key, schema, table) and any(_same(name, column) for name in key.ref_columns)
        ]
    return list(dict.fromkeys(found))


# ------------------------------------------------------------------ keys and foreign keys
def has_key(table: Table, columns: Iterable[str]) -> bool:
    """True when a PRIMARY KEY, a UNIQUE constraint or a unique index without a filter of the table
    has exactly these columns, in any order: what a foreign key may reference."""
    wanted = frozenset(fold(name) for name in columns)
    keys = [c.columns for c in table.constraints if isinstance(c, PrimaryKey | Unique)]
    keys += [i.columns for i in table.indexes if i.unique and i.filter is None]
    return any(wanted == frozenset(fold(k.name) for k in key) for key in keys)


def _need_no_key_user(model: Model, table: Table, columns: Iterable[KeyColumn], what: str) -> None:
    """Refuse the drop of a key when a foreign key references exactly its columns.

    Another key on the same columns does not free it. The engine binds a foreign key to one
    index (sys.foreign_keys.key_index_id) and refuses to drop that index (error 3725); the model
    does not hold which one it is. Drop the foreign key, drop the key, add the foreign key again.
    """
    wanted = [k.name for k in columns]
    users = [
        f"{_label(key)} of {names.qualified(other.schema, other.name)}"
        for other in _tables(model)
        for key in _foreign_keys(other)
        if _references(key, table.schema, table.name)
        and frozenset(map(fold, key.ref_columns)) == frozenset(map(fold, wanted))
    ]
    if users:
        _blocked(table.key, f"{what} is the key that {_some(users)} references: drop the foreign key first")


def _base_type(model: Model, type_: TypeRef) -> TypeRef:
    if type_.schema is None:
        return type_
    alias = model.get(names.object_key("TYPE", type_.schema, type_.name))
    return alias.base if isinstance(alias, AliasType) else type_


def _foreign_key_problem(model: Model, table: Table, key: ForeignKey) -> str | None:
    """Why the foreign key of the table cannot be created; None when it can."""
    if _references(key, table.schema, table.name):
        target = table  # the table may not be in the model yet (CREATE TABLE)
    else:
        found = model.get(names.object_key("TABLE", key.ref_schema, key.ref_table))
        if not isinstance(found, Table):
            return f"the referenced table {names.qualified(key.ref_schema, key.ref_table)} does not exist"
        target = found
    target_name = names.qualified(target.schema, target.name)
    for ref in key.ref_columns:
        if target.column(ref) is None:
            return f"the referenced table {target_name} has no column {names.quote(ref)}"
    if not has_key(target, key.ref_columns):
        columns = ", ".join(names.quote(name) for name in key.ref_columns)
        return f"{target_name} has no PRIMARY KEY, UNIQUE constraint or unique index on ({columns})"
    for own, ref in zip(key.columns, key.ref_columns, strict=True):
        a, b = _column(table, own), _column(target, ref)
        if a.type is None or b.type is None:
            continue  # a computed column: the model does not hold its data type
        collations = a.collation is None or b.collation is None or _same(a.collation, b.collation)
        if _base_type(model, a.type) != _base_type(model, b.type) or not collations:
            return f"column {names.quote(own)} and {target_name}.{names.quote(ref)} differ in data type"
    return None


# ------------------------------------------------------------------ preconditions
def _need_schema(model: Model, key: str, schema: str) -> None:
    # dbo exists in every database and has no file unless the repository gives it one
    if not _same(schema, "dbo") and names.object_key("SCHEMA", None, schema) not in model:
        _blocked(key, f"the schema {names.quote(schema)} does not exist")


def _need_free(model: Model, key: str, schema: str, name: str) -> None:
    holder = _holder(model, schema, name)
    if holder is not None:
        _blocked(key, f"the name {names.quote(name)} is taken by {holder}")
    _need_not_history(model, key, schema, name)


def _need_type(model: Model, key: str, type_: TypeRef | None, what: str) -> None:
    if type_ is None or type_.schema is None:
        return
    if not isinstance(model.get(names.object_key("TYPE", type_.schema, type_.name)), AliasType):
        _blocked(key, f"the data type {names.qualified(type_.schema, type_.name)} of {what} does not exist")


def _need_columns(table: Table | TableType, columns: Iterable[str], what: str) -> None:
    for name in columns:
        if table.column(name) is None:
            _blocked(table.key, f"{what} names the column {names.quote(name)}, which the table does not have")


def _need_parts(model: Model, table: Table | TableType) -> None:
    """The alias types and the column lists inside a new table or table type."""
    for column in table.columns:
        _need_type(model, table.key, column.type, f"column {names.quote(column.name)}")
    for constraint in table.constraints:
        kind = _KIND[type(constraint)]
        _need_columns(
            table, _constraint_columns(constraint), kind if constraint.name is None else _label(constraint)
        )
    for index in table.indexes:
        columns = (*(k.name for k in index.columns), *index.included)
        _need_columns(table, columns, f"index {names.quote(index.name)}")


def _type_users(model: Model, schema: str, name: str) -> list[str]:
    def is_it(type_: TypeRef | None) -> bool:
        return (
            type_ is not None
            and type_.schema is not None
            and _same(type_.schema, schema)
            and _same(type_.name, name)
        )

    users: list[str] = []
    for obj in model.values():
        if isinstance(obj, Table | TableType):
            users += [f"column {names.quote(c.name)} of {obj.key}" for c in obj.columns if is_it(c.type)]
        elif isinstance(obj, Sequence) and is_it(obj.type):
            users.append(obj.key)
    return users


def _next_value_for(expression: Expression, schema: str, name: str) -> bool:
    """True when the expression holds NEXT VALUE FOR the sequence (one-part name: schema dbo)."""
    tokens = expression.comparison
    wanted = (_name_token(schema), ".", _name_token(name))
    for i in range(len(tokens) - 3):
        if tokens[i : i + 3] != ("next", "value", "for"):
            continue
        if tokens[i + 3 : i + 6] == wanted:
            return True
        if _same(schema, "dbo") and tokens[i + 3] == wanted[2] and tokens[i + 4 : i + 5] != (".",):
            return True
    return False


def _sequence_users(model: Model, schema: str, name: str) -> list[str]:
    return [
        f"{_label(column.default)} of {names.qualified(table.schema, table.name)}"
        for table in _tables(model)
        for column in table.columns
        if column.default is not None and _next_value_for(column.default.expression, schema, name)
    ]


# ------------------------------------------------------------------ temporal tables
def _need_no_period_column(table: Table, column: Column, what: str) -> None:
    if column.generated is not None:
        message = f"{what} {names.quote(column.name)}: it is a period column (GENERATED ALWAYS) of the table"
        _blocked(table.key, message)


def _need_history(model: Model, table: Table, temporal: Temporal) -> None:
    """The history table of a temporal table: a schema that exists and a name that the model does
    not hold. The engine makes the history table, or takes one that fits; the model has neither."""
    history = names.qualified(temporal.history_schema, temporal.history_table)
    _need_schema(model, table.key, temporal.history_schema)
    same_schema = _same(temporal.history_schema, table.schema)
    if same_schema and _same(temporal.history_table, table.name):
        _blocked(table.key, f"the history table {history} is the table itself")
    holder = _holder(model, temporal.history_schema, temporal.history_table)
    if holder is None and same_schema:
        taken = any(_same(name, temporal.history_table) for name in _constraint_names(table))
        holder = "a constraint of the table" if taken else None
    if holder is not None:
        message = f"the history table {history} has the name of {holder}: a history table has no object file"
        _blocked(table.key, message)
    for other in _tables(model):
        if other.key != table.key and other.temporal is not None and _is_history(other.temporal, temporal):
            _blocked(table.key, f"the history table {history} is the history table of {other.key}")


def _is_history(temporal: Temporal, other: Temporal) -> bool:
    return _same(temporal.history_schema, other.history_schema) and _same(
        temporal.history_table, other.history_table
    )


def _need_not_history(model: Model, key: str, schema: str, name: str) -> None:
    """A new name cannot be the name of the history table of a table that is versioned now: the
    engine holds the history table as an object of that schema."""
    for table in _tables(model):
        t = table.temporal
        if t is not None and _same(t.history_schema, schema) and _same(t.history_table, name):
            _blocked(key, f"the name {names.quote(name)} is taken by the history table of {table.key}")


def _set_system_versioning(model: Model, op: SetSystemVersioning) -> Model:
    table = _table(model, op.schema, op.table)
    parts = (table.schema, table.name, table.columns, table.constraints, table.indexes)
    if not op.on:
        if table.temporal is None:
            _blocked(table.key, "SYSTEM_VERSIONING = OFF: the table is not system-versioned")
        return model.replace(UnversionedTable(*parts))
    if table.temporal is not None:
        _blocked(table.key, "SYSTEM_VERSIONING = ON: the table is system-versioned already")
    start = [c.name for c in table.columns if c.generated == "ROW_START"]
    end = [c.name for c in table.columns if c.generated == "ROW_END"]
    if len(start) != 1 or len(end) != 1:
        message = (
            "SYSTEM_VERSIONING = ON: the table has no period columns (GENERATED ALWAYS AS ROW START and "
            "ROW END with PERIOD FOR SYSTEM_TIME); this version does not read the statements that add them"
        )
        _blocked(table.key, message)
    if not any(isinstance(c, PrimaryKey) for c in table.constraints):
        _blocked(table.key, "SYSTEM_VERSIONING = ON: the table has no PRIMARY KEY")
    assert op.history_schema is not None and op.history_table is not None  # checked by the class
    temporal = Temporal(start[0], end[0], op.history_schema, op.history_table, op.retention)
    _need_history(model, table, temporal)
    try:
        return model.replace(Table(*parts, temporal))
    except ValueError as e:
        _blocked(table.key, str(e))


# ------------------------------------------------------------------ changes of one table
def _changed[T: (Table, Column)](key: str, item: T, **fields: object) -> T:
    """dataclasses.replace; what the model refuses (its invariants) blocks the statement."""
    try:
        return replace(item, **fields)
    except ValueError as e:
        _blocked(key, str(e))


def _with_column(table: Table, old: Column, new: Column) -> Table:
    return _changed(table.key, table, columns=tuple(new if c is old else c for c in table.columns))


def _is_columnstore(item: PrimaryKey | Unique | Index) -> bool:
    return isinstance(item, Index) and item.columnstore


def _need_clustering(table: Table, item: PrimaryKey | Unique | Index, what: str) -> None:
    """A new clustered key or index: the table has none yet, and a compressed heap gives its
    DATA_COMPRESSION to a clustered rowstore index that states none (the engine: it inherits)."""
    if not item.clustered:
        return
    existing = table.clustered_item()
    if existing is not None:
        name = names.quote(existing.name or "")
        _blocked(table.key, f"the table has the clustered index or key {name} already")
    stated = "DATA_COMPRESSION" in dict(item.options)
    if table.compression is not None and not _is_columnstore(item) and not stated:
        _blocked(
            table.key,
            f"the heap has DATA_COMPRESSION = {table.compression} and {what} states no DATA_COMPRESSION: "
            f"it would inherit {table.compression}. State DATA_COMPRESSION on it (NONE, ROW or PAGE)",
        )


def _heap_compression(item: PrimaryKey | Unique | Index) -> str | None:
    """The DATA_COMPRESSION that the heap keeps when this clustered key or index is dropped."""
    if not item.clustered or _is_columnstore(item):
        return None
    stated = dict(item.options).get("DATA_COMPRESSION")
    return stated if stated in TABLE_COMPRESSIONS else None


def _add_constraint(model: Model, op: AddConstraint) -> Model:
    table = _table(model, op.schema, op.table)
    constraint = op.constraint
    if constraint.name is None:
        _blocked(table.key, "a constraint of a table needs a name")
    _need_free(model, table.key, op.schema, constraint.name)
    if isinstance(constraint, DefaultConstraint):
        assert op.for_column is not None  # AddConstraint has checked it
        column = _column(table, op.for_column)
        if column.computed is not None:
            _blocked(table.key, f"column {names.quote(column.name)} is computed and cannot have a DEFAULT")
        if column.default is not None:
            _blocked(table.key, f"column {names.quote(column.name)} has {_label(column.default)} already")
        return model.replace(_with_column(table, column, replace(column, default=constraint)))
    _need_columns(table, _constraint_columns(constraint), _label(constraint))
    if isinstance(constraint, PrimaryKey | Unique) and table.index(constraint.name) is not None:
        _blocked(table.key, f"the table has an index named {names.quote(constraint.name)}")
    if isinstance(constraint, PrimaryKey):
        existing = [c for c in table.constraints if isinstance(c, PrimaryKey)]
        if existing:
            _blocked(table.key, f"the table has {_label(existing[0])} already")
    if isinstance(constraint, ForeignKey):
        problem = _foreign_key_problem(model, table, constraint)
        if problem is not None:
            _blocked(table.key, f"{_label(constraint)}: {problem}")
    compression = table.compression
    if isinstance(constraint, PrimaryKey | Unique):
        _need_clustering(table, constraint, _label(constraint))
        compression = None if constraint.clustered else compression
    return model.replace(
        _changed(table.key, table, constraints=(*table.constraints, constraint), compression=compression)
    )


def _drop_constraint(model: Model, op: DropConstraint) -> Model:
    table = _table(model, op.schema, op.table)
    constraint = table.constraint(op.name)
    if constraint is not None:
        if isinstance(constraint, PrimaryKey) and table.temporal is not None:
            message = f"{_label(constraint)} is the PRIMARY KEY of a system-versioned table: it cannot go"
            _blocked(table.key, message)
        compression = table.compression
        if isinstance(constraint, PrimaryKey | Unique) and constraint.clustered:
            compression = _heap_compression(constraint)
        rest = replace(
            table,
            constraints=tuple(c for c in table.constraints if c is not constraint),
            compression=compression,
        )
        if isinstance(constraint, PrimaryKey | Unique):
            _need_no_key_user(model, table, constraint.columns, _label(constraint))
        return model.replace(rest)
    for column in table.columns:
        default = column.default
        if default is not None and default.name is not None and _same(default.name, op.name):
            return model.replace(_with_column(table, column, replace(column, default=None)))
    _blocked(table.key, f"the table has no constraint {names.quote(op.name)}")


def _alter_column(model: Model, op: AlterColumn) -> Model:
    table = _table(model, op.schema, op.table)
    column = _column(table, op.column)
    shown = names.quote(column.name)
    if column.computed is not None:
        _blocked(table.key, f"column {shown} is computed: ALTER COLUMN cannot change it")
    _need_no_period_column(table, column, "ALTER COLUMN")
    widening = only_widens(column, op.type, op.nullable, op.collation)
    users = column_dependants(model, op.schema, op.table, op.column, widening=widening)
    if users:
        _blocked(table.key, f"ALTER COLUMN {shown} is blocked by {_some(users)}")
    held = [name for name, has in _properties(column).items() if has and name != "NOT FOR REPLICATION"]
    if held:
        # not proven on the engine: whether ALTER COLUMN keeps the property. The sure way is asked for.
        _blocked(
            table.key,
            f"column {shown} has {held[0]}: write ALTER COLUMN {shown} DROP {held[0]} before this "
            f"statement and ADD {held[0]} after it",
        )
    _need_type(model, table.key, op.type, f"column {shown}")
    # The engine removes the mask of the column with every ALTER COLUMN that states a data type
    # (seen on Azure SQL Database: wider type, other type, NOT NULL, other collation).
    changed = _changed(
        table.key, column, type=op.type, nullable=op.nullable, collation=op.collation, masked=None
    )
    return model.replace(_with_column(table, column, changed))


def _properties(column: Column) -> dict[str, bool]:
    return {
        "ROWGUIDCOL": column.rowguidcol,
        "SPARSE": column.sparse,
        "NOT FOR REPLICATION": column.identity is not None and column.identity.not_for_replication,
    }


def _alter_column_property(model: Model, op: AlterColumnProperty) -> Model:
    table = _table(model, op.schema, op.table)
    column = _column(table, op.column)
    shown, verb = names.quote(column.name), "ADD" if op.add else "DROP"
    if column.computed is not None:
        _blocked(table.key, f"column {shown} is computed: it has no {op.property}")
    _need_no_period_column(table, column, f"ALTER COLUMN {verb} {op.property}")
    if op.property == "NOT FOR REPLICATION" and column.identity is None:
        _blocked(table.key, f"column {shown} has no IDENTITY: NOT FOR REPLICATION is a property of it")
    if _properties(column)[op.property] == op.add:
        state = "already" if op.add else "not"
        _blocked(table.key, f"column {shown} is {state} {op.property}")
    if op.property == "SPARSE":
        users = column_dependants(model, op.schema, op.table, op.column)
        if users:
            _blocked(table.key, f"ALTER COLUMN {shown} {verb} {op.property} is blocked by {_some(users)}")
    if op.property == "ROWGUIDCOL":
        changed = _changed(table.key, column, rowguidcol=op.add)
    elif op.property == "SPARSE":
        changed = _changed(table.key, column, sparse=op.add)
    else:
        assert column.identity is not None
        changed = _changed(table.key, column, identity=replace(column.identity, not_for_replication=op.add))
    return model.replace(_with_column(table, column, changed))


def _rebuild_table(model: Model, op: RebuildTable) -> Model:
    table = _table(model, op.schema, op.table)
    item = table.clustered_item()
    if item is None:
        return model.replace(replace(table, compression=None if op.compression == "NONE" else op.compression))
    shown = names.quote(item.name or "")
    if _is_columnstore(item):
        _blocked(
            table.key,
            f"the table has the clustered columnstore index {shown}: its DATA_COMPRESSION is COLUMNSTORE "
            "or COLUMNSTORE_ARCHIVE and is stated on the index",
        )
    # the engine rebuilds the clustered index with this compression; the option is then stated
    options = tuple(sorted({**dict(item.options), "DATA_COMPRESSION": op.compression}.items()))
    rebuilt = replace(item, options=options)
    if isinstance(rebuilt, Index):
        return model.replace(
            replace(table, indexes=tuple(rebuilt if i is item else i for i in table.indexes))
        )
    constraints = tuple(rebuilt if c is item else c for c in table.constraints)
    return model.replace(replace(table, constraints=constraints))


def _mask_column(model: Model, op: MaskColumn | UnmaskColumn) -> Model:
    table = _table(model, op.schema, op.table)
    column = _column(table, op.column)
    shown = names.quote(column.name)
    what = "ADD MASKED" if isinstance(op, MaskColumn) else "DROP MASKED"
    if column.computed is not None:
        _blocked(table.key, f"column {shown} is computed: ALTER COLUMN {what} cannot change it")
    _need_no_period_column(table, column, f"ALTER COLUMN {what}")
    if isinstance(op, UnmaskColumn):
        if column.masked is None:
            _blocked(table.key, f"column {shown} does not have a masking function: DROP MASKED cannot run")
        return model.replace(_with_column(table, column, replace(column, masked=None)))
    return model.replace(_with_column(table, column, replace(column, masked=op.function)))


def _drop_column(model: Model, op: DropColumn) -> Model:
    table = _table(model, op.schema, op.table)
    column = _column(table, op.column)
    _need_no_period_column(table, column, "DROP COLUMN")
    users = column_dependants(model, op.schema, op.table, op.column)
    if column.default is not None:
        users.append(_label(column.default))
    if users:
        _blocked(table.key, f"DROP COLUMN {names.quote(column.name)} is blocked by {_some(users)}")
    if len(table.columns) == 1:
        _blocked(table.key, f"column {names.quote(column.name)} is the only column of the table")
    return model.replace(replace(table, columns=tuple(c for c in table.columns if c is not column)))


def _create_table(model: Model, table: Table) -> Model:
    _need_schema(model, table.key, table.schema)
    _need_free(model, table.key, table.schema, table.name)
    for name in _constraint_names(table):
        if _same(name, table.name):
            _blocked(table.key, f"the name {names.quote(name)} is the name of the table and of a constraint")
        _need_free(model, table.key, table.schema, name)
    _need_parts(model, table)
    if isinstance(table, UnversionedTable):
        _blocked(table.key, "CREATE TABLE with period columns needs SYSTEM_VERSIONING = ON")
    if table.temporal is not None:
        _need_history(model, table, table.temporal)
    for key in _foreign_keys(table):
        problem = _foreign_key_problem(model, table, key)
        if problem is not None:
            _blocked(table.key, f"{_label(key)}: {problem}")
    return model.add(table)


def _alter_sequence(model: Model, op: AlterSequence) -> Model:
    key = names.object_key("SEQUENCE", op.schema, op.name)
    sequence = model.get(key)
    if not isinstance(sequence, Sequence):
        _blocked(key, "the sequence does not exist")
    if op.cached is None and op.cache_size is not None:
        _blocked(key, "a cache size goes with CACHE")

    def new[T](value: T | None, old: T) -> T:
        return old if value is None else value

    return model.replace(
        replace(
            sequence,
            start=new(op.restart_with, sequence.start),  # RESTART WITH n sets the start value
            increment=new(op.increment, sequence.increment),
            minvalue=new(op.minvalue, sequence.minvalue),
            maxvalue=new(op.maxvalue, sequence.maxvalue),
            cycle=new(op.cycle, sequence.cycle),
            cached=new(op.cached, sequence.cached),
            cache_size=sequence.cache_size if op.cached is None else op.cache_size,
        )
    )


# ------------------------------------------------------------------ sp_rename
def _map_tables(model: Model, change: Callable[[Table], Table]) -> Model:
    return Model(change(obj) if isinstance(obj, Table) else obj for obj in model.values())


def _need_other_name(key: str, current: str | None, new: str) -> None:
    """sp_rename to the name that the object has, letter for letter: the engine answers that the
    name is in use (error 15335). The same name in other letters is a rename."""
    if current == new:
        _blocked(key, f"sp_rename to {names.quote(new)}: the new name is the old name")


def _rename_table(model: Model, schema: str, old: str, new: str) -> Model:
    table = _table(model, schema, old)
    _need_other_name(table.key, table.name, new)
    if not _same(old, new):
        _need_free(model, table.key, schema, new)

    def change(t: Table) -> Table:
        if t is not table and not any(_references(c, schema, old) for c in _foreign_keys(t)):
            return t
        # a foreign key follows the table that it references
        constraints = tuple(
            replace(c, ref_table=new) if isinstance(c, ForeignKey) and _references(c, schema, old) else c
            for c in t.constraints
        )
        return replace(t, name=new if t is table else t.name, constraints=constraints)

    return _map_tables(model, change)


def _rename_column(model: Model, schema: str, table_name: str, old: str, new: str) -> Model:
    table = _table(model, schema, table_name)
    column = _column(table, old)
    _need_no_period_column(table, column, "sp_rename of column")
    _need_other_name(table.key, column.name, new)
    if not _same(old, new) and table.column(new) is not None:
        _blocked(table.key, f"the table has a column {names.quote(new)} already")
    users = _expression_users(table, old)
    if users:
        message = f"sp_rename of column {names.quote(column.name)} is blocked by {_some(users)}"
        _blocked(table.key, f"{message}: drop it, rename the column, then add it again")

    def renamed(found: Iterable[str]) -> tuple[str, ...]:
        return tuple(new if _same(name, old) else name for name in found)

    def keys(found: Iterable[KeyColumn]) -> tuple[KeyColumn, ...]:
        return tuple(replace(k, name=new) if _same(k.name, old) else k for k in found)

    def constraint(c: Constraint, own: bool) -> Constraint:
        if isinstance(c, PrimaryKey | Unique):
            return replace(c, columns=keys(c.columns)) if own else c
        if isinstance(c, ForeignKey):
            columns = renamed(c.columns) if own else c.columns
            referenced = renamed(c.ref_columns) if _references(c, schema, table_name) else c.ref_columns
            return replace(c, columns=columns, ref_columns=referenced)
        return c

    def change(t: Table) -> Table:
        own = t is table
        if not own and not any(_references(c, schema, table_name) for c in _foreign_keys(t)):
            return t
        constraints = tuple(constraint(c, own) for c in t.constraints)
        if not own:
            return replace(t, constraints=constraints)
        return replace(
            t,
            columns=tuple(replace(c, name=new) if c is column else c for c in t.columns),
            constraints=constraints,
            indexes=tuple(
                replace(i, columns=keys(i.columns), included=renamed(i.included)) for i in t.indexes
            ),
        )

    return _map_tables(model, change)


def _rename_index(model: Model, schema: str, table_name: str, old: str, new: str) -> Model:
    """An index, or the index of a PRIMARY KEY or UNIQUE constraint: the constraint has the same name."""
    table = _table(model, schema, table_name)
    index, constraint = table.index(old), table.constraint(old)
    if index is None and not isinstance(constraint, PrimaryKey | Unique):
        _blocked(table.key, f"the table has no index {names.quote(old)}")
    holder = index if index is not None else constraint
    _need_other_name(table.key, holder.name if holder is not None else None, new)
    if not _same(old, new):
        if _has_index_name(table, new):
            _blocked(table.key, f"the table has an index or key named {names.quote(new)} already")
        if index is None:
            _need_free(model, table.key, schema, new)
    if index is not None:
        indexes = tuple(replace(i, name=new) if i is index else i for i in table.indexes)
        return model.replace(replace(table, indexes=indexes))
    constraints = tuple(replace(c, name=new) if c is constraint else c for c in table.constraints)
    return model.replace(replace(table, constraints=constraints))


def _rename_constraint(model: Model, schema: str, old: str, new: str, what: str) -> Model:
    owners = [
        t
        for t in _tables(model)
        if _same(t.schema, schema) and any(_same(n, old) for n in _constraint_names(t))
    ]
    if len(owners) != 1:
        where = names.object_key("SCHEMA", None, schema)
        _blocked(
            where, f"the schema has {'no' if not owners else 'more than one'} {what} named {names.quote(old)}"
        )
    table = owners[0]
    _need_other_name(table.key, next(n for n in _constraint_names(table) if _same(n, old)), new)
    if not _same(old, new):
        _need_free(model, table.key, schema, new)
        if isinstance(table.constraint(old), PrimaryKey | Unique) and table.index(new) is not None:
            _blocked(table.key, f"the table has an index named {names.quote(new)} already")
    columns = tuple(
        replace(c, default=replace(c.default, name=new))
        if c.default is not None and c.default.name is not None and _same(c.default.name, old)
        else c
        for c in table.columns
    )
    constraints = tuple(
        replace(c, name=new) if c.name is not None and _same(c.name, old) else c for c in table.constraints
    )
    return model.replace(replace(table, columns=columns, constraints=constraints))


def _rename(model: Model, op: Rename) -> Model:
    schema, new = op.old[0], op.new_name
    if not new:
        _blocked(names.object_key("SCHEMA", None, schema), "the new name of a rename is empty")
    if op.kind == "column":
        return _rename_column(model, schema, op.old[1], op.old[2], new)
    if op.kind == "index":
        return _rename_index(model, schema, op.old[1], op.old[2], new)
    # N'OBJECT' does not say what the name is: a table when the model has one of that name
    is_table = isinstance(model.get(names.object_key("TABLE", schema, op.old[1])), Table)
    if op.kind == "table" or (op.kind == "object" and is_table):
        return _rename_table(model, schema, op.old[1], new)
    what = "constraint" if op.kind == "constraint" else "table or constraint"
    return _rename_constraint(model, schema, op.old[1], new, what)


# ------------------------------------------------------------------ public
def apply(model: Model, op: Operation) -> Model:
    """The model after one operation. ReplayError (op_index 0) when a precondition fails.

    The model given is not changed. Execution options and WITH CHECK / NOCHECK / VALUES change
    nothing in the model. ALTER SEQUENCE RESTART WITH n sets the start value; RESTART alone
    changes nothing. SET (SYSTEM_VERSIONING = OFF) gives a model.UnversionedTable, which is equal
    to no Table: a history that ends there matches no declared model.
    """
    match op:
        case CreateSchema():
            if op.schema.key in model:
                _blocked(op.schema.key, "the schema exists already")
            if fold(op.schema.name) in ENGINE_SCHEMAS:
                _blocked(
                    op.schema.key,
                    "the schema exists in every database: CREATE SCHEMA fails, and it has no schema file",
                )
            if fold(op.schema.name) == TOOL_SCHEMA:
                _blocked(op.schema.key, "the schema of the tool: baseline creates it in every database")
            return model.add(op.schema)
        case DropSchema():
            key = names.object_key("SCHEMA", None, op.name)
            if key not in model:
                _blocked(key, "the schema does not exist")
            inside = [o.key for o in model.values() if not isinstance(o, Schema) and _same(o.schema, op.name)]
            inside += [
                f"the history table {names.qualified(t.temporal.history_schema, t.temporal.history_table)} "
                f"of {t.key}"
                for t in _tables(model)
                if t.temporal is not None and _same(t.temporal.history_schema, op.name)
            ]
            if inside:
                _blocked(key, f"the schema is not empty: it holds {_some(inside)}")
            return model.remove(key)
        case CreateType():
            key = op.type.key
            _need_schema(model, key, op.type.schema)
            if key in model:
                _blocked(key, "the type exists already")
            if isinstance(op.type, TableType):
                _need_parts(model, op.type)
            return model.add(op.type)
        case DropType():
            key = names.object_key("TYPE", op.schema, op.name)
            if key not in model:
                _blocked(key, "the type does not exist")
            users = _type_users(model, op.schema, op.name)
            if users:
                _blocked(key, f"the type is used by {_some(users)}")
            return model.remove(key)
        case CreateSequence():
            sequence = op.sequence
            _need_schema(model, sequence.key, sequence.schema)
            _need_free(model, sequence.key, sequence.schema, sequence.name)
            _need_type(model, sequence.key, sequence.type, "the sequence")
            return model.add(sequence)
        case AlterSequence():
            return _alter_sequence(model, op)
        case DropSequence():
            key = names.object_key("SEQUENCE", op.schema, op.name)
            if key not in model:
                _blocked(key, "the sequence does not exist")
            users = _sequence_users(model, op.schema, op.name)
            if users:
                _blocked(key, f"the sequence is used by {_some(users)}")
            return model.remove(key)
        case CreateSynonym():
            synonym = op.synonym
            _need_schema(model, synonym.key, synonym.schema)
            _need_free(model, synonym.key, synonym.schema, synonym.name)
            return model.add(synonym)
        case DropSynonym():
            key = names.object_key("SYNONYM", op.schema, op.name)
            if key not in model:
                _blocked(key, "the synonym does not exist")
            return model.remove(key)
        case CreateTable():
            return _create_table(model, op.table)
        case DropTable():
            table = _table(model, op.schema, op.name)
            if table.temporal is not None:
                message = (
                    "the table is system-versioned: ALTER TABLE ... SET (SYSTEM_VERSIONING = OFF) comes "
                    "before DROP TABLE (engine error 13552)"
                )
                _blocked(table.key, message)
            users = [
                f"{_label(key)} of {names.qualified(other.schema, other.name)}"
                for other in _tables(model)
                for key in _foreign_keys(other)
                if other is not table and _references(key, op.schema, op.name)
            ]
            if users:
                _blocked(table.key, f"the table is referenced by {_some(users)}: drop the foreign key first")
            return model.remove(table.key)
        case AddColumn():
            table, column = _table(model, op.schema, op.table), op.column
            if table.column(column.name) is not None:
                _blocked(table.key, f"the table has a column {names.quote(column.name)} already")
            if column.generated is not None:
                shown = names.quote(column.name)
                _blocked(
                    table.key, f"column {shown} is GENERATED ALWAYS: a period column is not added by ADD"
                )
            if table.temporal is not None and (column.computed is not None or column.identity is not None):
                what = "is computed" if column.computed is not None else "has IDENTITY"
                _blocked(
                    table.key,
                    f"column {names.quote(column.name)} {what}: the engine does not add it while "
                    "SYSTEM_VERSIONING is ON; SET (SYSTEM_VERSIONING = OFF) comes before this statement",
                )
            _need_type(model, table.key, column.type, f"column {names.quote(column.name)}")
            if column.default is not None:
                if column.default.name is None:
                    _blocked(table.key, f"the DEFAULT of column {names.quote(column.name)} needs a name")
                _need_free(model, table.key, op.schema, column.default.name)
            return model.replace(_changed(table.key, table, columns=(*table.columns, column)))
        case AlterColumn():
            return _alter_column(model, op)
        case AlterColumnProperty():
            return _alter_column_property(model, op)
        case RebuildTable():
            return _rebuild_table(model, op)
        case DropColumn():
            return _drop_column(model, op)
        case AddConstraint():
            return _add_constraint(model, op)
        case DropConstraint():
            return _drop_constraint(model, op)
        case CreateIndex():
            table, index = _table(model, op.schema, op.table), op.index
            if _has_index_name(table, index.name):
                _blocked(table.key, f"the table has an index or key named {names.quote(index.name)} already")
            columns = (*(k.name for k in index.columns), *index.included)
            _need_columns(table, columns, f"index {names.quote(index.name)}")
            _need_clustering(table, index, f"index {names.quote(index.name)}")
            compression = None if index.clustered else table.compression
            return model.replace(
                _changed(table.key, table, indexes=(*table.indexes, index), compression=compression)
            )
        case DropIndex():
            table = _table(model, op.schema, op.table)
            index = table.index(op.name)
            if index is None:
                key_of = table.constraint(op.name)
                hint = (
                    f": it is {_label(key_of)}, use DROP CONSTRAINT"
                    if isinstance(key_of, PrimaryKey | Unique)
                    else ""
                )
                _blocked(table.key, f"the table has no index {names.quote(op.name)}{hint}")
            compression = _heap_compression(index) if index.clustered else table.compression
            rest = replace(
                table, indexes=tuple(i for i in table.indexes if i is not index), compression=compression
            )
            if index.unique and index.filter is None:
                _need_no_key_user(model, table, index.columns, f"index {names.quote(index.name)}")
            return model.replace(rest)
        case Rename():
            return _rename(model, op)
        case SetSystemVersioning():
            return _set_system_versioning(model, op)
        case MaskColumn() | UnmaskColumn():
            return _mask_column(model, op)
        case _:
            assert_never(op)


def replay(model: Model, ops: Iterable[Operation]) -> Model:
    """The model after every operation, in order. A ReplayError carries the index of the operation."""
    for index, op in enumerate(ops):
        try:
            model = apply(model, op)
        except ReplayError as e:
            raise ReplayError(index, e.object_key, e.message) from None
    return model
