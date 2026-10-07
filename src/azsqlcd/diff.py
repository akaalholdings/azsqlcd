"""Model x model -> the ordered operations of a migration, and what the generator refuses.

diff() is the core of `azsqlcd gen` (design (b), generation step 2 and "Generator refuses"). It is
not trusted: replay.py proves every migration, generated or hand-written, against the head model.
So the rule set here is narrow. A change that has no safe statement in the fixed order is refused
with a code and a hand-write hint; the author then writes the SQL and the replay proves it.

Rules:
  * Nothing is inferred. An object that has another name at the head revision is a drop and a
    create, unless a RenameSpec says that it is a rename.
  * The statements come in the fixed order of _STEPS. Inside a step the order is the key order
    of the model, then the name.
  * A constraint or an index that differs is dropped and created again. A foreign key that does
    not change is dropped and added again when the migration drops a key on exactly the columns
    that it references, also when another key on those columns stays: the engine binds a foreign
    key to one index and refuses to drop that one.
  * A new table is created without its foreign keys and indexes; they follow as statements of
    their own, so a cycle of tables needs no special case.
  * A system-versioned temporal table is created by one CREATE TABLE and dropped by SET
    (SYSTEM_VERSIONING = OFF), then DROP TABLE; the history table stays, as an object that the
    tool does not manage. Columns, constraints and indexes change as on any table (the engine
    applies a column change to the history table). A change of the versioning itself, of the
    period or of a period column is the refusal TEMPORAL_CHANGE, and so is a new computed or
    IDENTITY column (the engine does not add one while versioning is on). The hint of a
    TEMPORAL_CHANGE is the hint of its reason (TEMPORAL_HINTS): a route to write by hand that
    the proof accepts, or "not supported in this version".
  * Dynamic data masking is a property of a column: a mask that comes or changes is ALTER COLUMN
    ... ADD MASKED, a mask that goes is ALTER COLUMN ... DROP MASKED (allow UNMASK). Both are in
    the step of ALTER COLUMN. The engine removes the mask with every ALTER COLUMN that states a
    data type, so for one column the order is DROP MASKED, ALTER COLUMN, ADD MASKED: the mask
    is written again after a type or nullability change.
  * The generator refuses what the replay would block. The dependants of an ALTER COLUMN are
    found with replay.column_dependants on the model as it is when ALTER COLUMN runs; a longer
    varchar, nvarchar or varbinary passes under UNIQUE, CHECK and an index (replay.only_widens). Before
    diff() returns, it replays its operations on the base model: a blocked statement is the
    refusal ORDER_BLOCKED, so diff() returns only operations that give the head model.

Also here: classify() (the destructive and locking codes of design (h) for one operation) and
touched_objects() (the object keys that operations change).

Public API: RenameSpec, RenameSpecKind, parse_rename_spec, diff, GenRefusal, GenRefused, REFUSALS,
TEMPORAL_HINTS, classify, touched_objects.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from collections.abc import Sequence as Seq
from dataclasses import dataclass, replace
from typing import Literal, assert_never, cast

from azsqlcd import names
from azsqlcd.lex import LexError, significant, tokenize
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
    ForeignKey,
    Index,
    MaskColumn,
    Model,
    ModelObject,
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
    UnmaskColumn,
    UnversionedTable,
    fold,
)
from azsqlcd.replay import ReplayError, apply, column_dependants, only_widens, replay

# Design (b), generation step 2: the fixed order. The design does not place ALTER SEQUENCE, DROP
# SYNONYM and DROP SCHEMA; a schema goes last because every object in it must go first. A table
# type can use an alias type, so it is created after it and dropped before it.
_STEPS = (
    "rename",
    "drop foreign key",
    "drop check or default",
    "drop index",
    "drop key",
    "drop column",
    "drop table",
    "create schema",
    "create alias type",
    "create table type",
    "create sequence",
    "alter sequence",
    "create table",
    "add column",
    "drop column property",
    "alter column",
    "add column property",
    "rebuild table",
    "add key",
    "create index",
    "add check or default",
    "add foreign key",
    "drop synonym",
    "create synonym",
    "drop sequence",
    "drop table type",
    "drop alias type",
    "drop schema",
)

# Refusal code -> the hand-write hint. The closed list of what diff() refuses.
REFUSALS: Mapping[str, str] = {
    "ORD001": (
        "Keep the columns of the base revision in their order and put each new column last. The engine "
        "cannot move a column: a table rebuild is a hand-written migration."
    ),
    "IDENTITY_CHANGE": (
        "Hand-write the migration: the engine cannot add IDENTITY to a column, remove it or change it. "
        "Make a new column or a new table, copy the rows in a data batch, drop the old one, rename."
    ),
    "COMPUTED_CHANGE": (
        "Hand-write the migration: drop what uses the column, DROP COLUMN, ADD the column with the new "
        "definition, add again what used it."
    ),
    "COLLATION_CHANGE": (
        "Hand-write the migration: drop what uses the column, ALTER COLUMN ... COLLATE ..., add again "
        "what used it."
    ),
    "ALTER_COLUMN_DEPENDANTS": (
        "Hand-write the migration: drop the objects that the message lists, ALTER COLUMN, add them again."
    ),
    "USER_TYPE_CHANGE": (
        "Hand-write the migration: a type cannot be altered. Use a new type name, or remove every use, "
        "DROP TYPE, CREATE TYPE."
    ),
    "SEQUENCE_CHANGE": (
        "Hand-write the migration. START WITH: ALTER SEQUENCE ... RESTART WITH n, which also sets the next "
        "value of the sequence. Data type: DROP SEQUENCE, then CREATE SEQUENCE."
    ),
    "SCHEMA_OWNER_CHANGE": (
        "No migration statement changes the owner of a schema. Keep the owner of the base revision in the "
        "file; a database administrator changes the owner."
    ),
    "ORDER_BLOCKED": (
        "The statements in the fixed order do not apply to the base revision; the message names the one "
        "that is blocked. Hand-write the migration in an order that works. When the message shows that the "
        "object files do not fit together, correct the files."
    ),
    # The general text. A refusal carries the hint of its reason: one of TEMPORAL_HINTS.
    "TEMPORAL_CHANGE": (
        "The generator does not switch versioning on or off for a table that stays, and it does not change "
        "the period columns, the PRIMARY KEY, the history table or the retention of a system-versioned "
        "table. The hint of each refusal says what a migration written by hand can do and what this "
        "version cannot do at all (docs/runbook.md, Temporal tables)."
    ),
    "COMPRESSION_INHERITED": (
        "State DATA_COMPRESSION on the clustered key or index in the table file: NONE to remove the "
        "compression, ROW or PAGE to keep one. The engine gives a clustered index that states none the "
        "compression of the heap that it is built on."
    ),
    "RENAME_INVALID": (
        "Write KIND:[schema].[table].[old]=[new] with the name at the base revision and the name at the "
        "head revision; a table and a constraint have no [table] part. Give the rename of a table before "
        "the renames inside it. A column that a CHECK, computed column or index filter names: hand-write "
        "drop, sp_rename, add."
    ),
}

_OFF = "an allow TEMPORAL_OFF line with ALTER TABLE ... SET (SYSTEM_VERSIONING = OFF)"
_ON = "ALTER TABLE ... SET (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [schema].[table]))"
# Reason of a TEMPORAL_CHANGE refusal -> its hint. A hint says "Hand-write" only for a route that
# parse and replay accept (tests/unit/test_diff.py proves each one); every other change has no
# statement that this version reads.
TEMPORAL_HINTS: Mapping[str, str] = {
    "not supported": (
        "This change is not supported in this version; the table must be changed outside the tool and "
        "then exported again. No statement that the tool reads adds or drops PERIOD FOR SYSTEM_TIME, makes "
        "a table that stays system-versioned, ends the versioning of a table that stays, or changes or "
        "renames a period column."
    ),
    "off and on": (
        f"Hand-write the migration, verify proves it: {_OFF}, then the change, then {_ON}. A PRIMARY KEY: "
        "DROP CONSTRAINT and ADD CONSTRAINT between the two. A retention: write HISTORY_RETENTION_PERIOD "
        "of the table file in the ON statement. Rows that change between OFF and ON get no history."
    ),
    "history table": (
        f"Hand-write the migration, verify proves it: {_OFF}, then {_ON} with the new history table. The "
        "old history table stays in the database as a plain table that the tool does not manage, with its "
        "rows: the engine does not move them."
    ),
    "add column": (
        f"Hand-write the migration, verify proves it: {_OFF}, then ALTER TABLE ... ADD of the column, then "
        "a raw batch that adds a plain column of the same name, data type and nullability to the history "
        f"table (the history table must be listed in [unmanaged] objects), then {_ON}."
    ),
    "schema": (
        "DROP TABLE leaves the history table, and the engine refuses DROP SCHEMA while it is there. "
        f"Hand-write the migration: {_OFF}, DROP TABLE, a raw batch of its own that drops the history "
        "table (it must be listed in [unmanaged] objects), then DROP SCHEMA."
    ),
}

RenameSpecKind = Literal["table", "column", "index", "constraint"]
_SPEC_PARTS: Mapping[str, int] = {"table": 2, "constraint": 2, "column": 3, "index": 3}
_IDENT = ("word", "bident", "qident")


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class RenameSpec:
    """One --rename value: an object under its name at the base revision, and its new name.

    old is (schema, table) for a table, (schema, constraint) for a constraint and
    (schema, table, name) for a column or an index.
    """

    kind: RenameSpecKind
    old: tuple[str, ...]
    new_name: str

    def __post_init__(self) -> None:
        if _SPEC_PARTS.get(self.kind) != len(self.old) or not self.new_name or not all(self.old):
            raise ValueError(f"not a rename of kind {self.kind!r}: {self.old!r} to {self.new_name!r}")


@dataclass(frozen=True)
class GenRefusal:
    """One change that the generator does not write."""

    code: str  # a key of REFUSALS
    object_key: str
    message: str  # what changed; names only, never expression text
    hint: str  # what to write by hand


class GenRefused(Exception):
    """diff() found changes that it does not write. refusals holds every one of them."""

    def __init__(self, refusals: Iterable[GenRefusal]) -> None:
        self.refusals = list(refusals)
        super().__init__("; ".join(f"{r.code} {r.object_key}: {r.message}" for r in self.refusals))


def parse_rename_spec(text: str) -> RenameSpec:
    """Read the command-line form KIND:[schema].[table].[old]=[new].

    Kinds: table, column, index, constraint. A table and a constraint have no [table] part:
    table:[sales].[Ordr]=[Order]. A name is in brackets, in double quotes or bare. ValueError
    for any other text.
    """
    kind, colon, rest = text.partition(":")
    kind = kind.strip().lower()
    parts = _SPEC_PARTS.get(kind)
    try:
        toks = significant(tokenize(rest))
    except LexError:
        toks = []
    shape = (["name", "."] * (parts or 0))[:-1] + ["=", "name"]
    fits = len(toks) == len(shape) and all(
        (tok.kind in _IDENT and tok.value != "")
        if want == "name"
        else (tok.kind == "op" and tok.text == want)
        for tok, want in zip(toks, shape, strict=True)
    )
    if not colon or parts is None or not fits:
        raise ValueError(
            f"not a rename: {text!r}. Write KIND:[schema].[table].[old]=[new] with the kind table, column, "
            "index or constraint; a table and a constraint have no [table] part"
        )
    values = [tok.value for tok in toks if tok.kind in _IDENT]
    return RenameSpec(cast(RenameSpecKind, kind), tuple(values[:-1]), values[-1])


class _Out:
    """What diff() collects: the operations of each step, and the refusals."""

    def __init__(self) -> None:
        self.steps: dict[str, list[Operation]] = {step: [] for step in _STEPS}
        self.refusals: list[GenRefusal] = []

    def refuse(self, code: str, key: str, message: str, temporal: str | None = None) -> None:
        """temporal: the key of TEMPORAL_HINTS, for a TEMPORAL_CHANGE; else the hint of the code."""
        hint = REFUSALS[code] if temporal is None else TEMPORAL_HINTS[temporal]
        self.refusals.append(GenRefusal(code, key, message, hint))


@dataclass
class _TableChange:
    """A table of both revisions and what the migration does with its constraints and indexes."""

    base: Table  # with the renames applied
    head: Table
    dropped: list[Constraint]
    added: list[Constraint]
    kept: list[Constraint]
    dropped_indexes: list[Index]
    added_indexes: list[Index]
    kept_indexes: list[Index]

    def middle(self) -> Table:
        """The table when ALTER COLUMN runs: every drop is done, no constraint or index is added yet."""
        constraints, indexes = tuple(self.kept), tuple(self.kept_indexes)
        head = self.head
        if head.temporal is not None and not any(isinstance(c, PrimaryKey) for c in constraints):
            # The model holds no temporal table without its key. diff() refuses this change
            # (TEMPORAL_CHANGE); until then the table stands here with its columns and no versioning.
            return UnversionedTable(head.schema, head.name, head.columns, constraints, indexes)
        return replace(head, constraints=constraints, indexes=indexes)

    def drops_key(self, columns: Iterable[str]) -> bool:
        """True when the migration drops a PRIMARY KEY, a UNIQUE constraint or a unique index
        without a filter on exactly these columns: a foreign key on them can be bound to it."""
        wanted = frozenset(fold(name) for name in columns)
        keys = [c.columns for c in self.dropped if isinstance(c, PrimaryKey | Unique)]
        keys += [i.columns for i in self.dropped_indexes if i.unique and i.filter is None]
        return any(wanted == frozenset(fold(k.name) for k in key) for key in keys)


# ------------------------------------------------------------------ helpers
def _table_key(schema: str, name: str) -> str:
    return names.object_key("TABLE", schema, name)


def _by_name[T: PrimaryKey | Unique | ForeignKey | Check | Index | DefaultConstraint](
    items: Iterable[T],
) -> list[T]:
    return sorted(items, key=lambda item: fold(item.name or ""))


def _clustered_first[T: PrimaryKey | Unique | Index](items: Iterable[T]) -> list[T]:
    # the engine builds every other index of a table on the clustered one
    return sorted(items, key=lambda item: (not item.clustered, fold(item.name or "")))


def _bare(table: Table) -> Table:
    """A new table as CREATE TABLE writes it: no foreign key, no index."""
    kept = tuple(c for c in table.constraints if not isinstance(c, ForeignKey))
    return replace(table, constraints=kept, indexes=())


def _foreign_keys(found: Iterable[Constraint]) -> list[ForeignKey]:
    return _by_name(c for c in found if isinstance(c, ForeignKey))


def _has_constraint(table: Table, name: str) -> bool:
    defaults = (c.default.name for c in table.columns if c.default is not None)
    return table.constraint(name) is not None or any(fold(d or "") == fold(name) for d in defaults)


# ------------------------------------------------------------------ renames
def _missing_at_head(head: Model, spec: RenameSpec) -> str | None:
    """Why the new name is not what the head revision has; None when it is there."""
    schema, new = spec.old[0], spec.new_name
    if spec.kind == "constraint":
        tables = (t for t in head.values() if isinstance(t, Table) and fold(t.schema) == fold(schema))
        if any(_has_constraint(t, new) for t in tables):
            return None
        return f"the head revision has no constraint {names.quote(new)} in schema {names.quote(schema)}"
    name = new if spec.kind == "table" else spec.old[1]
    table = head.get(_table_key(schema, name))
    if not isinstance(table, Table):
        return f"the head revision has no table {names.qualified(schema, name)}"
    if spec.kind == "column" and table.column(new) is None:
        return f"the head revision has no column {names.quote(new)} in {names.qualified(schema, name)}"
    is_key = isinstance(table.constraint(new), PrimaryKey | Unique)
    if spec.kind == "index" and table.index(new) is None and not is_key:
        return f"the head revision has no index {names.quote(new)} on {names.qualified(schema, name)}"
    return None


def _rename_all(base: Model, head: Model, renames: Iterable[RenameSpec], out: _Out) -> Model:
    """The base model with every rename applied. The replay decides whether a rename is possible."""
    for spec in renames:
        op = Rename(spec.kind, spec.old, spec.new_name)
        if spec.kind == "column":
            table = base.get(_table_key(spec.old[0], spec.old[1]))
            column = table.column(spec.old[2]) if isinstance(table, Table) else None
            if isinstance(table, Table) and column is not None and column.generated is not None:
                shown = names.quote(column.name)
                message = f"period column {shown} cannot be renamed"
                out.refuse("TEMPORAL_CHANGE", table.key, message, "not supported")
                continue
        try:
            renamed = apply(base, op)
        except ReplayError as e:
            out.refuse("RENAME_INVALID", e.object_key, e.message)
            continue
        missing = _missing_at_head(head, spec)
        if missing is not None:
            in_schema = names.object_key("SCHEMA", None, spec.old[0])
            key = in_schema if spec.kind == "constraint" else _table_key(spec.old[0], spec.old[1])
            out.refuse("RENAME_INVALID", key, missing)
            continue
        base = renamed
        out.steps["rename"].append(op)
    return base


# ------------------------------------------------------------------ whole objects
def _drop(obj: ModelObject, gone: Collection[str], changes: Mapping[str, _TableChange], out: _Out) -> None:
    match obj:
        case Schema():
            out.steps["drop schema"].append(DropSchema(obj.name))
        case AliasType():
            out.steps["drop alias type"].append(DropType(obj.schema, obj.name))
        case TableType():
            out.steps["drop table type"].append(DropType(obj.schema, obj.name))
        case Sequence():
            out.steps["drop sequence"].append(DropSequence(obj.schema, obj.name))
        case Synonym():
            out.steps["drop synonym"].append(DropSynonym(obj.schema, obj.name))
        case Table():
            # DROP TABLE comes after the drop of keys and of other tables. A foreign key of this
            # table would block the drop of the table or of the key that it references.
            for key in _foreign_keys(obj.constraints):
                target = fold(_table_key(key.ref_schema, key.ref_table))
                changed = changes.get(target)
                key_goes = changed is not None and changed.drops_key(key.ref_columns)
                if (target in gone and target != fold(obj.key)) or key_goes:
                    out.steps["drop foreign key"].append(DropConstraint(obj.schema, obj.name, key.name))
            if obj.temporal is not None:
                # the engine refuses DROP TABLE while versioning is on; the history table stays
                out.steps["drop table"].append(SetSystemVersioning(obj.schema, obj.name, False))
            out.steps["drop table"].append(DropTable(obj.schema, obj.name))
        case _:
            assert_never(obj)


def _create(obj: ModelObject, out: _Out) -> None:
    match obj:
        case Schema():
            out.steps["create schema"].append(CreateSchema(obj))
        case AliasType():
            out.steps["create alias type"].append(CreateType(obj))
        case TableType():
            out.steps["create table type"].append(CreateType(obj))
        case Sequence():
            out.steps["create sequence"].append(CreateSequence(obj))
        case Synonym():
            out.steps["create synonym"].append(CreateSynonym(obj))
        case Table():
            out.steps["create table"].append(CreateTable(_bare(obj)))
            out.steps["create index"] += [
                CreateIndex(obj.schema, obj.name, index) for index in _clustered_first(obj.indexes)
            ]
            out.steps["add foreign key"] += [
                AddConstraint(obj.schema, obj.name, key) for key in _foreign_keys(obj.constraints)
            ]
        case _:
            assert_never(obj)


def _alter_sequence(old: Sequence, new: Sequence, out: _Out) -> None:
    if old.type != new.type or old.start != new.start:
        what = "data type" if old.type != new.type else "START WITH value"
        out.refuse("SEQUENCE_CHANGE", new.key, f"the {what} of the sequence differs")
        return
    cache = (old.cached, old.cache_size) != (new.cached, new.cache_size)
    out.steps["alter sequence"].append(
        AlterSequence(
            new.schema,
            new.name,
            increment=None if old.increment == new.increment else new.increment,
            minvalue=None if old.minvalue == new.minvalue else new.minvalue,
            maxvalue=None if old.maxvalue == new.maxvalue else new.maxvalue,
            cycle=None if old.cycle == new.cycle else new.cycle,
            cached=new.cached if cache else None,
            cache_size=new.cache_size if cache else None,
        )
    )


def _change(old: ModelObject, new: ModelObject, out: _Out) -> None:
    """An object of both revisions that differs and is not a table."""
    match new:
        case Sequence():
            assert isinstance(old, Sequence)  # one key, one kind
            _alter_sequence(old, new, out)
        case Synonym():
            out.steps["drop synonym"].append(DropSynonym(new.schema, new.name))
            out.steps["create synonym"].append(CreateSynonym(new))
        case Schema():
            out.refuse("SCHEMA_OWNER_CHANGE", new.key, "the owner of the schema differs")
        case AliasType() | TableType():
            out.refuse("USER_TYPE_CHANGE", new.key, "the definition of the type differs")
        case Table():
            raise AssertionError("diff() compares tables itself")
        case _:
            assert_never(new)


# ------------------------------------------------------------------ tables
def _table_change(base: Table, head: Table) -> _TableChange:
    old, new = set(base.constraints), set(head.constraints)
    old_indexes, new_indexes = set(base.indexes), set(head.indexes)
    return _TableChange(
        base,
        head,
        dropped=[c for c in base.constraints if c not in new],
        added=[c for c in head.constraints if c not in old],
        kept=[c for c in head.constraints if c in old],
        dropped_indexes=[i for i in base.indexes if i not in new_indexes],
        added_indexes=[i for i in head.indexes if i not in old_indexes],
        kept_indexes=[i for i in head.indexes if i in old_indexes],
    )


def _force_foreign_keys(changes: Mapping[str, _TableChange]) -> None:
    """A foreign key that does not change, on columns whose key the migration drops: drop it and
    add it again. Another key on the same columns does not help: the foreign key can be bound to
    the one that goes."""
    for change in changes.values():
        for key in _foreign_keys(change.kept):
            target = changes.get(fold(_table_key(key.ref_schema, key.ref_table)))
            if target is not None and target.drops_key(key.ref_columns):
                change.kept = [c for c in change.kept if c is not key]
                change.dropped.append(key)
                change.added.append(key)


def _replication_only(old: Column, new: Column) -> bool:
    """Both have IDENTITY with one seed and one increment: at most NOT FOR REPLICATION differs."""
    a, b = old.identity, new.identity
    return a is not None and b is not None and (a.seed, a.increment) == (b.seed, b.increment)


def _column_properties(table: Table, old: Column, new: Column, middle: Model, out: _Out) -> None:
    """ALTER COLUMN ... ADD | DROP of ROWGUIDCOL, SPARSE and NOT FOR REPLICATION.

    The replay blocks ALTER COLUMN of a column that has ROWGUIDCOL or SPARSE. So a column whose
    data type or nullability changes loses the property before ALTER COLUMN and gets it again
    after it.
    """
    if old.computed is not None or new.computed is not None:
        return
    schema, name, column = table.schema, table.name, new.name
    retyped = old.type != new.type or old.nullable != new.nullable
    held = (
        ("ROWGUIDCOL", old.rowguidcol, new.rowguidcol),
        ("SPARSE", old.sparse, new.sparse),
    )
    drops: list[AlterColumnProperty] = []
    adds: list[AlterColumnProperty] = []
    for what, was, now in held:
        drop = was and (not now or retyped)
        if drop:
            drops.append(AlterColumnProperty(schema, name, column, False, what))
        if now and (not was or drop):
            adds.append(AlterColumnProperty(schema, name, column, True, what))
    if old.identity != new.identity and _replication_only(old, new):
        assert new.identity is not None
        add = new.identity.not_for_replication
        (adds if add else drops).append(AlterColumnProperty(schema, name, column, add, "NOT FOR REPLICATION"))
    needs_free = [op for op in (*drops, *adds) if op.property == "SPARSE"]
    if needs_free and not retyped:  # _alter_column has looked at a column that is retyped
        users = column_dependants(middle, schema, name, column)
        if users:
            first = needs_free[0]
            what = f"ALTER COLUMN {names.quote(column)} {'ADD' if first.add else 'DROP'} {first.property}"
            out.refuse("ALTER_COLUMN_DEPENDANTS", table.key, f"{what} is blocked by {', '.join(users)}")
    out.steps["drop column property"] += drops
    out.steps["add column property"] += adds


def _heap_before(change: _TableChange) -> str | None:
    """The DATA_COMPRESSION of the table as a heap, after the drops and before the adds. A heap
    keeps the compression of the clustered key or index that was dropped (replay.apply does the
    same). Only asked when the clustered key or index of the base revision is not kept."""
    was = change.base.clustered_item()
    if was is None:
        return change.base.compression
    stated = dict(was.options).get("DATA_COMPRESSION")
    columnstore = isinstance(was, Index) and was.columnstore
    return stated if stated in TABLE_COMPRESSIONS and not columnstore else None


def _heap_compression(change: _TableChange, out: _Out) -> RebuildTable | None:
    """What the DATA_COMPRESSION of the heap asks for: ALTER TABLE ... REBUILD when the table is a
    heap at the head revision and the heap after the drops has another compression; a refusal when
    a new clustered key or index would inherit a compression that the file does not state."""
    head = change.head
    clustered = head.clustered_item()
    if clustered is None:
        before = _heap_before(change)
        if before == head.compression:
            return None
        return RebuildTable(head.schema, head.name, head.compression or "NONE")
    new = any(clustered is item for item in (*change.added, *change.added_indexes))
    columnstore = isinstance(clustered, Index) and clustered.columnstore
    if new and not columnstore and "DATA_COMPRESSION" not in dict(clustered.options):
        before = _heap_before(change)
        if before is not None:
            out.refuse(
                "COMPRESSION_INHERITED",
                head.key,
                f"the clustered key or index {names.quote(clustered.name or '')} states no DATA_COMPRESSION; "
                f"it is created on a heap with DATA_COMPRESSION = {before}",
            )
    return None


def _alter_column(table: Table, old: Column, new: Column, middle: Model, out: _Out) -> AlterColumn | None:
    """ALTER COLUMN for a column of both revisions, or None: no change of the column, or a refusal."""
    refused = len(out.refusals)
    shown = f"column {names.quote(new.name)}"
    if old.computed is not None or new.computed is not None:
        if old != new:
            out.refuse("COMPUTED_CHANGE", table.key, f"the definition of computed {shown} differs")
        return None
    if old.identity != new.identity and not _replication_only(old, new):
        out.refuse("IDENTITY_CHANGE", table.key, f"the IDENTITY property of {shown} differs")
    if fold(old.collation or "") != fold(new.collation or ""):
        out.refuse("COLLATION_CHANGE", table.key, f"the collation of {shown} differs")
    if old.type == new.type and old.nullable == new.nullable:
        return None
    assert new.type is not None and new.nullable is not None  # Column has checked both
    # a longer varchar, nvarchar or varbinary passes under UNIQUE, CHECK and an index: the engine's rule
    widening = only_widens(old, new.type, new.nullable, new.collation)
    users = column_dependants(middle, table.schema, table.name, new.name, widening=widening)
    if users:
        out.refuse(
            "ALTER_COLUMN_DEPENDANTS", table.key, f"ALTER COLUMN of {shown} is blocked by {', '.join(users)}"
        )
    if len(out.refusals) > refused:
        return None
    return AlterColumn(table.schema, table.name, new.name, new.type, new.nullable, new.collation)


def _temporal_change(base: Table, head: Table) -> tuple[str, str] | None:
    """What differs in the versioning of a table of both revisions, and the key of its hint in
    TEMPORAL_HINTS; None when nothing differs."""
    old, new = base.temporal, head.temporal
    if old is None and new is None:
        return None
    no_route = "not supported"
    if old is None:
        return "the table is system-versioned at the head revision and not at the base revision", no_route
    if new is None:
        return "the table is system-versioned at the base revision and not at the head revision", no_route
    if (fold(old.period_start), fold(old.period_end)) != (fold(new.period_start), fold(new.period_end)):
        return "the period columns of PERIOD FOR SYSTEM_TIME differ", no_route
    for name in (new.period_start, new.period_end):
        before, after = base.column(name), head.column(name)
        assert before is not None and after is not None  # Table has checked both
        if replace(before, default=None) != replace(after, default=None):
            return f"the definition of period column {names.quote(after.name)} differs", no_route
    if (fold(old.history_schema), fold(old.history_table)) != (
        fold(new.history_schema),
        fold(new.history_table),
    ):
        return "the history table differs", "history table"
    if old.retention != new.retention:
        return "the HISTORY_RETENTION_PERIOD differs", "off and on"
    return None


def _alter_table(change: _TableChange, middle: Model, out: _Out) -> None:
    base, head = change.base, change.head
    schema, table = head.schema, head.name
    temporal = _temporal_change(base, head)
    if temporal is not None:
        # what follows would be noise: the period columns come and go with the versioning
        out.refuse("TEMPORAL_CHANGE", head.key, *temporal)
        return
    key = next((c for c in change.dropped if isinstance(c, PrimaryKey)), None)
    if head.temporal is not None and key is not None:
        message = f"PRIMARY KEY {names.quote(key.name or '')} of the system-versioned table differs"
        out.refuse("TEMPORAL_CHANGE", head.key, message, "off and on")
        return
    old_columns = {fold(c.name): c for c in base.columns}
    new_columns = {fold(c.name): c for c in head.columns}
    # a DEFAULT belongs to its column: the same name on another column is another constraint
    old_defaults = {(fold(c.name), c.default) for c in base.columns if c.default is not None}
    new_defaults = {(fold(c.name), c.default) for c in head.columns if c.default is not None}
    dropped_defaults = [d for name, d in old_defaults if (name, d) not in new_defaults]
    added_defaults = [  # the DEFAULT of a new column is in its ADD statement
        AddConstraint(schema, table, c.default, for_column=c.name)
        for c in head.columns
        if c.default is not None
        and fold(c.name) in old_columns
        and (fold(c.name), c.default) not in old_defaults
    ]

    def drop(constraint: Constraint | DefaultConstraint) -> Operation:
        assert constraint.name is not None  # Table has checked it
        return DropConstraint(schema, table, constraint.name)

    steps = out.steps
    steps["drop foreign key"] += [drop(c) for c in _foreign_keys(change.dropped)]
    checks = [c for c in change.dropped if isinstance(c, Check)]
    steps["drop check or default"] += [drop(c) for c in _by_name([*checks, *dropped_defaults])]
    steps["drop index"] += [DropIndex(schema, table, i.name) for i in _by_name(change.dropped_indexes)]
    steps["drop key"] += [drop(c) for c in _by_name(change.dropped) if isinstance(c, PrimaryKey | Unique)]
    steps["drop column"] += [
        DropColumn(schema, table, c.name) for c in base.columns if fold(c.name) not in new_columns
    ]

    added = [c for c in head.columns if fold(c.name) not in old_columns]
    if [n for n in old_columns if n in new_columns] != [n for n in new_columns if n in old_columns]:
        out.refuse("ORD001", head.key, "the columns are in another order than at the base revision")
    last = head.columns[len(head.columns) - len(added) :]
    early = [names.quote(c.name) for c in added if all(c is not other for other in last)]
    if early:
        out.refuse("ORD001", head.key, f"new column {', '.join(early)} is not the last column")
    special = [names.quote(c.name) for c in added if c.computed is not None or c.identity is not None]
    if head.temporal is not None and special:
        message = (
            f"new column {', '.join(special)} is computed or has IDENTITY: the engine does not add it "
            "while SYSTEM_VERSIONING is ON"
        )
        out.refuse("TEMPORAL_CHANGE", head.key, message, "add column")
    steps["add column"] += [AddColumn(schema, table, c) for c in added]
    for name, new in new_columns.items():
        if name in old_columns:
            old = old_columns[name]
            alter = _alter_column(head, old, new, middle, out)
            # The engine removes the mask with every ALTER COLUMN that states a data type. So a mask
            # that goes is dropped before it (DROP MASKED, allow UNMASK: nothing is unmasked without
            # that word), and a mask that stays or comes is written after it.
            if old.masked is not None and new.masked is None:
                steps["alter column"].append(UnmaskColumn(schema, table, new.name))
            if alter is not None:
                steps["alter column"].append(alter)
            if new.masked is not None and (alter is not None or old.masked != new.masked):
                steps["alter column"].append(MaskColumn(schema, table, new.name, new.masked))
            _column_properties(head, old, new, middle, out)
    rebuild = _heap_compression(change, out)
    if rebuild is not None:
        steps["rebuild table"].append(rebuild)

    keys = [c for c in change.added if isinstance(c, PrimaryKey | Unique)]
    steps["add key"] += [AddConstraint(schema, table, c) for c in _clustered_first(keys)]
    steps["create index"] += [CreateIndex(schema, table, i) for i in _clustered_first(change.added_indexes)]
    added_checks = [AddConstraint(schema, table, c) for c in change.added if isinstance(c, Check)]
    steps["add check or default"] += sorted(
        [*added_checks, *added_defaults], key=lambda op: fold(op.constraint.name or "")
    )
    steps["add foreign key"] += [AddConstraint(schema, table, c) for c in _foreign_keys(change.added)]


# ------------------------------------------------------------------ public: diff
def diff(base: Model, head: Model, renames: Seq[RenameSpec] = ()) -> list[Operation]:
    """The operations that change the base model into the head model, in the fixed order.

    replay.replay(base, diff(base, head, renames)) == head: diff() checks this before it
    returns. renames are applied first, in the order given; without one, an object with a new
    name is dropped and created. Raises GenRefused with every change that the generator does
    not write; then no operation is returned at all. A rename that is refused stops the
    comparison: what follows would be noise.
    """
    out = _Out()
    original = base
    base = _rename_all(base, head, renames, out)
    if out.refusals:
        raise GenRefused(out.refusals)
    gone = {fold(key) for key in base if key not in head}
    changes = {
        fold(old.key): _table_change(old, new)
        for old in base.values()
        if isinstance(old, Table) and isinstance(new := head.get(old.key), Table)
    }
    _force_foreign_keys(changes)
    new_tables = (_bare(t) for t in head.values() if isinstance(t, Table) and t.key not in base)
    middle = Model([*(change.middle() for change in changes.values()), *new_tables])
    for key in sorted({fold(key) for key in base} | {fold(key) for key in head}):
        old, new = base.get(key), head.get(key)
        if key in changes:
            _alter_table(changes[key], middle, out)
        elif old is None:
            assert new is not None
            _create(new, out)
        elif new is None:
            _drop(old, gone, changes, out)
            history = old.temporal if isinstance(old, Table) else None
            if history is not None and fold(names.object_key("SCHEMA", None, history.history_schema)) in gone:
                # DROP TABLE leaves the history table, and the engine refuses DROP SCHEMA while it is there
                left = names.qualified(history.history_schema, history.history_table)
                message = f"the history table {left} stays in a schema that the head revision does not have"
                out.refuse("TEMPORAL_CHANGE", old.key, message, "schema")
        elif old != new:
            _change(old, new, out)
    if out.refusals:
        raise GenRefused(out.refusals)
    ops = [op for step in _STEPS for op in out.steps[step]]
    # The generator proves its own output, so it never writes a migration that `verify` rejects.
    try:
        result = replay(original, ops)
    except ReplayError as e:
        out.refuse(
            "ORDER_BLOCKED", e.object_key, f"statement {e.op_index + 1} of {len(ops)} cannot run: {e.message}"
        )
        raise GenRefused(out.refusals) from None
    if result != head:
        raise RuntimeError(
            f"diff defect: the operations do not give the head model: {result.diff_paths(head)[:5]}"
        )
    return ops


# ------------------------------------------------------------------ public: classify
def _narrower(old: TypeRef, new: TypeRef) -> bool:
    """True when a value of the old type may not fit the new one. Both have one type name."""

    def size(length: int | Literal["max"] | None) -> float:
        return math.inf if length == "max" else (length or 0)

    old_scale, new_scale = old.scale or 0, new.scale or 0
    if size(new.length) < size(old.length) or new_scale < old_scale:
        return True
    if old.precision is None or new.precision is None:
        return False
    return new.precision - new_scale < old.precision - old_scale  # digits before the decimal point


def _alter_codes(op: AlterColumn, base: Model, existing: bool) -> list[str]:
    table = base.get(_table_key(op.schema, op.table))
    old = table.column(op.column) if isinstance(table, Table) else None
    if old is None or old.type is None:
        # The model does not hold the column as it is now (it was added or renamed earlier in the
        # same change): nothing can be ruled out.
        lossy = retyped = True
        not_null = not op.nullable
    else:
        other_type = (fold(old.type.name), fold(old.type.schema or "")) != (
            fold(op.type.name),
            fold(op.type.schema or ""),
        )
        # another collation can be another code page: characters can be lost
        recollated = fold(old.collation or "") != fold(op.collation or "")
        lossy = other_type or recollated or _narrower(old.type, op.type)
        retyped = recollated or old.type != op.type
        not_null = old.nullable is not False and not op.nullable
    found = (("ALTER_COLUMN_LOSSY", lossy), ("SET_NOT_NULL", not_null), ("LONG_LOCK", retyped and existing))
    return [code for code, is_found in found if is_found]


def classify(op: Operation, base: Model, *, created: Collection[str] = ()) -> list[str]:
    """The allow codes of design (h) that one model operation needs; [] when it needs none.

    base is the model that the operation is applied to: the base revision for the first statement
    of a change. A later statement can work on what an earlier one made (ADD NULL, then ALTER
    COLUMN NOT NULL; a rename, then a statement on the new name). For such a statement give the
    model before it (replay.apply) and, in created, the keys of the tables that the change
    created before it; then every answer is exact. With the base revision alone, an ALTER COLUMN
    of a column that base does not hold gets every code that cannot be ruled out, and a table
    under a name that base does not hold counts as new.

    DROP_TABLE, DROP_COLUMN, DROP_SEQUENCE, DROP_TYPE, DROP_SCHEMA, RENAME: by the statement.
    TEMPORAL_OFF: SET (SYSTEM_VERSIONING = OFF). The engine stops writing history, and the history
    table stays in the database as a table that the tool does not manage (also after DROP TABLE).
    UNMASK: ALTER COLUMN ... DROP MASKED. ADD MASKED needs no code. An ALTER COLUMN with a data
    type removes the mask of the column too and has no code for that: diff() never writes one that
    leaves a column unmasked, and the proof shows a hand-written one (the head file has no mask).
    ALTER COLUMN, against the column as base holds it: ALTER_COLUMN_LOSSY for another type name
    (so also Unicode to non-Unicode), a smaller length, precision or scale, or another collation;
    SET_NOT_NULL for NULL to NOT NULL. LONG_LOCK, only for a table that base holds and created
    does not: CREATE INDEX, ADD PRIMARY KEY or UNIQUE, ADD CHECK or FOREIGN KEY unless WITH
    NOCHECK (the engine checks every row when nothing is written), ALTER COLUMN that changes the
    data type or the collation. The order of the codes is fixed: lossy, not null, lock.
    """

    def existing(schema: str, table: str) -> bool:
        key = _table_key(schema, table)
        return isinstance(base.get(key), Table) and fold(key) not in {fold(k) for k in created}

    match op:
        case DropTable():
            return ["DROP_TABLE"]
        case DropColumn():
            return ["DROP_COLUMN"]
        case DropSequence():
            return ["DROP_SEQUENCE"]
        case DropType():
            return ["DROP_TYPE"]
        case DropSchema():
            return ["DROP_SCHEMA"]
        case Rename():
            return ["RENAME"]
        case CreateIndex():
            return ["LONG_LOCK"] if existing(op.schema, op.table) else []
        case AddConstraint():
            constraint = op.constraint
            checked = isinstance(constraint, ForeignKey | Check) and op.with_check is not False
            reads_rows = isinstance(constraint, PrimaryKey | Unique) or checked
            return ["LONG_LOCK"] if reads_rows and existing(op.schema, op.table) else []
        case AlterColumn():
            return _alter_codes(op, base, existing(op.schema, op.table))
        case AlterColumnProperty():
            # SPARSE changes how every row stores the column; the other properties are metadata
            rewrites = op.property == "SPARSE" and existing(op.schema, op.table)
            return ["LONG_LOCK"] if rewrites else []
        case RebuildTable():
            return ["LONG_LOCK"] if existing(op.schema, op.table) else []
        case (
            CreateSchema()
            | CreateType()
            | CreateSequence()
            | AlterSequence()
            | CreateSynonym()
            | DropSynonym()
            | CreateTable()
            | AddColumn()
            | DropConstraint()
            | DropIndex()
        ):
            return []
        case SetSystemVersioning():
            # OFF: the history is no longer written, and the history table becomes a plain table
            return [] if op.on else ["TEMPORAL_OFF"]
        case UnmaskColumn():
            return ["UNMASK"]  # every reader of the column sees the real values after it
        case MaskColumn():
            return []
        case _:
            assert_never(op)


# ------------------------------------------------------------------ public: touched objects
def _names(op: Operation, schema: str, name: str) -> bool:
    """True for DROP TABLE or DROP CONSTRAINT of this name in the schema."""
    return isinstance(op, DropTable | DropConstraint) and (fold(op.schema), fold(op.name)) == (
        fold(schema),
        fold(name),
    )


def _life_of(ops: Seq[Operation], at: int) -> tuple[list[str], str | None, bool]:
    """What the statements around the rename ops[at] of a table or a constraint say about the object.

    (every name that it has in these statements, the table of the DROP CONSTRAINT that removes
    it, True when a DROP TABLE removes it). The name is followed back and forward through the
    other renames: one model, the model before or the model after, holds the object under one of
    these names unless a later statement removes it. Names of tables and constraints share one
    namespace in a schema, so a name says which object it is.
    """
    rename = ops[at]
    assert isinstance(rename, Rename)
    schema = rename.old[0]
    known = [rename.old[1], rename.new_name]

    def other(op: Operation) -> Rename | None:
        """op when it is sp_rename of a table or a constraint of the schema."""
        if isinstance(op, Rename) and op.kind not in ("column", "index") and fold(op.old[0]) == fold(schema):
            return op
        return None

    for op in reversed(ops[:at]):
        earlier = other(op)
        if earlier is not None and fold(earlier.new_name) == fold(known[0]):
            known.insert(0, earlier.old[1])
    for op in ops[at + 1 :]:
        later = other(op)
        if later is not None and fold(later.old[1]) == fold(known[-1]):
            known.append(later.new_name)
        elif _names(op, schema, known[-1]):
            return known, (op.table if isinstance(op, DropConstraint) else None), isinstance(op, DropTable)
    return known, None, False


def _rename_touches(ops: Seq[Operation], at: int, model: Model | None) -> list[str]:
    op = ops[at]
    assert isinstance(op, Rename)
    schema, old, new = op.old[0], op.old[1], op.new_name
    if op.kind == "index":
        return [_table_key(schema, old)]
    if model is None:
        raise ValueError(
            "touched_objects needs the model for a rename of a table, column or constraint: the statement "
            "does not name every table that it changes"
        )
    tables = [obj for obj in model.values() if isinstance(obj, Table)]
    known, owner, table_dropped = ([old, new], None, False) if op.kind == "column" else _life_of(ops, at)
    if op.kind == "column":
        found, renamed, columns = [_table_key(schema, old)], [old], {fold(op.old[2]), fold(new)}
    elif op.kind == "table" or (
        op.kind == "object"
        and owner is None
        and (table_dropped or any(_table_key(schema, n) in model for n in known))
    ):
        found, renamed, columns = [_table_key(schema, old), _table_key(schema, new)], [old, new], None
    elif owner is not None:
        return [_table_key(schema, owner)]  # the DROP CONSTRAINT that removes it names its table
    else:
        owners = [
            t.key
            for t in tables
            if fold(t.schema) == fold(schema) and any(_has_constraint(t, n) for n in known)
        ]
        if len(owners) != 1:
            raise ValueError(
                f"touched_objects cannot find the table of {names.qualified(schema, old)} in the model: "
                f"{len(owners)} tables have a constraint of that name or of the new name"
            )
        return owners
    # the definition of a foreign key holds the names of the table and the columns that it references
    referenced = {fold(_table_key(schema, name)) for name in renamed}

    def follows(key: ForeignKey) -> bool:
        if fold(_table_key(key.ref_schema, key.ref_table)) not in referenced:
            return False
        return columns is None or any(fold(name) in columns for name in key.ref_columns)

    return found + [t.key for t in tables if any(follows(key) for key in _foreign_keys(t.constraints))]


def touched_objects(ops: Iterable[Operation], model: Model | None = None) -> list[str]:
    """Keys of the table-class objects that the operations change, in case-folded key order.

    A statement on a table gives the key of that table. A rename also changes what the statement
    does not name, so it needs model: the model before the operations or the model after them.
    A table rename gives the old key, the new key and every table with a foreign key to it; a
    column rename gives the table and every table with a foreign key to that column; a constraint
    rename gives the table that has the constraint. The renamed object is followed through the
    other renames of the list, and a later DROP CONSTRAINT or DROP TABLE of it says what it was,
    so the model need not hold it. ValueError when a rename cannot be resolved (model is None; or
    a renamed constraint goes with its table, so that no model and no statement names its table).
    The rename of an index needs no model.
    """
    found: dict[str, str] = {}
    ops = list(ops)
    for at, op in enumerate(ops):
        match op:
            case CreateSchema():
                keys = [op.schema.key]
            case DropSchema():
                keys = [names.object_key("SCHEMA", None, op.name)]
            case CreateType():
                keys = [op.type.key]
            case DropType():
                keys = [names.object_key("TYPE", op.schema, op.name)]
            case CreateSequence():
                keys = [op.sequence.key]
            case AlterSequence() | DropSequence():
                keys = [names.object_key("SEQUENCE", op.schema, op.name)]
            case CreateSynonym():
                keys = [op.synonym.key]
            case DropSynonym():
                keys = [names.object_key("SYNONYM", op.schema, op.name)]
            case CreateTable():
                keys = [op.table.key]
            case DropTable():
                keys = [_table_key(op.schema, op.name)]
            case (
                AddColumn()
                | AlterColumn()
                | AlterColumnProperty()
                | RebuildTable()
                | DropColumn()
                | AddConstraint()
                | DropConstraint()
                | CreateIndex()
                | DropIndex()
                | SetSystemVersioning()
                | MaskColumn()
                | UnmaskColumn()
            ):
                keys = [_table_key(op.schema, op.table)]
            case Rename():
                keys = _rename_touches(ops, at, model)
            case _:
                assert_never(op)
        for key in keys:
            found.setdefault(fold(key), key)
    return [found[folded] for folded in sorted(found)]
