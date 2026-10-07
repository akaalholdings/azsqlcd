"""Canonical SQL text: model object -> object file, operation -> statement, and the NF000 check.

Rules that hold for every text this module writes:
  * An identifier is bracket-quoted (names.quote). Keywords, built-in data type names (lower case,
    as the catalog holds them) and collation names are bare.
  * An expression is written as its stored tokens, one after the other. Only the spacing is
    chosen here, and the text must read back as the same tokens.
  * Constraints, indexes and options are written in a fixed order, whatever their order in the
    model. Names, expression tokens and included columns are written as the model holds them.
  * Nothing is dropped and nothing is guessed. A value that has no SQL spelling is a ValueError.

Public API: emit_object_file, emit_operation, emit_create_script, token_roundtrip_differences,
token_differences, NORMAL_FORM_EQUIVALENTS.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from collections.abc import Sequence as Seq
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import assert_never

from azsqlcd.lex import CURRENCY, Tok, significant, split_batches, tokenize
from azsqlcd.model import (
    RETENTION_UNITS,
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
    ModelObject,
    Operation,
    Options,
    PrimaryKey,
    RebuildTable,
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
    UnmaskColumn,
    UnversionedTable,
    fold,
    is_reserved,
)
from azsqlcd.names import qualified, quote, sql_literal
from azsqlcd.parse import parse_object_file

_INDENT = "    "
_NAME_KINDS = ("word", "bident", "qident")
_QUOTED = ("bident", "qident")
# Reserved words that are written as a call: CONVERT(date, [x]), not CONVERT (date, [x]).
_CALLED = frozenset({"COALESCE", "CONVERT", "LEFT", "NULLIF", "RIGHT", "TRY_CONVERT"})
_COLLATION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONSTRAINT_ORDER = (PrimaryKey, Unique, Check, ForeignKey)
_SWITCHES = ("ONLINE", "RESUMABLE", "SORT_IN_TEMPDB")
_LOW_PRIORITY = re.compile(r"MAX_DURATION = [0-9]+ MINUTES, ABORT_AFTER_WAIT = (?:NONE|SELF|BLOCKERS)")
_GENERATED = {"ROW_START": "ROW START", "ROW_END": "ROW END"}
_RENAME_ARGUMENT = {
    "column": "COLUMN",
    "index": "INDEX",
    "table": "OBJECT",
    "constraint": "OBJECT",
    "object": "OBJECT",
}


# ------------------------------------------------------------------ expressions
def _tight(toks: Seq[Tok], i: int) -> bool:
    """True when no space goes between toks[i - 1] and toks[i].

    Every pair that is not listed here gets a space, so two tokens can never join into another
    token ('-' '-' into a comment, N and a string into an N'' literal, two strings into one).
    """
    left, right = toks[i - 1], toks[i]
    if left.text == "(" or right.text in (")", ","):
        return True
    if right.text == "(":
        called = not is_reserved(left.text) or left.text.upper() in _CALLED
        return left.kind in _NAME_KINDS and called
    if left.text == ".":
        return right.kind in _NAME_KINDS
    if right.text == ".":
        return left.kind in _NAME_KINDS
    if left.kind == "op" and left.text in CURRENCY:  # a money literal: $5 and $-5, never '$ 5'
        return right.kind == "number" or (
            right.text in ("-", "+") and i + 1 < len(toks) and toks[i + 1].kind == "number"
        )
    if left.text in ("-", "+") and right.kind == "number":  # a sign, not a subtraction
        return i == 1 or (toks[i - 2].kind == "op" and toks[i - 2].text != ")")
    return False


def _expression(expression: Expression) -> str:
    toks = [tokenize(text)[0] for text in expression.tokens]
    text = "".join(("" if i == 0 or _tight(toks, i) else " ") + tok.text for i, tok in enumerate(toks))
    if tuple(tok.text for tok in significant(tokenize(text))) != expression.tokens:
        raise ValueError("an expression cannot be written so that it reads back as the same tokens")
    return text


# ------------------------------------------------------------------ parts of a statement
def _int(value: int | None) -> str:
    if type(value) is not int:  # a str in a number field would become SQL text
        raise ValueError(f"not a whole number: {value!r}")
    return str(value)


def _type(type_: TypeRef) -> str:
    if type_.schema is not None:
        return qualified(type_.schema, type_.name)
    name = type_.name.lower()  # TypeRef has checked that this is a built-in type name
    if type_.length is not None:
        return f"{name}({'max' if type_.length == 'max' else _int(type_.length)})"
    if type_.precision is not None:
        return f"{name}({_int(type_.precision)}, {_int(type_.scale)})"
    if type_.scale is not None:
        return f"{name}({_int(type_.scale)})"
    return name


def _collation(name: str) -> str:
    # Bare, as the engine scripts it. Bracket-quoted collation names are not proven on the engine.
    if not _COLLATION.fullmatch(name) or is_reserved(name):
        raise ValueError(f"not a collation name: {name!r}")
    return name


def _nullability(nullable: bool) -> str:
    return "NULL" if nullable else "NOT NULL"


def _constraint_name(name: str | None, in_type: bool) -> str:
    """'CONSTRAINT [name] ', or '' in a table type, where a constraint cannot be named."""
    if (name is None) != in_type:
        raise ValueError("a constraint has a name everywhere except in a table type")
    return "" if name is None else f"CONSTRAINT {quote(name)} "


def _key_columns(columns: Iterable[KeyColumn]) -> str:
    return "(" + ", ".join(quote(c.name) + (" DESC" if c.descending else "") for c in columns) + ")"


def _name_list(names: Iterable[str]) -> str:
    return "(" + ", ".join(quote(name) for name in names) + ")"


def _with(options: Options, exec_options: Options = ()) -> str:
    """' WITH (...)' with the model options, then the execution options, each in name order."""
    items = [f"{name} = {value}" for name, value in sorted(options)]
    execution = dict(exec_options)
    if len(execution) != len(exec_options):
        raise ValueError("an execution option is given twice")
    low_priority = execution.pop("WAIT_AT_LOW_PRIORITY", None)
    if low_priority is not None and (
        execution.get("ONLINE") != "ON" or not _LOW_PRIORITY.fullmatch(low_priority)
    ):
        raise ValueError("WAIT_AT_LOW_PRIORITY goes with ONLINE = ON and has a fixed form")
    for name, value in sorted(execution.items()):
        if name in _SWITCHES and value in ("ON", "OFF"):
            items.append(f"{name} = {value}")
            if name == "ONLINE" and low_priority is not None:
                items[-1] += f" (WAIT_AT_LOW_PRIORITY ({low_priority}))"
        elif name in ("MAX_DURATION", "MAXDOP") and value.isascii() and value.isdigit():
            items.append(f"{name} = {value}" + (" MINUTES" if name == "MAX_DURATION" else ""))
        else:
            raise ValueError(f"execution option {name} = {value!r} cannot be written")
    return f" WITH ({', '.join(items)})" if items else ""


def _masked(function: str) -> str:
    # The model holds the text between the quotes, a ' written twice. sql_literal refuses text that
    # would not arrive as written; a text that it does not give back holds a single quote that
    # would end the literal. The canonical text has no N: the engine stores the same function.
    literal = sql_literal(function.replace("''", "'"))[1:]
    if not function.strip() or literal != f"'{function}'":
        raise ValueError(f"not a masking function that can be written: {function!r}")
    return f"MASKED WITH (FUNCTION = {literal})"


def _column(column: Column, in_type: bool) -> str:
    parts = [quote(column.name)]
    if column.computed is not None:
        parts += ["AS", _expression(column.computed.expression)]
        if column.computed.persisted:
            parts.append("PERSISTED")
        if column.nullable is False and column.computed.persisted:
            parts.append("NOT NULL")
        elif column.nullable is not None:
            raise ValueError(f"computed column {column.name!r}: only PERSISTED NOT NULL states nullability")
        return " ".join(parts)
    assert column.type is not None and column.nullable is not None  # Column has checked both
    parts.append(_type(column.type))
    if column.collation is not None:
        parts += ["COLLATE", _collation(column.collation)]
    if column.sparse:
        # before MASKED WITH: the place that the grammar of the engine gives it
        parts.append("SPARSE")
    if column.masked is not None:
        # directly after the type and COLLATE: the engine reads MASKED WITH in no other place
        parts.append(_masked(column.masked))
    if column.identity is not None:
        parts.append(f"IDENTITY({_int(column.identity.seed)}, {_int(column.identity.increment)})")
        if column.identity.not_for_replication:
            parts.append("NOT FOR REPLICATION")
    if column.rowguidcol:
        parts.append("ROWGUIDCOL")
    if column.generated is not None:
        # GENERATED ALWAYS AS ROW START | END [HIDDEN] comes before NOT NULL, as the engine scripts it
        parts.append(f"GENERATED ALWAYS AS {_GENERATED[column.generated]}")
        if column.hidden:
            parts.append("HIDDEN")
    elif column.hidden:
        raise ValueError(f"column {column.name!r}: HIDDEN goes with GENERATED ALWAYS")
    parts.append(_nullability(column.nullable))
    if column.default is not None:
        # last, so that an expression without parentheses cannot run into NULL or NOT NULL
        named = _constraint_name(column.default.name, in_type)
        parts.append(f"{named}DEFAULT {_expression(column.default.expression)}")
    return " ".join(parts)


def _constraint(constraint: Constraint, in_type: bool, exec_options: Options = ()) -> str:
    named = _constraint_name(constraint.name, in_type)
    if isinstance(constraint, PrimaryKey | Unique):
        kind = "PRIMARY KEY" if isinstance(constraint, PrimaryKey) else "UNIQUE"
        clustering = "CLUSTERED" if constraint.clustered else "NONCLUSTERED"
        options = _with(constraint.options, exec_options)
        return f"{named}{kind} {clustering} {_key_columns(constraint.columns)}{options}"
    if exec_options:
        raise ValueError("execution options go with PRIMARY KEY and UNIQUE only")
    if isinstance(constraint, Check):
        replication = "NOT FOR REPLICATION " if constraint.not_for_replication else ""
        return f"{named}CHECK {replication}{_expression(constraint.expression)}"
    text = (
        f"{named}FOREIGN KEY {_name_list(constraint.columns)} REFERENCES "
        f"{qualified(constraint.ref_schema, constraint.ref_table)} {_name_list(constraint.ref_columns)}"
    )
    # NO ACTION is the absence of the clause; it is never written
    if constraint.on_delete != "NO ACTION":
        text += f" ON DELETE {constraint.on_delete}"
    if constraint.on_update != "NO ACTION":
        text += f" ON UPDATE {constraint.on_update}"
    if constraint.not_for_replication:
        text += " NOT FOR REPLICATION"
    return text


def _ordered_constraints(constraints: Iterable[Constraint], in_type: bool) -> list[str]:
    """PRIMARY KEY, UNIQUE, CHECK, FOREIGN KEY; by name inside a kind; by text when unnamed."""
    found = [
        (_CONSTRAINT_ORDER.index(type(c)), fold(c.name or ""), _constraint(c, in_type)) for c in constraints
    ]
    return [text for _, _, text in sorted(found)]


def _ordered_indexes(indexes: Iterable[Index]) -> list[Index]:
    """The clustered index first: the engine builds every other index of the table on it."""
    return sorted(indexes, key=lambda i: (not i.clustered, fold(i.name)))


def _index_clauses(index: Index, exec_options: Options) -> list[str]:
    clauses: list[str] = []
    if index.columnstore:
        if index.columns:
            clauses.append(f"ORDER {_name_list(c.name for c in index.columns)}")
    elif index.included:
        clauses.append(f"INCLUDE {_name_list(index.included)}")
    if index.filter is not None:
        clauses.append(f"WHERE {_expression(index.filter)}")
    options = _with(index.options, exec_options)
    if options:
        clauses.append(options.strip())
    return clauses


def _index_kind(index: Index) -> str:
    kind = ("UNIQUE " if index.unique else "") + ("CLUSTERED" if index.clustered else "NONCLUSTERED")
    return kind + (" COLUMNSTORE" if index.columnstore else "")


def _index_columns(index: Index) -> str:
    """' (...)': the key columns; for a columnstore index its column list, and none when clustered."""
    if not index.columnstore:
        return " " + _key_columns(index.columns)
    return " " + _name_list(index.included) if index.included else ""


def _create_index(schema: str, table: str, index: Index, exec_options: Options, separator: str) -> str:
    head = (
        f"CREATE {_index_kind(index)} INDEX {quote(index.name)} ON {qualified(schema, table)}"
        f"{_index_columns(index)}"
    )
    if index.columnstore and any(name not in ("ONLINE", "MAXDOP") for name, _ in exec_options):
        raise ValueError("a columnstore index takes the execution options ONLINE and MAXDOP only")
    return separator.join([head, *_index_clauses(index, exec_options)])


def _inline_index(index: Index) -> str:
    head = f"INDEX {quote(index.name)} {_index_kind(index)}{_index_columns(index)}"
    return " ".join([head, *_index_clauses(index, ())])


def _body(obj: Table | TableType) -> str:
    """( columns, then constraints, then inline indexes, then the period ), one on each line."""
    in_type = isinstance(obj, TableType)
    lines = [_column(c, in_type) for c in obj.columns]
    lines += _ordered_constraints(obj.constraints, in_type)
    lines += [_inline_index(i) for i in _ordered_indexes(obj.indexes)]
    if isinstance(obj, Table) and obj.temporal is not None:
        period = _name_list((obj.temporal.period_start, obj.temporal.period_end))
        lines.append(f"PERIOD FOR SYSTEM_TIME {period}")
    return "(\n" + ",\n".join(_INDENT + line for line in lines) + "\n)"


def _retention(retention: tuple[int, str] | None) -> str:
    """', HISTORY_RETENTION_PERIOD = n UNIT', or '' for INFINITE (the absence of the clause)."""
    if retention is None:
        return ""
    number, unit = retention
    if unit not in RETENTION_UNITS:
        raise ValueError(f"not a retention unit: {unit!r}")
    return f", HISTORY_RETENTION_PERIOD = {_int(number)} {unit}"


def _versioning_on(history_schema: str, history_table: str, retention: tuple[int, str] | None) -> str:
    history = qualified(history_schema, history_table)
    return f"SYSTEM_VERSIONING = ON (HISTORY_TABLE = {history}{_retention(retention)})"


def _table_options(temporal: Temporal | None, compression: str | None) -> str:
    """'\nWITH (...)': the compression of the heap, then the versioning; '' when there is neither."""
    options = [] if compression is None else [f"DATA_COMPRESSION = {compression}"]
    if temporal is not None:
        options.append(_versioning_on(temporal.history_schema, temporal.history_table, temporal.retention))
    return f"\nWITH ({', '.join(options)})" if options else ""


def _cache(cached: bool, size: int | None) -> str:
    if not cached and size is not None:
        raise ValueError("NO CACHE has no cache size")
    return "NO CACHE" if not cached else "CACHE" if size is None else f"CACHE {_int(size)}"


def _create(obj: ModelObject) -> str:
    """The CREATE statement of an object, without ';'. A table carries its indexes inline."""
    match obj:
        case Schema():
            owner = "" if obj.owner is None else f" AUTHORIZATION {quote(obj.owner)}"
            return f"CREATE SCHEMA {quote(obj.name)}{owner}"
        case AliasType():
            name = qualified(obj.schema, obj.name)
            return f"CREATE TYPE {name} FROM {_type(obj.base)} {_nullability(obj.nullable)}"
        case TableType():
            return f"CREATE TYPE {qualified(obj.schema, obj.name)} AS TABLE {_body(obj)}"
        case Sequence():
            return (
                f"CREATE SEQUENCE {qualified(obj.schema, obj.name)} AS {_type(obj.type)} "
                f"START WITH {_int(obj.start)} INCREMENT BY {_int(obj.increment)} "
                f"MINVALUE {_int(obj.minvalue)} MAXVALUE {_int(obj.maxvalue)} "
                f"{'CYCLE' if obj.cycle else 'NO CYCLE'} {_cache(obj.cached, obj.cache_size)}"
            )
        case Synonym():
            target = qualified(obj.target_schema, obj.target_name)
            return f"CREATE SYNONYM {qualified(obj.schema, obj.name)} FOR {target}"
        case Table():
            if isinstance(obj, UnversionedTable):
                # PERIOD without SYSTEM_VERSIONING = ON: a state inside a replay, not a table to create
                raise ValueError(
                    f"{obj.key} has period columns and versioning is off: it has no CREATE TABLE"
                )
            options = _table_options(obj.temporal, obj.compression)
            return f"CREATE TABLE {qualified(obj.schema, obj.name)} {_body(obj)}{options}"
        case _:
            assert_never(obj)


def _alter_sequence(op: AlterSequence) -> str:
    clauses: list[str] = []
    if op.restart:
        clauses.append("RESTART" if op.restart_with is None else f"RESTART WITH {_int(op.restart_with)}")
    elif op.restart_with is not None:
        raise ValueError("restart_with goes with restart")
    for keyword, value in (
        ("INCREMENT BY", op.increment),
        ("MINVALUE", op.minvalue),
        ("MAXVALUE", op.maxvalue),
    ):
        if value is not None:
            clauses.append(f"{keyword} {_int(value)}")
    if op.cycle is not None:
        clauses.append("CYCLE" if op.cycle else "NO CYCLE")
    if op.cached is not None:
        clauses.append(_cache(op.cached, op.cache_size))
    elif op.cache_size is not None:
        raise ValueError("cache_size goes with cached")
    if not clauses:
        raise ValueError("ALTER SEQUENCE changes at least one property")
    return f"ALTER SEQUENCE {qualified(op.schema, op.name)} {' '.join(clauses)}"


def _add_constraint(op: AddConstraint) -> str:
    check = {None: "", True: " WITH CHECK", False: " WITH NOCHECK"}[op.with_check]
    constraint = op.constraint
    if isinstance(constraint, DefaultConstraint):
        if op.exec_options:
            raise ValueError("execution options go with PRIMARY KEY and UNIQUE only")
        assert op.for_column is not None  # AddConstraint has checked it
        named = _constraint_name(constraint.name, False)
        text = f"{named}DEFAULT {_expression(constraint.expression)} FOR {quote(op.for_column)}"
    else:
        text = _constraint(constraint, False, op.exec_options)
    return f"ALTER TABLE {qualified(op.schema, op.table)}{check} ADD {text}"


def _statement(op: Operation) -> str:
    match op:
        case CreateSchema():
            return _create(op.schema)
        case CreateType():
            return _create(op.type)
        case CreateSequence():
            return _create(op.sequence)
        case CreateSynonym():
            return _create(op.synonym)
        case CreateTable():
            return _create(op.table)
        case DropSchema():
            return f"DROP SCHEMA {quote(op.name)}"
        case DropType():
            return f"DROP TYPE {qualified(op.schema, op.name)}"
        case DropSequence():
            return f"DROP SEQUENCE {qualified(op.schema, op.name)}"
        case DropSynonym():
            return f"DROP SYNONYM {qualified(op.schema, op.name)}"
        case DropTable():
            return f"DROP TABLE {qualified(op.schema, op.name)}"
        case AlterSequence():
            return _alter_sequence(op)
        case AddColumn():
            text = f"ALTER TABLE {qualified(op.schema, op.table)} ADD {_column(op.column, False)}"
            if op.with_values and op.column.default is None:
                raise ValueError("WITH VALUES goes with a DEFAULT")
            return text + (" WITH VALUES" if op.with_values else "")
        case AlterColumn():
            if any(name != "ONLINE" for name, _ in op.exec_options):
                # the engine: "The WAIT_AT_LOW_PRIORITY option can't be used with online ALTER COLUMN"
                raise ValueError("ALTER COLUMN takes the execution option ONLINE only")
            collation = "" if op.collation is None else f" COLLATE {_collation(op.collation)}"
            return (
                f"ALTER TABLE {qualified(op.schema, op.table)} ALTER COLUMN {quote(op.column)} "
                f"{_type(op.type)}{collation} {_nullability(op.nullable)}{_with((), op.exec_options)}"
            )
        case AlterColumnProperty():
            return (
                f"ALTER TABLE {qualified(op.schema, op.table)} ALTER COLUMN {quote(op.column)} "
                f"{'ADD' if op.add else 'DROP'} {op.property}"
            )
        case RebuildTable():
            allowed = ("ONLINE", "MAXDOP", "SORT_IN_TEMPDB", "WAIT_AT_LOW_PRIORITY")
            if any(name not in allowed for name, _ in op.exec_options):
                raise ValueError("REBUILD takes the execution options ONLINE, MAXDOP and SORT_IN_TEMPDB only")
            options = _with((("DATA_COMPRESSION", op.compression),), op.exec_options)
            return f"ALTER TABLE {qualified(op.schema, op.table)} REBUILD{options}"
        case DropColumn():
            return f"ALTER TABLE {qualified(op.schema, op.table)} DROP COLUMN {quote(op.column)}"
        case AddConstraint():
            return _add_constraint(op)
        case DropConstraint():
            return f"ALTER TABLE {qualified(op.schema, op.table)} DROP CONSTRAINT {quote(op.name)}"
        case CreateIndex():
            return _create_index(op.schema, op.table, op.index, op.exec_options, " ")
        case DropIndex():
            return f"DROP INDEX {quote(op.name)} ON {qualified(op.schema, op.table)}"
        case SetSystemVersioning():
            head = f"ALTER TABLE {qualified(op.schema, op.table)} SET"
            if not op.on:
                return f"{head} (SYSTEM_VERSIONING = OFF)"
            assert op.history_schema is not None and op.history_table is not None  # checked by the class
            return f"{head} ({_versioning_on(op.history_schema, op.history_table, op.retention)})"
        case MaskColumn():
            head = f"ALTER TABLE {qualified(op.schema, op.table)} ALTER COLUMN {quote(op.column)}"
            return f"{head} ADD {_masked(op.function)}"
        case UnmaskColumn():
            return f"ALTER TABLE {qualified(op.schema, op.table)} ALTER COLUMN {quote(op.column)} DROP MASKED"
        case Rename():
            if not op.new_name:
                raise ValueError("empty identifier")
            old = sql_literal(".".join(quote(part) for part in op.old))
            # sp_rename takes the new name as it is: brackets would become part of the name
            return f"EXEC sys.sp_rename {old}, {sql_literal(op.new_name)}, N'{_RENAME_ARGUMENT[op.kind]}'"
        case _:
            assert_never(op)


# ------------------------------------------------------------------ public: text
def emit_object_file(obj: ModelObject) -> str:
    """The canonical text of the object file of one table-class object.

    A table file is CREATE TABLE (columns with their DEFAULT constraints, then PRIMARY KEY,
    UNIQUE, CHECK and FOREIGN KEY constraints, then PERIOD FOR SYSTEM_TIME and, after the body,
    WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = ...)) for a temporal table), then one GO and one
    CREATE INDEX batch for each index. Every other file is one statement. parse_object_file reads
    the text back as an equal object when the path is names.path_for() of the object.
    """
    if isinstance(obj, Table):
        separator = "\n" + _INDENT
        batches = [_create(replace(obj, indexes=()))]
        batches += [
            _create_index(obj.schema, obj.name, i, (), separator) for i in _ordered_indexes(obj.indexes)
        ]
    else:
        batches = [_create(obj)]
    return "\nGO\n".join(batch + ";" for batch in batches) + "\n"


def emit_operation(op: Operation) -> str:
    """The one SQL statement of a migration operation, ending with ';'.

    parse_statement reads it back as an equal operation. The exception is a Rename of kind
    'table' or 'constraint': sp_rename has one word for both, N'OBJECT', and that reads back as
    kind 'object'.
    """
    return _statement(op) + ";"


def emit_create_script(model: Model) -> list[str]:
    """Statements, one for each batch, that create the whole model on an empty database.

    Order: schemas, alias types, table types, sequences, tables without their foreign keys,
    indexes, foreign keys (each its own ALTER TABLE, so a cycle of tables is no problem),
    synonyms. Inside a group the order is the key order of the model.
    """

    def of[T](cls: type[T]) -> list[T]:
        return [obj for obj in model.values() if isinstance(obj, cls)]

    def foreign_keys(table: Table) -> list[ForeignKey]:
        found = [c for c in table.constraints if isinstance(c, ForeignKey)]
        return sorted(found, key=lambda c: fold(c.name))

    def alone(table: Table) -> Table:
        kept = tuple(c for c in table.constraints if not isinstance(c, ForeignKey))
        return replace(table, constraints=kept, indexes=())

    tables = of(Table)
    operations: list[Operation] = [
        *(CreateSchema(schema) for schema in of(Schema)),
        *(CreateType(alias) for alias in of(AliasType)),
        *(CreateType(table_type) for table_type in of(TableType)),
        *(CreateSequence(sequence) for sequence in of(Sequence)),
        *(CreateTable(alone(table)) for table in tables),
        *(CreateIndex(t.schema, t.name, index) for t in tables for index in _ordered_indexes(t.indexes)),
        *(AddConstraint(t.schema, t.name, key) for t in tables for key in foreign_keys(t)),
        *(CreateSynonym(synonym) for synonym in of(Synonym)),
    ]
    return [emit_operation(op) for op in operations]


# ------------------------------------------------------------------ NF000: the token round trip
@dataclass(frozen=True)
class _Token:
    key: str  # what is compared
    text: str  # what a message shows
    line: int  # line in the text
    name: str  # the identifier inside brackets or double quotes; the text of every other token


_Batches = list[list[_Token]]
_ELEMENT_WORDS = frozenset({"constraint", "primary", "unique", "check", "foreign", "index"})
_GO = "\nGO"  # the key of a batch separator; no token has it (only a literal holds a line break)
_SHOWN = 12  # tokens of one side that a difference shows
_STRING = re.compile(r"\A([Nn]?)'.*", re.DOTALL)  # a string literal token, with or without N


def _keys(tokens: Iterable[_Token]) -> list[str]:
    return [t.key for t in tokens]


def _closing(tokens: Seq[_Token], opening: int) -> int:
    """Index of the ')' that closes tokens[opening]; len(tokens) when nothing closes it."""
    depth = 0
    for i in range(opening, len(tokens)):
        depth += {"(": 1, ")": -1}.get(tokens[i].key, 0)
        if depth == 0:
            return i
    return len(tokens)


def _items(tokens: Iterable[_Token]) -> list[list[_Token]]:
    """Split on each ',' that is outside every parenthesis."""
    items: list[list[_Token]] = [[]]
    depth = 0
    for tok in tokens:
        if tok.key == "," and depth == 0:
            items.append([])
            continue
        depth += {"(": 1, ")": -1}.get(tok.key, 0)
        items[-1].append(tok)
    return items


def _joined(items: Iterable[list[_Token]]) -> list[_Token]:
    """The items with a ',' between them again. A ',' takes the line of the token after it."""
    out: list[_Token] = []
    line = 1
    for n, item in enumerate(items):
        line = item[0].line if item else line
        if n:
            out.append(_Token(",", ",", line, ","))
        out += item
    return out


def _spelling(batches: _Batches) -> _Batches:
    # The model's own token comparison: one definition of "the same token" for the proof and here.
    return [[replace(t, key=Expression((t.text,)).comparison[0]) for t in batch] for batch in batches]


def _terminator(batches: _Batches) -> _Batches:
    return [batch[:-1] if batch and batch[-1].key == ";" else batch for batch in batches]


def _option_order(batches: _Batches) -> _Batches:
    out: _Batches = []
    for tokens in batches:
        batch = list(tokens)
        for i in range(len(batch) - 1):
            if batch[i].key == "with" and batch[i + 1].key == "(":
                close = _closing(batch, i + 1)
                batch[i + 2 : close] = _joined(sorted(_items(batch[i + 2 : close]), key=_keys))
        out.append(batch)
    return out


def _is_element(item: Seq[_Token]) -> bool:
    """A constraint, an inline index or PERIOD FOR ...; a column may be called [Period]."""
    if not item:
        return False
    return item[0].key in _ELEMENT_WORDS or _keys(item[:2]) == ["period", "for"]


def _element_order(batches: _Batches) -> _Batches:
    if not batches or "(" not in _keys(batches[0]):
        return batches
    first, keys = batches[0], _keys(batches[0])
    opening = keys.index("(")
    is_type = keys[:2] == ["create", "type"] and keys[max(opening - 2, 0) : opening] == ["as", "table"]
    if keys[:2] != ["create", "table"] and not is_type:
        return batches
    close = _closing(first, opening)
    items = _items(first[opening + 1 : close])
    free = [item for item in items if _is_element(item)]
    columns = [item for item in items if not _is_element(item)]
    body = _joined([*columns, *sorted(free, key=_keys)])
    return [first[: opening + 1] + body + first[close:], *batches[1:]]


def _batch_order(batches: _Batches) -> _Batches:
    return [*batches[:1], *sorted(batches[1:], key=_keys)]


# The one table of what NF000 accepts (A14): name -> (what may differ, how both texts are brought to
# one form). The steps run in this order on the file and on the canonical text; what is left must
# be equal token by token. Everything that is not here is a difference, also when the parser reads
# both spellings as one model: ASC, ON [PRIMARY], TEXTIMAGE_ON, ON DELETE NO ACTION, IDENTITY
# without (seed, increment), a PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK written on a column, an
# INDEX written inside CREATE TABLE, the order of the clauses of a column or a sequence.
NORMAL_FORM_EQUIVALENTS: Mapping[str, tuple[str, Callable[[_Batches], _Batches]]] = {
    "SPELLING": (
        "the letter case of keywords, built-in type names and numbers, and a name written bare, in "
        "brackets or in double quotes ([select] is never the keyword SELECT); the letters of a name "
        "and a string literal are exact",
        _spelling,
    ),
    "TERMINATOR": ("the ';' at the end of a batch, written or not", _terminator),
    "OPTION_ORDER": ("the order of the options in WITH ( ... )", _option_order),
    "ELEMENT_ORDER": (
        "the order of the constraints, inline indexes and PERIOD FOR SYSTEM_TIME in a table body, and "
        "their place among the columns",
        _element_order,
    ),
    "BATCH_ORDER": ("the order of the CREATE INDEX batches of a table file", _batch_order),
}


def _comparable(text: str) -> list[_Token]:
    batches: _Batches = [
        [
            _Token(t.text, t.text, batch.first_line - 1 + t.line, t.value if t.kind in _QUOTED else t.text)
            for t in significant(tokenize(batch.text))
        ]
        for batch in split_batches(text)
    ]
    for _, step in NORMAL_FORM_EQUIVALENTS.values():
        batches = step(batches)
    out: list[_Token] = []
    for n, batch in enumerate(batches):
        if n:
            out.append(_Token(_GO, "GO", out[-1].line if out else 1, "GO"))
        out += batch
    return out


def _respelled(file: Seq[_Token], canonical: Seq[_Token]) -> list[str]:
    """Names that the canonical text writes in other letters than the file. The keys are equal.

    The keys compare names case-folded, as the model does. A count of the exact spellings shows
    a name whose letters changed on the way, and it does not depend on the order of the tokens.
    """
    written = Counter(t.name for t in file)
    first_line = {t.key: t.line for t in reversed(file)}
    out: list[str] = []
    for tok in canonical:
        if tok.name == tok.text:  # not a name in brackets
            continue
        if written[tok.name]:
            written[tok.name] -= 1
        else:
            line = first_line[tok.key]
            out.append(f"line {line}: the canonical form has {tok.text}; the file has other letters")
    return out


def _shown(tokens: Seq[_Token]) -> str:
    """Token texts for a message. The text of a string literal is never repeated."""
    words = [_STRING.sub(r"\1'...'", t.text) for t in tokens[:_SHOWN]]
    return " ".join(words) + (" ..." if len(tokens) > _SHOWN else "")


def token_differences(file_text: str, canonical_text: str) -> list[str]:
    """Where the significant tokens of a file differ from those of a canonical text.

    Both texts are brought to one form by the steps of NORMAL_FORM_EQUIVALENTS and by nothing
    else. Each difference is 'line <n>: ...' with the line in the file and the tokens of each
    side; the text of a string literal is shown as '...'. [] means equal. Raises LexError when a
    text cannot be tokenized.
    """
    file, canonical = _comparable(file_text), _comparable(canonical_text)
    if _keys(file) == _keys(canonical):
        return _respelled(file, canonical)  # the usual case; the matcher is slow on long texts
    matcher = SequenceMatcher(None, _keys(file), _keys(canonical), autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        line = file[i1].line if i1 < i2 else file[i1 - 1].line if i1 else 1
        if tag == "delete":
            out.append(f"line {line}: the file has {_shown(file[i1:i2])}; the canonical form does not")
        elif tag == "insert":
            out.append(f"line {line}: the canonical form has {_shown(canonical[j1:j2])}; the file does not")
        else:
            has, wants = _shown(file[i1:i2]), _shown(canonical[j1:j2])
            out.append(f"line {line}: the file has {has}; the canonical form has {wants}")
    return out


def token_roundtrip_differences(file_text: str, path: str) -> list[str]:
    """NF000 (A14): the differences between an object file and emit_object_file(parse(file)).

    A token that the parser read and did not keep, or did not read and filled in, is a
    difference. [] means that the file says exactly what the model holds. Raises ParseError when
    the file does not parse (that is another finding, not NF000).
    """
    (obj,) = parse_object_file(file_text, path)
    return token_differences(file_text, emit_object_file(obj))
